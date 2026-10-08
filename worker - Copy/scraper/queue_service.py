import json
import logging
import boto3
from dataclasses import asdict

from worker.scraper.config import (
    AWS_REGION, INPUT_QUEUE_URL, OUTPUT_QUEUE_URL, NOTIFICATION_PARAM,
    MAX_MESSAGES, VISIBILITY_TIMEOUT, WAIT_TIME_SECONDS,
)
from worker.scraper.models import Product

logger = logging.getLogger(__name__)


def strike_id_of(msg: dict) -> str:
    """strike_id of an SQS message (falls back to the message id when the body is unreadable)."""
    try:
        return str(json.loads(msg["Body"]).get("strike_id") or msg.get("MessageId", "unknown"))
    except Exception:
        return str(msg.get("MessageId", "unknown"))


class QueueService:

    def __init__(self):
        self.sqs = boto3.client("sqs", region_name=AWS_REGION)
        self._notification_url = None

    def receive_messages(self) -> list[dict]:
        if not INPUT_QUEUE_URL:
            logger.error("No INPUT_QUEUE_URL configured")
            return []

        resp = self.sqs.receive_message(
            QueueUrl=INPUT_QUEUE_URL,
            MaxNumberOfMessages=MAX_MESSAGES,
            VisibilityTimeout=VISIBILITY_TIMEOUT,
            WaitTimeSeconds=WAIT_TIME_SECONDS,
            AttributeNames=["ApproximateReceiveCount"],
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
        """Make message immediately visible again in the queue."""
        try:
            self.sqs.change_message_visibility(
                QueueUrl=INPUT_QUEUE_URL,
                ReceiptHandle=msg["ReceiptHandle"],
                VisibilityTimeout=0,
            )
            logger.info("Returned to input queue strike_id %s", strike_id_of(msg))
        except Exception as e:
            logger.error("Failed to return message: %s", e)

    def send_alert(self, text: str) -> bool:
        """Post an alert to the notification queue (the same queue the dispatcher posts to).
        Never raises. Returns True when the alert was sent."""
        try:
            if self._notification_url is None:
                ssm = boto3.client("ssm", region_name=AWS_REGION)
                self._notification_url = ssm.get_parameter(
                    Name=NOTIFICATION_PARAM, WithDecryption=True
                )["Parameter"]["Value"]
            self.sqs.send_message(QueueUrl=self._notification_url, MessageBody=text)
            return True
        except Exception as e:
            logger.error("Alert could not be sent %s", e)
            return False

    def delete_message(self, msg: dict):
        self.sqs.delete_message(
            QueueUrl=INPUT_QUEUE_URL,
            ReceiptHandle=msg["ReceiptHandle"],
        )

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
