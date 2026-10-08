import os

AWS_REGION              = os.getenv("AWS_REGION", "us-east-1")
ENV                     = os.getenv("ENV", "dev")
INPUT_QUEUE_URL         = os.getenv("INPUT_QUEUE_URL", "")
OUTPUT_QUEUE_URL        = os.getenv("OUTPUT_QUEUE_URL")

# Time (seconds) to stay alive after the queue empties before scaling down.
QUEUE_IDLE_TIMEOUT      = int(os.getenv("QUEUE_IDLE_TIMEOUT", "30"))

# SQS
MAX_MESSAGES       = int(os.getenv("MAX_MESSAGES", "10"))
VISIBILITY_TIMEOUT = int(os.getenv("VISIBILITY_TIMEOUT", "300"))
WAIT_TIME_SECONDS  = int(os.getenv("WAIT_TIME_SECONDS", "5"))

# Scraper
PAGE_LOAD_WAIT       = int(os.getenv("PAGE_LOAD_WAIT", "8"))
MAX_MORE_STORES_CLICKS = int(os.getenv("MAX_MORE_STORES_CLICKS", "3"))
MAX_SELLERS          = int(os.getenv("MAX_SELLERS", "25"))
DELAY_MIN            = int(os.getenv("DELAY_MIN", "1"))
DELAY_MAX            = int(os.getenv("DELAY_MAX", "3"))

# ASG
ASG_NAME = f"{ENV}-google-scraper-engine-worker"

# AWS Instance Metadata Service (IMDS) — standard AWS link-local address
IMDS_BASE         = "http://169.254.169.254"
IMDS_TOKEN_URL    = f"{IMDS_BASE}/latest/api/token"
IMDS_INSTANCE_URL = f"{IMDS_BASE}/latest/meta-data/instance-id"
IMDS_SPOT_URL     = f"{IMDS_BASE}/latest/meta-data/spot/instance-action"
