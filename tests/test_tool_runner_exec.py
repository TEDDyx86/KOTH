import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models import Challenge, Event
from app.services.storage import StorageService
from app.tooling.config import load_tools_config
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
        ch = Challenge(event_id=event.id, title="c", target=None)
        session.add(ch)
        session.commit()
        session.refresh(ch)
        yield session, ch


def _runner(tmp_path):
    return ToolRunner(load_tools_config(), StorageService(tmp_path / "storage"))


def test_run_python_captures_output(tmp_path, env):
    session, ch = env
    res = _runner(tmp_path).run_raw(ch, "python", ["-c", "print('hello-ctf')"])
    assert res.returncode == 0
    assert "hello-ctf" in res.stdout
    assert res.truncated is False


def test_run_timeout(tmp_path, env):
    session, ch = env
    res = _runner(tmp_path).run_raw(ch, "python", ["-c", "import time; time.sleep(10)"], timeout_s=2)
    assert res.returncode is None
    assert res.duration_s >= 2


def test_run_truncates_output(tmp_path, env):
    session, ch = env
    res = _runner(tmp_path).run_raw(ch, "python", ["-c", "print('x' * 100000)"], output_limit_mb=0)
    assert len(res.stdout) < 100000
    assert res.truncated is True


def test_workdir_created_under_sandbox(tmp_path, env):
    session, ch = env
    _runner(tmp_path).run_raw(ch, "python", ["-c", "print(1)"])
    assert (tmp_path / "storage" / "sandbox" / str(ch.event_id) / str(ch.id)).exists()
