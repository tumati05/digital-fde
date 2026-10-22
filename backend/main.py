"""Digital FDE backend. The only business endpoint is the agent.

POST /api/chat  -> validates one turn, runs the Digital FDE workflow, returns its reply.
GET  /api/health -> the process is up.
GET  /api/ready  -> configuration is complete (token present and not expired, workflow id set), plus
                    live_verified / version_match, which are only filled once a real agent reply has been seen.
                    Credits and the live workflow can only be proven by running a turn.

Cost controls (the agent spends credits, this layer must not waste them):
  * input caps (message 2000 chars, state 14000, 6 recent turns of 600)
  * identical turns are answered from a bounded, expiring cache
  * identical in-flight turns share one agent run
  * a run is cancelled when every caller waiting on it has gone away (Stop button, closed tab)
  * a per-client rate limit with bounded memory
Cache and rate limit live in this process. With several workers, move them to Redis.
"""
from __future__ import annotations

import ipaddress

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationInfo, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agent import cells as agent_cells

from . import agent_client as ac
from . import local_fde

log = logging.getLogger("fde")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


def app_env() -> str:
    return os.environ.get("APP_ENV", "development").strip().lower() or "development"


def is_production() -> bool:
    return app_env() == "production"


MAX_MESSAGE = 2000
MAX_STATE = 14000
MAX_TURNS = 6
CACHE_TTL_S = 600
CACHE_MAX = 200
HITS_MAX_KEYS = 5000
RATE_LIMIT = int(_env_float("RATE_LIMIT", 20))  # chat turns per client per minute
CHAT_TIMEOUT_S = _env_float("CHAT_TIMEOUT_S", 120)
MANUAL_TIMEOUT_S = _env_float("MANUAL_TIMEOUT_S", 240)
TRUST_PROXY = _truthy("TRUST_PROXY")  # only behind a proxy that overwrites X-Forwarded-For
ALLOWED_SAMPLE_HOSTS = [h.strip().lower() for h in os.environ.get("ALLOWED_SAMPLE_HOSTS", "").split(",") if h.strip()]
ALLOWED_FILE_HOSTS = [h.strip().lower() for h in os.environ.get("ALLOWED_FILE_HOSTS", "").split(",") if h.strip()]
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
RECORD_DIR = os.environ.get("RECORD_STREAMS_DIR", "").strip()
RATE_CARD_JSON = os.environ.get("RATE_CARD_JSON", "").strip()
FDE_MODE = os.environ.get("FDE_MODE", "hybrid").strip().lower() or "hybrid"  # hybrid | local | agent
LARGE_MESSAGE_CHARS = int(_env_float("LARGE_MESSAGE_CHARS", 400))
EXPECTED_AGENT_VERSION = re.search(r"AGENT_VERSION = '([^']+)'", agent_cells.N1).group(1)

# Inline CSS/JS in frontend/index.html; connect-src self for same-origin /api/chat.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

# Overridable in tests.
client_factory: ac.ClientFactory = ac.default_client_factory
agent_config: ac.AgentConfig | None = None

_cache: "OrderedDict[str, tuple[float, dict[str, Any]]]" = OrderedDict()
_flights: dict[str, "Flight"] = {}
_hits: dict[str, list[float]] = {}
_seen: dict[str, Any] = {"version": "", "at": 0.0}   # last agent reply this process actually received


class Turn(BaseModel):
    role: str
    text: str

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        if v not in ("user", "assistant"):
            raise ValueError("role must be user or assistant")
        return v

    @field_validator("text", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError("must be text")
        return v[:600]


class ChatIn(BaseModel):
    action: str = "chat"
    message: str = Field("", validate_default=True)
    state: dict[str, Any] | None = None
    recent_turns: list[Turn] = []
    sample_file_url: str = ""

    @field_validator("message", "sample_file_url", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> str:
        if v is None:
            return ""
        if not isinstance(v, str):
            raise ValueError("must be text")
        return v.strip()

    @field_validator("message")
    @classmethod
    def _msg(cls, v: str, info: ValidationInfo) -> str:
        if not v and info.data.get("action") != "manual":
            raise ValueError("message is required")
        if len(v) > MAX_MESSAGE:
            raise ValueError(f"message is too long (max {MAX_MESSAGE} characters)")
        return v

    @field_validator("state")
    @classmethod
    def _state(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is not None and len(json.dumps(v)) > MAX_STATE:
            raise ValueError("state is too large")
        return v

    @field_validator("action")
    @classmethod
    def _action(cls, v: str) -> str:
        if v not in ("chat", "manual"):
            raise ValueError("action must be chat or manual")
        return v

    @field_validator("recent_turns")
    @classmethod
    def _turns(cls, v: list[Turn]) -> list[Turn]:
        return v[-MAX_TURNS:]

    @field_validator("sample_file_url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not v:
            return v
        p = urlparse(v)
        if p.scheme != "https" or not p.netloc:
            raise ValueError("sample_file_url must be a public https link")
        host = (p.hostname or "").lower()
        if host == "localhost" or host.endswith(".local") or host.endswith(".internal") or _is_ip_literal(host):
            raise ValueError("sample_file_url must use a public host name, not an address or local name")
        if ALLOWED_SAMPLE_HOSTS and not any(host == h or host.endswith("." + h) for h in ALLOWED_SAMPLE_HOSTS):
            raise ValueError("sample_file_url host is not allowed")
        return v


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _jwt_expiry(token: str) -> float | None:
    try:
        part = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return float(data["exp"]) if "exp" in data else None
    except Exception:
        return None


def config_problems() -> list[str]:
    """Configuration gaps that block a real agent turn. Used by /api/ready and production startup."""
    problems: list[str] = []
    cfg = agent_config
    token = cfg.token if cfg else os.environ.get("SUPERVITY_TOKEN", "").strip()
    wf = cfg.workflow_id if cfg else os.environ.get("WORKFLOW_ID", "").strip()
    if not token:
        problems.append("SUPERVITY_TOKEN is not set")
    else:
        exp = _jwt_expiry(token)
        if exp is not None and exp < time.time():
            problems.append("SUPERVITY_TOKEN has expired")
    if not wf:
        problems.append("WORKFLOW_ID is not set")
    return problems


def ensure_production_config() -> None:
    """Refuse to start in production when required config is missing or the token is expired."""
    if not is_production():
        return
    problems = config_problems()
    if problems:
        raise RuntimeError("Production config incomplete: " + "; ".join(problems))


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_production_config()
    yield


_prod = is_production()
app = FastAPI(
    title="Digital FDE",
    lifespan=lifespan,
    docs_url=None if _prod else "/docs",
    redoc_url=None if _prod else "/redoc",
    openapi_url=None if _prod else "/openapi.json",
)


@app.middleware("http")
async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = _CSP
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


if ALLOWED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.exception_handler(ac.AgentError)
async def _agent_error(_: Request, exc: ac.AgentError) -> JSONResponse:
    return _err(exc.status, exc.code, exc.message)


@app.exception_handler(RequestValidationError)
async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
    details = []
    for e in exc.errors():
        loc = ".".join(str(x) for x in e.get("loc", []) if x != "body")
        msg = str(e.get("msg", "")).removeprefix("Value error, ")
        details.append({"field": loc or "body", "message": msg})
    return JSONResponse(status_code=422, content={"error": {"code": "invalid_input", "message": "That message could not be sent.", "details": details}})


# ---------- reading the agent's result ----------
def _shape(obj: Any, depth: int = 0) -> Any:
    """A compact description of a result's structure, for logs. Never includes values."""
    if depth > 3:
        return "..."
    if isinstance(obj, dict):
        return {k: _shape(v, depth + 1) for k, v in list(obj.items())[:12]}
    if isinstance(obj, list):
        return [_shape(obj[0], depth + 1), f"x{len(obj)}"] if obj else []
    return type(obj).__name__


def find_response(obj: Any, depth: int = 0) -> dict[str, Any] | None:
    """Find fde_response inside the agent's result, whatever envelope the platform wraps it in.

    The agent prints one JSON document with a top-level fde_response key. The platform's
    result event has not been observed, so this accepts it as a dict key, inside a JSON
    string, or inside noisy text. If it is not found the shape is logged (see _shape) and,
    with RECORD_STREAMS_DIR set, the raw stream is saved so the contract can be pinned.
    """
    if depth > 8:
        return None
    if isinstance(obj, str):
        t = obj.strip()
        if "fde_response" not in t:
            return None
        try:
            return find_response(json.loads(t), depth + 1)
        except ValueError:
            i = t.find("{")
            while i >= 0:
                try:
                    return find_response(json.JSONDecoder().raw_decode(t[i:])[0], depth + 1)
                except ValueError:
                    i = t.find("{", i + 1)
        return None
    if isinstance(obj, dict):
        if isinstance(obj.get("fde_response"), dict):
            return obj["fde_response"]
        for v in obj.values():
            r = find_response(v, depth + 1)
            if r:
                return r
    if isinstance(obj, list):
        for v in obj:
            r = find_response(v, depth + 1)
            if r:
                return r
    return None


def _s(v: Any, n: int) -> str:
    return v[:n] if isinstance(v, str) else ""


def clean_response(raw: dict[str, Any]) -> dict[str, Any]:
    """Whitelist and bound what the browser receives. Nothing from the agent is trusted as markup or as a link."""
    warnings: list[str] = []
    reply = _s(raw.get("reply"), 4000).strip()
    if not reply:
        raise ac.AgentRunFailed("The agent returned an empty reply. Try again.")
    out: dict[str, Any] = {"reply": reply}
    for k in ("phase", "agent_version", "readiness"):
        out[k] = _s(raw.get(k), 40)
    engine = _s(raw.get("engine"), 20) or "agent"
    if engine not in ("local", "agent"):
        engine = "agent"
    out["engine"] = engine
    if raw.get("fallback"):
        out["fallback"] = True
    for k in ("key_confirmed", "key_total"):
        out[k] = raw.get(k) if isinstance(raw.get(k), int) else 0
    out["completeness"] = raw.get("completeness") if isinstance(raw.get("completeness"), (int, float)) else 0
    areas = []
    for a in (raw.get("areas") if isinstance(raw.get("areas"), list) else [])[:20]:
        if isinstance(a, dict):
            areas.append({"key": _s(a.get("key"), 60), "label": _s(a.get("label"), 60), "status": _s(a.get("status"), 20), "value": _s(a.get("value"), 400), "critical": bool(a.get("critical"))})
    out["areas"] = areas
    fit = raw.get("fit") if isinstance(raw.get("fit"), dict) else {}
    out["fit"] = {"verdict": _s(fit.get("verdict"), 20) or "UNSURE"}
    for k in ("reasons", "kill_risks", "ask_to_see"):
        out["fit"][k] = [_s(x, 220) for x in (fit.get(k) if isinstance(fit.get(k), list) else [])[:3] if isinstance(x, str)]
    out["open_questions"] = [
        {"label": _s(q.get("label"), 60), "question": _s(q.get("question"), 400), "critical": bool(q.get("critical"))}
        for q in (raw.get("open_questions") if isinstance(raw.get("open_questions"), list) else [])[:6] if isinstance(q, dict)
    ]
    est = raw.get("estimate")
    out["estimate"] = (
        {"low_hours": est.get("low_hours"), "high_hours": est.get("high_hours"), "calibrated": bool(est.get("calibrated")), "basis": _s(est.get("basis"), 120)}
        if isinstance(est, dict) and isinstance(est.get("low_hours"), (int, float)) and isinstance(est.get("high_hours"), (int, float)) else None
    )
    out["notices"] = [_s(n, 240) for n in (raw.get("notices") if isinstance(raw.get("notices"), list) else [])[:5] if isinstance(n, str)]
    state = raw.get("state")
    if isinstance(state, dict):
        out["state"] = state
    else:
        out["state"] = None
        warnings.append("state_missing")

    m = raw.get("manual")
    out["manual"] = None
    if isinstance(m, dict):
        url = _s(m.get("url"), 2000)
        p = urlparse(url)
        host_ok = not ALLOWED_FILE_HOSTS or any(p.hostname and (p.hostname.lower() == h or p.hostname.lower().endswith("." + h)) for h in ALLOWED_FILE_HOSTS)
        if p.scheme == "https" and p.netloc and host_ok:
            bp = []
            for b in (m.get("blueprint") if isinstance(m.get("blueprint"), list) else [])[:8]:
                if isinstance(b, dict):
                    bp.append({"step": _s(b.get("step"), 120), "does": _s(b.get("does"), 300), "worker": _s(b.get("worker"), 120), "review": bool(b.get("review"))})
            out["manual"] = {"url": url, "title": _s(m.get("title"), 200) or "Solution manual", "verdict": _s(m.get("verdict"), 20), "blueprint": bp}
        else:
            warnings.append("manual_link_rejected")
            log.warning("Rejected manual link: scheme/host not allowed (host=%s)", p.hostname)

    if engine != "local" and out["agent_version"] != EXPECTED_AGENT_VERSION:
        warnings.append("agent_version_mismatch")
        log.warning("Agent version %r differs from this repo's %r. The published workflow and agent/cells.py have drifted.", out["agent_version"], EXPECTED_AGENT_VERSION)
    out["warnings"] = warnings
    return out


# ---------- bounded cache and rate limit ----------
def _cache_get(key: str) -> dict[str, Any] | None:
    hit = _cache.get(key)
    if not hit:
        return None
    if time.monotonic() - hit[0] >= CACHE_TTL_S:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return hit[1]


def _cache_put(key: str, value: dict[str, Any]) -> None:
    now = time.monotonic()
    for k in [k for k, (t, _) in _cache.items() if now - t >= CACHE_TTL_S]:
        _cache.pop(k, None)
    _cache[key] = (now, value)
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)


def _client_id(request: Request) -> str:
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for", "")
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def _rate_ok(who: str) -> bool:
    now = time.monotonic()
    if len(_hits) > HITS_MAX_KEYS:  # drop clients idle for a minute so the table cannot grow without bound
        for k in [k for k, v in _hits.items() if not v or now - v[-1] >= 60]:
            _hits.pop(k, None)
    window = [t for t in _hits.get(who, []) if now - t < 60]
    if len(window) >= RATE_LIMIT:
        _hits[who] = window
        return False
    window.append(now)
    _hits[who] = window
    return True


def _key(body: "ChatIn") -> str:
    raw = json.dumps([body.action, body.message, body.state, [t.model_dump() for t in body.recent_turns], body.sample_file_url], sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------- health ----------
@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/api/ready")
async def ready() -> Any:
    problems = config_problems()
    seen = _seen["version"]
    body = {"ready": not problems, "problems": problems, "expected_agent_version": EXPECTED_AGENT_VERSION,
            # "ready" means configured. These two say whether a real agent reply has been seen by this process.
            "live_verified": bool(_seen["at"]),
            "last_seen_agent_version": seen or None,
            "version_match": (seen == EXPECTED_AGENT_VERSION) if seen else None,
            "note": "ready = configuration present. live_verified stays false until a real turn succeeds; run python scripts/live_smoke.py"}
    return JSONResponse(status_code=200 if not problems else 503, content=body)


# ---------- the agent call ----------
class ClientGone(Exception):
    pass


class Flight:
    def __init__(self, task: "asyncio.Task[dict[str, Any]]") -> None:
        self.task = task
        self.waiters = 0


def _record(action: str, raw_events: list[tuple[str, str]]) -> None:
    if not RECORD_DIR:
        return
    try:
        Path(RECORD_DIR).mkdir(parents=True, exist_ok=True)
        path = Path(RECORD_DIR) / f"{int(time.time())}_{action}.json"
        path.write_text(json.dumps([{"event": e, "data": d} for e, d in raw_events], ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        log.exception("Could not record the agent stream")


def _turn_is_large(body: "ChatIn") -> bool:
    return bool(body.sample_file_url) or len(body.message or "") > LARGE_MESSAGE_CHARS


async def _execute(body: "ChatIn") -> dict[str, Any]:
    inputs = {
        "user_message": body.message or ("Please create the manual." if body.action == "manual" else ""),
        "action": body.action,
        "state_json": json.dumps(body.state, separators=(",", ":")) if body.state else "",
        "recent_turns": json.dumps([t.model_dump() for t in body.recent_turns], separators=(",", ":")) if body.recent_turns else "",
        "sample_file": body.sample_file_url,
        "rate_card_json": RATE_CARD_JSON,
    }
    cfg = agent_config or ac.AgentConfig.from_env()
    cfg = replace(cfg, timeout_s=min(cfg.timeout_s, MANUAL_TIMEOUT_S if body.action == "manual" else CHAT_TIMEOUT_S))
    try:
        out = await ac.run_agent(inputs, config=cfg, client_factory=client_factory)
    except ac.AgentError:
        if FDE_MODE == "hybrid" and body.action == "chat":
            fb = local_fde.respond(
                body.message,
                body.state,
                rate_card_json=RATE_CARD_JSON,
                force=True,
                notice="The agent is unavailable, so the built-in rules answered.",
            )
            if fb:
                fb["fallback"] = True
                return clean_response(fb)
        raise
    _record(body.action, out.get("raw") or [])
    raw = find_response(out.get("result"))
    if not raw:
        log.error("No fde_response in the agent result. Shape: %s", json.dumps(_shape(out.get("result"))))
        if FDE_MODE == "hybrid" and body.action == "chat":
            fb = local_fde.respond(
                body.message,
                body.state,
                rate_card_json=RATE_CARD_JSON,
                force=True,
                notice="The agent is unavailable, so the built-in rules answered.",
            )
            if fb:
                fb["fallback"] = True
                return clean_response(fb)
        raise ac.AgentRunFailed("The agent finished but did not return a reply. Try again.")
    raw.setdefault("engine", "agent")
    try:
        resp = clean_response(raw)
    except ac.AgentError:
        if FDE_MODE == "hybrid" and body.action == "chat":
            fb = local_fde.respond(
                body.message,
                body.state,
                rate_card_json=RATE_CARD_JSON,
                force=True,
                notice="The agent is unavailable, so the built-in rules answered.",
            )
            if fb:
                fb["fallback"] = True
                return clean_response(fb)
        raise
    _seen["version"] = resp.get("agent_version") or ""
    _seen["at"] = time.time()
    return resp


async def _wait(flight: Flight, request: Request) -> dict[str, Any]:
    """Wait for a shared run. If this caller disconnects and nobody else is waiting, cancel the run."""
    flight.waiters += 1
    waiter = asyncio.ensure_future(asyncio.shield(flight.task))
    try:
        while True:
            done, _ = await asyncio.wait({waiter}, timeout=0.5)
            if done:
                return waiter.result()
            if await request.is_disconnected():
                raise ClientGone()
    finally:
        waiter.cancel()
        flight.waiters -= 1
        if flight.waiters <= 0 and not flight.task.done():
            flight.task.cancel()
            log.info("Cancelled an agent run: every caller waiting on it had gone away.")


@app.post("/api/chat")
async def chat(body: ChatIn, request: Request) -> Any:
    if not _rate_ok(_client_id(request)):
        return _err(429, "rate_limited", "Too many messages. Wait a moment and try again.")

    mode = FDE_MODE if FDE_MODE in ("hybrid", "local", "agent") else "hybrid"
    if body.action == "manual" and mode == "local":
        return _err(503, "agent_required", "The manual needs the Supervity agent. Set FDE_MODE to hybrid or agent.")

    if body.action == "chat" and mode != "agent" and (mode == "local" or not _turn_is_large(body)):
        local = local_fde.respond(
            body.message,
            body.state,
            rate_card_json=RATE_CARD_JSON,
            force=(mode == "local"),
        )
        if local is not None:
            return {"response": clean_response(local), "cached": False, "shared": False}

    key = _key(body)
    hit = _cache_get(key)
    if hit is not None:
        return {"response": hit, "cached": True, "shared": False}

    flight = _flights.get(key)
    shared = flight is not None
    if flight is None:
        # No await between the lookup above and this registration, so two requests cannot both start a run.
        task = asyncio.create_task(_execute(body))
        flight = Flight(task)
        _flights[key] = flight

        def _done(t: "asyncio.Task[dict[str, Any]]", key: str = key) -> None:
            _flights.pop(key, None)
            if not t.cancelled() and t.exception() is None:
                result = t.result()
                if not result.get("fallback"):
                    _cache_put(key, result)
            elif not t.cancelled():
                t.exception()  # mark retrieved

        task.add_done_callback(_done)
    try:
        resp = await _wait(flight, request)
    except ClientGone:
        return _err(499, "client_closed", "The request was cancelled.")
    return {"response": resp, "cached": False, "shared": shared}


_front = Path(__file__).resolve().parent.parent / "frontend"
if _front.exists():
    app.mount("/static", StaticFiles(directory=_front), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_front / "index.html")
