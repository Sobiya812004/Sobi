import os

AWS_REGION              = os.getenv("AWS_REGION", "us-east-1")
ENV                     = os.getenv("ENV", "dev")
INPUT_QUEUE_URL         = os.getenv("INPUT_QUEUE_URL", "")
OUTPUT_QUEUE_URL        = os.getenv("OUTPUT_QUEUE_URL")

# Queue that receives bad (unparseable) messages. Can be the same queue that is
# configured as the redrive DLQ of the input queue.
DLQ_URL                 = os.getenv("DLQ_URL", "")

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

# Seconds to wait (polling) for the oapv response after a "More stores" click.
OAPV_WAIT_TIMEOUT    = int(os.getenv("OAPV_WAIT_TIMEOUT", "10"))

# NO_SELLER handling: how many times a URL is re-checked in a NEW Chrome after the
# first Selenium check also found no sellers (original + MAX_RETRIES).
MAX_RETRIES          = int(os.getenv("MAX_RETRIES", "1"))

# CAPTCHA handling: quit Chrome, wait random seconds, open new Chrome, retry.
CAPTCHA_MAX_RETRIES  = int(os.getenv("CAPTCHA_MAX_RETRIES", "2"))
CAPTCHA_DELAY_MIN    = float(os.getenv("CAPTCHA_DELAY_MIN", "3"))
CAPTCHA_DELAY_MAX    = float(os.getenv("CAPTCHA_DELAY_MAX", "6"))

# Seller names: allow non-Latin letters (Hindi, Chinese, Tamil, ...). Undecided -> off.
ALLOW_NON_LATIN_SELLER_NAMES = os.getenv("ALLOW_NON_LATIN_SELLER_NAMES", "false").lower() == "true"

# ASG
ASG_NAME = f"{ENV}-google-scraper-engine-worker"

# AWS Instance Metadata Service (IMDS) — standard AWS link-local address
IMDS_BASE         = "http://169.254.169.254"
IMDS_TOKEN_URL    = f"{IMDS_BASE}/latest/api/token"
IMDS_INSTANCE_URL = f"{IMDS_BASE}/latest/meta-data/instance-id"
IMDS_SPOT_URL     = f"{IMDS_BASE}/latest/meta-data/spot/instance-action"
