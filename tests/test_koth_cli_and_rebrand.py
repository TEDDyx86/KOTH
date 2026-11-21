import io
import json
import re
import time

from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlmodel import Session, SQLModel, create_engine

from app.agents.cli_op import OpCliAction, process_cli_turn
from app.agents.jury import JuryAgent
from app.agents.orchestrator import Orchestrator
from app.agents.specialists import SpecialistRunner
from app.main import create_app
from app.models import Challenge, Event
from app.services.storage import StorageService
from app.tooling.config import load_tools_config
from app.tooling.runner import ToolRunner


def _scripted(payloads: list[dict]):
    state = {"n": 0}

    def fn(messages, info):
        idx = state["n"]
        state["n"] += 1
        blob = "\n".join(str(p) for m in messages for p in (m.parts or []))
        match = re.search(r"['\"](\w*result\w*)['\"]", blob)
        name = match.group(1) if match else "final_result"
        payload = payloads[min(idx, len(payloads) - 1)]
        return ModelResponse(parts=[ToolCallPart(tool_name=name, args=json.dumps(payload))])

    return FunctionModel(fn)


def test_koth_cli_home_dashboard_and_settings(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'koth.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    storage = StorageService(tmp_path / "storage")

    import app.agents.api as agent_api
    import app.web.routes as web_routes

    def _fake_orch(storage_dir):
        runner = ToolRunner(load_tools_config(), storage)
        jury = JuryAgent(_scripted([
            {"action": "DISPATCH", "reason": "go",
             "order": {"category": "crypto", "task": "solve"}, "findings": []},
            {"action": "SUBMIT", "reason": "ok",
             "flag_candidate": "CTF{koth_summit}", "findings": ["found"]},
        ]), persona_text="jury")
        spec = SpecialistRunner(_scripted([
            {"status": "SOLVED", "flag_candidate": "CTF{koth_summit}", "findings": ["done"]},
        ]), persona_text="spec")
        return Orchestrator(runner=runner, jury=jury, specialists={"crypto": spec})

    monkeypatch.setattr(agent_api, "_make_orchestrator", _fake_orch)

    # Isolate AGENTS_PATH for settings test
    tmp_agents = tmp_path / "agents.yaml"
    tmp_agents.write_text("roles:\n  op:\n    persona: personas/op.md\n    model: strong\nlimits:\n  max_attempts: 12\n  max_minutes: 60\n", encoding="utf-8")
    monkeypatch.setattr(web_routes, "AGENTS_PATH", tmp_agents)

    app = create_app(engine=engine, storage_dir=tmp_path / "storage")
    with TestClient(app) as c:
        r_home = c.get("/")
        assert r_home.status_code == 200
        assert "KOTH" in r_home.text
        assert "Terminal do Orquestrador" in r_home.text
        assert "koth@summit:~$" in r_home.text

        # Slash commands via CLI
        assert "Comandos disponíveis" in c.post("/cli/message", data={"message": "/help"}).text
        assert "[KOTH // STATUS]" in c.post("/cli/message", data={"message": "/status"}).text

        # Direct challenge launch from CLI with attachment in Full Auto
        r_solve = c.post(
            "/cli/message",
            data={
                "message": "/solve SummitCrypto | cat=crypto | pts=500 | crack rsa",
                "mode": "auto",
            },
            files=[("files", ("chal.txt", io.BytesIO(b"n=123"), "text/plain"))],
        )
        assert r_solve.status_code == 200
        assert "SummitCrypto" in r_solve.text

        # Wait briefly for background auto-loop to solve
        for _ in range(40):
            feed = c.get("/cli/feed").text
            if "CTF{koth_summit}" in feed:
                break
            time.sleep(0.05)
        assert "CTF{koth_summit}" in feed

        # /rank works after challenge exists
        assert "SummitCrypto" in c.post("/cli/message", data={"message": "/rank"}).text

        # Dashboard and Settings pages
        r_dash = c.get("/dashboard")
        assert r_dash.status_code == 200
        assert "KOTH Arena" in r_dash.text
        assert "1/1 resolvidos" in r_dash.text

        r_set = c.get("/settings")
        assert r_set.status_code == 200
        assert "Tool Doctor" in r_set.text
        r_lim = c.post("/settings/limits", data={"max_attempts": "20", "max_minutes": "90"}, follow_redirects=False)
        assert r_lim.status_code == 303
        assert "20" in tmp_agents.read_text(encoding="utf-8")

        # /clear resets CLI history
        assert "Histórico limpo" in c.post("/cli/message", data={"message": "/clear"}).text


def test_cli_op_natural_language_launch(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cli.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    storage = StorageService(tmp_path / "storage")
    with Session(engine) as session:
        def fake_responder(sess, text, files, active_event, recent):
            return OpCliAction(
                action="LAUNCH_CHALLENGE",
                reply="[KOTH // OP] Desafio web detectado, subindo escalada.",
                title="SQLi Login",
                category="web",
                points=300,
                target="10.0.0.8:8080",
                description=text,
            )

        rec = process_cli_turn(
            session, storage, None,
            "Tem um login em 10.0.0.8:8080 vulnerável a sqli",
            op_responder=fake_responder,
        )
        assert rec.challenge_id is not None
        assert "SQLi Login" in rec.content
        ch = session.get(Challenge, rec.challenge_id)
        assert ch.category == "web"
        assert ch.target == "10.0.0.8:8080"
