# Digital FDE

A small web app that helps scope client automation work the way a forward deployed engineer would: ask clear questions, push back on vague answers, say whether something is worth building, and optionally draft a Word solution manual.

Built by [Tumati Tejasri](https://github.com/tumati05) while interning as a Forward Deployed AI Engineer at [Supervity](https://supervity.ai).

---

## The idea

Delivery folks often ask the same discovery questions again and again. Calling a full AI agent for every short reply also burns credits.

Digital FDE splits the work:

- **Simple turns** (things like “300 invoices a day in SAP”) are handled by a local rules engine in Python. No model call, no credits.
- **Harder turns** — long or messy messages, sample files, or “write the manual” — go to a **Supervity Auto** workflow (the Digital FDE agent).
- If the agent is down during a normal chat, hybrid mode can fall back to the rules so the conversation doesn’t die.

It is a **discovery helper**, not a replacement for a real FDE. Someone should still review any manual before it goes to a client.

---

## What you can do in the app

- Chat through discovery: one focused question at a time across twelve areas (eight of them “key”).
- See progress in a side panel: what is known, inferred, or still assumed.
- Get a plain verdict: worth building, not yet, don’t build, or too early to say — plus who might block it and what to look at first.
- Attach a public https link to a sample CSV / Excel / PDF / Word / text file for the agent to profile.
- Ask for a Word manual with a blueprint and open questions (agent path only).

---

## How it’s put together

| Piece | What it is |
|--------|------------|
| `frontend/index.html` | The UI (plain HTML/CSS/JS) |
| `backend/main.py` | FastAPI app: validation, hybrid routing, cache, rate limits |
| `backend/local_fde.py` | Local rules for simple chats |
| `backend/agent_client.py` | Talks to Supervity over their streaming API |
| `agent/cells.py` | The agent’s five steps (prompts + logic) |
| `agent/workflow.json` | What you publish on Supervity Auto |

There is **no database**. The browser keeps the conversation `state` and the last few turns (sessionStorage) and sends them with every chat request. The server treats each request as self-contained. A small in-memory cache and rate limit live in the process only to save credits — they vanish on restart.

```text
You (browser)
   → POST /api/chat
   → FastAPI decides: local rules or Supervity agent
   → JSON reply (message, progress, optional manual link)
   → UI updates; state saved for the next message
```

When the agent runs, the backend opens **one** streaming HTTP request to Supervity (Server-Sent Events), waits for the result, and returns cleaned JSON. It does not poll on a timer and does not use webhooks for chat.

---

## Running it

**Windows**

```powershell
./run.ps1
```

**macOS / Linux**

```bash
./run.sh
```

The first run creates a virtualenv, installs dependencies, and copies `.env.example` to `.env`. Put your Supervity token and workflow id in `.env`, run again, then open http://localhost:8000.

```env
SUPERVITY_TOKEN=...     # from Supervity API keys
WORKFLOW_ID=...         # id of the published Digital FDE Auto
FDE_MODE=hybrid         # hybrid | local | agent
```

Keep `.env` private — it is gitignored on purpose.

---

## Modes

| `FDE_MODE` | Behaviour |
|------------|-----------|
| `hybrid` (default) | Rules for short/simple chats; agent for large messages, files, and the manual. Agent chat failures can fall back to rules. |
| `local` | Rules only. Asking for a manual returns an error that the agent is required. |
| `agent` | Always call Supervity. |

Local rules match patterns (volume, system names, and so on). They do **not** “understand” a messy client story the way the agent can. That limit is intentional.

---

## Useful details if you’re curious

**Endpoints:** `GET /` (UI), `GET /api/health`, `GET /api/ready`, `POST /api/chat`.

**Supervity:** the workflow id is the UUID of the published Digital FDE Auto. The backend sends multipart form fields (`user_message`, `state_json`, etc.) to  
`https://auto.supervity.ai/workflow/api/v1/workflow-runs/execute/stream`  
and reads the stream until a result arrives.

**Agent steps** (in `agent/cells.py`): load conversation → one model call to understand the turn → check evidence and pick the next question → optional Word manual → return a structured `fde_response`.

**Frontend:** each send is a normal `fetch` to `/api/chat`. Stop uses `AbortController`. Replies and the side panel are rendered from JSON; text is escaped before it hits the page.

**Deploy:** one Uvicorn worker is the intended setup (cache and rate limit are in-process). Docker:

```bash
docker build -t digital-fde .
docker run --rm -p 8000:8000 \
  -e SUPERVITY_TOKEN=... \
  -e WORKFLOW_ID=... \
  -e APP_ENV=production \
  -e FDE_MODE=hybrid \
  digital-fde
```

**Tests**

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

For a real agent smoke test (needs credits): `python scripts/live_smoke.py`.

More env vars are listed in `.env.example`.

---

## What’s in the repo

```text
agent/       Agent cells + workflow export
backend/     FastAPI + rules + Supervity client
frontend/    Single-page UI
scripts/     Build workflow / live smoke
tests/       Backend, agent, sync, and drift checks
```

---

## Limits (worth knowing)

- Local rules are intake helpers, not deep judgment.
- Effort hours use a starter rate card until you supply real delivery hours.
- The agent path needs Supervity credits.
- Always have a person review the manual.
- Scaling to many workers would need shared cache/rate-limit storage (e.g. Redis); that is not in this repo yet.
