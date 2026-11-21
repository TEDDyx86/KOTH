import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlmodel import Session, SQLModel, create_engine

from app.agents.payloads import TaskOrder
from app.agents.recon import recon_bundle
from app.agents.specialists import SpecialistRunner
from app.main import create_app
from app.models import Attachment, Challenge, Event
from app.services.storage import StorageService
from app.tooling.config import ToolSettings, ToolSpec
from app.tooling.doctor import inventory
from app.tooling.koth_mcp import (
    build_koth_mcp_server,
    koth_list_challenges_data,
    koth_list_tools_data,
    koth_recon_file_data,
    koth_search_arsenal_data,
    safe_sandbox_file,
)
from app.tooling.mcp_servers import (
    add_mcp_server,
    build_pydantic_toolsets,
    delete_mcp_server,
    koth_mcp_client_snippet,
    load_mcp_config,
    mcp_status_rows,
    toggle_mcp_server,
)
from app.tooling.runner import ToolRunner


def _make_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mcp.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    ev = Event(name="KOTH-Test")
    session.add(ev)
    session.commit()
    session.refresh(ev)
    ch = Challenge(event_id=ev.id, title="BinChal", category="reversing", target=None)
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return engine, session, ev, ch


def test_doctor_inventory_docker_batch(monkeypatch):
    cfg = ToolSettings(
        tools={
            "ffuf": ToolSpec(mode="docker", guard="strict"),
            "radare2": ToolSpec(mode="docker", guard="strict"),
            "missingtool": ToolSpec(mode="docker", guard="strict"),
        }
    )
    cfg.defaults.docker_image = "koth-sandbox:latest"
    monkeypatch.setattr("app.tooling.doctor.shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        "app.tooling.doctor.docker_status",
        lambda img: {"daemon_running": True, "image_present": True, "image": img},
    )
    monkeypatch.setattr(
        "app.tooling.doctor.subprocess.run",
        lambda cmd, **kw: SimpleNamespace(
            returncode=0,
            stdout="ffuf=/usr/bin/ffuf\nradare2=/usr/bin/radare2\nmissingtool=\n",
            stderr="",
        ),
    )
    rows = {r["name"]: r for r in inventory(cfg)}
    assert rows["ffuf"]["present"] is True
    assert rows["ffuf"]["path"] == "docker:koth-sandbox:latest:/usr/bin/ffuf"
    assert rows["radare2"]["present"] is True
    assert rows["missingtool"]["present"] is False


def test_recon_bundle_passes_staged_filename_for_docker_compat(tmp_path):
    engine, session, _ev, ch = _make_env(tmp_path)
    storage = StorageService(tmp_path / "storage")
    path = storage.save(ch.id, "crackme.bin", b"\x7fELFhello")
    att = Attachment(
        challenge_id=ch.id,
        name="crackme.bin",
        path=str(path.relative_to(storage.storage_dir)),
        mime="application/octet-stream",
        size=9,
        type="download",
    )
    session.add(att)
    session.commit()

    seen_args: list[list[str]] = []

    class FakeRunner:
        def __init__(self):
            self.storage = storage
            self.real = ToolRunner(ToolSettings(), storage)

        def stage_downloads(self, s, c):
            return self.real.stage_downloads(s, c)

        def run_raw(self, c, tool, args, **kw):
            seen_args.append([tool, *args])
            return SimpleNamespace(stdout="ELF_MARKER\n", stderr="", returncode=0, truncated=False)

    digest = recon_bundle(FakeRunner(), session, ch, storage=storage)
    assert seen_args == [["strings", "crackme.bin"]]
    assert "ELF_MARKER" in digest.summary
    session.close()


def test_mcp_servers_crud_and_toolsets(tmp_path, monkeypatch):
    mcp_file = tmp_path / "mcp.yaml"
    assert load_mcp_config(mcp_file) == {}

    spec = add_mcp_server(
        "playwright",
        label="Playwright MCP",
        category="web",
        transport="stdio",
        command="python",
        args=["-m", "fake_pw"],
        description="Browser automation",
        enabled=False,
        path=mcp_file,
    )
    assert spec.name == "playwright"
    assert spec.enabled is False

    add_mcp_server(
        "ghidra",
        label="Ghidra SSE",
        category="reversing",
        transport="sse",
        url="http://127.0.0.1:8080/sse",
        enabled=True,
        path=mcp_file,
    )

    assert toggle_mcp_server("playwright", True, path=mcp_file) is True
    rows = {r["name"]: r for r in mcp_status_rows(mcp_file)}
    assert rows["playwright"]["enabled"] is True
    assert rows["playwright"]["ready"] is True
    assert rows["ghidra"]["enabled"] is True
    assert rows["ghidra"]["ready"] is True

    # Web specialist receives only web/all MCPs, not reversing-only ghidra
    monkeypatch.setattr("app.tooling.mcp_servers.shutil.which", lambda cmd: "/usr/bin/python")
    web_sets = build_pydantic_toolsets(category="web", path=mcp_file)
    assert len(web_sets) == 1
    rev_sets = build_pydantic_toolsets(category="reversing", path=mcp_file)
    assert len(rev_sets) == 1

    assert delete_mcp_server("playwright", path=mcp_file) is True
    assert "playwright" not in load_mcp_config(mcp_file)
    assert "koth-ctf" in koth_mcp_client_snippet(tmp_path)


def test_probe_mcp_server_unwraps_prefixed_toolset(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.tooling.mcp_servers import probe_mcp_server

    mcp_file = tmp_path / "mcp.yaml"
    add_mcp_server(
        "ghidra",
        label="PyGhidra",
        category="reversing",
        transport="http",
        url="http://127.0.0.1:8080/mcp",
        enabled=True,
        path=mcp_file,
    )

    class FakeInner:
        async def list_tools(self):
            return [SimpleNamespace(name="decompile_function"), SimpleNamespace(name="import_binary")]

    class FakePrefixed:
        def __init__(self):
            self.wrapped = FakeInner()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("app.tooling.mcp_servers._make_toolset", lambda spec: FakePrefixed())
    ok, msg = probe_mcp_server("ghidra", path=mcp_file)
    assert ok is True
    assert "decompile_function" in msg


def test_specialist_ai_helper_tools_and_koth_mcp(tmp_path):
    engine, session, _ev, ch = _make_env(tmp_path)
    storage = StorageService(tmp_path / "storage")
    runner = ToolRunner(ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")}), storage)

    # Reject path traversal in safe_sandbox_file
    with pytest.raises(ValueError):
        safe_sandbox_file(runner.sandbox_workdir(ch), "../escape.py")

    calls: list[dict] = []

    def fake_model(messages, info):
        step = len(calls)
        calls.append({"step": step, "tools": [t.name for t in info.function_tools]})
        if step == 0:
            return ModelResponse(parts=[
                ToolCallPart(
                    tool_name="run_python_script",
                    args=json.dumps({"filename": "solve.py", "code": "print('CTF{py_helper}')"}),
                )
            ])
        if step == 1:
            return ModelResponse(parts=[
                ToolCallPart(tool_name="list_sandbox_files", args="{}")
            ])
        if step == 2:
            return ModelResponse(parts=[
                ToolCallPart(
                    tool_name="read_sandbox_file",
                    args=json.dumps({"filename": "solve.py", "offset": 0, "max_bytes": 200}),
                )
            ])
        return ModelResponse(parts=[
            ToolCallPart(
                tool_name="final_result",
                args=json.dumps({
                    "status": "SOLVED",
                    "flag_candidate": "CTF{py_helper}",
                    "findings": ["executed python script"],
                    "tools_used": ["python"],
                }),
            )
        ])

    spec = SpecialistRunner(FunctionModel(fake_model), persona_text="spec", category="crypto")
    report = spec.run(session, ch, TaskOrder(category="crypto", task="solve", max_tool_runs=3), runner)
    assert report.status == "SOLVED"
    assert report.flag_candidate == "CTF{py_helper}"
    for name in ("run_tool", "run_python_script", "list_sandbox_files", "read_sandbox_file", "search_arsenal"):
        assert name in calls[0]["tools"]

    # KOTH FastMCP server data & tool registration
    srv = build_koth_mcp_server()
    assert srv.name == "koth-ctf"
    assert "tools" in koth_list_tools_data()
    assert koth_list_challenges_data(session)[0]["title"] == "BinChal"
    assert koth_recon_file_data(runner.sandbox_workdir(ch) / "solve.py")["exists"] is True
    assert koth_search_arsenal_data(session, "sql injection") is not None
    session.close()


def test_settings_mcp_routes(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ui_mcp.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)

    mcp_file = tmp_path / "mcp.yaml"
    mcp_file.write_text(
        "servers:\n  radare2:\n    label: Radare2 MCP\n    category: reversing\n"
        "    transport: stdio\n    command: python\n    args: []\n    enabled: false\n",
        encoding="utf-8",
    )
    import app.web.routes as web_routes

    monkeypatch.setattr(web_routes, "MCP_PATH", mcp_file)
    monkeypatch.setattr(
        "app.web.routes.probe_mcp_server",
        lambda name, path=None: (True, f"MCP OK ({name})"),
    )

    app = create_app(engine=engine, storage_dir=tmp_path / "storage")
    with TestClient(app) as c:
        r_set = c.get("/settings?tab=runtime")
        assert r_set.status_code == 200
        assert "Servidores MCP" in r_set.text
        assert "Radare2 MCP" in r_set.text

        r_tog = c.post("/settings/mcp/toggle", data={"name": "radare2"}, follow_redirects=False)
        assert r_tog.status_code == 303
        assert load_mcp_config(mcp_file)["radare2"].enabled is True

        r_add = c.post(
            "/settings/mcp/add",
            data={
                "name": "custom-http",
                "label": "Custom HTTP MCP",
                "category": "web",
                "transport": "http",
                "url": "http://127.0.0.1:9999/mcp",
            },
            follow_redirects=False,
        )
        assert r_add.status_code == 303
        assert "custom-http" in load_mcp_config(mcp_file)

        r_test = c.post("/settings/mcp/test", data={"name": "radare2"})
        assert r_test.status_code == 200
        assert "MCP OK (radare2)" in r_test.text

        r_del = c.post("/settings/mcp/delete", data={"name": "custom-http"}, follow_redirects=False)
        assert r_del.status_code == 303
        assert "custom-http" not in load_mcp_config(mcp_file)
