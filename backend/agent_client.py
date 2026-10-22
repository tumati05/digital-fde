"""Thin client for the Digital FDE agent (a Supervity Auto workflow).

The agent IS the backend: this module only forwards validated input to the
workflow's streaming endpoint and turns its Server-Sent Events into one result.
No model is called here, so no model key is needed and no tokens are spent
outside the agent's own model calls (one per chat turn, two when it writes the manual).
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Callable

import httpx

DEFAULT_BASE_URL = "https://auto.supervity.ai"
EXECUTE_PATH = "/workflow/api/v1/workflow-runs/execute/stream"


class AgentError(Exception):
    """Base class. `status` is the HTTP status this backend should answer with."""

    status = 502
    code = "agent_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class AgentNotConfigured(AgentError):
    status, code = 500, "not_configured"


class AgentOutOfCredits(AgentError):
    status, code = 503, "agent_out_of_credits"


class AgentTimeout(AgentError):
    status, code = 504, "agent_timeout"


class AgentAuthError(AgentError):
    status, code = 502, "agent_auth_failed"


class AgentRunFailed(AgentError):
    status, code = 502, "agent_run_failed"


@dataclass
class AgentConfig:
    token: str
    workflow_id: str
    base_url: str = DEFAULT_BASE_URL
    timeout_s: float = 240.0

    @classmethod
    def from_env(cls) -> "AgentConfig":
        token = os.environ.get("SUPERVITY_TOKEN", "").strip()
        wf = os.environ.get("WORKFLOW_ID", "").strip()
        if not token or not wf:
            raise AgentNotConfigured(
                "Set SUPERVITY_TOKEN and WORKFLOW_ID in the environment."
            )
        return cls(
            token=token,
            workflow_id=wf,
            base_url=os.environ.get("SUPERVITY_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            timeout_s=float(os.environ.get("AGENT_TIMEOUT_S", "240")),
        )


def parse_sse_line_stream(lines: list[str]) -> list[tuple[str, str]]:
    """Group raw SSE lines into (event, data) pairs. Pure function, easy to test."""
    events: list[tuple[str, str]] = []
    event, data = "message", []
    for line in lines:
        if line == "":
            if data:
                events.append((event, "\n".join(data)))
            event, data = "message", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        events.append((event, "\n".join(data)))
    return events


def _looks_out_of_credits(text: str) -> bool:
    t = text.lower()
    return "credit" in t and ("exhaust" in t or "insufficient" in t or "purchase" in t)


ClientFactory = Callable[[float], httpx.AsyncClient]


def default_client_factory(timeout_s: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=15.0))


async def run_agent(
    inputs: dict[str, str],
    config: AgentConfig | None = None,
    client_factory: ClientFactory = default_client_factory,
) -> dict[str, Any]:
    """Run the workflow once and return {"result": ..., "events": n, "raw": [(event, data), ...]}."""
    cfg = config or AgentConfig.from_env()
    files = {"workflowId": (None, cfg.workflow_id)}
    for k, v in inputs.items():
        if v:  # omit blank optionals so the agent sees them as absent
            files[f"inputs.{k}"] = (None, v)

    url = cfg.base_url + EXECUTE_PATH
    headers = {"Authorization": f"Bearer {cfg.token}"}
    lines: list[str] = []

    async def _stream() -> None:
        async with client_factory(cfg.timeout_s) as client:
            async with client.stream("POST", url, headers=headers, files=files) as resp:
                if resp.status_code in (401, 403):
                    raise AgentAuthError("The agent rejected the token (401/403).")
                if resp.status_code == 402:
                    raise AgentOutOfCredits("The agent account is out of credits.")
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")[:500]
                    if _looks_out_of_credits(body):
                        raise AgentOutOfCredits("The agent account is out of credits.")
                    raise AgentRunFailed(f"Agent returned HTTP {resp.status_code}.")
                async for line in resp.aiter_lines():
                    lines.append(line)

    try:
        # httpx timeouts are per read; this is the overall deadline for the whole run.
        await asyncio.wait_for(_stream(), timeout=cfg.timeout_s)
    except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
        raise AgentTimeout("The agent did not finish in time.") from exc
    except httpx.TransportError as exc:
        raise AgentRunFailed(f"Could not reach the agent: {type(exc).__name__}.") from exc

    events = [(e, d) for e, d in parse_sse_line_stream(lines) if e != "ping"]
    if not events:
        raise AgentRunFailed("The agent returned no events.")

    for ev, data in events:
        if ev == "error":
            if _looks_out_of_credits(data):
                raise AgentOutOfCredits("The agent account is out of credits.")
            raise AgentRunFailed("The agent reported an error: " + data[:300])
    for ev, data in events:
        if ev == "result":
            try:
                payload: Any = json.loads(data) if data else None
            except json.JSONDecodeError:
                payload = data
            return {"result": payload, "events": len(events), "raw": events}
    raise AgentRunFailed("The agent stream ended without a result.")
