import os

AWS_REGION              = os.getenv("AWS_REGION", "us-east-1")
ENV                     = os.getenv("ENV", "dev")
INPUT_QUEUE_URL         = os.getenv("INPUT_QUEUE_URL", "")
OUTPUT_QUEUE_URL        = os.getenv("OUTPUT_QUEUE_URL")

# Notification queue used for alerts (same queue the dispatcher posts to). Its URL is read
# from this SSM parameter.
NOTIFICATION_PARAM      = f"/google-scraper-engine/{ENV}/notification_sqs_url"

# Time (seconds) to stay alive after the queue empties before scaling down.
QUEUE_IDLE_TIMEOUT      = int(os.getenv("QUEUE_IDLE_TIMEOUT", "30"))

# SQS
MAX_MESSAGES       = int(os.getenv("MAX_MESSAGES", "10"))
VISIBILITY_TIMEOUT = int(os.getenv("VISIBILITY_TIMEOUT", "300"))
WAIT_TIME_SECONDS  = int(os.getenv("WAIT_TIME_SECONDS", "5"))

# Scraper
PAGE_LOAD_WAIT       = int(os.getenv("PAGE_LOAD_WAIT", "8"))
MAX_MORE_STORES_CLICKS = int(os.getenv("MAX_MORE_STORES_CLICKS", "4"))
MAX_SELLERS          = int(os.getenv("MAX_SELLERS", "25"))
DELAY_MIN            = int(os.getenv("DELAY_MIN", "1"))
DELAY_MAX            = int(os.getenv("DELAY_MAX", "3"))

# Seconds to wait (polling) for the oapv response after a "More stores" click.
OAPV_WAIT_TIMEOUT    = int(os.getenv("OAPV_WAIT_TIMEOUT", "10"))

# NO_SELLER handling flag.
#   true  : a NO_SELLER from the request method is checked again with Chrome, and a Chrome
#           that also finds no sellers is retried in a NEW Chrome (see MAX_RETRIES).
#   false : old method - a NO_SELLER from the request method is final.
NO_SELLER_RETRY      = os.getenv("NO_SELLER_RETRY", "true").lower() == "true"

# How many times a URL is re-checked in a NEW Chrome after the first Selenium check also
# found no sellers (original + MAX_RETRIES). Only used when NO_SELLER_RETRY is true.
MAX_RETRIES          = int(os.getenv("MAX_RETRIES", "1"))

# Session scope flag.
#   true  : the session is used only inside the batch it was collected in. The first input of
#           every batch collects a new session with Chrome.
#   false : the session can be used by the next batches too.
BATCH_SESSION        = os.getenv("BATCH_SESSION", "false").lower() == "true"

# Stop starting new work and return the unfinished inputs when a batch has been running this
# many seconds. Must stay below VISIBILITY_TIMEOUT.
BATCH_TIME_LIMIT_SEC = int(os.getenv("BATCH_TIME_LIMIT_SEC", "280"))

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
