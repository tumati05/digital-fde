"""Runs the REAL Digital FDE agent end to end and records what it sends back.

    python scripts/live_smoke.py             # two chat turns and the manual (about 4 model calls)
    python scripts/live_smoke.py --no-manual # chat turns only

Needs SUPERVITY_TOKEN and WORKFLOW_ID, and an account with credits. Every raw stream is saved to
tests/fixtures/recorded_<n>.json, and tests/test_backend.py then replays them, so the backend is
checked against what the platform really sends instead of what we guessed. Exit code 1 on any failure.
It uses your credits and your real account; the messages below are made up."""
import asyncio
import json
import pathlib
import sys
import time

root = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from backend import agent_client as ac  # noqa: E402
from backend import main  # noqa: E402

FIXTURES = root / "tests" / "fixtures"
TURNS = [
    "We want to automate our invoice thing, it takes too long. Finance handles it in SAP and a lot comes by email as PDFs. Maybe 300 a day.",
    "About three hundred a day, more at month end. Anything that does not match a purchase order goes to the AP lead, Priya.",
]
failures = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> {detail}"))
    if not cond:
        failures.append(name)


async def one(cfg, inputs, label):
    t0 = time.time()
    out = await ac.run_agent(inputs, config=cfg)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    n = len(list(FIXTURES.glob("recorded_*.json"))) + 1
    path = FIXTURES / f"recorded_{n:02d}_{label}.json"
    path.write_text(json.dumps([{"event": e, "data": d} for e, d in out["raw"]], ensure_ascii=False, indent=1), encoding="utf-8")
    raw = main.find_response(out["result"])
    check(f"{label}: fde_response found in the result", raw is not None, json.dumps(main._shape(out["result"])))
    if raw is None:
        return None
    resp = main.clean_response(raw)
    print(f"      {time.time() - t0:.0f}s, recorded to {path.relative_to(root)}")
    check(f"{label}: agent version matches this repo", "agent_version_mismatch" not in resp["warnings"], resp["agent_version"])
    check(f"{label}: state returned", resp["state"] is not None)
    return resp


async def main_async(with_manual):
    cfg = ac.AgentConfig.from_env()
    state, turns = None, []
    for i, msg in enumerate(TURNS, 1):
        inputs = {"user_message": msg, "action": "chat", "state_json": json.dumps(state) if state else "",
                  "recent_turns": json.dumps(turns[-6:]) if turns else ""}
        r = await one(cfg, inputs, f"chat{i}")
        if not r:
            return
        print("\nFDE says:", r["reply"], "\n")
        check(f"chat{i}: asks at most one question", r["reply"].count("?") <= 2, r["reply"])
        state = r["state"]
        turns += [{"role": "user", "text": msg}, {"role": "assistant", "text": r["reply"]}]
    check("facts were captured from the messages", any(a["status"] == "FACT" for a in r["areas"]))
    if with_manual:
        r = await one(cfg, {"user_message": "Please create the manual.", "action": "manual",
                            "state_json": json.dumps(state), "recent_turns": json.dumps(turns[-6:])}, "manual")
        if r:
            check("manual link is https", bool(r["manual"] and r["manual"]["url"].startswith("https://")), r["manual"])
            check("manual has a blueprint", bool(r["manual"] and r["manual"]["blueprint"]))


if __name__ == "__main__":
    try:
        asyncio.run(main_async("--no-manual" not in sys.argv))
    except ac.AgentError as e:
        failures.append(e.message)
        print("FAIL  agent error:", e.message)
    print("\n" + ("All checks passed." if not failures else f"{len(failures)} check(s) failed: {failures}"))
    sys.exit(1 if failures else 0)
