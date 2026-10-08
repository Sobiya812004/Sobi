import json
import logging
import boto3
from dataclasses import asdict

from worker.scraper.config import (
    AWS_REGION, INPUT_QUEUE_URL, OUTPUT_QUEUE_URL, DLQ_URL,
    MAX_MESSAGES, VISIBILITY_TIMEOUT, WAIT_TIME_SECONDS,
)
from worker.scraper.models import Product

logger = logging.getLogger(__name__)

# A returned message stays hidden this long before it becomes visible again.
# Fixed on purpose (no env var): applies to every return path.
RETURN_DELAY_SEC = 20


class QueueService:

    def __init__(self):
        self.sqs = boto3.client("sqs", region_name=AWS_REGION)

    def receive_messages(self) -> list[dict]:
        if not INPUT_QUEUE_URL:
            logger.error("No INPUT_QUEUE_URL configured")
            return []

        resp = self.sqs.receive_message(
            QueueUrl=INPUT_QUEUE_URL,
            MaxNumberOfMessages=MAX_MESSAGES,
            VisibilityTimeout=VISIBILITY_TIMEOUT,
            WaitTimeSeconds=WAIT_TIME_SECONDS,
        )
        return resp.get("Messages", [])

    def get_total_messages(self) -> int:
        """Returns total visible + in-flight messages in the input queue."""
        try:
            resp = self.sqs.get_queue_attributes(
                QueueUrl=INPUT_QUEUE_URL,
                AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"]
            )
            attrs = resp["Attributes"]
            return int(attrs.get("ApproximateNumberOfMessages", 0)) + int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0))
        except Exception:
            return 0

    def return_message(self, msg: dict):
        """Give the message back to the queue. It becomes visible again after
        RETURN_DELAY_SEC seconds and its receive count keeps rising until the SQS
        redrive policy (maxReceiveCount) moves it to the DLQ."""
        try:
            self.sqs.change_message_visibility(
                QueueUrl=INPUT_QUEUE_URL,
                ReceiptHandle=msg["ReceiptHandle"],
                VisibilityTimeout=RETURN_DELAY_SEC,
            )
        except Exception as e:
            logger.error("Failed to return message: %s", e)

    def extend_visibility(self, messages: list[dict], seconds: int = VISIBILITY_TIMEOUT):
        """Heartbeat: reset the visibility timeout of every not-yet-processed message to
        `seconds` counted from now. Never raises — a failed heartbeat is only logged."""
        if not messages:
            return
        try:
            for start in range(0, len(messages), 10):          # SQS batch limit is 10
                chunk = messages[start:start + 10]
                entries = [
                    {"Id": str(i), "ReceiptHandle": m["ReceiptHandle"], "VisibilityTimeout": seconds}
                    for i, m in enumerate(chunk)
                ]
                resp = self.sqs.change_message_visibility_batch(
                    QueueUrl=INPUT_QUEUE_URL, Entries=entries
                )
                failed = resp.get("Failed", [])
                if failed:
                    logger.warning("HEARTBEAT_PARTIAL_FAIL failed=%d first=%s", len(failed), failed[0])
            logger.info("HEARTBEAT_EXTENDED messages=%d seconds=%d", len(messages), seconds)
        except Exception as e:
            logger.warning("HEARTBEAT_FAILED: %s", e)

    def delete_message(self, msg: dict):
        self.sqs.delete_message(
            QueueUrl=INPUT_QUEUE_URL,
            ReceiptHandle=msg["ReceiptHandle"],
        )

    def send_to_dlq(self, msg: dict, reason: str) -> bool:
        """Send the original body of a bad message to the DLQ with the failure reason.
        Returns True when the message is safely stored (caller may then delete it)."""
        if not DLQ_URL:
            logger.error("DLQ_URL not configured — bad message is NOT stored in a DLQ")
            return False
        try:
            self.sqs.send_message(
                QueueUrl=DLQ_URL,
                MessageBody=msg.get("Body", ""),
                MessageAttributes={
                    "failure_reason": {"DataType": "String", "StringValue": reason[:500] or "unknown"},
                    "source_message_id": {"DataType": "String", "StringValue": str(msg.get("MessageId", ""))},
                },
            )
            logger.info("BAD_MESSAGE_TO_DLQ message_id=%s reason=%s", msg.get("MessageId"), reason[:200])
            return True
        except Exception as e:
            logger.error("Failed to send bad message to DLQ: %s", e)
            return False

    def send_output(self, product: Product):
        msg = asdict(product)
        msg["client"] = self._get_client_from_queue()
        self.sqs.send_message(
            QueueUrl=OUTPUT_QUEUE_URL,
            MessageBody=json.dumps(msg),
        )
        logger.info("Output sent: strike_id=%s status=%s", product.strike_id, product.status)

    def _get_client_from_queue(self) -> str:
        """Extract client name from the input queue URL."""
        try:
            # Queue name format: dev-google-scraper-{client}-input-queue
            return INPUT_QUEUE_URL.split("-")[-3]
        except Exception:
            return "unknown"
