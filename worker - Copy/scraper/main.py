import json
import re
import signal
import logging
import threading
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
    DELAY_MIN, DELAY_MAX, AWS_REGION, QUEUE_IDLE_TIMEOUT, VISIBILITY_TIMEOUT,
    NO_SELLER_RETRY, MAX_RETRIES, BATCH_SESSION, BATCH_TIME_LIMIT_SEC,
    CAPTCHA_MAX_RETRIES, CAPTCHA_DELAY_MIN, CAPTCHA_DELAY_MAX,
)
from worker.scraper.models import Request, Product, Session
from worker.scraper.scraper import Scraper, CaptchaError, SessionExpiredError, ChromeInitError
from worker.scraper.queue_service import QueueService, strike_id_of

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

ID_PATTERN = re.compile(r"(gpcid|catalogid):\d+")


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
    items: deque = field(default_factory=deque)      # parsed inputs of the current batch still to do
    pending: list = field(default_factory=list)      # SQS messages of the current batch not yet finished
    session: Optional[Session] = None                # current request method session
    fresh_fail: int = 0                              # fresh sessions that failed on their first use in a row
    processed: int = 0
    captcha_count: int = 0
    batch_number: int = 0
    batch_started: float = 0.0                       # time.time() right after the batch was received
    timed_out: threading.Event = field(default_factory=threading.Event)
    case_b_streak: int = 0                           # inputs in a row finished with DOM sellers only


def build_requests_session(browser_cookies: list) -> http_requests.Session:
    session = http_requests.Session()
    for c in browser_cookies:
        session.cookies.set(c["name"], c["value"], domain=c.get("domain", ".google.com"))
    return session


def build_session(ctx: WorkerContext) -> Session:
    """Build a Session from the CURRENT browser cookies and the template captured in the same
    Chrome. It starts unvalidated. The first real request made with it validates it."""
    scraper = ctx.scraper
    session_id = uuid.uuid4().hex[:12]
    created_at = datetime.now()
    session = Session(
        requests_session=build_requests_session(scraper.driver.get_cookies()),
        oapv_template="",            # filled by the caller
        session_id=session_id,
        created_at=created_at,
        validated=False,
        batch_number=ctx.batch_number,
    )
    return session


def end_session(ctx: WorkerContext, reason: str):
    """Forget the current session and log how long and for how many inputs it was used."""
    s = ctx.session
    if s is None:
        return
    seconds = int((datetime.now() - s.created_at).total_seconds())
    logger.info("Session %s ended input count %d seconds %d reason %s",
                s.session_id, s.input_count, seconds, reason)
    ctx.session = None


def log_batch_session(ctx: WorkerContext):
    s = ctx.session
    if s is None:
        logger.info("Batch number %d has no session", ctx.batch_number)
    else:
        logger.info("Batch number %d using session from batch number %d session %s input count so far %d",
                    ctx.batch_number, s.batch_number, s.session_id, s.input_count)


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
    """Give one input back to the input queue (visible again at once)."""
    ctx.queue.return_message(item.msg)
    if item.msg in ctx.pending:
        ctx.pending.remove(item.msg)


def return_all_pending(ctx: WorkerContext):
    for msg in list(ctx.pending):
        ctx.queue.return_message(msg)
    ctx.pending.clear()
    ctx.items.clear()


def strikes_of(messages) -> str:
    return " ".join(strike_id_of(m) for m in messages)


# ------------------bad input------------------

def find_request_problem(req: Request) -> Optional[str]:
    """Return why an input is not usable, or None when it is fine."""
    if not str(req.strike_id).strip():
        return "strike id is empty"
    url = str(req.google_shopping_url or "").strip()
    if not url:
        return "url is empty"
    if not ID_PATTERN.search(url):
        return "url has no gpcid or catalogid"
    return None


def handle_bad_message(ctx: WorkerContext, msg: dict, reason: str):
    """A message with the wrong input format is never processed and never goes back to the input
    queue or to a DLQ. An alert is sent to the notification queue and the message is deleted."""
    body = msg.get("Body", "")
    logger.error("Bad message %s reason %s body %s", msg.get("MessageId"), reason, body)
    text = (f"ALERT\nInput format is wrong. The message was not processed and was removed.\n"
            f"Message id {msg.get('MessageId')}\nReason {reason}\nBody {body[:1500]}")
    sent = ctx.queue.send_alert(text)
    logger.info("Bad message alert sent %s message id %s", sent, msg.get("MessageId"))
    ctx.queue.delete_message(msg)
    if msg in ctx.pending:
        ctx.pending.remove(msg)


# ------------------batch time limit------------------

def batch_time_exceeded(ctx: WorkerContext) -> bool:
    return ctx.timed_out.is_set() or (time.time() - ctx.batch_started) >= BATCH_TIME_LIMIT_SEC


def _batch_watchdog(ctx: WorkerContext):
    """Runs in a timer thread when the batch reaches BATCH_TIME_LIMIT_SEC. Killing Chrome makes a
    blocked Selenium call fail at once, so the input in progress can be returned in time."""
    ctx.timed_out.set()
    logger.warning("Batch watchdog fired after %d seconds", BATCH_TIME_LIMIT_SEC)
    ctx.scraper.force_quit()


def handle_batch_timeout(ctx: WorkerContext):
    """The batch ran too long. Log the inputs that were not processed and return them (and the
    input that was in progress) to the input queue before the 300 second visibility ends."""
    logger.warning("Timeout of %d sec to crossed %d sec not processed strike ids %s",
                   VISIBILITY_TIMEOUT, BATCH_TIME_LIMIT_SEC, strikes_of(ctx.pending))
    return_all_pending(ctx)


def captcha_terminate(ctx: WorkerContext):
    """CAPTCHA persists even with a new Chrome. Return the CAPTCHA input and every other unfinished
    input of the batch to the input queue without any marking, stop Chrome and terminate this
    instance so the ASG launches a replacement with a fresh IP."""
    logger.error("Captcha persists after %d retries returning %d inputs strike ids %s",
                 CAPTCHA_MAX_RETRIES, len(ctx.pending), strikes_of(ctx.pending))
    return_all_pending(ctx)
    ctx.scraper.stop()
    terminate_for_fresh_ip()
    _handle_signal(None, None)


# ------------------Selenium fallback------------------

def _open_chrome(ctx: WorkerContext):
    """Open Chrome lazily. Raises ChromeInitError after all start attempts failed."""
    if not ctx.scraper.is_running:
        logger.info("SELENIUM_OPENED")
        ctx.scraper.start()


def _return_after_chrome_failure(ctx: WorkerContext, current: Item):
    """Chrome could not be started. With a usable session only the input that needed Chrome is
    returned. Without a session every pending input needs Selenium too, so all are returned."""
    if ctx.session is not None:
        logger.error("Chrome could not start returning 1 inputs strike ids %s", current.req.strike_id)
        return_item(ctx, current)
        return
    logger.error("Chrome could not start and no session exists returning %d inputs strike ids %s",
                 len(ctx.pending), strikes_of(ctx.pending))
    return_all_pending(ctx)


def _next_item(ctx: WorkerContext) -> Optional[Item]:
    """Next input of the batch for the same Chrome (Case B) or None."""
    if _shutdown or is_spot_interrupted() or batch_time_exceeded(ctx) or not ctx.items:
        return None
    return ctx.items.popleft()


def run_selenium_fallback(ctx: WorkerContext, first: Item, trigger: str):
    """
    Selenium fallback used when there is no session, the session expired, or the request method
    returned NO_SELLER or was blocked. One Chrome is reused across inputs and is ALWAYS quit at
    the end (finally).

      Case A  sellers and "More stores" worked -> send OK, build session, quit Chrome.
      Case B  sellers but no working "More stores" -> send OK from the DOM, NO session, the same
              Chrome loads the next input of the batch.
      Case C  no sellers -> quit Chrome, new Chrome, retry (MAX_RETRIES times, only when
              NO_SELLER_RETRY is true), then final NO_SELLERS. Never collects a session. The
              existing session is kept.

    Raises CaptchaPersistsError when the CAPTCHA does not go away (the caller terminates).
    """
    scraper = ctx.scraper
    current: Optional[Item] = first
    no_seller_attempt = 0
    captcha_retries = 0
    t0 = datetime.now()

    try:
        _open_chrome(ctx)
        while current is not None:
            if batch_time_exceeded(ctx):
                return                  # unfinished inputs are returned by the batch timeout handler
            req = current.req
            scraper.context = req.strike_id
            try:
                result = scraper.scrape_browser(req.google_shopping_url, req.seller_limit,
                                                need_session=True)
            except CaptchaError:
                if ctx.timed_out.is_set():
                    return
                if captcha_retries >= CAPTCHA_MAX_RETRIES:
                    raise CaptchaPersistsError(f"CAPTCHA persists on strike id {req.strike_id}")
                captcha_retries += 1
                wait = random.uniform(CAPTCHA_DELAY_MIN, CAPTCHA_DELAY_MAX)
                logger.warning("CAPTCHA_RETRY(%d/%d) strike_id %s",
                               captcha_retries, CAPTCHA_MAX_RETRIES, req.strike_id)
                scraper.stop()
                logger.info("CHROME_QUIT")
                logger.info("Captcha wait seconds %.1f", wait)
                time.sleep(wait)
                _open_chrome(ctx)
                continue
            except (ChromeInitError, CaptchaPersistsError):
                raise
            except Exception as e:
                if ctx.timed_out.is_set():
                    return
                logger.error("Selenium error strike_id %s %s", req.strike_id, e, exc_info=True)
                return_item(ctx, current)
                return

            try:
                scrape_ms = int((datetime.now() - t0).total_seconds() * 1000)
                if result.sellers:
                    logger.info("SELLERS_FOUND strike_id %s sellers %d more_stores %s template %s",
                                req.strike_id, len(result.sellers), result.has_more_stores,
                                bool(result.oapv_template))
                    if trigger == "no_seller" and current is first:
                        logger.info("FALSE_NO_SELLER_FROM_REQUEST strike_id %s browser found sellers",
                                    req.strike_id)

                    if result.oapv_template:                              # Case A
                        # Build the session BEFORE Chrome is quit (cookies come from the driver)
                        # and swap it in only when it is complete.
                        new_session = build_session(ctx)
                        new_session.oapv_template = result.oapv_template
                        end_session(ctx, "replaced by a new session")
                        ctx.session = new_session
                        ctx.case_b_streak = 0
                        logger.info("SESSION_COLLECTED session %s", new_session.session_id)
                        logger.info("Batch number %d collected new session %s",
                                    ctx.batch_number, new_session.session_id)
                        finish(ctx, current, result.sellers, scrape_ms)
                        return

                    # Case B: no session can be collected from this URL
                    ctx.case_b_streak += 1
                    logger.info("MORE_STORES_MISSING_TRY_NEXT strike_id %s more_stores_button %s",
                                req.strike_id, result.has_more_stores)
                    logger.info("Case B inputs in a row %d", ctx.case_b_streak)
                    finish(ctx, current, result.sellers, scrape_ms)
                    current = _next_item(ctx)
                    no_seller_attempt = 0
                    captcha_retries = 0
                    t0 = datetime.now()
                    continue

                # Case C: no sellers in the browser
                reason = result.no_sellers_reason or "no sellers"
                if NO_SELLER_RETRY and no_seller_attempt < MAX_RETRIES:
                    no_seller_attempt += 1
                    logger.info("RETRY_NEW_CHROME(%d/%d) strike_id %s reason %s",
                                no_seller_attempt, MAX_RETRIES, req.strike_id, reason)
                    scraper.restart()                                    # quit and a brand new Chrome
                    continue
                if not NO_SELLER_RETRY:
                    logger.info("NO_SELLER retry flag is false so no new Chrome retry strike_id %s",
                                req.strike_id)
                logger.info("FINAL_NO_SELLER strike_id %s reason %s", req.strike_id, reason)
                ctx.case_b_streak = 0
                finish(ctx, current, [], scrape_ms)
                return                                                   # the old session is kept
            except ChromeInitError:
                raise
            except Exception as e:
                if ctx.timed_out.is_set():
                    return
                logger.error("Error finishing strike_id %s %s", req.strike_id, e, exc_info=True)
                return_item(ctx, current)
                return
    except ChromeInitError as e:
        logger.error("Chrome could not be started %s", e)
        _return_after_chrome_failure(ctx, current if current is not None else first)
    finally:
        scraper.stop()
        logger.info("CHROME_QUIT")


# ------------------request method------------------

def process_item(ctx: WorkerContext, item: Item):
    """Process one input with the request method when a session exists, otherwise with Selenium."""
    req = item.req
    ctx.scraper.context = req.strike_id
    logger.info("Input number %d method %s client %s strike_id %s", ctx.processed + 1,
                "requests" if ctx.session else "browser", req.client_name, req.strike_id)

    if ctx.session is None:
        run_selenium_fallback(ctx, item, trigger="no_session")
        return

    session = ctx.session
    session.input_count += 1
    logger.info("Session %s input count %d strike_id %s",
                session.session_id, session.input_count, req.strike_id)
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
        logger.warning("Session expired strike_id %s reason %s", req.strike_id, e)
        end_session(ctx, "session expired")     # known bad so it is discarded
        if not session.validated:
            ctx.fresh_fail += 1
            logger.warning("Fresh session failed on first use count %d", ctx.fresh_fail)
            if ctx.fresh_fail >= 2:
                logger.error("Fresh session failed %d times in a row returning strike_id %s without opening Chrome",
                             ctx.fresh_fail, req.strike_id)
                ctx.fresh_fail = 0
                return_item(ctx, item)
                return
        run_selenium_fallback(ctx, item, trigger="session_expired")
        return
    except CaptchaError as e:
        wait = random.uniform(CAPTCHA_DELAY_MIN, CAPTCHA_DELAY_MAX)
        logger.warning("Request method blocked strike_id %s reason %s checking with Chrome", req.strike_id, e)
        logger.info("Captcha wait seconds %.1f", wait)
        time.sleep(wait)
        run_selenium_fallback(ctx, item, trigger="request_captcha")
        return

    scrape_ms = int((datetime.now() - t0).total_seconds() * 1000)

    if not sellers:
        logger.info("NO_SELLER_FROM_REQUEST strike_id %s", req.strike_id)
        if NO_SELLER_RETRY:
            run_selenium_fallback(ctx, item, trigger="no_seller")
            return
        logger.info("NO_SELLER retry flag is false so final NO_SELLERS strike_id %s", req.strike_id)
        ctx.case_b_streak = 0
        finish(ctx, item, [], scrape_ms)
        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
        return

    if not session.validated:
        session.validated = True
        logger.info("SESSION_VALIDATED session %s", session.session_id)
    ctx.fresh_fail = 0
    ctx.case_b_streak = 0
    finish(ctx, item, sellers, scrape_ms)
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


# ------------------main------------------

def parse_batch(ctx: WorkerContext, messages: list):
    """Turn the received messages into inputs. Bad messages are alerted and deleted here."""
    ctx.pending = list(messages)
    ctx.items = deque()
    for msg in messages:
        count = msg.get("Attributes", {}).get("ApproximateReceiveCount", "unknown")
        logger.info("strike_id %s receive count %s", strike_id_of(msg), count)
        try:
            req = Request.from_message(json.loads(msg["Body"]))
        except Exception as e:
            handle_bad_message(ctx, msg, f"{type(e).__name__} {e}")
            continue
        problem = find_request_problem(req)
        if problem:
            handle_bad_message(ctx, msg, problem)
            continue
        ctx.items.append(Item(msg=msg, req=req))


def run_batch(ctx: WorkerContext, messages: list):
    """Process one received batch. Every input ends up finished (output sent and deleted) or
    returned to the input queue."""
    ctx.batch_started = time.time()          # the 300 second visibility clock starts now
    ctx.batch_number += 1
    ctx.timed_out.clear()
    logger.info("Batch number %d received %d inputs", ctx.batch_number, len(messages))
    parse_batch(ctx, messages)
    log_batch_session(ctx)

    watchdog = threading.Timer(
        max(0.1, BATCH_TIME_LIMIT_SEC - (time.time() - ctx.batch_started)),
        _batch_watchdog, args=(ctx,))
    watchdog.daemon = True
    watchdog.start()

    try:
        while ctx.items:
            if _shutdown or is_spot_interrupted() or batch_time_exceeded(ctx):
                break
            item = ctx.items.popleft()
            try:
                process_item(ctx, item)
            except CaptchaPersistsError as e:
                ctx.captcha_count += 1
                logger.error("%s so this instance is terminated for a fresh IP", e)
                captcha_terminate(ctx)
                break
            except Exception as e:
                if ctx.timed_out.is_set():
                    break                    # stays pending and is returned below
                logger.error("Error processing strike_id %s %s", item.req.strike_id, e, exc_info=True)
                if item.msg in ctx.pending:
                    return_item(ctx, item)
    finally:
        watchdog.cancel()

    if ctx.pending and batch_time_exceeded(ctx):
        handle_batch_timeout(ctx)
    elif ctx.pending and (_shutdown or is_spot_interrupted()):
        logger.warning("Returning %d unprocessed inputs strike ids %s",
                       len(ctx.pending), strikes_of(ctx.pending))
        return_all_pending(ctx)
    if ctx.timed_out.is_set():
        ctx.scraper.stop()                   # Chrome was killed by the watchdog

    if BATCH_SESSION and ctx.session is not None:
        logger.info("Batch number %d session discarded at batch end", ctx.batch_number)
        end_session(ctx, "batch end")


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    queue   = QueueService()
    scraper = Scraper()                 # Chrome is NOT started here. It is opened lazily.
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

            run_batch(ctx, messages)

            if _shutdown or is_spot_interrupted():
                break

    finally:
        scraper.stop()
        end_session(ctx, "worker stop")
        elapsed = datetime.now() - start_time
        logger.info("Worker stopped. processed=%d captchas=%d time=%s",
                    ctx.processed, ctx.captcha_count, elapsed)


if __name__ == "__main__":
    main()
