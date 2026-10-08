import json
import os
import logging
import boto3
from datetime import datetime, timezone
from decimal import Decimal

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")

TABLE_NAME = os.environ["TABLE_NAME"]
ENV = os.environ.get("ENV", "dev")


def parse_output(output_str: str) -> list[dict]:
    parts = output_str.split("|")
    sellers = []
    for i in range(1, len(parts) - 7, 8):
        try:
            sellers.append({
                "name":      parts[i],
                "price":     Decimal(parts[i + 1]) if parts[i + 1].replace(".", "", 1).isdigit() else parts[i + 1],
                "shipping":  Decimal(parts[i + 2]) if parts[i + 2].replace(".", "", 1).isdigit() else parts[i + 2],
                "url":       parts[i + 3],
                "stock":     parts[i + 4],
                "rating":    parts[i + 5],
                "reviews":   parts[i + 6],
                "condition": parts[i + 7],
            })
        except Exception:
            continue
    return sellers


def lambda_handler(event, context):
    table = dynamodb.Table(TABLE_NAME)
    processed = 0
    failed = 0

    for record in event["Records"]:
        try:
            body = json.loads(record["body"])

            message_id          = body["message_id"]
            client_name         = body["client_name"]
            report_date         = body["report_date"]
            batch_id            = int(body.get("batch_id", 1))
            strike_id           = body["strike_id"]
            client_metadata     = body["client_metadata"]
            google_shopping_url = body["google_shopping_url"]
            seller_limit        = int(body.get("seller_limit", 25))
            status              = body["status"]
            output              = body.get("output")

            now        = datetime.now(timezone.utc)
            scraped_at = now.strftime("%Y-%m-%d %H:%M:%S")
            sellers    = parse_output(output) if output else []

            item = {
                # PK: client#date#batch_id
                "client_date":        f"{client_name}#{report_date}#{batch_id}",
                # SK
                "strike_id":          strike_id,
                "client_date_only":   f"{client_name}#{report_date}",
                # Message fields
                "message_id":         message_id,
                "client_name":        client_name,
                "report_date":        report_date,
                "batch_id":           batch_id,
                "client_metadata":    client_metadata,
                "google_shopping_url": google_shopping_url,
                "seller_limit":       seller_limit,
                # Scrape results
                "status":             status,
                "scraped_at":         scraped_at,
                "sellers":            sellers,
                "seller_count":       len(sellers),
                # Meta
                "env":                ENV,
                "ttl":                int(now.timestamp()) + (90 * 24 * 60 * 60),
            }

            table.put_item(Item=item)
            processed += 1

        except Exception as e:
            logger.error("Error processing record: %s | body: %s", e, record.get("body"), exc_info=True)
            failed += 1

    logger.info("Processed: %d, Failed: %d", processed, failed)
    return {"statusCode": 200, "body": json.dumps({"processed": processed, "failed": failed})}
