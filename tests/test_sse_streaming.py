import asyncio
import json
import sys

from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

import app.agents.api as agent_api
from app.agents.events import SessionEventBus
from app.agents.jury import JuryAgent
from app.agents.orchestrator import Orchestrator
from app.database import configure_sqlite_engine
from app.main import create_app
from app.models import Challenge, Event
from app.services.storage import StorageService
from app.tooling.config import ToolSettings, ToolSpec
from app.tooling.runner import ToolRunner


def test_event_bus_pub_sub_replay_and_unsubscribe():
    bus = SessionEventBus(max_history=10)
    ev1 = bus.publish("s1", "live_event", {"kind": "recon", "detail": "step 1"})
    ev2 = bus.publish("s1", "stdout_chunk", {"stream": "stdout", "text": "line 1"})
    assert ev2["id"] == ev1["id"] + 1

    async def _run():
        loop = asyncio.get_running_loop()
        replay, q, unsub = bus.subscribe("s1", loop, last_event_id=ev1["id"])
        assert len(replay) == 1
        assert replay[0]["id"] == ev2["id"]
        bus.publish("s1", "state_change", {"state": "solved"})
        live = await asyncio.wait_for(q.get(), timeout=1.0)
        assert live["type"] == "state_change"
        assert bus.subscriber_count("s1") == 1
        unsub()
        assert bus.subscriber_count("s1") == 0

    asyncio.run(_run())


def test_tool_runner_streams_stdout_chunks_and_detects_flag(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'sse_runner.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    storage = StorageService(tmp_path / "storage")

    with Session(engine) as session:
        ev = Event(name="SSE-Test", flag_format=r"CTF\{[^}]+\}")
        session.add(ev)
        session.commit()
        session.refresh(ev)
        ch = Challenge(event_id=ev.id, title="Streamer", category="misc", points=100)
        session.add(ch)
        session.commit()
        session.refresh(ch)

        runner = ToolRunner(
            ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")}),
            storage,
        )
        chunks: list[tuple[str, str]] = []
        res = runner.run(
            session,
            ch,
            "python",
            ["-c", "import sys; print('chunk-one', flush=True); print('CTF{stream_ok}', flush=True)"],
            on_output_chunk=lambda stream, txt: chunks.append((stream, txt.strip())),
        )
        assert res.returncode == 0
        assert any("chunk-one" in txt for _s, txt in chunks)
        assert "CTF{stream_ok}" in res.detected_flags


def test_sse_stream_endpoint_delivers_typed_events(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'sse_web.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    storage = StorageService(tmp_path / "storage")

    with Session(engine) as session:
        ev = Event(name="SSE-Web")
        session.add(ev)
        session.commit()
        session.refresh(ev)
        ch = Challenge(event_id=ev.id, title="LiveSSE", category="web", points=100)
        session.add(ch)
        session.commit()
        session.refresh(ch)
        cid = ch.id

    orch = Orchestrator(
        runner=ToolRunner(ToolSettings(), storage),
        jury=JuryAgent(None, "jury") if False else None,  # not invoked in stream test
        specialists={},
    )
    from app.agents.orchestrator import AgentSession
    sess = AgentSession(id="sse_sess_1", challenge_id=cid, state="running", mode="auto")
    orch._active_workers[sess.id] = cid
    orch.sessions[sess.id] = sess

    monkeypatch.setattr(agent_api, "get_orchestrator", lambda req: orch)
    monkeypatch.setattr("app.web.agent_routes.get_orchestrator", lambda req: orch)

    # Publish 2 events before connecting with Last-Event-ID: 0 so stream replays deterministically and closes via max_events=2
    orch._log_live(sess, "tool", "nmap -p 80 target")
    orch._emit_stdout(sess, "stdout", "80/tcp open http")

    app = create_app(engine=engine, storage_dir=tmp_path / "storage")
    with TestClient(app) as client:
        with client.stream(
            "GET",
            f"/challenges/{cid}/agent/stream?session_id={sess.id}&max_events=2",
            headers={"Last-Event-ID": "0"},
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
            body = "".join(resp.iter_text())
        assert "event: live_event" in body
        assert "event: stdout_chunk" in body
        assert "nmap -p 80 target" in body
        assert "80/tcp open http" in body
