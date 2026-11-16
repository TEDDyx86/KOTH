from app.tooling.config import load_tools_config
from app.tooling.doctor import inventory


def test_inventory_marks_present_tools():
    rows = inventory(load_tools_config())
    by_name = {r["name"]: r for r in rows}
    assert by_name["python"]["present"] is True
    assert by_name["python"]["path"] is not None
    assert by_name["nmap"]["present"] is True


def test_inventory_marks_missing_tools(tmp_path):
    cfg = tmp_path / "tools.yaml"
    cfg.write_text("tools:\n  definitely_not_a_real_tool_xyz:\n    mode: native\n")
    rows = inventory(load_tools_config(cfg))
    assert rows[0]["name"] == "definitely_not_a_real_tool_xyz"
    assert rows[0]["present"] is False
    assert rows[0]["path"] is None
