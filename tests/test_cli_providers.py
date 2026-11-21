import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.agents.cli_providers import build_cli_command, check_cli_provider_connection, make_cli_model
from app.agents.config import ModelRef, ModelsConfig, load_models
from app.agents.jury import JuryAgent
from app.agents.models import ProviderUnavailable, resolve_model
from app.agents.payloads import TaskOrder
from app.agents.specialists import SpecialistRunner
from app.main import create_app
from app.models import Challenge, Event
from app.services.storage import StorageService
from app.tooling.config import load_tools_config
from app.tooling.runner import ToolRunner


def test_build_cli_commands_portable():
    assert build_cli_command("cli_claude", "claude", "sonnet") == [
        "claude", "-p", "--output-format", "text", "--no-session-persistence", "--tools", "", "--model", "sonnet",
    ]
    cmd_codex = build_cli_command("cli_codex", "codex", "default")
    assert cmd_codex[:4] == ["codex", "exec", "--skip-git-repo-check", "-s"]
    assert "-m" not in cmd_codex
    assert build_cli_command("cli_gemini", "gemini", "gemini-2.5-pro") == [
        "gemini", "-p", " ", "-m", "gemini-2.5-pro",
    ]
    assert build_cli_command("cli_agy", "agy", "gemini-4-neon-high") == [
        "agy", "--input-format", "stream-json", "--output-format", "stream-json", "--disable-slash-commands", "--model", "gemini-4-neon-high",
    ]


def test_missing_cli_raises_provider_unavailable(monkeypatch):
    monkeypatch.setattr("app.agents.cli_providers.shutil.which", lambda name: None)
    with pytest.raises(ProviderUnavailable):
        resolve_model(ModelRef(provider="cli_claude", name="sonnet"), ModelsConfig())

    ok, msg = check_cli_provider_connection("cli_codex")
    assert ok is False
    assert "PATH" in msg


def test_jury_and_specialist_work_over_cli_models(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'cp.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr("app.agents.cli_providers.shutil.which", lambda name: f"/usr/bin/{name}")

    # 1. Jury over Claude CLI returning markdown-fenced JSON
    def fake_claude_run(cmd, input, text, encoding, errors, capture_output, timeout, creationflags):
        payload = {
            "action": "DISPATCH",
            "order": {"category": "crypto", "task": "decode", "hints": [], "max_tool_runs": 3},
            "reason": "start crypto",
            "findings": ["initial"],
        }
        return SimpleNamespace(returncode=0, stdout=f"```json\n{json.dumps(payload)}\n```", stderr="")

    monkeypatch.setattr("app.agents.cli_providers.subprocess.run", fake_claude_run)
    with Session(engine) as session:
        ev = Event(name="ctf")
        session.add(ev)
        session.commit()
        session.refresh(ev)
        ch = Challenge(event_id=ev.id, title="c1")
        session.add(ch)
        session.commit()
        session.refresh(ch)

        jury = JuryAgent(resolve_model(ModelRef(provider="cli_claude", name="sonnet"), ModelsConfig()), "jury")
        dec = jury.decide(session, ch, [])
        assert dec.action == "DISPATCH"
        assert dec.order.category == "crypto"

        # 2. Specialist over Codex CLI calling run_tool then returning final report
        calls = {"n": 0}

        def fake_codex_run(cmd, input, text, encoding, errors, capture_output, timeout, creationflags):
            calls["n"] += 1
            out_idx = cmd.index("-o") + 1
            out_file = cmd[out_idx]
            if calls["n"] == 1:
                body = json.dumps({"tool": "python", "args": ["-c", "print('ok')"]})
            else:
                body = json.dumps({"output": {"status": "SOLVED", "flag_candidate": "CTF{cli}", "findings": ["ok"]}})
            with open(out_file, "w", encoding="utf-8") as fh:
                fh.write(body)
            return SimpleNamespace(returncode=0, stdout="codex banner", stderr="")

        monkeypatch.setattr("app.agents.cli_providers.subprocess.run", fake_codex_run)
        runner = ToolRunner(load_tools_config(), StorageService(tmp_path / "storage"))
        spec = SpecialistRunner(make_cli_model("cli_codex", "default"), "spec")
        rep = spec.run(session, ch, TaskOrder(category="crypto", task="decode"), runner=runner)
        assert rep.status == "SOLVED"
        assert rep.flag_candidate == "CTF{cli}"


def test_settings_cli_providers_activate_and_test(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'cs.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    tmp_models = tmp_path / "models.yaml"
    tmp_models.write_text(
        "providers: {}\nroles:\n  strong:\n    provider: nvidia\n    name: m1\n  fast:\n    provider: nvidia\n    name: m2\n",
        encoding="utf-8",
    )

    import app.web.routes as web_routes
    monkeypatch.setattr(web_routes, "MODELS_PATH", tmp_models)
    monkeypatch.setattr("app.agents.cli_providers.shutil.which", lambda name: f"/bin/{name}" if name == "gemini" else None)

    def fake_gemini_run(cmd, input, text, encoding, errors, capture_output, timeout, creationflags):
        return SimpleNamespace(returncode=0, stdout="PONG", stderr="")

    monkeypatch.setattr("app.agents.cli_providers.subprocess.run", fake_gemini_run)

    app = create_app(engine=engine, storage_dir=tmp_path / "storage")
    with TestClient(app) as c:
        html = c.get("/settings").text
        assert "Provedores por Assinatura (CLI Oficial)" in html
        assert "instalado (gemini)" in html
        assert "não encontrado no PATH" in html

        r_test = c.post("/settings/cli-providers/cli_gemini/test")
        assert r_test.status_code == 200
        assert "autenticado e respondendo" in r_test.text

        # Activate strong only (keeps fast as nvidia/m2)
        r_act = c.post("/settings/cli-providers/cli_claude/activate", data={"scope": "strong"}, follow_redirects=False)
        assert r_act.status_code == 303
        cfg = load_models(tmp_models)
        assert cfg.roles["strong"].provider == "cli_claude"
        assert cfg.roles["fast"].provider == "nvidia"

        # Activate all
        c.post("/settings/cli-providers/cli_gemini/activate", data={"scope": "all"}, follow_redirects=False)
        cfg2 = load_models(tmp_models)
        assert cfg2.roles["strong"].provider == "cli_gemini"
        assert cfg2.roles["fast"].provider == "cli_gemini"


def test_agy_cli_stream_json_jury_and_specialist(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'agy.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr("app.agents.cli_providers.shutil.which", lambda name: "/usr/bin/agy" if name == "agy" else None)

    calls = {"n": 0}

    def fake_agy_run(cmd, input, text, encoding, errors, capture_output, timeout, creationflags):
        calls["n"] += 1
        sent = json.loads(input.strip())
        assert sent["event"] == "user"
        assert "content" in sent["message"]
        if calls["n"] == 1:
            resp_obj = {
                "action": "DISPATCH",
                "order": {"category": "crypto", "task": "solve", "hints": [], "max_tool_runs": 2},
                "reason": "agy ok",
                "findings": ["init"],
            }
        else:
            resp_obj = {"output": {"status": "SOLVED", "flag_candidate": "CTF{agy_ok}", "findings": ["cracked"]}}
        ndjson = "\n".join([
            json.dumps({"event": "init", "conversation_id": "c1"}),
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": json.dumps(resp_obj)}}),
        ])
        return SimpleNamespace(returncode=0, stdout=ndjson, stderr="")

    monkeypatch.setattr("app.agents.cli_providers.subprocess.run", fake_agy_run)
    with Session(engine) as session:
        ev = Event(name="ctf")
        session.add(ev)
        session.commit()
        session.refresh(ev)
        ch = Challenge(event_id=ev.id, title="c1")
        session.add(ch)
        session.commit()
        session.refresh(ch)

        jury = JuryAgent(resolve_model(ModelRef(provider="cli_agy", name="gemini-4-neon-high"), ModelsConfig()), "jury")
        dec = jury.decide(session, ch, [])
        assert dec.action == "DISPATCH"

        runner = ToolRunner(load_tools_config(), StorageService(tmp_path / "storage"))
        spec = SpecialistRunner(resolve_model(ModelRef(provider="cli_agy", name="gemini-3.8-flash-medium"), ModelsConfig()), "spec")
        rep = spec.run(session, ch, TaskOrder(category="crypto", task="solve"), runner=runner)
        assert rep.status == "SOLVED"
        assert rep.flag_candidate == "CTF{agy_ok}"
