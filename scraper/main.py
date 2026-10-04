import json
import signal
import logging
import time
import random
import uuid
import boto3
import requests as http_requests
from datetime import datetime

from worker.scraper.config import (
    IMDS_TOKEN_URL, IMDS_INSTANCE_URL, IMDS_SPOT_URL,
    DELAY_MIN, DELAY_MAX, AWS_REGION, QUEUE_IDLE_TIMEOUT,
)
from worker.scraper.models import Request, Product
from worker.scraper.scraper import Scraper, CaptchaError, SessionExpiredError
from worker.scraper.queue_service import QueueService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_shutdown = False


def _handle_signal(sig, frame):
    global _shutdown
    logger.info("Received signal %s — shutting down", sig)
    _shutdown = True


# ------------------IMDS / ASG------------------

def _imds_token() -> str:
    return http_requests.put(
        IMDS_TOKEN_URL,
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        timeout=2
    ).text.strip()


def is_spot_interrupted() -> bool:
    try:
        token = _imds_token()
        r = http_requests.get(
            IMDS_SPOT_URL,
            headers={"X-aws-ec2-metadata-token": token},
            timeout=2
        )
        return r.status_code == 200
    except Exception:
        return False


def get_instance_id() -> str:
    token = _imds_token()
    return http_requests.get(
        IMDS_INSTANCE_URL,
        headers={"X-aws-ec2-metadata-token": token},
        timeout=2
    ).text.strip()


def scale_down_self():
    try:
        instance_id = get_instance_id()
        asg_client = boto3.client("autoscaling", region_name=AWS_REGION)
        logger.info("Terminating self (%s) and decrementing ASG desired", instance_id)
        for attempt in range(5):
            try:
                asg_client.terminate_instance_in_auto_scaling_group(
                    InstanceId=instance_id,
                    ShouldDecrementDesiredCapacity=True,
                )
                return
            except Exception as e:
                if attempt < 4:
                    wait = (2 ** attempt) * 10 + random.uniform(0, 10)
                    logger.warning("Scale down throttled (attempt %d/5) — retrying in %.1fs: %s", attempt + 1, wait, e)
                    time.sleep(wait)
                else:
                    raise
    except Exception as e:
        logger.error("Failed to scale down: %s", e)


def terminate_for_fresh_ip():
    try:
        instance_id = get_instance_id()
        asg_client = boto3.client("autoscaling", region_name=AWS_REGION)
        logger.info("Terminating self (%s) for fresh IP — ASG will replace", instance_id)
        asg_client.terminate_instance_in_auto_scaling_group(
            InstanceId=instance_id,
            ShouldDecrementDesiredCapacity=False,
        )
    except Exception as e:
        logger.error("Failed to terminate for fresh IP: %s", e)


# ------------------helpers------------------

def build_requests_session(browser_cookies: list) -> http_requests.Session:
    session = http_requests.Session()
    for c in browser_cookies:
        session.cookies.set(c["name"], c["value"], domain=c.get("domain", ".google.com"))
    return session

def new_session(scraper) -> tuple:
    """Build a fresh requests session from the current browser cookies, tagging it
    with a unique session_id and creation timestamp so it can be traced end-to-end."""
    session_id = uuid.uuid4().hex[:12]
    created_at = datetime.now()
    session = build_requests_session(scraper.driver.get_cookies())
    logger.info("Session created: session_id=%s created_at=%s", session_id, created_at.isoformat())
    return session, session_id, created_at

def log_session_expired(session_id: str, created_at: datetime):
    """Log how long a session stayed valid before it expired."""
    if not session_id or not created_at:
        return
    valid_period_s = (datetime.now() - created_at).total_seconds()
    logger.info("Session expired: session_id=%s valid_period_s=%.1f", session_id, valid_period_s)

def log_session_payload(session_id: str, strike_id: str, payload_ms: int):
    """Log the time taken to fetch a payload (oapv/HTML response) using a given session."""
    logger.info("Session payload: session_id=%s strike_id=%s payload_ms=%d",
                session_id, strike_id, payload_ms)

def format_output(sellers) -> str:
    date_str = datetime.now().strftime("%Y-%m-%d")
    sellers_str = "|".join(
        "|".join([s.name, s.price, s.shipping, s.prod_url, str(s.stock), s.rating, s.reviews, s.condition])
        for s in sellers
    )
    return f"{date_str}|{sellers_str}"


def send_and_delete(queue, msg, req, sellers, scrape_ms: int = 0):
    status  = "OK" if sellers else "NO_SELLERS"
    output  = format_output(sellers) if sellers else None
    product = Product.from_request(req, output, status)
    queue.send_output(product)
    queue.delete_message(msg)
    logger.info("client=%s strike_id=%s status=%s sellers=%d scrape_ms=%d",
                req.client_name, req.strike_id, status, len(sellers), scrape_ms)


def _forward_on_error(queue: QueueService, msg: dict):
    """On error: return to queue — SQS's redrive policy (maxReceiveCount)
    promotes it to DLQ automatically after retries."""
    queue.return_message(msg)
    logger.info("Error: returned to queue for retry (SQS redrive handles DLQ)")


def _browser_navigate_with_retry(scraper, url: str, seller_limit: int):
    """Navigate Chrome to URL for session refresh.
    On captcha: WARNING + restart driver + full scrape_and_capture_template to properly
    warm up the new session (plain navigation after restart doesn't give OAPV-valid cookies).
    Returns (sellers, oapv_template) if restart occurred — caller uses browser result directly.
    Returns (None, None) if no captcha — caller should refresh cookies and retry via requests.
    Raises CaptchaError if captcha persists after restart."""
    scraper.driver.get(url)
    scraper.wait_for_page_ready()
    if "This page checks to see if it's really you" in scraper.driver.page_source:
        logger.warning("CAPTCHA during browser navigation — restarting driver once")
        scraper.restart()
        sellers, template = scraper.scrape_and_capture_template(url, seller_limit)
        return sellers, template
    return None, None


def captcha_terminate(queue, pending_messages, scraper):
    for msg in pending_messages:
        try:
            # Return to queue — SQS's redrive policy promotes to DLQ after maxReceiveCount retries.
            queue.return_message(msg)
            logger.info("Captcha: returned %s to queue",
                        json.loads(msg["Body"]).get("strike_id", "?"))
        except Exception as e:
            logger.error("Failed to handle captcha message: %s — returning to queue", e)
            queue.return_message(msg)

    scraper.stop()
    terminate_for_fresh_ip()
    _handle_signal(None, None)


# ------------------main------------------

def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    queue   = QueueService()
    scraper = Scraper()
    scraper.start()

    oapv_template    = None
    requests_session = None
    session_id       = None
    session_created_at = None

    processed             = 0
    captcha_count         = 0
    empty_polls           = 0
    session_expiry_fails  = 0
    request_captcha_fails = 0
    queue_empty_since     = None
    start_time            = datetime.now()
    logger.info("Worker started")

    # Random startup delay to avoid all instances hitting Google simultaneously
    startup_delay = random.uniform(5, 30)
    logger.info("Startup delay: %.1fs", startup_delay)
    time.sleep(startup_delay)

    try:
        while not _shutdown:

            if is_spot_interrupted():
                logger.warning("Spot interruption — stopping")
                break

            messages = queue.receive_messages()
            if not messages:
                empty_polls += 1
                if queue_empty_since is None:
                    queue_empty_since = datetime.now()
                    logger.info("Queue empty — will terminate in %ds if no new messages", QUEUE_IDLE_TIMEOUT)
                idle_secs = (datetime.now() - queue_empty_since).total_seconds()
                logger.info("Empty poll #%d (idle %ds)", empty_polls, int(idle_secs))
                if idle_secs >= QUEUE_IDLE_TIMEOUT:
                    if queue.get_total_messages() == 0:
                        logger.info("Own queue idle for %ds — scaling down", int(idle_secs))
                        scale_down_self()
                        break
                    else:
                        queue_empty_since = None
                        empty_polls = 0
                continue

            if queue_empty_since is not None:
                logger.info("Messages arrived after %ds idle — resuming",
                            int((datetime.now() - queue_empty_since).total_seconds()))
            queue_empty_since = None

            empty_polls       = 0
            pending           = list(messages)

            # ------ Message 1: browser scrape — refreshes session ------
            msg0 = messages[0]
            try:
                req0 = Request.from_message(json.loads(msg0["Body"]))
            except Exception as e:
                logger.error("Bad message: %s", e)
                queue.delete_message(msg0)
                pending.remove(msg0)
                messages = messages[1:]
            else:
                logger.info("[%d] browser | client=%s strike_id=%s",
                            processed + 1, req0.client_name, req0.strike_id)
                try:
                    t0 = datetime.now()
                    sellers, captured_template = scraper.scrape_and_capture_template(
                        req0.google_shopping_url, req0.seller_limit
                    )
                    scrape_ms = int((datetime.now() - t0).total_seconds() * 1000)
                    if captured_template:
                        oapv_template    = captured_template
                        requests_session, session_id, session_created_at = new_session(scraper)
                        logger.info("Session refreshed")
                    send_and_delete(queue, msg0, req0, sellers, scrape_ms)
                    pending.remove(msg0)
                    processed += 1
                    messages = messages[1:]
                except CaptchaError:
                    logger.warning("CAPTCHA on message 1 — restarting driver and retrying once")
                    scraper.restart()
                    try:
                        t0 = datetime.now()
                        sellers, captured_template = scraper.scrape_and_capture_template(
                            req0.google_shopping_url, req0.seller_limit
                        )
                        scrape_ms = int((datetime.now() - t0).total_seconds() * 1000)
                        if captured_template:
                            oapv_template    = captured_template
                            requests_session, session_id, session_created_at = new_session(scraper)
                            logger.info("Session refreshed after driver restart")
                        send_and_delete(queue, msg0, req0, sellers, scrape_ms)
                        pending.remove(msg0)
                        processed += 1
                        messages = messages[1:]
                    except CaptchaError:
                        captcha_count += 1
                        logger.error("CAPTCHA on message 1 after driver restart — terminating")
                        captcha_terminate(queue, pending, scraper)
                        break
                except Exception as e:
                    logger.error("Error on message 1 %s: %s", req0.strike_id, e, exc_info=True)
                    _forward_on_error(queue, msg0)
                    pending.remove(msg0)
                    messages = messages[1:]

            # ------ Messages 2-10: pure requests ------
            for msg in messages:
                if _shutdown or is_spot_interrupted():
                    break

                try:
                    req = Request.from_message(json.loads(msg["Body"]))
                except Exception as e:
                    logger.error("Bad message: %s", e)
                    queue.delete_message(msg)
                    pending.remove(msg)
                    continue

                logger.info("[%d] requests | client=%s strike_id=%s",
                            processed + 1, req.client_name, req.strike_id)

                try:
                    t0 = datetime.now()
                    if oapv_template and requests_session:
                        try:
                            payload_t0 = datetime.now()
                            sellers = scraper.scrape_via_requests(
                                req.google_shopping_url,
                                requests_session,
                                oapv_template,
                                req.seller_limit,
                            )
                            log_session_payload(
                                session_id, req.strike_id,
                                int((datetime.now() - payload_t0).total_seconds() * 1000)
                            )
                            session_expiry_fails  = 0
                            request_captcha_fails = 0
                        except SessionExpiredError:
                            # Plain navigation doesn't re-establish OAPV-valid cookies — only a
                            # full scrape (with the "More stores" click) does. Always restart
                            # Chrome and do a full re-scrape rather than just refreshing cookies.
                            logger.warning("Session expired on %s — restarting driver and refreshing session (%d/3)",
                                           req.strike_id, session_expiry_fails + 1)
                            log_session_expired(session_id, session_created_at)
                            scraper.restart()
                            try:
                                sellers, new_template = scraper.scrape_and_capture_template(
                                    req.google_shopping_url, req.seller_limit
                                )
                                if new_template:
                                    oapv_template = new_template
                                requests_session, session_id, session_created_at = new_session(scraper)
                                session_expiry_fails = 0
                                logger.info("Session refreshed via driver restart on %s", req.strike_id)
                            except CaptchaError:
                                session_expiry_fails += 1
                                if session_expiry_fails >= 3:
                                    raise CaptchaError(
                                        f"Session refresh failed {session_expiry_fails} consecutive times"
                                    )
                                logger.warning("Session refresh retry failed — forwarding (%d/3)", session_expiry_fails)
                                _forward_on_error(queue, msg)
                                pending.remove(msg)
                                continue
                        except CaptchaError:
                            logger.warning("Requests blocked on %s — refreshing browser (%d/3)",
                                           req.strike_id, request_captcha_fails + 1)
                            browser_sellers, new_template = _browser_navigate_with_retry(
                                scraper, req.google_shopping_url, req.seller_limit
                            )
                            if new_template:
                                oapv_template = new_template
                            requests_session, session_id, session_created_at = new_session(scraper)
                            if browser_sellers is not None:
                                sellers = browser_sellers
                                request_captcha_fails = 0
                                logger.info("Session fully refreshed after driver restart on %s", req.strike_id)
                            else:
                                try:
                                    payload_t0 = datetime.now()
                                    sellers = scraper.scrape_via_requests(
                                        req.google_shopping_url,
                                        requests_session,
                                        oapv_template,
                                        req.seller_limit,
                                    )
                                    log_session_payload(
                                        session_id, req.strike_id,
                                        int((datetime.now() - payload_t0).total_seconds() * 1000)
                                    )
                                    request_captcha_fails = 0
                                except (CaptchaError, SessionExpiredError):
                                    request_captcha_fails += 1
                                    if request_captcha_fails >= 3:
                                        raise CaptchaError(
                                            f"Requests captcha retry failed {request_captcha_fails} consecutive times"
                                        )
                                    logger.warning("Requests retry failed — forwarding (%d/3)", request_captcha_fails)
                                    _forward_on_error(queue, msg)
                                    pending.remove(msg)
                                    continue
                    else:
                        sellers, captured_template = scraper.scrape_and_capture_template(
                            req.google_shopping_url, req.seller_limit
                        )
                        if captured_template:
                            oapv_template    = captured_template
                            requests_session, session_id, session_created_at = new_session(scraper)
                    scrape_ms = int((datetime.now() - t0).total_seconds() * 1000)
                    send_and_delete(queue, msg, req, sellers, scrape_ms)
                    pending.remove(msg)
                    processed += 1
                    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

                except CaptchaError:
                    captcha_count += 1
                    logger.error("CAPTCHA on %s after driver restart — terminating", req.strike_id)
                    captcha_terminate(queue, pending, scraper)
                    break

                except Exception as e:
                    logger.error("Error processing %s: %s", req.strike_id, e, exc_info=True)
                    _forward_on_error(queue, msg)
                    pending.remove(msg)

            if _shutdown or is_spot_interrupted():
                logger.warning("Returning %d unprocessed messages", len(pending))
                for msg in pending:
                    queue.return_message(msg)
                break

    finally:
        scraper.stop()
        elapsed = datetime.now() - start_time
        logger.info("Worker stopped. processed=%d captchas=%d time=%s",
                    processed, captcha_count, elapsed)


if __name__ == "__main__":
    main()
