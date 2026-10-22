"""agent/workflow.json is generated from agent/cells.py. If they differ, someone edited one by hand."""
import json
import pathlib
import sys

root = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "scripts"))
import build_workflow  # noqa: E402


def test_workflow_json_matches_cells():
    on_disk = json.loads((root / "agent" / "workflow.json").read_text(encoding="utf-8"))
    assert on_disk == json.loads(json.dumps(build_workflow.build())), "Run: python scripts/build_workflow.py"


def test_every_step_cell_compiles_and_chain_is_linear():
    import ast
    wf = build_workflow.build()
    for i, s in enumerate(wf["steps"], 1):
        compile(s["code_cell"], s["id"], "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        assert s["depends_on"] == ([f"step_{i-1}"] if i > 1 else [])
    assert wf["start_at"] == ["step_1"]
    assert [x["name"] for x in wf["inputs"]] == ["user_message", "action", "state_json", "recent_turns", "sample_file", "rate_card_json"]
