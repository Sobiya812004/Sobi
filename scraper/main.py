import json
import signal
import logging
import time
import random
import uuid
import boto3
import requests as http_requests
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from worker.scraper.config import (
    IMDS_TOKEN_URL, IMDS_INSTANCE_URL, IMDS_SPOT_URL,
    DELAY_MIN, DELAY_MAX, AWS_REGION, QUEUE_IDLE_TIMEOUT,
    MAX_RETRIES, CAPTCHA_MAX_RETRIES, CAPTCHA_DELAY_MIN, CAPTCHA_DELAY_MAX,
)
from worker.scraper.models import Request, Product, Session
from worker.scraper.scraper import Scraper, CaptchaError, SessionExpiredError, ChromeInitError
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

class CaptchaPersistsError(Exception):
    """CAPTCHA was still shown after all retries with a new Chrome."""


@dataclass
class Item:
    """One parsed input message."""
    msg: dict
    req: Request


@dataclass
class WorkerContext:
    """Everything the processing functions share for the life of the worker."""
    queue: QueueService
    scraper: Scraper
    items: deque = field(default_factory=deque)      # parsed items of the current batch still to do
    pending: list = field(default_factory=list)      # SQS messages of the current batch not yet finished
    session: Optional[Session] = None                # current request-method session
    fresh_fail: int = 0                              # consecutive first-use failures of fresh sessions
    processed: int = 0
    captcha_count: int = 0


def build_requests_session(browser_cookies: list) -> http_requests.Session:
    session = http_requests.Session()
    for c in browser_cookies:
        session.cookies.set(c["name"], c["value"], domain=c.get("domain", ".google.com"))
    return session


def build_session(scraper: Scraper, oapv_template: str) -> Session:
    """Build a Session from the CURRENT browser cookies and the template that was captured in
    the same Chrome. It starts unvalidated; the first real request made with it validates it."""
    session_id = uuid.uuid4().hex[:12]
    created_at = datetime.now()
    session = Session(
        requests_session=build_requests_session(scraper.driver.get_cookies()),
        oapv_template=oapv_template,
        session_id=session_id,
        created_at=created_at,
        validated=False,
    )
    logger.info("SESSION_COLLECTED session_id=%s created_at=%s", session_id, created_at.isoformat())
    return session


def log_session_expired(session: Optional[Session]):
    """Log how long a session stayed valid before it expired."""
    if not session:
        return
    valid_period_s = (datetime.now() - session.created_at).total_seconds()
    logger.info("Session expired: session_id=%s valid_period_s=%.1f", session.session_id, valid_period_s)


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


def finish(ctx: WorkerContext, item: Item, sellers, scrape_ms: int = 0):
    """Send the output, delete the message and remove it from the pending list."""
    send_and_delete(ctx.queue, item.msg, item.req, sellers, scrape_ms)
    if item.msg in ctx.pending:
        ctx.pending.remove(item.msg)
    ctx.processed += 1


def return_item(ctx: WorkerContext, item: Item):
    """Give one message back to the queue (hidden RETURN_DELAY_SEC, then visible again).
    SQS's redrive policy (maxReceiveCount) moves it to the DLQ after too many receives."""
    ctx.queue.return_message(item.msg)
    if item.msg in ctx.pending:
        ctx.pending.remove(item.msg)
    logger.info("Returned to queue for retry: strike_id=%s", item.req.strike_id)


def handle_bad_message(ctx: WorkerContext, msg: dict, error: Exception):
    """Unparseable / incomplete message: it would fail every time, so it is not retried.
    Log the full body, store it in the DLQ with the reason, then delete it. If the DLQ
    send fails the message is returned instead, so nothing is lost."""
    logger.error("Bad message %s: %s | body=%s", msg.get("MessageId"), error, msg.get("Body"))
    if ctx.queue.send_to_dlq(msg, f"{type(error).__name__}: {error}"):
        ctx.queue.delete_message(msg)
    else:
        ctx.queue.return_message(msg)
    if msg in ctx.pending:
        ctx.pending.remove(msg)


def captcha_terminate(queue, pending_messages, scraper):
    """CAPTCHA persists even with a new Chrome: return every unfinished message, stop Chrome
    and terminate this instance so the ASG launches a replacement with a fresh IP."""
    for msg in list(pending_messages):
        try:
            queue.return_message(msg)
            logger.info("Captcha: returned %s to queue",
                        json.loads(msg["Body"]).get("strike_id", "?"))
        except Exception as e:
            logger.error("Failed to return captcha message: %s", e)

    scraper.stop()
    terminate_for_fresh_ip()
    _handle_signal(None, None)


# ------------------Selenium fallback------------------

def _open_chrome(ctx: WorkerContext):
    """Open Chrome lazily. Raises ChromeInitError after all start attempts failed."""
    if not ctx.scraper.is_running:
        logger.info("SELENIUM_OPENED")
        ctx.scraper.start()


def _return_after_chrome_failure(ctx: WorkerContext, current: Item):
    """Chrome could not be started. With a usable session only the message that needed
    Chrome is returned; without a session every pending message needs Selenium too."""
    if ctx.session is not None:
        return_item(ctx, current)
        return
    logger.error("Chrome failed and no session exists — returning all %d pending messages",
                 len(ctx.pending))
    for msg in list(ctx.pending):
        ctx.queue.return_message(msg)
    ctx.pending.clear()
    ctx.items.clear()


def _next_item(ctx: WorkerContext) -> Optional[Item]:
    """Next input of the batch for the same Chrome (Case B), or None."""
    if _shutdown or is_spot_interrupted() or not ctx.items:
        return None
    ctx.queue.extend_visibility(ctx.pending)                 # heartbeat before each message
    return ctx.items.popleft()


def run_selenium_fallback(ctx: WorkerContext, first: Item, trigger: str):
    """
    Selenium fallback used when there is no session, the session expired, or the request
    method returned NO_SELLER / was blocked. One Chrome is reused across inputs and is ALWAYS
    quit at the end (finally).

      Case A  sellers + "More stores" worked -> send OK, build session, quit Chrome.
      Case B  sellers, no (working) "More stores" -> send OK from the DOM, NO session,
              same Chrome loads the next input of the batch.
      Case C  no sellers -> quit Chrome, new Chrome, retry (MAX_RETRIES times), then final
              NO_SELLERS. Never collects a session; the existing session is kept.

    Raises CaptchaPersistsError when the CAPTCHA does not go away (caller terminates).
    """
    scraper = ctx.scraper
    current: Optional[Item] = first
    no_seller_attempt = 0
    captcha_retries = 0
    t0 = datetime.now()

    ctx.queue.extend_visibility(ctx.pending)                 # heartbeat before the fallback
    try:
        _open_chrome(ctx)
        while current is not None:
            req = current.req
            scraper.context = req.strike_id
            try:
                result = scraper.scrape_browser(req.google_shopping_url, req.seller_limit,
                                                need_session=True)
            except CaptchaError:
                if captcha_retries >= CAPTCHA_MAX_RETRIES:
                    raise CaptchaPersistsError(f"CAPTCHA persists on {req.strike_id}")
                captcha_retries += 1
                logger.warning("CAPTCHA_RETRY(%d/%d) strike_id=%s",
                               captcha_retries, CAPTCHA_MAX_RETRIES, req.strike_id)
                scraper.stop()
                logger.info("CHROME_QUIT")
                time.sleep(random.uniform(CAPTCHA_DELAY_MIN, CAPTCHA_DELAY_MAX))
                _open_chrome(ctx)
                continue
            except (ChromeInitError, CaptchaPersistsError):
                raise
            except Exception as e:
                logger.error("Selenium error on %s: %s", req.strike_id, e, exc_info=True)
                return_item(ctx, current)
                return

            try:
                if result.sellers:
                    logger.info("SELLERS_FOUND strike_id=%s sellers=%d more_stores=%s template=%s",
                                req.strike_id, len(result.sellers), result.has_more_stores,
                                bool(result.oapv_template))
                    if trigger == "no_seller" and current is first:
                        logger.info("FALSE_NO_SELLER_FROM_REQUEST strike_id=%s — browser found sellers",
                                    req.strike_id)
                    scrape_ms = int((datetime.now() - t0).total_seconds() * 1000)

                    if result.oapv_template:                              # Case A
                        # Build the session BEFORE Chrome is quit (cookies come from the driver).
                        ctx.session = build_session(scraper, result.oapv_template)
                        finish(ctx, current, result.sellers, scrape_ms)
                        return

                    # Case B: no session can be collected from this URL
                    logger.info("MORE_STORES_MISSING_TRY_NEXT strike_id=%s (more_stores_button=%s)",
                                req.strike_id, result.has_more_stores)
                    finish(ctx, current, result.sellers, scrape_ms)
                    current = _next_item(ctx)
                    no_seller_attempt = 0
                    captcha_retries = 0
                    t0 = datetime.now()
                    continue

                # Case C: no sellers in the browser
                if no_seller_attempt < MAX_RETRIES:
                    no_seller_attempt += 1
                    logger.info("RETRY_NEW_CHROME(%d/%d) strike_id=%s reason=%s",
                                no_seller_attempt, MAX_RETRIES, req.strike_id,
                                result.no_sellers_reason or "no_sellers")
                    scraper.restart()                                    # quit + brand new Chrome
                    continue
                logger.info("FINAL_NO_SELLER strike_id=%s reason=%s",
                            req.strike_id, result.no_sellers_reason or "no_sellers")
                finish(ctx, current, [], int((datetime.now() - t0).total_seconds() * 1000))
                return                                                   # old session is kept
            except ChromeInitError:
                raise
            except Exception as e:
                logger.error("Error finishing %s: %s", req.strike_id, e, exc_info=True)
                return_item(ctx, current)
                return
    except ChromeInitError as e:
        logger.error("Chrome could not be started: %s", e)
        _return_after_chrome_failure(ctx, current if current is not None else first)
    finally:
        scraper.stop()
        logger.info("CHROME_QUIT")


# ------------------request method------------------

def process_item(ctx: WorkerContext, item: Item):
    """Process one input: request method when a session exists, otherwise Selenium."""
    req = item.req
    ctx.scraper.context = req.strike_id
    logger.info("[%d] %s | client=%s strike_id=%s", ctx.processed + 1,
                "requests" if ctx.session else "browser", req.client_name, req.strike_id)

    if ctx.session is None:
        run_selenium_fallback(ctx, item, trigger="no_session")
        return

    session = ctx.session
    t0 = datetime.now()
    try:
        payload_t0 = datetime.now()
        sellers = ctx.scraper.scrape_via_requests(
            req.google_shopping_url, session.requests_session,
            session.oapv_template, req.seller_limit,
        )
        log_session_payload(session.session_id, req.strike_id,
                            int((datetime.now() - payload_t0).total_seconds() * 1000))
    except SessionExpiredError as e:            # must come before CaptchaError (subclass)
        logger.warning("Session expired on %s: %s", req.strike_id, e)
        log_session_expired(session)
        ctx.session = None                      # known bad — replace it
        if not session.validated:
            ctx.fresh_fail += 1
            if ctx.fresh_fail >= 2:
                logger.error("Fresh session failed on first use %d times in a row — "
                             "returning %s instead of opening Chrome again", ctx.fresh_fail, req.strike_id)
                ctx.fresh_fail = 0
                return_item(ctx, item)
                return
        run_selenium_fallback(ctx, item, trigger="session_expired")
        return
    except CaptchaError as e:
        logger.warning("Requests blocked on %s: %s — checking with a browser", req.strike_id, e)
        time.sleep(random.uniform(CAPTCHA_DELAY_MIN, CAPTCHA_DELAY_MAX))
        run_selenium_fallback(ctx, item, trigger="request_captcha")
        return

    if not sellers:
        logger.info("NO_SELLER_FROM_REQUEST strike_id=%s — verifying in Selenium", req.strike_id)
        run_selenium_fallback(ctx, item, trigger="no_seller")
        return

    if not session.validated:
        session.validated = True
        logger.info("SESSION_VALIDATED session_id=%s", session.session_id)
    ctx.fresh_fail = 0
    scrape_ms = int((datetime.now() - t0).total_seconds() * 1000)
    finish(ctx, item, sellers, scrape_ms)
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


# ------------------main------------------

def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    queue   = QueueService()
    scraper = Scraper()                 # Chrome is NOT started here — it is opened lazily
    ctx     = WorkerContext(queue=queue, scraper=scraper)

    empty_polls       = 0
    queue_empty_since = None
    start_time        = datetime.now()
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

            # ------ parse the whole batch ------
            ctx.pending = list(messages)
            ctx.items   = deque()
            for msg in messages:
                try:
                    ctx.items.append(Item(msg=msg, req=Request.from_message(json.loads(msg["Body"]))))
                except Exception as e:
                    handle_bad_message(ctx, msg, e)

            # ------ process one by one ------
            while ctx.items:
                if _shutdown or is_spot_interrupted():
                    break
                ctx.queue.extend_visibility(ctx.pending)              # heartbeat before each message
                item = ctx.items.popleft()
                try:
                    process_item(ctx, item)
                except CaptchaPersistsError as e:
                    ctx.captcha_count += 1
                    logger.error("%s — terminating for a fresh IP", e)
                    captcha_terminate(queue, ctx.pending, scraper)
                    break
                except Exception as e:
                    logger.error("Error processing %s: %s", item.req.strike_id, e, exc_info=True)
                    if item.msg in ctx.pending:
                        return_item(ctx, item)

            if _shutdown or is_spot_interrupted():
                if ctx.pending:
                    logger.warning("Returning %d unprocessed messages", len(ctx.pending))
                    for msg in list(ctx.pending):
                        queue.return_message(msg)
                    ctx.pending.clear()
                break

    finally:
        scraper.stop()
        elapsed = datetime.now() - start_time
        logger.info("Worker stopped. processed=%d captchas=%d time=%s",
                    ctx.processed, ctx.captcha_count, elapsed)


if __name__ == "__main__":
    main()
