import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models import Attempt, AttemptResult, Attachment, Challenge, Event
from app.services import tracking
from app.services.storage import StorageService
from app.tooling.config import ToolSettings, ToolSpec
from app.tooling.runner import ToolRunner


@pytest.fixture
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        event = Event(name="e")
        session.add(event)
        session.commit()
        session.refresh(event)
        ch = Challenge(event_id=event.id, title="c")
        session.add(ch)
        session.commit()
        session.refresh(ch)
        yield session, ch


def test_run_persists_attempt_and_attachment(tmp_path, env):
    session, ch = env
    cfg = ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")})
    runner = ToolRunner(cfg, StorageService(tmp_path / "storage"))
    res = runner.run(session, ch, "python", ["-c", "print('tracked')"])
    assert res.attachment_id is not None and res.attempt_id is not None

    att = session.get(Attachment, res.attachment_id)
    assert att.type.value == "output"
    assert att.name == "python_output.txt"

    attempt = session.get(Attempt, res.attempt_id)
    assert attempt.type.value == "tool"
    assert "tracked" in attempt.learning

    summary = tracking.challenge_summary(session, ch)
    assert summary["attempt_count"] == 1


def test_run_persists_stderr_in_attempt_learning(tmp_path, env):
    session, ch = env
    cfg = ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")})
    runner = ToolRunner(cfg, StorageService(tmp_path / "storage"))
    res = runner.run(session, ch, "python", ["-c", "import sys; sys.stderr.write('boom_err\\n'); sys.exit(2)"])
    assert res.returncode == 2
    attempt = session.get(Attempt, res.attempt_id)
    assert "[stderr] boom_err" in attempt.learning
