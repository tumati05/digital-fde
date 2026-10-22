"""Backend edge cases. The agent is replaced by an in-process fake that speaks the same SSE
format, so these verify THIS backend. The agent's own logic is tested in test_agent_cells.py,
and the real stream contract is pinned by tests/fixtures/recorded_*.json once
scripts/live_smoke.py has recorded one."""
import asyncio
import base64
import glob
import json
import os
import time

import httpx
import pytest

from backend import agent_client as ac
from backend import main

GOOD = {"message": "We want to automate our invoice thing, it takes too long."}
CFG = ac.AgentConfig(token="t", workflow_id="wf-1", base_url="https://agent.test", timeout_s=5)


def good_resp(**over):
    r = {"reply": "Got it. Which systems does it touch?", "phase": "discover", "agent_version": main.EXPECTED_AGENT_VERSION,
         "key_confirmed": 1, "key_total": 8, "completeness": 0.1, "readiness": "NEEDS_CLARIFICATION",
         "areas": [{"key": "business_goal", "label": "Business goal", "status": "FACT", "value": "Automate invoices", "critical": True}],
         "fit": {"verdict": "UNSURE", "reasons": [], "kill_risks": [], "ask_to_see": []},
         "open_questions": [], "estimate": None, "manual": None, "notices": [], "state": {"turn": 1}}
    r.update(over)
    return r


def sse(*events):
    out = ""
    for ev, data in events:
        out += f"event: {ev}\ndata: {data if isinstance(data, str) else json.dumps(data)}\n\n"
    return out.encode()


def stream_of(resp):
    return sse(("ping", ""), ("workflow-run", {"id": "r1"}), ("result", {"output": json.dumps({"fde_response": resp})}))


OK_STREAM = stream_of(good_resp())


class Fake:
    def __init__(self, handler):
        self.calls = 0
        self.last_request = None
        self._handler = handler

    def __call__(self, timeout_s):
        def h(request):
            self.calls += 1
            self.last_request = request
            return self._handler(request)
        return httpx.AsyncClient(transport=httpx.MockTransport(h))


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    main._cache.clear(); main._flights.clear(); main._hits.clear()
    main.agent_config = CFG
    for name, val in [
        ("TRUST_PROXY", False),
        ("ALLOWED_FILE_HOSTS", []),
        ("RECORD_DIR", ""),
        ("RATE_CARD_JSON", ""),
        ("FDE_MODE", "agent"),
        ("LARGE_MESSAGE_CHARS", 400),
    ]:
        monkeypatch.setattr(main, name, val)
    yield
    main.client_factory = ac.default_client_factory
    main.agent_config = None


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


def use(handler):
    f = Fake(handler)
    main.client_factory = f
    return f


def ok(_req):
    return httpx.Response(200, content=OK_STREAM)


def body_of(f):
    return f.last_request.content.decode("utf-8", "replace")


# ---------- input validation ----------
@pytest.mark.parametrize("payload,field", [
    ({"message": ""}, "message"),
    ({"message": "   "}, "message"),
    ({"message": "x" * 2001}, "message"),
    ({"message": "hi", "action": "delete"}, "action"),
    ({"message": "hi", "sample_file_url": "http://insecure.test/a.csv"}, "sample_file_url"),
    ({"message": "hi", "sample_file_url": "C:/local/file.csv"}, "sample_file_url"),
    ({"message": "hi", "recent_turns": [{"role": "system", "text": "x"}]}, "recent_turns.0.role"),
    ({"message": "hi", "state": {"blob": "x" * 15000}}, "state"),
])
async def test_invalid_input_rejected_without_calling_agent(client, payload, field):
    f = use(ok)
    r = await client.post("/api/chat", json=payload)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "invalid_input" and field in [d["field"] for d in err["details"]]
    assert f.calls == 0


async def test_missing_body_and_bad_json(client):
    f = use(ok)
    assert (await client.post("/api/chat", json={})).status_code == 422
    assert (await client.post("/api/chat", content="nope", headers={"content-type": "application/json"})).status_code == 422
    assert (await client.post("/api/chat", json={"message": 123})).status_code == 422
    assert f.calls == 0


async def test_manual_action_allows_empty_message(client):
    f = use(ok)
    r = await client.post("/api/chat", json={"message": "", "action": "manual", "state": {"turn": 2}})
    assert r.status_code == 200 and "Please create the manual." in body_of(f)


async def test_recent_turns_trimmed_and_text_capped(client):
    f = use(ok)
    turns = [{"role": "user", "text": "t%d " % i + "x" * 900} for i in range(10)]
    assert (await client.post("/api/chat", json={**GOOD, "recent_turns": turns})).status_code == 200
    raw = body_of(f)
    assert "t9 " in raw and "t3 " not in raw and "x" * 601 not in raw


# ---------- happy path ----------
async def test_happy_path_returns_agent_reply(client):
    f = use(ok)
    r = await client.post("/api/chat", json=GOOD)
    j = r.json()
    assert r.status_code == 200 and j["response"]["reply"].startswith("Got it") and j["cached"] is False and f.calls == 1
    assert j["response"]["warnings"] == []


async def test_agent_receives_expected_inputs(client):
    f = use(ok)
    await client.post("/api/chat", json={"message": "  hello there  ", "state": {"turn": 3}, "sample_file_url": "https://x.test/a.csv"})
    raw = body_of(f)
    assert 'name="inputs.user_message"' in raw and "hello there" in raw and "  hello" not in raw
    assert '{"turn":3}' in raw and "https://x.test/a.csv" in raw and 'name="workflowId"' in raw
    assert "inputs.recent_turns" not in raw and "inputs.rate_card_json" not in raw
    assert f.last_request.headers["authorization"] == "Bearer t"


async def test_rate_card_comes_from_server_config_only(client, monkeypatch):
    monkeypatch.setattr(main, "RATE_CARD_JSON", '{"WORKFLOW_AUTO_BUILD": 30}')
    f = use(ok)
    await client.post("/api/chat", json=GOOD)
    assert "WORKFLOW_AUTO_BUILD" in body_of(f)


async def test_unicode_and_markup_pass_through_safely(client):
    f = use(ok)
    text = "हमें इनवॉइस automate करना है <script>alert(1)</script> 'quotes' \"dq\" {braces}"
    assert (await client.post("/api/chat", json={"message": text})).status_code == 200
    assert "alert(1)" in body_of(f)


# ---------- reading results ----------
@pytest.mark.parametrize("result", [
    {"fde_response": good_resp()},
    {"output": {"fde_response": good_resp()}},
    {"output": json.dumps({"fde_response": good_resp()})},
    {"steps": [{"stdout": "noise {"}, {"stdout": "log\n" + json.dumps({"fde_response": good_resp()}, indent=2)}]},
    json.dumps({"data": {"fde_response": good_resp()}}),
])
async def test_reply_found_in_any_result_shape(client, result):
    use(lambda _r: httpx.Response(200, content=sse(("result", result))))
    r = await client.post("/api/chat", json=GOOD)
    assert r.status_code == 200 and r.json()["response"]["reply"].startswith("Got it")


async def test_result_without_reply_is_an_error_and_shape_is_logged(client, caplog):
    use(lambda _r: httpx.Response(200, content=sse(("result", {"output": {"stdout": "no reply here"}}))))
    r = await client.post("/api/chat", json=GOOD)
    assert r.status_code == 502 and "did not return a reply" in r.json()["error"]["message"]
    assert "Shape" in caplog.text and "no reply here" not in caplog.text  # structure only, never content


async def test_empty_reply_is_rejected(client):
    use(lambda _r: httpx.Response(200, content=stream_of(good_resp(reply="   "))))
    assert (await client.post("/api/chat", json=GOOD)).status_code == 502


# ---------- response cleaning ----------
@pytest.mark.parametrize("url", ["http://files.test/a.docx", "javascript:alert(1)", "//files.test/a.docx", "files.test/a.docx", "data:text/html,x", ""])
async def test_unsafe_manual_links_are_dropped(client, url):
    use(lambda _r: httpx.Response(200, content=stream_of(good_resp(manual={"url": url, "title": "T", "blueprint": []}))))
    j = (await client.post("/api/chat", json=GOOD)).json()["response"]
    assert j["manual"] is None and "manual_link_rejected" in j["warnings"]


async def test_https_manual_link_kept_with_blueprint(client):
    m = {"url": "https://files.test/m.docx", "title": "T", "verdict": "BUILD", "blueprint": [{"step": "Read", "does": "Reads", "worker": "AI Employee", "review": True, "extra": "x"}]}
    use(lambda _r: httpx.Response(200, content=stream_of(good_resp(manual=m))))
    j = (await client.post("/api/chat", json={"message": "", "action": "manual"})).json()["response"]
    assert j["manual"]["url"] == m["url"] and j["manual"]["blueprint"] == [{"step": "Read", "does": "Reads", "worker": "AI Employee", "review": True}]


async def test_allowed_file_hosts_enforced(client, monkeypatch):
    monkeypatch.setattr(main, "ALLOWED_FILE_HOSTS", ["supervity.ai"])
    use(lambda _r: httpx.Response(200, content=stream_of(good_resp(manual={"url": "https://evil.test/m.docx", "title": "T"}))))
    j = (await client.post("/api/chat", json=GOOD)).json()["response"]
    assert j["manual"] is None
    main._cache.clear()
    use(lambda _r: httpx.Response(200, content=stream_of(good_resp(manual={"url": "https://files.supervity.ai/m.docx", "title": "T"}))))
    assert (await client.post("/api/chat", json=GOOD)).json()["response"]["manual"] is not None
    main._cache.clear()
    use(lambda _r: httpx.Response(200, content=stream_of(good_resp(manual={"url": "https://notsupervity.ai/m.docx", "title": "T"}))))
    assert (await client.post("/api/chat", json=GOOD)).json()["response"]["manual"] is None


async def test_agent_version_drift_is_flagged(client, caplog):
    use(lambda _r: httpx.Response(200, content=stream_of(good_resp(agent_version="old"))))
    j = (await client.post("/api/chat", json=GOOD)).json()["response"]
    assert "agent_version_mismatch" in j["warnings"] and "drifted" in caplog.text


async def test_missing_state_is_flagged_and_unknown_fields_dropped(client):
    r = good_resp(); r.pop("state"); r["<script>"] = "x"
    use(lambda _r: httpx.Response(200, content=stream_of(r)))
    j = (await client.post("/api/chat", json=GOOD)).json()["response"]
    assert "state_missing" in j["warnings"] and "<script>" not in j


# ---------- agent failure modes ----------
async def test_out_of_credits_http_402(client):
    use(lambda _r: httpx.Response(402, text="payment required"))
    r = await client.post("/api/chat", json=GOOD)
    assert r.status_code == 503 and r.json()["error"]["code"] == "agent_out_of_credits"


async def test_out_of_credits_message_in_body(client):
    use(lambda _r: httpx.Response(400, text="Your credit allowance for this billing period is exhausted"))
    assert (await client.post("/api/chat", json=GOOD)).status_code == 503


async def test_out_of_credits_error_event(client):
    use(lambda _r: httpx.Response(200, content=sse(("error", "credit allowance exhausted"))))
    assert (await client.post("/api/chat", json=GOOD)).status_code == 503


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure(client, status):
    use(lambda _r: httpx.Response(status))
    r = await client.post("/api/chat", json=GOOD)
    assert r.status_code == 502 and r.json()["error"]["code"] == "agent_auth_failed"


async def test_agent_server_error(client):
    use(lambda _r: httpx.Response(500, text="boom"))
    r = await client.post("/api/chat", json=GOOD)
    assert r.status_code == 502 and r.json()["error"]["code"] == "agent_run_failed"


async def test_error_event_surfaces(client):
    use(lambda _r: httpx.Response(200, content=sse(("workflow-run", {}), ("error", "step 2 failed"))))
    r = await client.post("/api/chat", json=GOOD)
    assert r.status_code == 502 and "step 2 failed" in r.json()["error"]["message"]


async def test_stream_without_result_and_empty_streams(client):
    use(lambda _r: httpx.Response(200, content=sse(("workflow-run", {"id": 1}))))
    r = await client.post("/api/chat", json=GOOD)
    assert r.status_code == 502 and "without a result" in r.json()["error"]["message"]
    use(lambda _r: httpx.Response(200, content=b""))
    assert (await client.post("/api/chat", json={"message": "second message here"})).status_code == 502
    use(lambda _r: httpx.Response(200, content=sse(("ping", ""))))
    assert (await client.post("/api/chat", json={"message": "third message here"})).status_code == 502


async def test_plain_text_result_is_rejected(client):
    use(lambda _r: httpx.Response(200, content=b"event: result\ndata: plain text done\n\n"))
    assert (await client.post("/api/chat", json=GOOD)).status_code == 502


async def test_timeout_from_transport(client):
    def boom(_r):
        raise httpx.ReadTimeout("slow")
    use(boom)
    r = await client.post("/api/chat", json=GOOD)
    assert r.status_code == 504 and r.json()["error"]["code"] == "agent_timeout"


async def test_overall_deadline_applies_even_when_the_stream_trickles(client, monkeypatch):
    monkeypatch.setattr(main, "CHAT_TIMEOUT_S", 0.2)

    def factory(_t):
        async def slow(_req):
            await asyncio.sleep(5)
            return httpx.Response(200, content=OK_STREAM)
        return httpx.AsyncClient(transport=httpx.MockTransport(slow))

    main.client_factory = factory
    t0 = time.monotonic()
    r = await client.post("/api/chat", json=GOOD)
    assert r.status_code == 504 and time.monotonic() - t0 < 3


async def test_manual_gets_a_longer_deadline_than_chat(client, monkeypatch):
    seen = []
    real = ac.run_agent

    async def spy(inputs, config=None, client_factory=None):
        seen.append(config.timeout_s)
        return await real(inputs, config=config, client_factory=client_factory)

    monkeypatch.setattr(ac, "run_agent", spy)
    monkeypatch.setattr(main, "CHAT_TIMEOUT_S", 1)
    monkeypatch.setattr(main, "MANUAL_TIMEOUT_S", 4)
    main.agent_config = ac.AgentConfig(token="t", workflow_id="w", base_url="https://a.test", timeout_s=100)
    use(ok)
    await client.post("/api/chat", json=GOOD)
    await client.post("/api/chat", json={"message": "", "action": "manual"})
    assert seen == [1, 4]


async def test_network_failure(client):
    def boom(_r):
        raise httpx.ConnectError("down")
    use(boom)
    r = await client.post("/api/chat", json=GOOD)
    assert r.status_code == 502 and r.json()["error"]["code"] == "agent_run_failed"


async def test_not_configured(client, monkeypatch):
    main.agent_config = None
    monkeypatch.delenv("SUPERVITY_TOKEN", raising=False)
    monkeypatch.delenv("WORKFLOW_ID", raising=False)
    use(ok)
    r = await client.post("/api/chat", json=GOOD)
    assert r.status_code == 500 and r.json()["error"]["code"] == "not_configured"


async def test_failures_are_not_cached(client):
    state = {"n": 0}

    def flaky(_r):
        state["n"] += 1
        return httpx.Response(500) if state["n"] == 1 else httpx.Response(200, content=OK_STREAM)

    use(flaky)
    assert (await client.post("/api/chat", json=GOOD)).status_code == 502
    assert (await client.post("/api/chat", json=GOOD)).status_code == 200


# ---------- cost controls ----------
async def test_identical_request_served_from_cache(client):
    f = use(ok)
    a = await client.post("/api/chat", json=GOOD)
    b = await client.post("/api/chat", json=GOOD)
    assert a.json()["cached"] is False and b.json()["cached"] is True and b.json()["shared"] is False and f.calls == 1


async def test_changed_input_is_a_new_run(client):
    f = use(ok)
    await client.post("/api/chat", json=GOOD)
    await client.post("/api/chat", json={**GOOD, "message": "300 invoices a day, SAP"})
    assert f.calls == 2


async def test_cache_is_bounded_and_expires(monkeypatch):
    monkeypatch.setattr(main, "CACHE_MAX", 3)
    for i in range(10):
        main._cache_put(f"k{i}", {"i": i})
    assert len(main._cache) == 3 and main._cache_get("k0") is None and main._cache_get("k9") == {"i": 9}
    monkeypatch.setattr(main, "CACHE_TTL_S", 0)
    assert main._cache_get("k9") is None and "k9" not in main._cache


async def test_concurrent_identical_requests_share_one_run(client):
    gate, entered = asyncio.Event(), asyncio.Event()
    calls = {"n": 0}

    def factory(_t):
        async def slow(_req):
            calls["n"] += 1
            entered.set()
            await gate.wait()
            return httpx.Response(200, content=OK_STREAM)
        return httpx.AsyncClient(transport=httpx.MockTransport(slow))

    main.client_factory = factory
    t1 = asyncio.create_task(client.post("/api/chat", json=GOOD))
    await asyncio.wait_for(entered.wait(), 2)                      # the first run is really in flight
    t2 = asyncio.create_task(client.post("/api/chat", json=GOOD))
    for _ in range(200):                                           # the second caller has joined it
        fl = next(iter(main._flights.values()), None)
        if fl and fl.waiters == 2:
            break
        await asyncio.sleep(0.01)
    assert fl.waiters == 2
    gate.set()
    r1, r2 = await asyncio.gather(t1, t2)
    assert r1.status_code == r2.status_code == 200 and calls["n"] == 1
    assert [r1.json()["cached"], r2.json()["cached"]] == [False, False]      # nothing came from the cache
    assert sorted([r1.json()["shared"], r2.json()["shared"]]) == [False, True]


class FakeRequest:
    def __init__(self, gone):
        self._gone = gone

    async def is_disconnected(self):
        return self._gone


async def test_run_is_cancelled_when_the_only_caller_leaves():
    task = asyncio.create_task(asyncio.sleep(30))
    fl = main.Flight(task)
    with pytest.raises(main.ClientGone):
        await main._wait(fl, FakeRequest(True))
    await asyncio.sleep(0)
    assert task.cancelled() and fl.waiters == 0


async def test_run_survives_one_caller_leaving_when_another_waits():
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return {"ok": 1}

    task = asyncio.create_task(work())
    fl = main.Flight(task)
    staying = asyncio.create_task(main._wait(fl, FakeRequest(False)))
    await asyncio.sleep(0.05)
    with pytest.raises(main.ClientGone):
        await main._wait(fl, FakeRequest(True))
    assert not task.cancelled()
    gate.set()
    assert await staying == {"ok": 1}


async def test_rate_limit(client):
    f = use(ok)
    codes = []
    for i in range(main.RATE_LIMIT + 2):
        codes.append((await client.post("/api/chat", json={**GOOD, "message": f"message number {i}"})).status_code)
    assert codes.count(200) == main.RATE_LIMIT and codes[-1] == 429 and f.calls == main.RATE_LIMIT


async def test_rate_limit_uses_forwarded_client_only_when_trusted(client, monkeypatch):
    use(ok)
    monkeypatch.setattr(main, "RATE_LIMIT", 2)
    for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):               # untrusted: header ignored, one shared bucket
        r = await client.post("/api/chat", json={**GOOD, "message": "m " + ip}, headers={"X-Forwarded-For": ip})
    assert r.status_code == 429
    main._hits.clear()
    monkeypatch.setattr(main, "TRUST_PROXY", True)
    codes = [(await client.post("/api/chat", json={**GOOD, "message": "m " + ip}, headers={"X-Forwarded-For": ip + ", 10.0.0.1"})).status_code for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3")]
    assert codes == [200, 200, 200]


async def test_rate_table_cannot_grow_without_bound(monkeypatch):
    monkeypatch.setattr(main, "HITS_MAX_KEYS", 10)
    old = time.monotonic() - 120
    for i in range(50):
        main._hits[f"ip{i}"] = [old]
    main._rate_ok("fresh")
    assert len(main._hits) <= 2


# ---------- health, readiness, recording ----------
def jwt(exp):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc({'exp': exp})}.sig"


async def test_health_and_index(client):
    assert (await client.get("/api/health")).json() == {"ok": True}
    assert (await client.get("/")).status_code == 200


async def test_security_headers_on_health_and_index(client):
    for path in ("/api/health", "/"):
        r = await client.get(path)
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["Referrer-Policy"] == "no-referrer"
        assert "default-src 'self'" in r.headers["Content-Security-Policy"]
        if path.startswith("/api/"):
            assert r.headers["Cache-Control"] == "no-store"


def test_production_startup_requires_config(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    main.agent_config = None
    monkeypatch.delenv("SUPERVITY_TOKEN", raising=False)
    monkeypatch.delenv("WORKFLOW_ID", raising=False)
    with pytest.raises(RuntimeError, match="Production config incomplete"):
        main.ensure_production_config()


def test_production_startup_ok_when_configured(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    main.agent_config = ac.AgentConfig(token=jwt(time.time() + 3600), workflow_id="w")
    main.ensure_production_config()


def test_development_startup_skips_config_gate(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    main.agent_config = None
    monkeypatch.delenv("SUPERVITY_TOKEN", raising=False)
    monkeypatch.delenv("WORKFLOW_ID", raising=False)
    main.ensure_production_config()


async def test_ready_reports_configuration(client, monkeypatch):
    main.agent_config = None
    monkeypatch.delenv("SUPERVITY_TOKEN", raising=False); monkeypatch.delenv("WORKFLOW_ID", raising=False)
    r = await client.get("/api/ready")
    assert r.status_code == 503 and len(r.json()["problems"]) == 2
    main.agent_config = ac.AgentConfig(token=jwt(time.time() - 10), workflow_id="w")
    r = await client.get("/api/ready")
    assert r.status_code == 503 and "expired" in r.json()["problems"][0]
    main.agent_config = ac.AgentConfig(token=jwt(time.time() + 3600), workflow_id="w")
    r = await client.get("/api/ready")
    assert r.status_code == 200 and r.json()["ready"] and r.json()["expected_agent_version"] == main.EXPECTED_AGENT_VERSION


async def test_stream_recording_when_enabled(client, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "RECORD_DIR", str(tmp_path))
    use(ok)
    await client.post("/api/chat", json=GOOD)
    files = list(tmp_path.glob("*_chat.json"))
    assert len(files) == 1 and any(e["event"] == "result" for e in json.loads(files[0].read_text()))


# ---------- SSE parser ----------
def test_sse_parser_handles_comments_multiline_and_no_trailing_blank():
    lines = [": keepalive", "event: result", "data: {\"a\":", "data: 1}", "", "event: ping", "data:"]
    assert ac.parse_sse_line_stream(lines)[0] == ("result", '{"a":\n1}')


# ---------- the real contract ----------
RECORDED = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "fixtures", "recorded_*.json")))


@pytest.mark.skipif(not RECORDED, reason="No recorded live stream yet. Run scripts/live_smoke.py once the account has credits.")
@pytest.mark.parametrize("path", RECORDED or ["-"])
async def test_recorded_live_stream_is_understood(client, path):
    events = json.load(open(path, encoding="utf-8"))
    raw = "".join(f"event: {e['event']}\ndata: {e['data']}\n\n" for e in events).encode()
    use(lambda _r: httpx.Response(200, content=raw))
    r = await client.post("/api/chat", json=GOOD)
    assert r.status_code == 200 and r.json()["response"]["reply"]


async def test_ready_is_honest_about_live_verification(client):
    main.agent_config = ac.AgentConfig(token=jwt(time.time() + 3600), workflow_id="w")
    main._seen.update({"version": "", "at": 0.0})
    j = (await client.get("/api/ready")).json()
    assert j["ready"] is True and j["live_verified"] is False and j["version_match"] is None
    use(ok)
    await client.post("/api/chat", json=GOOD)
    j = (await client.get("/api/ready")).json()
    assert j["live_verified"] is True and j["version_match"] is True
    main._seen.update({"version": "old", "at": time.time()})
    assert (await client.get("/api/ready")).json()["version_match"] is False


@pytest.mark.parametrize("url", ["https://127.0.0.1/a.csv", "https://[::1]/a.csv", "https://localhost/a.csv",
                                 "https://169.254.169.254/latest", "https://db.internal/a.csv", "http://example.com/a.csv"])
async def test_sample_url_rejects_local_and_address_hosts(client, url):
    use(ok)
    r = await client.post("/api/chat", json={**GOOD, "sample_file_url": url})
    assert r.status_code == 422


async def test_sample_url_allowlist(client, monkeypatch):
    use(ok)
    monkeypatch.setattr(main, "ALLOWED_SAMPLE_HOSTS", ["files.example.com"])
    assert (await client.post("/api/chat", json={**GOOD, "sample_file_url": "https://other.com/a.csv"})).status_code == 422
    assert (await client.post("/api/chat", json={**GOOD, "sample_file_url": "https://files.example.com/a.csv"})).status_code == 200


# ---------- hybrid / local engine ----------
async def test_hybrid_simple_turn_never_calls_agent(client, monkeypatch):
    monkeypatch.setattr(main, "FDE_MODE", "hybrid")
    f = use(ok)
    r = await client.post("/api/chat", json={"message": "Automate invoices in SAP, about 300 a day."})
    assert r.status_code == 200
    body = r.json()["response"]
    assert body["engine"] == "local" and body["agent_version"] == "local-rules-1"
    assert f.calls == 0
    assert "agent_version_mismatch" not in body.get("warnings", [])


async def test_hybrid_long_message_goes_to_agent(client, monkeypatch):
    monkeypatch.setattr(main, "FDE_MODE", "hybrid")
    monkeypatch.setattr(main, "LARGE_MESSAGE_CHARS", 40)
    f = use(ok)
    msg = "x" * 50
    r = await client.post("/api/chat", json={"message": msg})
    assert r.status_code == 200 and f.calls == 1
    assert r.json()["response"].get("engine") == "agent"


async def test_hybrid_attachment_goes_to_agent(client, monkeypatch):
    monkeypatch.setattr(main, "FDE_MODE", "hybrid")
    f = use(ok)
    r = await client.post("/api/chat", json={**GOOD, "sample_file_url": "https://files.example.com/a.csv"})
    assert r.status_code == 200 and f.calls == 1


async def test_hybrid_manual_goes_to_agent(client, monkeypatch):
    monkeypatch.setattr(main, "FDE_MODE", "hybrid")
    f = use(ok)
    r = await client.post("/api/chat", json={"action": "manual", "message": "Please create the manual."})
    assert r.status_code == 200 and f.calls == 1


async def test_hybrid_agent_failure_falls_back_and_is_not_cached(client, monkeypatch):
    monkeypatch.setattr(main, "FDE_MODE", "hybrid")
    monkeypatch.setattr(main, "LARGE_MESSAGE_CHARS", 10)
    f = use(lambda _r: httpx.Response(500, text="boom"))
    msg = "This is a long enough message that hybrid sends it to the agent path first."
    r1 = await client.post("/api/chat", json={"message": msg})
    assert r1.status_code == 200
    body = r1.json()["response"]
    assert body["engine"] == "local" and body.get("fallback") is True
    assert f.calls == 1
    r2 = await client.post("/api/chat", json={"message": msg})
    assert r2.status_code == 200 and f.calls == 2  # not served from cache


async def test_hybrid_manual_failure_is_not_hidden(client, monkeypatch):
    monkeypatch.setattr(main, "FDE_MODE", "hybrid")
    use(lambda _r: httpx.Response(500, text="boom"))
    r = await client.post("/api/chat", json={"action": "manual", "message": "Please create the manual."})
    assert r.status_code == 502 and r.json()["error"]["code"] == "agent_run_failed"


async def test_local_mode_never_calls_agent(client, monkeypatch):
    monkeypatch.setattr(main, "FDE_MODE", "local")
    f = use(ok)
    r = await client.post("/api/chat", json={"message": "x" * 500})
    assert r.status_code == 200 and f.calls == 0 and r.json()["response"]["engine"] == "local"
    m = await client.post("/api/chat", json={"action": "manual", "message": "Please create the manual."})
    assert m.status_code == 503 and m.json()["error"]["code"] == "agent_required" and f.calls == 0
