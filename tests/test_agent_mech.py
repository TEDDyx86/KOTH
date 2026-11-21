import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.agents.planner import planner_rank, stop_criteria
from app.agents.recon import recon_bundle
from app.models import Attachment, Challenge, Event
from app.services import tracking
from app.services.storage import StorageService
from app.tooling.config import load_tools_config
from app.tooling.runner import ToolRunner


@pytest.fixture
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        event = Event(name="ctf")
        session.add(event)
        session.commit()
        session.refresh(event)
        yield session, event


def _ch(session, event, title, points, difficulty, status="open"):
    from app.models import ChallengeStatus, Difficulty

    ch = Challenge(event_id=event.id, title=title, points=points,
                   difficulty=Difficulty(difficulty),
                   status=ChallengeStatus(status))
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return ch


def test_planner_rank_prioritizes_points_and_difficulty(env):
    session, event = env
    easy_low = _ch(session, event, "a", 50, "easy")
    hard_high = _ch(session, event, "b", 500, "hard")
    ranked = planner_rank(session, event)
    titles = [c.title for c, _ in ranked]
    assert titles[0] == "b" and titles[1] == "a"
    scores = [s for _, s in ranked]
    assert scores[0] > scores[1]


def test_stop_criteria_defaults(env):
    session, event = env
    crit = stop_criteria({})
    assert crit.max_attempts == 12 and crit.max_minutes == 60
    crit2 = stop_criteria({"max_attempts": 3})
    assert crit2.max_attempts == 3 and crit2.max_minutes == 60


def test_recon_bundle_strings_attachment(env, tmp_path):
    session, event = env
    ch = _ch(session, event, "c", 100, "easy")
    storage = StorageService(tmp_path / "storage")
    path = storage.save(ch.id, "notes.txt", b"flag_hint_here_xyz\n")
    att = Attachment(challenge_id=ch.id, name="notes.txt",
                     path=str(path.relative_to(storage.storage_dir)),
                     mime="text/plain", size=18, type="download")
    session.add(att)
    session.commit()

    from app.tooling.config import ToolSettings, ToolSpec

    cfg = ToolSettings(tools={
        "strings": ToolSpec(mode="native", guard="strict"),
        "nmap": ToolSpec(mode="native", guard="strict"),
    })
    runner = ToolRunner(cfg, storage)
    digest = recon_bundle(runner, session, ch, storage=storage)

    assert "flag_hint_here_xyz" in digest.summary
    from app.services import tracking

    summary = tracking.challenge_summary(session, ch)
    assert summary["attempt_count"] >= 1  # recon attempts recorded


def test_recon_bundle_skips_nmap_without_target(env, tmp_path):
    session, event = env
    ch = _ch(session, event, "d", 100, "easy")
    runner = ToolRunner(load_tools_config(), StorageService(tmp_path / "storage"))
    digest = recon_bundle(runner, session, ch,
                          storage=StorageService(tmp_path / "storage"))
    assert "nmap" not in (digest.summary or "").lower() or "no target" in digest.summary.lower()
