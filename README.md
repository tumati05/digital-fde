# Digital FDE

**Author:** [Tumati Tejasri](https://github.com/tumati05) · Forward Deployed AI Engineer (internship), [Supervity](https://supervity.ai)

A conversational **digital forward deployed engineer**: scopes a client automation problem, challenges vague answers, gives a build / not-yet / don’t-build verdict, and can write a Word solution manual. Built as a FastAPI app that either answers with **local deterministic rules** (no LLM credits) or calls a published **Supervity Auto** workflow (the Digital FDE agent).

This README is written so an interviewer can see **what was built, why, and how every piece connects**.

---

## What is this project?

It is **not** a chatbot glued to a model key in the browser.

It is a **cost-aware discovery co-pilot** for Supervity delivery:

| Layer | Role |
|--------|------|
| `frontend/index.html` | Single-page UI: chat, progress panel, manual download |
| `backend/main.py` | FastAPI: validate input, hybrid routing, cache, rate limit, security |
| `backend/local_fde.py` | Rules engine for simple turns (no Supervity call) |
| `backend/agent_client.py` | HTTP client to Supervity’s **SSE** execute/stream API |
| `agent/cells.py` | Source of truth for the five-step Auto workflow (prompts + logic) |
| `agent/workflow.json` | Generated workflow payload to publish on Supervity Auto |

**No database.** Conversation memory is **client-carried state**: the browser stores `state` + last turns (sessionStorage) and sends them on every `POST /api/chat`. The API is **stateless** between requests. Process-local cache / single-flight / rate limit are optional in-memory optimizations, not durable storage.

---

## What does it do?

1. **Discover** — one concrete question at a time across 12 areas (8 critical).
2. **Push back** — “a lot” / “soon” are rejected as too vague.
3. **Judge fit** — `BUILD` / `NOT_YET` / `DONT_BUILD` / `UNSURE`, with reasons, kill risks, and “look at first”.
4. **Propose or advise** — when enough is known, propose an Auto; when the answer is no, say so clearly.
5. **Manual** — on request, the Supervity agent writes a Word doc (blueprint, open questions, effort range). A human must review it.
6. **Hybrid cost control** — short patterned chats use local rules (free); large / unclear / attachment / manual use the agent (credits).

---

## Architecture (end to end)

```text
Browser (index.html)
    |  POST /api/chat  { message, state, recent_turns, action?, sample_file_url? }
    v
FastAPI (backend/main.py)
    |-- validate + rate limit
    |-- FDE_MODE=hybrid?
    |      |-- short + rules match --> local_fde.respond() --> JSON (engine: local)
    |      |-- large / attach / manual / no match --> agent_client.run_agent()
    |              |
    |              v
    |         Supervity Auto  POST .../workflow-runs/execute/stream  (multipart)
    |              |  Server-Sent Events (SSE) until event: result
    |              v
    |         find fde_response --> clean_response --> JSON (engine: agent)
    |-- agent chat failure in hybrid --> local_fde force fallback (not cached)
    v
Browser updates reply + panel; keeps state for next turn
```

### Is there polling? Webhooks? Tool calling?

| Mechanism | Used? | How |
|-----------|--------|-----|
| **Polling** | No | The backend does **not** poll Supervity on a timer. |
| **Webhooks** | No | Supervity does not push back to us; we hold one HTTP stream open. |
| **SSE (Server-Sent Events)** | **Yes** | `POST …/execute/stream` returns an event stream. We read lines until `event: result` (or `error`). That is a **single long-lived HTTP response**, not polling. |
| **Tool calling** | Inside the **agent**, not in FastAPI | The Supervity Auto steps use platform `ai_generate` with Pydantic schemas, file profile, Word upload, etc. FastAPI only forwards inputs and parses the final `fde_response`. |

So: **fetch = one multipart POST + consume SSE until result.** Accurate and minimal because the UI only paints the whitelisted JSON fields; the model is constrained by schemas and post-checks in the workflow cells.

---

## Why FastAPI?

- Async-friendly for waiting on a long Supervity stream without blocking the process incorrectly.
- Clear `POST /api/chat` / `GET /api/health` / `GET /api/ready` surface for a thin proxy.
- Pydantic validation on every turn (length caps, URL rules) before any credit is spent.
- Easy to serve the static frontend from the same process in demos.

FastAPI here is a **control plane**: validate, route (rules vs agent), protect cost, sanitize output. It does **not** host the LLM.

---

## How is the Supervity agent connected?

1. Publish the Digital FDE workflow on **Supervity Auto** (team Marketplace - Auto). That gives a **`WORKFLOW_ID`** (UUID in the Auto URL).
2. Create an API key / token → `SUPERVITY_TOKEN` (Bearer).
3. Backend (`agent_client.py`) calls:

   `POST https://auto.supervity.ai/workflow/api/v1/workflow-runs/execute/stream`

   Multipart fields:

   - `workflowId` = env `WORKFLOW_ID`
   - `inputs.user_message`, `inputs.action`, `inputs.state_json`, `inputs.recent_turns`, `inputs.sample_file`, `inputs.rate_card_json`

4. Stream is parsed as SSE; we search the result envelope for `fde_response`.
5. `clean_response` whitelists fields, bounds sizes, checks manual HTTPS links, flags `agent_version` drift vs `agent/cells.py`.

**Yes — the workflow ID is the one from Supervity Auto** (e.g. the published Digital FDE operator). It is **not** committed; it lives only in `.env` / deploy secrets.

---

## Stateless API and “state” (no database)

There is **no Postgres / Redis / Mongo** for chat history in this design.

- Each `POST /api/chat` is independent from the server’s durable store.
- The **client** sends:
  - `state` — structured notes `{ client_name, fields: { area: { v, p, e } }, wbs, risks, turn }`
  - `recent_turns` — last 6 messages (capped)
- The engine (local or agent) returns **updated `state`**; the browser saves it (sessionStorage) and sends it next time.

So memory is **request-carried state**, not a DB. That keeps the demo deployable as one process and makes the API easy to reason about. Tradeoff: refresh / new browser tab starts clean unless sessionStorage still has the session; multi-device sync would need a DB later.

In-process **cache / single-flight / rate limit** exist only to save credits and load; they are bounded memory and reset on restart. Documented: scale-out needs Redis.

---

## Hybrid engines (why responses feel fast and cheap)

| Mode | Behaviour |
|------|-----------|
| `hybrid` (default) | Short chats → `local_fde` if rules match. Large message, attachment, unmatched text, or manual → Supervity agent. Agent chat failure → rules fallback (`fallback: true`, not cached). |
| `local` | Rules only. Manual → `503 agent_required`. |
| `agent` | Always Supervity. |

**Local rules** (`local_fde.py`): regex / keywords for volume (incl. “three hundred”, `2k/week` → per-day), systems (SAP, Outlook, …), compliance, formats, etc. Same 12 areas / 8 critical / questions / starter rate card as `agent/cells.py` (drift test enforces sync).

**Honest limit:** rules are **structured intake, not a judge**. They cannot understand free-form client stories, design a real blueprint, or write the Word manual. That is why the agent still exists.

**Why “accurate and minimalistic”?**

- Structured JSON out (`reply`, `phase`, `fit`, `areas`, …), not free HTML from the model into the page.
- Backend whitelist + length caps.
- Agent uses **schema-constrained** generation (`response_schema_pydantic_model`) and Python post-steps (evidence check, phase machine).
- Local path is **O(message length)** regex work — no network — so short turns feel instant and burn **zero** credits.

---

## Frontend: how messages are sent and shown

File: `frontend/index.html` (vanilla JS, no React).

1. User types in a textarea; Enter submits the form.
2. JS builds JSON: `{ action: "chat", message, state, recent_turns, sample_file_url? }`.
3. `fetch("/api/chat", { method: "POST", body: JSON.stringify(...) })` — **one request per turn** (not websockets for chat).
4. While waiting, a “thinking” row shows elapsed seconds; **Stop** aborts via `AbortController` (server can cancel a shared agent run if no waiters remain).
5. On success: append assistant reply, update “What I understand” panel (`areas`, `fit`, estimate), store `state` + turns in `sessionStorage`.
6. Manual button sets `action: "manual"`; if the response includes `manual.url` (https), show download + blueprint list.

Escaping: user/agent text is HTML-escaped before insert. Manual links must be `https://`.

---

## Agent workflow (what runs on Supervity)

Five cells in `agent/cells.py` (published via `workflow.json`):

1. **Read conversation** — load state, turns, optional sample file (https only, no redirects, 10 MB, no IP/localhost).
2. **Understand message** — one `ai_generate` call → structured updates, next question, fit verdict.
3. **Check and plan** — verify FACT evidence against source text; gaps; phase; effort from rate card.
4. **Manual** — second model call only if requested → Word doc → `upload_output_file`.
5. **Reply** — print `fde_response` JSON for the backend to find.

`agent_version` (e.g. `2026-10-22.4`) must match the published Auto; mismatch surfaces a warning in the UI.

---

## Backend endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /api/health` | Process up |
| `GET /api/ready` | Config present (token + workflow); `live_verified` only after a real agent reply in this process |
| `POST /api/chat` | One turn: rules and/or agent |
| `GET /` | Serves the UI |

Cost controls on the agent path: input caps, identical-turn cache, single-flight (shared in-flight runs), disconnect cancel, per-client rate limit.

Production harden (optional): `APP_ENV=production` fail-fast, security headers, TrustedHost via `ALLOWED_HOSTS`, Docker single worker.

---

## Repository layout

```text
agent/          cells.py, workflow.json, rate_card.example.json
backend/        main.py, agent_client.py, local_fde.py
frontend/       index.html
scripts/        build_workflow.py, live_smoke.py
tests/          backend, agent cells, workflow sync, local_fde drift
Dockerfile      single-worker image
.env.example    secrets template (never commit .env)
```

---

## Run locally

Windows:

```powershell
./run.ps1
```

macOS / Linux:

```bash
./run.sh
```

First run creates `.venv`, installs deps, copies `.env.example` → `.env`. Fill:

```env
SUPERVITY_TOKEN=...          # Bearer from Supervity API keys
WORKFLOW_ID=...              # UUID of published Digital FDE Auto
FDE_MODE=hybrid
LARGE_MESSAGE_CHARS=400
```

Then open http://localhost:8000.

**Never commit `.env`.** `.gitignore` excludes `.env`, `.venv`, caches, `*.zip`, recorded streams, editor folders.

---

## Configuration

| Variable | Purpose |
|----------|---------|
| `SUPERVITY_TOKEN`, `WORKFLOW_ID` | Agent path auth + which Auto to run |
| `FDE_MODE` | `hybrid` \| `local` \| `agent` |
| `LARGE_MESSAGE_CHARS` | Hybrid: longer chats go to agent (default 400) |
| `APP_ENV` | `development` \| `production` |
| `ALLOWED_HOSTS` | Optional TrustedHost list |
| `CHAT_TIMEOUT_S`, `MANUAL_TIMEOUT_S` | Deadlines |
| `RATE_LIMIT`, `TRUST_PROXY` | Rate limit + X-Forwarded-For |
| `ALLOWED_FILE_HOSTS`, `ALLOWED_SAMPLE_HOSTS` | Link allowlists |
| `RATE_CARD_JSON` | Delivery hours override |
| `RECORD_STREAMS_DIR` | Debug: save raw SSE |

---

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Covers agent cell logic (scripted model), backend (fake SSE), workflow sync, local_fde drift vs `cells.py`, hybrid routing.

Live quality (needs credits):

```bash
python scripts/live_smoke.py
```

---

## Deploy

**One Uvicorn worker** (in-process cache / single-flight / rate limit).

```bash
docker build -t digital-fde .
docker run --rm -p 8000:8000 \
  -e SUPERVITY_TOKEN=... \
  -e WORKFLOW_ID=... \
  -e APP_ENV=production \
  -e FDE_MODE=hybrid \
  digital-fde
```

---

## Interview cheat sheet (say this out loud)

1. **Problem:** FDEs repeat the same discovery questions; agent calls cost credits.
2. **Solution:** FastAPI proxy + hybrid rules/agent + Supervity Auto workflow; no DB — state in the request.
3. **Transport to LLM platform:** multipart POST + **SSE stream**, not polling/webhooks.
4. **Why FastAPI:** validation, async wait on stream, one deployable surface for UI + API.
5. **Accuracy:** schemas + evidence checks + response whitelist; local path is deterministic regex.
6. **Limits:** rules don’t understand free text; manual needs a human; not a replacement for a real FDE; horizontal scale needs Redis.
7. **What I’d add next:** explicit escalate-to-human FDE phase; live fixture from `live_smoke`; Redis if multi-worker.

---

## Honest limits

- Rules = structured intake, **not** a judge.
- Starter rate card until real delivery hours are supplied.
- Agent path needs Supervity credits; prove with one live conversation.
- A person must review every manual before it is used with a client.
- In-memory cache/rate limit do not survive multi-worker or restart.
