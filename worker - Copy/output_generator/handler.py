"""
Output Generator Lambda
- Queries DynamoDB for all records for a client on a given date and batch
- Generates a TSV output file (tab-separated columns, pipe-separated client_metadata)
- Seller fields are expanded into individual columns (seller_limit * 8 columns)
- Uploads to S3

Event format:
{
    "client": "officesupply",
    "report_date": "2025-05-18",  # optional, defaults to today
    "batch_id": 1                 # optional, defaults to 1
}
"""

import json
import os
import boto3
import logging
from datetime import datetime, timezone
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")

TABLE_NAME = os.environ["TABLE_NAME"]
DATAFILES_BUCKET = os.environ["DATAFILES_BUCKET"]
ENV = os.environ.get("ENV", "dev")

CLIENTS_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "clients.json")
ALL_SELLER_FIELDS = ["name", "price", "shipping", "url", "stock", "rating", "reviews", "condition"]


def load_client_config(client: str) -> tuple[int, list[str]]:
    with open(CLIENTS_CONFIG_PATH) as f:
        configs = json.load(f)
    cfg = configs.get(client, {})
    seller_limit  = cfg.get("seller_limit", 25)
    seller_fields = cfg.get("seller_fields", ALL_SELLER_FIELDS)
    return seller_limit, seller_fields


def build_header(seller_limit: int, seller_fields: list[str]) -> str:
    base = ["strike_id", "client_metadata", "status", "scraped_at", "seller_count"]
    seller_cols = [f"seller{i+1}_{field}" for i in range(seller_limit) for field in seller_fields]
    return "\t".join(base + seller_cols)


def build_row(item: dict, seller_limit: int, seller_fields: list[str]) -> str:
    client_metadata = item.get("client_metadata", "").replace("\t", "|")
    base = [
        item.get("strike_id", ""),
        client_metadata,
        item.get("status", ""),
        item.get("scraped_at", ""),
        str(item.get("seller_count", 0)),
    ]
    sellers = item.get("sellers", [])
    seller_cols = []
    for i in range(seller_limit):
        if i < len(sellers):
            s = sellers[i]
            seller_cols.extend([
                s.get("name", "").replace("\t", " ").replace("|", " ").strip() if field == "name" else
                str(s.get(field, ""))
                for field in seller_fields
            ])
        else:
            seller_cols.extend([""] * len(seller_fields))
    return "\t".join(base + seller_cols)


def lambda_handler(event, context):
    client = event["client"]
    report_date = event.get("report_date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    batch_id = int(event.get("batch_id", 1))

    logger.info("Generating output for client=%s report_date=%s batch_id=%d", client, report_date, batch_id)

    seller_limit, seller_fields = load_client_config(client)
    client_date_key = f"{client}#{report_date}#{batch_id}"
    logger.info("Querying DynamoDB table=%s key=%s seller_limit=%d seller_fields=%s",
                TABLE_NAME, client_date_key, seller_limit, seller_fields)

    table = dynamodb.Table(TABLE_NAME)
    kwargs = {"KeyConditionExpression": Key("client_date").eq(client_date_key)}
    query_start = datetime.now(timezone.utc)

    timestamp = datetime.now(timezone.utc).strftime("%m%d%Y_%H%M")
    s3_key    = f"price_crawling/{client}/google/output/output_{timestamp}.txt"
    tmp_path  = f"/tmp/output_{timestamp}.txt"

    row_count    = 0
    status_counts = {}
    write_start  = datetime.now(timezone.utc)

    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(build_header(seller_limit, seller_fields) + "\n")
        while True:
            resp = table.query(**kwargs)
            for item in resp["Items"]:
                f.write(build_row(item, seller_limit, seller_fields) + "\n")
                row_count += 1
                s = item.get("status", "UNKNOWN")
                status_counts[s] = status_counts.get(s, 0) + 1
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    query_ms = int((datetime.now(timezone.utc) - query_start).total_seconds() * 1000)
    logger.info("DynamoDB query+write complete: records=%d query_ms=%d", row_count, query_ms)

    if row_count == 0:
        os.remove(tmp_path)
        return {"statusCode": 200, "body": json.dumps({"records": 0, "s3_key": None})}

    logger.info("Status breakdown: %s", " ".join(f"{k}={v}" for k, v in sorted(status_counts.items())))
    write_ms = int((datetime.now(timezone.utc) - write_start).total_seconds() * 1000)
    logger.info("File written to /tmp: rows=%d write_ms=%d", row_count, write_ms)

    upload_start = datetime.now(timezone.utc)
    s3.upload_file(tmp_path, DATAFILES_BUCKET, s3_key)
    upload_ms = int((datetime.now(timezone.utc) - upload_start).total_seconds() * 1000)
    os.remove(tmp_path)

    logger.info(
        "Output written to s3://%s/%s rows=%d upload_ms=%d total_ms=%d",
        DATAFILES_BUCKET,
        s3_key,
        row_count,
        upload_ms,
        int((datetime.now(timezone.utc) - query_start).total_seconds() * 1000),
    )

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "records": row_count,
                "s3_key": s3_key,
                "s3_bucket": DATAFILES_BUCKET,
                "batch_id": batch_id,
            }
        ),
    }
