"""
Dispatcher Lambda
- Reads client config from clients.json
- Reads input file from S3
- Builds structured message per row
- Sends to the client's SQS input queue
- Scales up ASG workers

Event format:
{
    "client": "officesupply",
    "report_date": "2025-05-18",   # optional, defaults to today
    "batch_id": 1,                 # optional, defaults to 1
    "worker_count": 2              # optional, defaults to client default_workers in clients.json
    "start": 1,                    # optional, no default — if omitted (along with "end"), all rows are read
    "end": 10000                   # optional, no default — 1-indexed, inclusive
                                   # Used to split a large (100k+) input file across multiple scheduled
                                   # invocations, e.g. start=1/end=10000, start=10001/end=20000, etc.
}
"""

import json
import os
import uuid
import boto3
import logging
from datetime import datetime, timezone
import re
import math
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
ssm = boto3.client("ssm")

ENV            = os.environ.get("ENV", "dev")
CLIENTS_CONFIG = os.path.join(os.path.dirname(__file__), "clients.json")
DATAFILES_BUCKET = os.environ.get("DATAFILES_BUCKET", "sap-datafiles")
TABLE_NAME       = os.environ.get("TABLE_NAME", "")
REGION           = "us-east-1"

dynamodb = boto3.resource("dynamodb")

# 1 worker per every 10 inputs (rounded up), minimum 1 worker.
INPUTS_PER_WORKER = 10
# Once input count goes beyond this, cap worker_count at MAX_WORKER_COUNT instead
# of continuing to scale linearly.
INPUT_COUNT_CAP_THRESHOLD = 1000
MAX_WORKER_COUNT = 100


def compute_worker_count(input_count: int) -> int:
    """Derive worker_count from input row count: 1 worker per 10 inputs, min 1,
    capped at MAX_WORKER_COUNT once input_count exceeds INPUT_COUNT_CAP_THRESHOLD."""
    if input_count <= 0:
        return 0
    if input_count > INPUT_COUNT_CAP_THRESHOLD:
        return MAX_WORKER_COUNT
    return max(1, math.ceil(input_count / INPUTS_PER_WORKER))

def get_chitti_sqs_url() -> str:
    param_name = f"/google-scraper-engine/{ENV}/notification_sqs_url"
    return ssm.get_parameter(Name=param_name, WithDecryption=True)["Parameter"]["Value"]

def chitti_post(sqs_url: str, message: str):
    try:
        sqs = boto3.client("sqs", region_name=REGION)
        sqs.send_message(QueueUrl=sqs_url, MessageBody=message)
        logger.info("Chitti post sent: %s", message)
    except Exception as e:
        logger.error("Chitti post failed: %s", e)

def load_client_config(client: str) -> dict:
    with open(CLIENTS_CONFIG) as f:
        configs = json.load(f)
    if client not in configs:
        raise ValueError(f"Unknown client: {client}")
    return configs[client]


def get_account_id() -> str:
    return boto3.client("sts").get_caller_identity()["Account"]


def get_dlq_url(client: str, account_id: str) -> str:
    return f"https://sqs.{REGION}.amazonaws.com/{account_id}/{ENV}-google-scraper-engine-{client}-input-dlq"


def drain_dlq_messages(client: str, account_id: str) -> list:
    """Read (and hold, not delete) all messages currently sitting in the client DLQ."""
    dlq_url = get_dlq_url(client, account_id)
    sqs     = boto3.client("sqs", region_name=REGION)

    all_messages = []
    while True:
        resp = sqs.receive_message(
            QueueUrl=dlq_url,
            MaxNumberOfMessages=10,
            VisibilityTimeout=120,
            WaitTimeSeconds=5,
        )
        batch = resp.get("Messages", [])
        if not batch:
            break
        all_messages.extend(batch)
        logger.info("Collected %d messages from DLQ so far (client=%s)", len(all_messages), client)

    if not all_messages:
        logger.info("DLQ empty for client=%s", client)
    else:
        logger.info("Total %d messages collected from DLQ for client=%s", len(all_messages), client)

    return all_messages


def redispatch_messages(client: str, account_id: str, all_messages: list) -> dict:
    """Redispatch previously-drained DLQ messages to the input queue in batches of 10."""
    dlq_url   = get_dlq_url(client, account_id)
    queue_url = get_queue_url(client, account_id)
    sqs       = boto3.client("sqs", region_name=REGION)

    if not all_messages:
        return {"sent": 0, "dlq_empty": True}

    # Redispatch in batches of 10
    total_sent   = 0
    total_failed = 0
    to_delete    = []  # receipt handles of successfully dispatched messages

    for i in range(0, len(all_messages), 10):
        batch = all_messages[i:i + 10]

        entries   = []
        batch_map = {}
        for idx, msg in enumerate(batch):
            try:
                entry_id = str(idx)
                entries.append({"Id": entry_id, "MessageBody": msg["Body"]})
                batch_map[entry_id] = msg
            except Exception as e:
                logger.error("Failed to build redispatch entry: %s", e)
                total_failed += 1

        if not entries:
            continue

        try:
            result = sqs.send_message_batch(QueueUrl=queue_url, Entries=entries)
            for s in result.get("Successful", []):
                total_sent += 1
                to_delete.append(batch_map[s["Id"]]["ReceiptHandle"])
            for f in result.get("Failed", []):
                total_failed += 1
                logger.error("Failed to send msg %s: %s", f["Id"], f)
            logger.info("Redispatched batch (%d msgs)", len(result.get("Successful", [])))
        except Exception as e:
            logger.error("Batch send error: %s", e)
            total_failed += len(entries)

    # Step 3: Delete successfully dispatched messages from DLQ in batches of 10
    for i in range(0, len(to_delete), 10):
        delete_batch = [{"Id": str(j), "ReceiptHandle": rh} for j, rh in enumerate(to_delete[i:i + 10])]
        try:
            sqs.delete_message_batch(QueueUrl=dlq_url, Entries=delete_batch)
        except Exception as e:
            logger.error("Failed to delete DLQ batch: %s", e)

    logger.info("DLQ redispatch done: sent=%d failed=%d", total_sent, total_failed)
    return {"sent": total_sent, "failed": total_failed}

def get_queue_url(client: str, account_id: str) -> str:
    return f"https://sqs.{REGION}.amazonaws.com/{account_id}/{ENV}-google-scraper-engine-{client}-input-queue"


def scale_workers(client: str, desired: int) -> int:
    asg_name   = f"{ENV}-google-scraper-engine-{client}-worker"
    asg_client = boto3.client("autoscaling", region_name=REGION)

    try:
        resp = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        groups = resp.get("AutoScalingGroups", [])
        if groups:
            max_capacity = groups[0]["MaxSize"]
            if desired > max_capacity:
                logger.warning("Requested worker_count=%d exceeds max_capacity=%d for %s; capping to %d", desired, max_capacity, asg_name, max_capacity,)
                desired = max_capacity
        else:
            logger.error("ASG %s not found while checking max_capacity", asg_name)
    except Exception as e:
        logger.error("Failed to describe ASG %s for max_capacity check: %s", asg_name, e)

    logger.info("Scaling %s to desired=%d", asg_name, desired)
    try:
        asg_client.update_auto_scaling_group(AutoScalingGroupName=asg_name, MinSize=desired)
        asg_client.set_desired_capacity(AutoScalingGroupName=asg_name, DesiredCapacity=desired, HonorCooldown=False)
        logger.info("ASG %s scaled to %d", asg_name, desired)
    except Exception as e:
        logger.error("Failed to scale ASG %s: %s", asg_name, e)
    finally:
        try:
            asg_client.update_auto_scaling_group(AutoScalingGroupName=asg_name, MinSize=0)
            logger.info("ASG %s min reset to 0", asg_name)
        except Exception as e:
            logger.error("Failed to reset MinSize for ASG %s: %s", asg_name, e)

    return desired

def write_pending_records(rows: list, client: str, report_date: str, batch_id: int, url_column: int, seller_limit: int):
    """Write PENDING records to DynamoDB before sending to SQS."""
    if not TABLE_NAME:
        logger.warning("TABLE_NAME not set — skipping PENDING writes")
        return
    table = dynamodb.Table(TABLE_NAME)
    now = datetime.now(timezone.utc)
    ttl = int(now.timestamp()) + (90 * 24 * 60 * 60)

    def _put(row):
        msg = build_message(row, client, report_date, batch_id, url_column, seller_limit)
        table.put_item(Item={
            "client_date":         f"{client}#{report_date}#{batch_id}",
            "strike_id":           msg["strike_id"],
            "client_date_only":    f"{client}#{report_date}",
            "client_name":         client,
            "report_date":         report_date,
            "batch_id":            batch_id,
            "client_metadata":     msg["client_metadata"],
            "google_shopping_url": msg["google_shopping_url"],
            "seller_limit":        seller_limit,
            "status":              "PENDING",
            "env":                 ENV,
            "ttl":                 ttl,
        })

    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = [ex.submit(_put, row) for row in rows]
        failed = 0
        for f in futures:
            try:
                f.result()
            except Exception as e:
                logger.error("Failed to write PENDING record: %s", e)
                failed += 1
    logger.info("PENDING records written: %d succeeded, %d failed", len(rows) - failed, failed)


def transform_google_shopping_url(url: str) -> str:
    if "async/oapv" not in url:
        return url

    id_match = re.search(r"(catalogid|gpcid):(\d+)", url)
    if not id_match:
        return url
    id_type, id_value = id_match.group(1), id_match.group(2)

    prds = f"{id_type}:{id_value}"

    # Append pvf if present in the async params
    pvf_match = re.search(r"pvf:([^,&]+)", url)
    if pvf_match:
        prds += f",pvf:{pvf_match.group(1)}"

    return f"https://www.google.com/search?ibp=oshop&q=Pivot&prds={prds}"


def build_message(
    row: str, client_name: str, report_date: str, batch_id: int, url_column: int, seller_limit: int
) -> dict:
    parts = row.split("\t")
    strike_id = parts[0].strip()
    google_shopping_url = parts[url_column].strip() if len(parts) > url_column else ""
    google_shopping_url = transform_google_shopping_url(google_shopping_url)

    if len(parts) > url_column:
        parts[url_column] = google_shopping_url
        client_metadata = "\t".join(parts)
    else:
        client_metadata = row

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    return {
        "message_id": str(uuid.uuid4()),
        "client_name": client_name,
        "report_date": report_date,
        "batch_id": batch_id,
        "strike_id": strike_id,
        "client_metadata": client_metadata,
        "google_shopping_url": google_shopping_url,
        "seller_limit": seller_limit,
        "created_at": now,
    }


ID_PATTERN = re.compile(r"(catalogid|gpcid):\d+")


def validate_rows(rows: list, url_column: int, row_offset: int = 0, client_name: str = "",
                  report_date: str = "", batch_id: int = 1, seller_limit: int = 25) -> list:
    """Check every input row BEFORE any worker is started, by building the message exactly the way
    the dispatch will build it (so the Travelhouse async/oapv url change is checked at that time).

    A row is bad when
      - the strike id is empty,
      - the url column is missing or the url is empty,
      - the message cannot be built (the row format is wrong),
      - the url (after the Travelhouse change) has no gpcid or catalogid.
    Returns a list of {"row": data row number, "strike_id": ..., "reason": ..., "travelhouse": bool}."""
    bad = []
    for idx, row in enumerate(rows):
        parts = row.split("\t")
        strike_id = parts[0].strip()
        raw_url = parts[url_column].strip() if len(parts) > url_column else ""
        travelhouse = "async/oapv" in raw_url
        reason = None
        if not strike_id:
            reason = "strike id is empty"
        elif len(parts) <= url_column:
            reason = f"url column {url_column} is missing as the row has only {len(parts)} columns"
        elif not raw_url:
            reason = "url is empty"
        else:
            try:
                msg = build_message(row, client_name, report_date, batch_id, url_column, seller_limit)
            except Exception as e:
                reason = f"row format is wrong {type(e).__name__} {e}"
            else:
                if not ID_PATTERN.search(msg.get("google_shopping_url", "")):
                    reason = ("travelhouse url could not be changed as it has no gpcid or catalogid"
                              if travelhouse else "url has no gpcid or catalogid")
        if reason:
            bad.append({"row": row_offset + idx + 1, "strike_id": strike_id or "empty",
                        "reason": reason, "travelhouse": travelhouse})
    return bad


def lambda_handler(event, context):
    client = event["client"]
    explicit_worker_count = event.get("worker_count")
    
    # DLQ redispatch: read all messages from DLQ and redistribute to the input queue.
    # Event: {"client": "officesupply", "from_dlq": true, "worker_count": 50}
    if event.get("from_dlq"):
        account_id = get_account_id()
        messages = drain_dlq_messages(client, account_id)

        if not messages:       #in dlq no meassages don't need to redispatch
            logger.info("DLQ redispatch skipped: no messages for client=%s", client)
            return {"statusCode": 200, "body": json.dumps({"sent": 0, "dlq_empty": True, "worker_count": 0})}

        worker_count = int(explicit_worker_count) if explicit_worker_count is not None else compute_worker_count(len(messages))
        logger.info("DLQ redispatch client=%s messages=%d requested_workers=%d", client, len(messages), worker_count)
        worker_count = scale_workers(client, worker_count)
        result = redispatch_messages(client, account_id, messages)
        result["worker_count"] = worker_count
        chitti_post(get_chitti_sqs_url(), f"{client} DLQ  messages={len(messages)} dispatched successfully.")
        return {"statusCode": 200, "body": json.dumps(result)}

    report_date = event.get("report_date", datetime.now().strftime("%Y-%m-%d"))
    batch_id = int(event.get("batch_id", 1))
    start = event.get("start")  # 1-indexed, inclusive — optional, no default
    end   = event.get("end")    # 1-indexed, inclusive — optional, no default
    config = load_client_config(client)
    url_column = config.get("url_column", 2)
    seller_limit = config.get("seller_limit", 25)
    logger.info("Client config: url_column=%d seller_limit=%d", url_column, seller_limit)

    s3_bucket = event.get("s3_bucket", DATAFILES_BUCKET)
    s3_key = event.get("s3_key", f"price_crawling/{client}/google/input/input.txt")

    account_id = get_account_id()
    queue_url  = get_queue_url(client, account_id)
    sqs        = boto3.client("sqs", region_name=REGION)

    obj = s3.get_object(Bucket=s3_bucket, Key=s3_key)
    content = obj["Body"].read().decode("utf-8", errors="replace")

    rows = [line.strip() for line in content.splitlines() if line.strip() and not line.startswith("#")]
    logger.info("Parsed %d rows", len(rows))

    if start is not None or end is not None:
        start_idx = max(int(start) - 1, 0) if start is not None else 0
        end_idx   = int(end) if end is not None else len(rows)
        rows = rows[start_idx:end_idx]
        logger.info("Partition [%s:%s] -> %d rows", start, end, len(rows))

    sqs_url = get_chitti_sqs_url()

    if not rows:
        chitti_post(sqs_url, f"ALERT\n{client} Batch {batch_id} — No input rows found. Please check S3 input file: {s3_key}")
        return {"statusCode": 200, "body": json.dumps({"sent": 0})}
    
    # Bad input must be fixed first. Nothing is scaled, written or sent until every row is fine.
    row_offset = max(int(start) - 1, 0) if start is not None else 0
    bad_rows = validate_rows(rows, url_column, row_offset, client, report_date, batch_id, seller_limit)
    if bad_rows:
        chitti_post(sqs_url, (f"ALERT\nCrawling initiation failed for {client} Batch {batch_id} — Input format is wrong.\nBad rows {len(bad_rows)} of {len(rows)}"))
        return {"statusCode": 200, "body": json.dumps({"sent": 0, "blocked": True, "bad_rows": len(bad_rows)})}
    logger.info("Input validation passed rows %d", len(rows))

    worker_count = int(explicit_worker_count) if explicit_worker_count is not None else compute_worker_count(len(rows))
        
    worker_count = scale_workers(client, worker_count)
    write_pending_records(rows, client, report_date, batch_id, url_column, seller_limit)
    
    logger.info("Dispatching client=%s report_date=%s batch_id=%d workers=%d start=%s end=%s input_count=%d",
            client, report_date, batch_id, worker_count, start, end, len(rows))
    
    # Scan for legacy async/oapv URLs before dispatch
    async_oapv_count = 0
    for row in rows:
        parts = row.split("\t")
        row_url = parts[url_column].strip() if len(parts) > url_column else ""
        if "async/oapv" in row_url:
            async_oapv_count += 1
    logger.info("Client=%s Travelhouse URL found: %d / %d", client, async_oapv_count, len(rows))

    range_str = f"Range: {start}–{end}" if (start is not None or end is not None) else "Range: all"
    chitti_post(sqs_url, f"{client} Batch {batch_id}, Travelhouse URL {async_oapv_count}, input count - {len(rows)} ({range_str}) dispatched successfully.")

    sent = 0
    failed = 0
    for i in range(0, len(rows), 10):
        batch = rows[i:i + 10]

        entries = []
        id_to_row = {}
        for idx, row in enumerate(batch):
            try:
                entry_id = str(idx)
                entries.append({
                    "Id": entry_id,
                    "MessageBody": json.dumps(build_message(row, client, report_date, batch_id, url_column, seller_limit)),
                })
                id_to_row[entry_id] = row
            except Exception as e:
                logger.error("Failed to build message row %d: %s | row: %.200s", i + idx, e, row)
                failed += 1
        if not entries:
            continue
        try:
            resp = sqs.send_message_batch(QueueUrl=queue_url, Entries=entries)
            sent += len(resp.get("Successful", []))
            failed += len(resp.get("Failed", []))
            for f in resp.get("Failed", []):
                logger.error("Failed to send row %d: %s | row: %.200s",
                             i + int(f["Id"]), f, id_to_row.get(f["Id"], "?"))
        except Exception as e:
            logger.error("Batch send error at row %d: %s", i, e)
            failed += len(entries)

    logger.info("Done. sent=%d failed=%d", sent, failed)
    return {"statusCode": 200, "body": json.dumps({"sent": sent, "failed": failed})}
