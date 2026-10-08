"""
Flow tests with fake Chrome / SQS objects (no real Chrome, Google or AWS needed).
Run from the project root (the folder that contains the `worker` package):
    python -m worker.scraper.tests.test_flow
"""
import sys, types, json, collections, logging, threading, time, re, ast, os, io

# ---- stub third-party modules that are not installed ----
def _stub(name, **attrs):
    if name in sys.modules:
        return
    try:
        __import__(name)
        return
    except Exception:
        pass
    m = types.ModuleType(name); m.__dict__.update(attrs); sys.modules[name] = m

_stub("undetected_chromedriver", Chrome=object, ChromeOptions=object)
_stub("selenium"); _stub("selenium.webdriver"); _stub("selenium.webdriver.common")
_stub("selenium.webdriver.common.by", By=types.SimpleNamespace(XPATH="x", CSS_SELECTOR="c"))
_stub("selenium.webdriver.support"); _stub("selenium.webdriver.support.ui", WebDriverWait=object)
_stub("selenium.webdriver.support.expected_conditions")
_stub("selenium.common"); _stub("selenium.common.exceptions", TimeoutException=type("TimeoutException", (Exception,), {}))


class _FakeBoto:
    """Minimal boto3 stand in so the modules can be imported without AWS."""
    class _Any:
        def __getattr__(self, name):
            return lambda *a, **k: {}
    @classmethod
    def client(cls, *a, **k): return cls._Any()
    @classmethod
    def resource(cls, *a, **k): return cls._Any()

if "boto3" not in sys.modules:
    try:
        import boto3  # noqa
    except Exception:
        _stub("boto3", client=_FakeBoto.client, resource=_FakeBoto.resource)

import worker.scraper.main as M
import worker.scraper.scraper as S
import worker.scraper.queue_service as QS
from worker.scraper.models import Request, Seller, Session

REAL_SLEEP = time.sleep
M.time.sleep = lambda s: None
S.time.sleep = lambda s: None
M.is_spot_interrupted = lambda: False
M.CAPTCHA_DELAY_MIN = M.CAPTCHA_DELAY_MAX = 0
M.DELAY_MIN = M.DELAY_MAX = 0

# ---- capture log output ----
class _Cap(logging.Handler):
    def __init__(self): super().__init__(); self.lines = []
    def emit(self, record): self.lines.append(record.getMessage())
CAP = _Cap(); logging.getLogger().addHandler(CAP); logging.getLogger().setLevel(logging.INFO)
def logged(text): return any(text in l for l in CAP.lines)
def clear_logs(): CAP.lines.clear()

# ---------------- fakes ----------------
class FakeQueue:
    def __init__(self):
        self.sent, self.deleted, self.returned, self.dlq = [], [], [], []
        self.dlq_ok = True
    def send_output(self, product):
        if getattr(self, "output_fails_for", None) == product.status:
            raise RuntimeError("output queue down")
        self.sent.append(product)
    def delete_message(self, msg): self.deleted.append(msg["MessageId"])
    def return_message(self, msg):
        self.returned.append(msg["MessageId"])
        logging.getLogger("fake").info("Returned to input queue strike_id %s", QS.strike_id_of(msg))
    def send_to_dlq(self, msg, reason):
        if self.dlq_ok:
            self.dlq.append((msg["MessageId"], reason)); return True
        return False

class FakeDriver:
    def get_cookies(self): return [{"name": "NID", "value": "x", "domain": ".google.com"}]

def seller(n="Shop"): return Seller(name=n, price="10.00", shipping="0.00", prod_url="http://x/" + n)

class FakeScraper(S.Scraper):
    def __init__(self, browser_results=(), request_results=(), init_fail=False):
        super().__init__()
        self.browser_results = list(browser_results); self.request_results = list(request_results)
        self.starts = self.stops = self.restarts = self.forced = 0
        self.init_fail = init_fail; self.browser_calls = []
        self.block = threading.Event()
    def start(self):
        if self.init_fail: raise S.ChromeInitError("boom")
        if self.driver is None: self.driver = FakeDriver(); self.starts += 1
    def stop(self):
        if self.driver is not None: self.stops += 1
        self.driver = None
    def restart(self):
        self.restarts += 1; self.stop(); self.start()
    def force_quit(self):
        self.forced += 1; self.block.set()
    def scrape_browser(self, url, seller_limit=25, need_session=False):
        self.browser_calls.append(url)
        r = self.browser_results.pop(0)
        if r == "BLOCK":                       # a Selenium call that hangs until Chrome is killed
            self.block.wait(5)
            raise RuntimeError("chrome was killed")
        if isinstance(r, Exception): raise r
        return r
    def scrape_via_requests(self, url, session, template, seller_limit=25):
        r = self.request_results.pop(0)
        if callable(r): r = r()
        if isinstance(r, Exception): raise r
        return r

def BR(sellers=(), template=None, more=False, reason=""):
    return S.BrowserResult(sellers=list(sellers), oapv_template=template, has_more_stores=more, no_sellers_reason=reason)

def body(i, **over):
    b = dict(message_id=f"m{i}", client_name="c", report_date="d", strike_id=f"s{i}", client_metadata="md",
             google_shopping_url=f"http://g/search?prds=gpcid:{i}00,pvo:25", created_at="t")
    b.update(over); return b

def make_msg(i, count=1, **over):
    return {"MessageId": f"M{i}", "ReceiptHandle": f"R{i}", "Body": json.dumps(body(i, **over)),
            "Attributes": {"ApproximateReceiveCount": str(count)}}

def setup(n, scraper, session=None, count=1):
    q = FakeQueue()
    ctx = M.WorkerContext(queue=q, scraper=scraper)
    msgs = [make_msg(i, count=count) for i in range(1, n + 1)]
    ctx.pending = list(msgs)
    ctx.items = collections.deque(M.Item(m, Request.from_message(json.loads(m["Body"]))) for m in msgs)
    ctx.session = session
    ctx.batch_started = time.time(); ctx.batch_number = 1
    return ctx, q

def run_all(ctx):
    while ctx.items:
        item = ctx.items.popleft()
        M.process_item(ctx, item)

def good_session(validated=True, batch=0):
    return Session(requests_session=object(), oapv_template="T", session_id="old", created_at=M.datetime.now(),
                   validated=validated, batch_number=batch)

passed = 0
def check(name, cond):
    global passed
    print(("PASS " if cond else "FAIL ") + name)
    assert cond, name
    passed += 1

# ================= session / Selenium fallback =================
sc = FakeScraper(browser_results=[BR([seller()], "TPL", True)])
ctx, q = setup(1, sc); run_all(ctx)
check("1 CaseA: session saved unvalidated and tagged with the batch number",
      ctx.session and ctx.session.oapv_template == "TPL" and not ctx.session.validated and ctx.session.batch_number == 1)
check("1 CaseA: OK output deleted Chrome quit once", q.sent[0].status == "OK" and q.deleted == ["M1"] and sc.stops == 1 and not sc.is_running)

sc = FakeScraper(request_results=[[seller()]])
ctx, q = setup(1, sc, session=good_session(validated=False)); run_all(ctx)
check("2 request success validates the session and counts the input", ctx.session.validated and ctx.session.input_count == 1 and sc.starts == 0)

old = good_session()
sc = FakeScraper(request_results=[[]], browser_results=[BR([seller()], "NEWTPL", True)])
ctx, q = setup(1, sc, session=old); clear_logs(); run_all(ctx)
check("3 false NO_SELLER: new session replaces old and the old one is logged as ended",
      ctx.session is not old and ctx.session.oapv_template == "NEWTPL" and logged("Session old ended input count"))
check("3 false NO_SELLER: logged", logged("FALSE_NO_SELLER_FROM_REQUEST") and logged("Batch number 1 collected new session"))

old = good_session()
sc = FakeScraper(request_results=[[]], browser_results=[BR([], reason="no_buying_options"), BR([], reason="no_buying_options")])
ctx, q = setup(1, sc, session=old); clear_logs(); run_all(ctx)
check("4 CaseC: exactly one new Chrome retry then final NO_SELLERS", q.sent[0].status == "NO_SELLERS" and sc.restarts == 1 and len(sc.browser_calls) == 2)
check("4 CaseC: old session kept Chrome quit message deleted", ctx.session is old and not sc.is_running and q.deleted == ["M1"])
check("4 CaseC: retry and final logged", logged("RETRY_NEW_CHROME(1/1)") and logged("FINAL_NO_SELLER"))

sc = FakeScraper(request_results=[[], [seller()]], browser_results=[BR([]), BR([])])
ctx, q = setup(2, sc, session=good_session()); run_all(ctx)
check("4c CaseC final then the NEXT input uses the previous session with the request method",
      q.sent[0].status == "NO_SELLERS" and q.sent[1].status == "OK" and ctx.session.session_id == "old")

# NO_SELLER_RETRY flag = false -> old method
M.NO_SELLER_RETRY = False
sc = FakeScraper(request_results=[[]])
ctx, q = setup(1, sc, session=good_session()); clear_logs(); run_all(ctx)
check("4d flag false: request NO_SELLER is final and Chrome never opens", q.sent[0].status == "NO_SELLERS" and sc.starts == 0 and logged("NO_SELLER retry flag is false"))
sc = FakeScraper(browser_results=[BR([])])
ctx, q = setup(1, sc); clear_logs(); run_all(ctx)
check("4e flag false: browser Case C has no new Chrome retry", q.sent[0].status == "NO_SELLERS" and sc.restarts == 0 and len(sc.browser_calls) == 1)
M.NO_SELLER_RETRY = True

sc = FakeScraper(browser_results=[BR([seller("a")]), BR([seller("b")]), BR([seller("c")], "TPL", True)])
ctx, q = setup(3, sc); clear_logs(); run_all(ctx)
check("5 CaseB chain: 3 OK one session same Chrome", [p.status for p in q.sent] == ["OK"] * 3 and ctx.session and sc.starts == 1 and sc.stops == 1)
check("5 CaseB in a row is logged 1 then 2 and reset by CaseA", logged("Case B inputs in a row 1") and logged("Case B inputs in a row 2") and ctx.case_b_streak == 0)

sc = FakeScraper(browser_results=[BR([seller("a")]), BR([seller("b")])])
ctx, q = setup(2, sc); run_all(ctx)
check("6 CaseB inputs run out: no session Chrome quit", len(q.sent) == 2 and ctx.session is None and not sc.is_running)

sc = FakeScraper(request_results=[S.SessionExpiredError("x"), S.SessionExpiredError("y")], browser_results=[BR([seller()], "TPL2", True)])
ctx, q = setup(2, sc, session=good_session(validated=False)); clear_logs(); run_all(ctx)
check("7 first fresh failure goes to the fallback and its input is finished OK", q.sent[0].status == "OK")
check("7 second fresh failure goes back to the input queue for a retry without opening Chrome again",
      [p.status for p in q.sent] == ["OK"] and q.returned == ["M2"] and sc.starts == 1
      and logged("Fresh session failed") and logged("Input returned to the input queue for retry number 1 of 3 strike_id s2"))
sc = FakeScraper(request_results=[S.SessionExpiredError("x"), S.SessionExpiredError("y")], browser_results=[BR([seller()], "TPL2", True)])
ctx, q = setup(2, sc, session=good_session(validated=False), count=4); run_all(ctx)
check("7b after 3 queue retries the same failure closes the input with status ERROR",
      [p.status for p in q.sent] == ["OK", "ERROR"] and q.deleted == ["M1", "M2"] and not q.returned)

sc = FakeScraper(request_results=[S.SessionExpiredError("x")], browser_results=[BR([seller()], "TPL", True)])
ctx, q = setup(1, sc, session=good_session(validated=True)); run_all(ctx)
check("14 validated session that expired is replaced and does not count as a fresh failure", ctx.session.oapv_template == "TPL" and ctx.fresh_fail == 0)

sc = FakeScraper(request_results=[S.CaptchaError("429")], browser_results=[BR([seller()], "TPL", True)])
ctx, q = setup(1, sc, session=good_session()); run_all(ctx)
check("15 request side 429 goes to the browser fallback", q.sent[0].status == "OK")

sc = FakeScraper(browser_results=[RuntimeError("selenium crashed")])
ctx, q = setup(1, sc); run_all(ctx)
check("13 unexpected selenium error goes back to the input queue for a retry and Chrome quits",
      q.returned == ["M1"] and not q.sent and not q.deleted and not sc.is_running)
sc = FakeScraper(browser_results=[RuntimeError("selenium crashed")])
ctx, q = setup(1, sc, count=4); run_all(ctx)
check("13b after 3 queue retries the selenium error closes the input with status ERROR (output empty deleted not returned)",
      q.sent[0].status == "ERROR" and q.sent[0].output is None and q.deleted == ["M1"] and not q.returned)
for n in (1, 2, 3):
    sc = FakeScraper(browser_results=[RuntimeError("x")]); ctx, q = setup(1, sc, count=n); run_all(ctx)
    check(f"13c receive number {n} is still returned for a retry", q.returned == ["M1"] and not q.sent)

# ================= CAPTCHA =================
sc = FakeScraper(browser_results=[S.CaptchaError("c"), S.CaptchaError("c"), S.CaptchaError("c")])
ctx, q = setup(1, sc); clear_logs()
raised = False
try: run_all(ctx)
except M.CaptchaPersistsError: raised = True
check("8 CAPTCHA: original plus 2 retries (3 Chrome launches) then CaptchaPersistsError", raised and sc.starts == 3 and len(sc.browser_calls) == 3)
check("8 CAPTCHA: each retry and wait is logged", logged("CAPTCHA_RETRY(1/2)") and logged("CAPTCHA_RETRY(2/2)") and logged("Captcha wait seconds"))

sc = FakeScraper(browser_results=[S.CaptchaError("c"), BR([seller()], "TPL", True)])
ctx, q = setup(1, sc); run_all(ctx)
check("8b CAPTCHA once then clears", q.sent and q.sent[0].status == "OK" and sc.starts == 2)

# whole batch: input 3 hits a persistent CAPTCHA -> inputs 3..5 return unmarked and the instance terminates
terminated = []
M.terminate_for_fresh_ip = lambda: terminated.append(1)
M._shutdown = False
sc = FakeScraper(request_results=[[seller()], [seller()], S.CaptchaError("429")],
                 browser_results=[S.CaptchaError("c"), S.CaptchaError("c"), S.CaptchaError("c")])
ctx, q = setup(5, sc, session=good_session()); clear_logs()
M.run_batch.__globals__["time"].time  # touch
ctx.batch_started = time.time()
# drive the batch loop the same way run_batch does, but with the prepared items
msgs = list(ctx.pending)
ctx.pending, ctx.items = [], collections.deque()
M.parse_batch(ctx, msgs)
while ctx.items:
    item = ctx.items.popleft()
    try: M.process_item(ctx, item)
    except M.CaptchaPersistsError as e:
        M.fail_or_retry(ctx, e.item, "CAPTCHA", "captcha persists after all retries")
        M.captcha_terminate(ctx); break
check("8c CAPTCHA mid batch: input 3 and the rest go back to the input queue with no status and no output",
      [(p.strike_id, p.status) for p in q.sent] == [("s1", "OK"), ("s2", "OK")]
      and q.deleted == ["M1", "M2"] and sorted(q.returned) == ["M3", "M4", "M5"] and not ctx.pending)
check("8c CAPTCHA mid batch: instance terminated and the returned inputs are logged", terminated == [1] and logged("Captcha persists after 2 retries returning 2 inputs strike ids s4 s5"))
M._shutdown = False

# ================= Chrome init failure =================
sc = FakeScraper(init_fail=True)
ctx, q = setup(3, sc); clear_logs(); run_all(ctx)
check("9 Chrome init fail and no session: all pending returned", sorted(q.returned) == ["M1", "M2", "M3"] and not ctx.pending and not ctx.items)
check("9 logged", logged("Chrome could not start and no session exists returning 3 inputs strike ids s1 s2 s3"))

sc = FakeScraper(init_fail=True, request_results=[[], [seller()]])
ctx, q = setup(2, sc, session=good_session()); run_all(ctx)
check("10 Chrome init fail with a session: only that input returned the next continues", q.returned == ["M1"] and q.deleted == ["M2"])

# ================= CAPTCHA and ERROR close the input (nothing goes to the DLQ) =================
M._shutdown = False; terminated.clear()
sc = FakeScraper(request_results=[[seller()]], browser_results=[S.CaptchaError("c")] * 3)
q = FakeQueue(); ctx = M.WorkerContext(queue=q, scraper=sc); ctx.session = good_session(); clear_logs()
sc.request_results = [[seller()], S.CaptchaError("429")]
M.run_batch(ctx, [make_msg(i) for i in range(1, 5)])
check("8d run_batch CAPTCHA: input 2 and the rest (3 4) go back to the input queue and the instance terminates",
      [(p.strike_id, p.status) for p in q.sent] == [("s1", "OK")] and q.deleted == ["M1"]
      and sorted(q.returned) == ["M2", "M3", "M4"] and terminated == [1] and not ctx.pending)
M._shutdown = False; terminated.clear()
sc = FakeScraper(browser_results=[S.CaptchaError("c")] * 3, request_results=[[seller()], S.CaptchaError("429")])
q = FakeQueue(); ctx = M.WorkerContext(queue=q, scraper=sc); ctx.session = good_session(); clear_logs()
M.run_batch(ctx, [make_msg(1), make_msg(2, count=4), make_msg(3), make_msg(4)])
check("8d2 CAPTCHA input that was already tried 3 times in the queue is closed with status CAPTCHA the rest return",
      [(p.strike_id, p.status) for p in q.sent] == [("s1", "OK"), ("s2", "CAPTCHA")] and q.deleted == ["M1", "M2"]
      and sorted(q.returned) == ["M3", "M4"] and terminated == [1])
check("8d CAPTCHA close is logged", logged("Input closed with status CAPTCHA strike_id s2"))
M._shutdown = False; terminated.clear()

sc = FakeScraper(request_results=[RuntimeError("network down"), [seller()]])
q = FakeQueue(); ctx = M.WorkerContext(queue=q, scraper=sc); ctx.session = good_session(); clear_logs()
M.run_batch(ctx, [make_msg(1), make_msg(2)])
check("8e error in the middle of a batch: that input goes back to the input queue and the remaining inputs still run",
      [(p.strike_id, p.status) for p in q.sent] == [("s2", "OK")] and q.returned == ["M1"] and q.deleted == ["M2"])
sc = FakeScraper(request_results=[RuntimeError("network down"), [seller()]])
q = FakeQueue(); ctx = M.WorkerContext(queue=q, scraper=sc); ctx.session = good_session(); clear_logs()
M.run_batch(ctx, [make_msg(1, count=4), make_msg(2)])
check("8e2 the same error after 3 queue retries closes the input with status ERROR and the batch continues",
      [(p.strike_id, p.status) for p in q.sent] == [("s1", "ERROR"), ("s2", "OK")] and q.deleted == ["M1", "M2"] and not q.returned)
check("8e ERROR close is logged with the reason", logged("Input closed with status ERROR strike_id s1 reason RuntimeError network down"))

sc = FakeScraper(request_results=[RuntimeError("boom")])
q = FakeQueue(); q.output_fails_for = "ERROR"; ctx = M.WorkerContext(queue=q, scraper=sc); ctx.session = good_session(); clear_logs()
M.run_batch(ctx, [make_msg(1, count=4)])
check("8f if the ERROR output itself cannot be sent the input is returned (so it is not lost)",
      q.returned == ["M1"] and not q.deleted and logged("Status ERROR output could not be sent"))

# ================= bad messages =================
def run_parse(msgs):
    q = FakeQueue(); ctx = M.WorkerContext(queue=q, scraper=FakeScraper())
    ctx.pending = list(msgs); clear_logs(); M.parse_batch(ctx, msgs); return ctx, q

bad_json = {"MessageId": "B1", "ReceiptHandle": "RB1", "Body": "{not json"}
missing = {"MessageId": "B2", "ReceiptHandle": "RB2", "Body": json.dumps({"client_name": "c"})}
no_id = make_msg(7); no_id["Body"] = json.dumps(body(7, strike_id="  "))
no_url = make_msg(8); no_url["Body"] = json.dumps(body(8, google_shopping_url=""))
no_gpcid = make_msg(9); no_gpcid["Body"] = json.dumps(body(9, google_shopping_url="https://www.google.com/search?q=abc"))
catalog = make_msg(10); catalog["Body"] = json.dumps(body(10, google_shopping_url="https://x/?prds=catalogid:5551,pvo:3"))
good = make_msg(1)
ctx, q = run_parse([bad_json, missing, no_id, no_url, no_gpcid, catalog, good])
check("11 bad messages: each one is sent to the DLQ then deleted from the input queue (not returned)",
      len(q.dlq) == 5 and sorted(q.deleted) == ["B1", "B2", "M7", "M8", "M9"] and not q.returned)
check("11 bad messages: logged as Bad messages with the id and reason",
      logged("Bad messages found message id B1") and logged("Bad messages sent to DLQ message id M9"))
check("11 bad messages: only good inputs are queued for processing and pending is correct",
      [i.req.strike_id for i in ctx.items] == ["s10", "s1"] and len(ctx.pending) == 2)
check("11 DLQ reason names the problem", any("url has no gpcid or catalogid" in r for _, r in q.dlq) and any("strike id is empty" in r for _, r in q.dlq))
ctx, q = (lambda c, qq: (c, qq))(*run_parse([]))
q.dlq_ok = False; ctx.pending = [bad_json]; clear_logs(); M.handle_bad_message(ctx, bad_json, "x")
check("11b DLQ send fails: the bad message is returned to the input queue not lost and not deleted",
      q.returned == ["B1"] and not q.deleted and logged("Bad messages DLQ send failed"))

# receive count log
ctx, q = run_parse([make_msg(1, count=3)])
check("12 receive count is logged per input", logged("strike_id s1 receive count 3"))

# ================= batch session flag =================
def run_batch_with(ctx, n_msgs):
    msgs = [make_msg(i) for i in range(1, n_msgs + 1)]
    ctx.pending, ctx.items = [], collections.deque()
    M.run_batch(ctx, msgs)

M.BATCH_SESSION = False
sc = FakeScraper(browser_results=[BR([seller()], "TPL", True)], request_results=[[seller()], [seller()]])
q = FakeQueue(); ctx = M.WorkerContext(queue=q, scraper=sc); clear_logs()
run_batch_with(ctx, 1)
check("16 BATCH_SESSION false: session survives the batch", ctx.session is not None and logged("Batch number 1 has no session"))
clear_logs(); run_batch_with(ctx, 2)
check("16 BATCH_SESSION false: next batch uses the session of batch 1 and it is logged",
      logged("Batch number 2 using session from batch number 1") and ctx.session.input_count == 2 and sc.starts == 1)
check("16 session input count is logged", logged("Session ") and logged("input count 2"))

M.BATCH_SESSION = True
sc = FakeScraper(browser_results=[BR([seller()], "TPL", True), BR([seller()], "TPL2", True)], request_results=[[seller()]])
q = FakeQueue(); ctx = M.WorkerContext(queue=q, scraper=sc); clear_logs()
run_batch_with(ctx, 2)
check("17 BATCH_SESSION true: session is discarded at batch end", ctx.session is None and logged("Batch number 1 session discarded at batch end"))
clear_logs(); run_batch_with(ctx, 1)
check("17 BATCH_SESSION true: the next batch collects a new session with Chrome", logged("Batch number 2 has no session") and sc.starts == 2)
M.BATCH_SESSION = False

# ================= 280 second batch limit =================
M.BATCH_TIME_LIMIT_SEC = 1
M.VISIBILITY_TIMEOUT = 300
# (a) slow input -> remaining inputs are returned and listed
sc = FakeScraper(request_results=[lambda: REAL_SLEEP(1.3) or [seller()]])
q = FakeQueue(); ctx = M.WorkerContext(queue=q, scraper=sc); ctx.session = good_session(); clear_logs()
run_batch_with(ctx, 4)
check("18 time limit: the finished input is kept the rest are returned", [p.strike_id for p in q.sent] == ["s1"] and sorted(q.returned) == ["M2", "M3", "M4"])
check("18 time limit: log names the strike ids not processed", logged("Timeout of 300 sec to crossed 1 sec not processed strike ids s2 s3 s4"))
# (b) input stuck in Selenium -> watchdog kills Chrome and the stuck input is returned too
sc = FakeScraper(browser_results=["BLOCK"])
q = FakeQueue(); ctx = M.WorkerContext(queue=q, scraper=sc); clear_logs()
run_batch_with(ctx, 3)
check("19 watchdog: Chrome is force quit when the limit is reached", sc.forced == 1 and not sc.is_running)
check("19 watchdog: the stuck input and the remaining inputs are returned and listed", sorted(q.returned) == ["M1", "M2", "M3"] and logged("not processed strike ids s1 s2 s3") and not q.sent)
M.BATCH_TIME_LIMIT_SEC = 280

# ================= queue service =================
class FakeSqs:
    def __init__(self): self.calls = []
    def receive_message(self, **k): self.calls.append(("receive", k)); return {"Messages": []}
    def change_message_visibility(self, **k): self.calls.append(("vis", k))
    def send_message(self, **k): self.calls.append(("send", k))
qs = QS.QueueService.__new__(QS.QueueService); qs.sqs = FakeSqs(); qs._notification_url = None
QS.INPUT_QUEUE_URL = "https://sqs/input"
qs.receive_messages()
check("20 receive asks for the receive count attribute", qs.sqs.calls[0][1].get("AttributeNames") == ["ApproximateReceiveCount"])
clear_logs(); qs.return_message(make_msg(1))
check("20 return_message is unchanged (VisibilityTimeout 0) and logs the strike id",
      qs.sqs.calls[1][1]["VisibilityTimeout"] == 0 and logged("Returned to input queue strike_id s1"))
QS.DLQ_URL = "https://sqs/dlq"
ok = qs.send_to_dlq(make_msg(1), "url is empty")
call = qs.sqs.calls[-1][1]
check("20 send_to_dlq sends the original body with the reason to the DLQ",
      ok and call["QueueUrl"] == "https://sqs/dlq" and call["MessageBody"] == make_msg(1)["Body"]
      and call["MessageAttributes"]["failure_reason"]["StringValue"] == "url is empty")
QS.DLQ_URL = ""
check("20 no DLQ url means the send is not done and False is returned", qs.send_to_dlq(make_msg(1), "x") is False)
check("20 heartbeat and alert code are gone from the worker queue service",
      not hasattr(qs, "extend_visibility") and not hasattr(qs, "send_alert"))
import worker.scraper.config as CFG
check("20 DLQ url is derived from the input queue url",
      CFG._derive_dlq_url("https://sqs.us-east-1.amazonaws.com/1/dev-google-scraper-engine-acme-input-queue")
      == "https://sqs.us-east-1.amazonaws.com/1/dev-google-scraper-engine-acme-input-dlq" and CFG._derive_dlq_url("x") == "")

# ================= seller names =================
s = S.Scraper(); s.context = "s1"
check("21 name: / ! (R) (TM) # % $ * = ] allowed", all(s.clean_seller_name(n) == n for n in ["A/B Store", "Wow!", "Acme\u00ae", "Acme\u2122", "Best #1 100% Co*", "A=B [x]"]))
clear_logs()
check("21 name: emoji rejected and logged", s.clean_seller_name("Shop \U0001F600") == "" and logged("SELLER_NAME_REJECTED strike_id s1"))
src = open(S.__file__, encoding="utf-8").read()
check("21 name: pipe and tab are replaced by a space before cleaning exactly like the old code", src.count('.replace("\\t", " ").replace("|", " ")') == 2)
check("21 name: non Latin rejected by default", s.clean_seller_name("\u0ba4\u0bae\u0bbf\u0bb4\u0bcd") == "")
S.ALLOW_NON_LATIN_SELLER_NAMES = True
check("21 name: non Latin allowed with the flag", s.clean_seller_name("\u0ba4\u0bae\u0bbf\u0bb4\u0bcd \u0b95\u0b9f\u0bc8") != "" and s.clean_seller_name("\u5546\u5e97") == "\u5546\u5e97")
S.ALLOW_NON_LATIN_SELLER_NAMES = False

# ================= oapv polling =================
class PollDriver:
    def __init__(self): self.served = False; self.calls = 0
    def get_log(self, kind):
        if self.served: return []
        self.served = True
        return [{"message": json.dumps({"message": {"method": "Network.responseReceived",
                 "params": {"requestId": "r1", "response": {"url": "https://g/async/oapv?x=1"}}}})}]
    def execute_cdp_cmd(self, cmd, args):
        self.calls += 1
        if self.calls == 1: raise Exception("No resource with given identifier found")
        return {"body": ")]}'\n{\"ProductDetailsResult\": []}"}
sc2 = S.Scraper(); sc2.driver = PollDriver()
got = list(sc2._poll_oapv(set(), 5))
check("22 oapv body that was not ready is retried on the next poll", len(got) == 1 and sc2.driver.calls == 2)

# ================= dispatcher =================
import worker.dispatcher.handler as D
rows = [
    "1001\tname\thttps://www.google.com/search?prds=gpcid:111,pvo:2",
    "1002\tname\thttps://x/?prds=catalogid:222",
    "1003\tname\thttps://www.google.com/search?q=nothing",
    "\tname\thttps://www.google.com/search?prds=gpcid:1",
    "1005\tname",
    "1006\tname\t",
    "1007\tname\thttps://www.google.com/async/oapv?async=a:b,catalogid:333,pvf:x",
    "1008\tname\thttps://www.google.com/async/oapv?async=a:b,pvf:x",
]
bad = D.validate_rows(rows, 2, row_offset=10)
check("23 dispatcher: good rows pass (gpcid catalogid and a convertible async oapv url)",
      [b["row"] for b in bad] == [13, 14, 15, 16, 18])
check("23 dispatcher: reasons are right",
      bad[0]["reason"] == "url has no gpcid or catalogid" and bad[1]["reason"] == "strike id is empty"
      and "missing" in bad[2]["reason"] and bad[3]["reason"] == "url is empty")
check("23 dispatcher: a Travelhouse url that cannot be changed (no gpcid or catalogid) is reported as Travelhouse",
      bad[4]["travelhouse"] and bad[4]["reason"] == "travelhouse url could not be changed as it has no gpcid or catalogid"
      and not any(b["travelhouse"] for b in bad[:4]))
_orig_bm = D.build_message
D.build_message = lambda *a, **k: (_ for _ in ()).throw(ValueError("boom"))
bad2 = D.validate_rows(["1001\tname\thttps://x/?prds=gpcid:1"], 2)
D.build_message = _orig_bm
check("23 dispatcher: a row whose message cannot be built is a wrong format bad row", len(bad2) == 1 and "row format is wrong ValueError boom" in bad2[0]["reason"])

calls = {"scale": 0, "pending": 0, "alert": [], "sent": 0}
class _S3:
    def get_object(self, **k): return {"Body": io.BytesIO(("\n".join(rows) + "\n").encode())}
D.s3 = _S3()
D.get_account_id = lambda: "1"
D.get_queue_url = lambda c, a: "https://sqs/q"
D.get_chitti_sqs_url = lambda: "https://sqs/notify"
D.chitti_post = lambda url, text: calls["alert"].append(text)
D.scale_workers = lambda c, n: calls.__setitem__("scale", calls["scale"] + 1) or n
D.write_pending_records = lambda *a, **k: calls.__setitem__("pending", calls["pending"] + 1)
D.load_client_config = lambda c: {"url_column": 2, "seller_limit": 25}
D.boto3.client = lambda *a, **k: types.SimpleNamespace(send_message_batch=lambda **kw: calls.__setitem__("sent", calls["sent"] + 1) or {"Successful": [], "Failed": []})
clear_logs()
res = D.lambda_handler({"client": "c1"}, None)
check("24 dispatcher: any bad row blocks the whole run (no workers no PENDING nothing sent)",
      json.loads(res["body"]).get("blocked") is True and calls["scale"] == 0 and calls["pending"] == 0 and calls["sent"] == 0)
check("24 dispatcher: one ALERT with the bad rows", len(calls["alert"]) == 1 and calls["alert"][0].startswith("ALERT") and "row 3 strike_id 1003" in calls["alert"][0])
check("24 dispatcher: validation logs", logged("Input validation failed bad rows 5 run blocked") and logged("Bad row number 3 strike_id 1003")
      and logged("Travelhouse url bad rows 1") and "Travelhouse url bad rows 1" in calls["alert"][0])

good_rows = [r for i, r in enumerate(rows) if i in (0, 1, 6)]
D.s3 = types.SimpleNamespace(get_object=lambda **k: {"Body": io.BytesIO(("\n".join(good_rows) + "\n").encode())})
calls.update(scale=0, pending=0, alert=[], sent=0); clear_logs()
res = D.lambda_handler({"client": "c1"}, None)
check("25 dispatcher: all rows good runs as before", calls["scale"] == 1 and calls["pending"] == 1 and calls["sent"] == 1 and logged("Input validation passed rows 3"))

# ================= new log lines never use the forbidden symbols =================
sys.path.insert(0, "/tmp")
def log_strings(path):
    out = set()
    tree = ast.parse(open(path, encoding="utf-8").read().replace("\r", ""))
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("info", "warning", "error", "debug") and n.args:
            a = n.args[0]
            if isinstance(a, ast.Constant) and isinstance(a.value, str): out.add(a.value)
    return out
ORIG = os.environ.get("ORIGINAL_ROOT")
if ORIG:
    problems = []
    for rel in ("scraper/main.py", "scraper/scraper.py", "scraper/queue_service.py", "dispatcher/handler.py"):
        new = log_strings(os.path.join(os.path.dirname(os.path.dirname(S.__file__)), rel))
        old = log_strings(os.path.join(ORIG, rel))
        problems += [(rel, s) for s in new - old if re.search(r"[:,;']", s)]
    check("26 no new log line contains a colon comma semicolon or apostrophe", not problems)
else:
    print("SKIP 26 (set ORIGINAL_ROOT to the original worker folder to run the log symbol check)")

print(f"\nALL {passed} CHECKS PASSED")
