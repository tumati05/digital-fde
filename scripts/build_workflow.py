"""Builds agent/workflow.json from agent/cells.py, the single source of truth for the agent.

Edit agent/cells.py, run `python scripts/build_workflow.py`, then publish agent/workflow.json to
Supervity (create_workflow, or patch_workflow with the changed steps). Bump AGENT_VERSION in
cells.py whenever the agent changes: the backend compares it with what the published agent
reports and warns when the two have drifted apart."""
import hashlib
import json
import pathlib
import re
import sys

root = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from agent import cells as C  # noqa: E402

STEPS = [
    ("Read the conversation", "Loads the saved conversation, the new message and any sample file.", C.N1),
    ("Understand the message", "One model call updates what is known, drafts the reply and judges whether this is worth building.", C.N2),
    ("Check and plan", "Verifies evidence, finds gaps, picks the next question, sets the build verdict and sizes the work.", C.N3),
    ("Write the manual", "Writes the solution manual as a Word file when asked.", C.N4),
    ("Reply", "Returns the reply, progress and state as JSON.", C.N5),
]


def inp(name, typ, label, required, description):
    return {"name": name, "type": typ, "label": label, "required": required, "description": description}


def build():
    steps = []
    for i, (name, desc, code) in enumerate(STEPS, 1):
        steps.append({"id": f"step_{i}", "name": name, "description": desc, "services": [],
                      "depends_on": [f"step_{i-1}"] if i > 1 else [],
                      "next_steps": [{"step_id": f"step_{i+1}"}] if i < len(STEPS) else [],
                      "code_cell": code.strip() + "\n"})
    return {
        "name": "Digital FDE",
        "description": "A conversational forward deployed engineer. Scopes a client problem, says honestly whether it is worth building, and writes the solution manual on request.",
        "business_functions": ["Delivery", "Solutions Engineering"], "envs": [],
        "inputs": [
            inp("user_message", "textarea", "Message", True, "What the person just said."),
            inp("action", "text", "Action", False, "chat or manual. Leave blank for chat."),
            inp("state_json", "textarea", "Saved state", False, "State returned by the previous turn."),
            inp("recent_turns", "textarea", "Recent turns", False, "JSON list of the last few turns."),
            inp("sample_file", "file", "Sample file", False, "Optional CSV, Excel, PDF, Word or text file."),
            inp("rate_card_json", "textarea", "Rate card", False, "Optional JSON of hours per work unit from your delivery history, for example {\"WORKFLOW_AUTO_BUILD\": 40}. Without it the estimate uses a placeholder card."),
        ],
        "start_at": ["step_1"], "steps": steps,
        "pip_packages": ["pandas", "openpyxl", "pypdf", "python-docx", "httpx"], "schedule": None,
    }


if __name__ == "__main__":
    wf = build()
    out = root / "agent" / "workflow.json"
    out.write_text(json.dumps(wf, indent=1, ensure_ascii=False), encoding="utf-8")
    version = re.search(r"AGENT_VERSION = '([^']+)'", C.N1).group(1)
    print(f"Wrote {out.relative_to(root)} (agent version {version})")
    for s in wf["steps"]:
        print(" ", s["id"], hashlib.sha256(s["code_cell"].encode()).hexdigest()[:12], s["name"])
