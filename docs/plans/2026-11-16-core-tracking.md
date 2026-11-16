# Core Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the tracking core of the CTF Orchestrator — events, challenges, attempts, versioned notes, and attachments — with a REST API and an HTMX web UI in the black/red theme.

**Architecture:** Monolith FastAPI in a single process: services layer (tracking + storage) consumed by both REST routers and page routes; SQLModel/SQLite persistence; Jinja2 + HTMX frontend with CSS custom-property theme tokens. See `docs/specs/2026-11-16-ctf-orchestrator-design.md` (sections 4, 5, 9).

**Tech Stack:** Python 3.12+, FastAPI, SQLModel (SQLite), Jinja2, HTMX (CDN), pytest + httpx TestClient.

## Global Constraints

- Python >= 3.12 (`requires-python = ">=3.12"` in pyproject)
- Persistence: SQLite via SQLModel. Notes are append-only (INSERT per save, never UPDATE).
- Derived metrics (tempo_ativo, attempt count) are computed at query time, never stored.
- Categories enum: `pwn, web, crypto, reverse, forensics, osint, misc, ai-ml, custom` (stored as lowercase str).
- Theme tokens in CSS custom properties, never hardcoded colors in templates: bg `#0a0a0a`→`#1a0d0d`, accent `#e01b24`, resolved flag green reserved for solve confirmation.
- Tool target restriction, ToolRunner, agent runtime, CTFd import: OUT OF SCOPE for this plan.
- Commit messages: conventional commits (`feat:`, `test:`, `docs:`, `chore:`).
- All tests run with `python -m pytest tests/ -v` from `C:\Users\Administrator\ctf-orchestrator`.

---

### Task 1: Project scaffold + app factory + health endpoint

**Files:**
- Create: `pyproject.toml`
- Create: `app/__init__.py` (empty)
- Create: `app/config.py`
- Create: `app/database.py`
- Create: `app/main.py`
- Create: `tests/__init__.py` (empty)
- Create: `tests/conftest.py`
- Create: `tests/test_health.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces: `create_app(engine: Engine | None = None, storage_dir: Path | None = None) -> FastAPI`; `app.config.settings` (db_path, storage_dir, flag_format); `app.database.default_engine() -> Engine`; test fixture `client` in `tests/conftest.py` used by every later API task.

- [ ] **Step 1: Write pyproject.toml**

```toml
[project]
name = "ctf-orchestrator"
version = "0.1.0"
description = "CTF orchestrator: tracking core, tool sandbox, agent runtime"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "sqlmodel>=0.0.22",
  "jinja2>=3.1",
  "python-multipart>=0.0.9",
  "pydantic>=2.7",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "httpx>=0.27"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["app*"]
```

- [ ] **Step 2: Install dependencies**

Run: `python -m pip install -e ".[dev]"`
Expected: `Successfully installed ...` (or already satisfied).

- [ ] **Step 3: Write app/config.py**

```python
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings:
    def __init__(self) -> None:
        self.db_path = Path(os.environ.get("CTF_DB_PATH", str(BASE_DIR / "ctf.db")))
        self.storage_dir = Path(os.environ.get("CTF_STORAGE_DIR", str(BASE_DIR / "storage")))
        self.flag_format = os.environ.get("CTF_FLAG_FORMAT", "flag{...}")


settings = Settings()
```

- [ ] **Step 4: Write app/database.py**

```python
from pathlib import Path

from sqlmodel import Session, create_engine
from sqlmodel import SQLModel

from .config import settings


def default_engine():
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{settings.db_path}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    return engine


def get_session():
    engine = default_engine()
    with Session(engine) as session:
        yield session
```

- [ ] **Step 5: Write app/main.py (health only for now)**

```python
from pathlib import Path

from fastapi import FastAPI

from .database import default_engine, get_session


def create_app(engine=None, storage_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="CTF Orchestrator")

    if engine is not None or storage_dir is not None:
        import sqlmodel

        test_engine = engine or default_engine()
        sqlmodel.SQLModel.metadata.create_all(test_engine)

        def session_override():
            from sqlmodel import Session
            with Session(test_engine) as session:
                yield session

        app.dependency_overrides[get_session] = session_override

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()
```

- [ ] **Step 6: Write tests/conftest.py**

```python
import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

from app.main import create_app


@pytest.fixture
def client(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    app = create_app(engine=engine, storage_dir=tmp_path / "storage")
    with TestClient(app) as c:
        yield c
```

- [ ] **Step 7: Write failing test tests/test_health.py**

```python
def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 8: Run test to verify it passes**

Run: `python -m pytest tests/test_health.py -v`
Expected: `1 passed`.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml app tests
git commit -m "feat: scaffold FastAPI app with health endpoint and test fixtures"
```

---

### Task 2: Data models (Event, Challenge, Attempt, Note, Attachment)

**Files:**
- Create: `app/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: SQLModel.
- Produces (used by every later task — exact names):
  - `Event`, `EventStatus` (`upcoming|active|finished`)
  - `Challenge`, `ChallengeStatus` (`open|solved|gave_up`), `Difficulty` (`easy|medium|hard|insane`)
  - `Attempt`, `AttemptType` (`manual|tool|agent`), `AttemptResult` (`failed|partial|breakthrough|solved`)
  - `Note`, `Attachment`, `AttachmentType` (`screenshot|download|output`)
  - `utcnow() -> datetime`
  - `Challenge.tags: str` comma-separated; `Challenge.target: str | None` (future ToolRunner contract); `Challenge.first_interaction / solved_at: datetime | None`.

- [ ] **Step 1: Write failing test tests/test_models.py**

```python
from sqlmodel import Session, select

from app.models import (
    Attempt, AttemptResult, AttemptType, Attachment, AttachmentType,
    Challenge, ChallengeStatus, Event, EventStatus, Note,
)


def test_event_roundtrip(tmp_path):
    from sqlmodel import SQLModel, create_engine
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        event = Event(name="picoCTF 2027", status=EventStatus.active)
        session.add(event)
        session.commit()
        session.refresh(event)
        rows = session.exec(select(Event)).all()
        assert len(rows) == 1
        assert rows[0].status is EventStatus.active


def test_challenge_defaults_and_fields(tmp_path):
    from sqlmodel import SQLModel, create_engine
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        event = Event(name="e")
        session.add(event)
        session.commit()
        session.refresh(event)
        ch = Challenge(event_id=event.id, title="_rsa_revenge", category="crypto", points=500,
                       difficulty="hard", target="chal.example.com:1337", tags="rsa,lattice")
        session.add(ch)
        session.commit()
        session.refresh(ch)
        assert ch.status is ChallengeStatus.open
        assert ch.first_interaction is None
        assert ch.solved_at is None


def test_attempt_note_attachment_rows(tmp_path):
    from sqlmodel import SQLModel, create_engine
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        event = Event(name="e")
        session.add(event)
        session.commit()
        session.refresh(event)
        ch = Challenge(event_id=event.id, title="t")
        session.add(ch)
        session.commit()
        session.refresh(ch)
        session.add(Attempt(challenge_id=ch.id, type=AttemptType.manual, description="tried ROP",
                            result=AttemptResult.failed, learning="no gadgets at 0x40"))
        session.add(Note(challenge_id=ch.id, content="# v1"))
        session.add(Note(challenge_id=ch.id, content="# v2"))
        session.add(Attachment(challenge_id=ch.id, name="shot.png", path="1/shot.png",
                               mime="image/png", size=10, type=AttachmentType.screenshot))
        session.commit()
        assert len(session.exec(select(Attempt)).all()) == 1
        assert len(session.exec(select(Note)).all()) == 2
        assert len(session.exec(select(Attachment)).all()) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models'`.

- [ ] **Step 3: Write app/models.py**

```python
from datetime import datetime
from enum import Enum

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now()


class EventStatus(str, Enum):
    upcoming = "upcoming"
    active = "active"
    finished = "finished"


class ChallengeStatus(str, Enum):
    open = "open"
    solved = "solved"
    gave_up = "gave_up"


class Difficulty(str, Enum):
    easy = "easy"
    medium = "medium"
    hard = "hard"
    insane = "insane"


class AttemptType(str, Enum):
    manual = "manual"
    tool = "tool"
    agent = "agent"


class AttemptResult(str, Enum):
    failed = "failed"
    partial = "partial"
    breakthrough = "breakthrough"
    solved = "solved"


class AttachmentType(str, Enum):
    screenshot = "screenshot"
    download = "download"
    output = "output"


class Event(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    url: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    status: EventStatus = EventStatus.upcoming
    created_at: datetime = Field(default_factory=utcnow)


class Challenge(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="event.id", index=True)
    title: str
    category: str = "misc"
    points: int = 0
    difficulty: Difficulty | None = None
    status: ChallengeStatus = ChallengeStatus.open
    flag: str | None = None
    flag_format: str | None = None
    description: str = ""
    target: str | None = None
    tags: str = ""  # comma-separated tags
    created_at: datetime = Field(default_factory=utcnow)
    first_interaction: datetime | None = None
    solved_at: datetime | None = None


class Attempt(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    challenge_id: int = Field(foreign_key="challenge.id", index=True)
    timestamp: datetime = Field(default_factory=utcnow)
    type: AttemptType
    description: str
    tool_command: str | None = None
    result: AttemptResult
    learning: str = ""


class Note(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    challenge_id: int = Field(foreign_key="challenge.id", index=True)
    timestamp: datetime = Field(default_factory=utcnow)
    content: str


class Attachment(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    challenge_id: int = Field(foreign_key="challenge.id", index=True)
    name: str
    path: str
    mime: str = "application/octet-stream"
    size: int = 0
    type: AttachmentType
    created_at: datetime = Field(default_factory=utcnow)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_models.py -v`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add app/models.py tests/test_models.py
git commit -m "feat: core data models (event, challenge, attempt, note, attachment)"
```

---

### Task 3: TrackingService — events and challenges

**Files:**
- Create: `app/services/__init__.py` (empty)
- Create: `app/services/tracking.py`
- Test: `tests/test_tracking_events.py`

**Interfaces:**
- Consumes: `app.models` (Task 2).
- Produces (used by Tasks 4–8):
  - `create_event(session: Session, name: str, url: str | None = None, starts_at=None, ends_at=None) -> Event`
  - `list_events(session) -> list[Event]`
  - `set_event_status(session, event_id: int, status: EventStatus) -> Event`
  - `create_challenge(session, event_id: int, title: str, category: str = "misc", points: int = 0, difficulty: Difficulty | None = None, description: str = "", target: str | None = None, tags: str = "") -> Challenge`
  - `get_challenge(session, challenge_id: int) -> Challenge | None`
  - `list_challenges(session, event_id: int | None = None, category: str | None = None, status: ChallengeStatus | None = None, tag: str | None = None) -> list[Challenge]`
  - Raises `LookupError` when event_id/challenge_id does not exist.

- [ ] **Step 1: Write failing test tests/test_tracking_events.py**

```python
from sqlmodel import Session, SQLModel, create_engine

from app.models import ChallengeStatus, EventStatus
from app.services import tracking


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_create_and_list_events(session):
    tracking.create_event(session, "picoCTF")
    tracking.create_event(session, "HTB Business", url="https://ctf.htb.com")
    events = tracking.list_events(session)
    assert [e.name for e in events] == ["picoCTF", "HTB Business"]
    assert events[1].url == "https://ctf.htb.com"


def test_set_event_status(session):
    event = tracking.create_event(session, "picoCTF")
    updated = tracking.set_event_status(session, event.id, EventStatus.active)
    assert updated.status is EventStatus.active


def test_set_event_status_missing_raises(session):
    import pytest
    with pytest.raises(LookupError):
        tracking.set_event_status(session, 999, EventStatus.active)


def test_create_and_filter_challenges(session):
    event = tracking.create_event(session, "e")
    tracking.create_challenge(session, event.id, "web1", category="web", points=100, tags="ssti,xss")
    tracking.create_challenge(session, event.id, "pwn1", category="pwn", points=300, tags="rop")
    web_only = tracking.list_challenges(session, event_id=event.id, category="web")
    assert [c.title for c in web_only] == ["web1"]
    rop = tracking.list_challenges(session, tag="rop")
    assert [c.title for c in rop] == ["pwn1"]
    solved = tracking.list_challenges(session, status=ChallengeStatus.open)
    assert len(solved) == 2


def test_create_challenge_missing_event_raises(session):
    import pytest
    with pytest.raises(LookupError):
        tracking.create_challenge(session, 999, "x")


def test_tag_filter_matches_any_tag(session):
    event = tracking.create_event(session, "e")
    tracking.create_challenge(session, event.id, "a", tags="rop,heap")
    tracking.create_challenge(session, event.id, "b", tags="ssti")
    matches = tracking.list_challenges(session, tag="heap")
    assert [c.title for c in matches] == ["a"]
```

- [ ] **Step 2: Add missing import to the test file**

Add at the top of `tests/test_tracking_events.py`:

```python
import pytest
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_tracking_events.py -v`
Expected: FAIL with `ImportError: cannot import name 'tracking'`.

- [ ] **Step 4: Write app/services/tracking.py (events + challenges)**

```python
from sqlmodel import Session, select

from ..models import (
    Challenge, ChallengeStatus, Difficulty, Event, EventStatus,
)


def create_event(session: Session, name: str, url: str | None = None,
                 starts_at=None, ends_at=None) -> Event:
    event = Event(name=name, url=url, starts_at=starts_at, ends_at=ends_at)
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def list_events(session: Session) -> list[Event]:
    return list(session.exec(select(Event).order_by(Event.created_at)).all())


def set_event_status(session: Session, event_id: int, status: EventStatus) -> Event:
    event = session.get(Event, event_id)
    if event is None:
        raise LookupError(f"event {event_id} not found")
    event.status = status
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def create_challenge(session: Session, event_id: int, title: str, category: str = "misc",
                     points: int = 0, difficulty: Difficulty | None = None,
                     description: str = "", target: str | None = None,
                     tags: str = "") -> Challenge:
    if session.get(Event, event_id) is None:
        raise LookupError(f"event {event_id} not found")
    challenge = Challenge(event_id=event_id, title=title, category=category, points=points,
                          difficulty=difficulty, description=description, target=target, tags=tags)
    session.add(challenge)
    session.commit()
    session.refresh(challenge)
    return challenge


def get_challenge(session: Session, challenge_id: int) -> Challenge | None:
    return session.get(Challenge, challenge_id)


def list_challenges(session: Session, event_id: int | None = None,
                    category: str | None = None, status: ChallengeStatus | None = None,
                    tag: str | None = None) -> list[Challenge]:
    query = select(Challenge)
    if event_id is not None:
        query = query.where(Challenge.event_id == event_id)
    if category is not None:
        query = query.where(Challenge.category == category)
    if status is not None:
        query = query.where(Challenge.status == status)
    challenges = list(session.exec(query).all())
    if tag is not None:
        challenges = [c for c in challenges if tag in [t.strip() for t in c.tags.split(",") if t.strip()]]
    return challenges
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_tracking_events.py -v`
Expected: `6 passed`.

- [ ] **Step 6: Commit**

```bash
git add app/services tests/test_tracking_events.py
git commit -m "feat: tracking service for events and challenges with filters"
```

---

### Task 4: TrackingService — attempts, challenge updates, derived metrics

**Files:**
- Modify: `app/services/tracking.py` (append functions)
- Test: `tests/test_tracking_attempts.py`

**Interfaces:**
- Consumes: Task 3 functions.
- Produces:
  - `record_attempt(session, challenge_id: int, type: AttemptType, description: str, result: AttemptResult, tool_command: str | None = None, learning: str = "", flag: str | None = None) -> Attempt` — sets `first_interaction` if unset; when `result == solved` sets `status=solved`, `solved_at=now` and `flag` (if given).
  - `update_challenge(session, challenge_id: int, status: ChallengeStatus | None = None, flag: str | None = None) -> Challenge` — status `solved` also stamps `solved_at`.
  - `list_attempts(session, challenge_id: int) -> list[Attempt]` (chronological)
  - `challenge_summary(session, challenge: Challenge) -> dict` with keys `attempt_count: int`, `active_seconds: float | None` (= (solved_at or now) − first_interaction, None if no first_interaction).

- [ ] **Step 1: Write failing test tests/test_tracking_attempts.py**

```python
import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models import AttemptResult, AttemptType, ChallengeStatus
from app.services import tracking


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def challenge(session):
    event = tracking.create_event(session, "e")
    return tracking.create_challenge(session, event.id, "rop1", category="pwn")


def test_record_attempt_sets_first_interaction(session, challenge):
    assert challenge.first_interaction is None
    tracking.record_attempt(session, challenge.id, AttemptType.manual,
                            "tried ret2libc", AttemptResult.failed)
    refreshed = tracking.get_challenge(session, challenge.id)
    assert refreshed.first_interaction is not None


def test_solve_attempt_finalizes(session, challenge):
    tracking.record_attempt(session, challenge.id, AttemptType.agent,
                            "leaked canary then ROP", AttemptResult.solved,
                            flag="flag{rop_w1n}")
    refreshed = tracking.get_challenge(session, challenge.id)
    assert refreshed.status is ChallengeStatus.solved
    assert refreshed.solved_at is not None
    assert refreshed.flag == "flag{rop_w1n}"


def test_second_attempt_does_not_reset_solved_at(session, challenge):
    tracking.record_attempt(session, challenge.id, AttemptType.manual, "x", AttemptResult.failed)
    tracking.record_attempt(session, challenge.id, AttemptType.manual, "y", AttemptResult.solved, flag="flag{a}")
    first_solved_at = tracking.get_challenge(session, challenge.id).solved_at
    tracking.record_attempt(session, challenge.id, AttemptType.manual, "z", AttemptResult.solved, flag="flag{b}")
    assert tracking.get_challenge(session, challenge.id).solved_at == first_solved_at
    assert tracking.get_challenge(session, challenge.id).flag == "flag{b}"


def test_update_challenge_marks_solved(session, challenge):
    updated = tracking.update_challenge(session, challenge.id, status=ChallengeStatus.solved, flag="flag{u}")
    assert updated.solved_at is not None
    assert updated.flag == "flag{u}"


def test_challenge_summary(session, challenge):
    tracking.record_attempt(session, challenge.id, AttemptType.manual, "a", AttemptResult.failed)
    tracking.record_attempt(session, challenge.id, AttemptType.tool, "b", AttemptResult.breakthrough, tool_command="nmap -p 1337 chal")
    summary = tracking.challenge_summary(session, tracking.get_challenge(session, challenge.id))
    assert summary["attempt_count"] == 2
    assert summary["active_seconds"] is not None and summary["active_seconds"] >= 0


def test_summary_before_interaction(session, challenge):
    summary = tracking.challenge_summary(session, challenge)
    assert summary["attempt_count"] == 0
    assert summary["active_seconds"] is None


def test_record_attempt_missing_challenge_raises(session):
    with pytest.raises(LookupError):
        tracking.record_attempt(session, 999, AttemptType.manual, "x", AttemptResult.failed)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tracking_attempts.py -v`
Expected: FAIL with `AttributeError: module 'app.services.tracking' has no attribute 'record_attempt'`.

- [ ] **Step 3: Append to app/services/tracking.py**

Append at the end of the file (imports line `from ..models import (...)` becomes):

```python
from datetime import datetime

from ..models import (
    Attempt, AttemptResult, AttemptType, Challenge, ChallengeStatus, Difficulty,
    Event, EventStatus, Note,
)
```

Append functions:

```python
def record_attempt(session: Session, challenge_id: int, type: AttemptType,
                   description: str, result: AttemptResult,
                   tool_command: str | None = None, learning: str = "",
                   flag: str | None = None) -> Attempt:
    challenge = session.get(Challenge, challenge_id)
    if challenge is None:
        raise LookupError(f"challenge {challenge_id} not found")
    now = datetime.now()
    if challenge.first_interaction is None:
        challenge.first_interaction = now
    if result is AttemptResult.solved and challenge.solved_at is None:
        challenge.status = ChallengeStatus.solved
        challenge.solved_at = now
        if flag is not None:
            challenge.flag = flag
    session.add(challenge)
    attempt = Attempt(challenge_id=challenge_id, type=type, description=description,
                      tool_command=tool_command, result=result, learning=learning)
    session.add(attempt)
    session.commit()
    session.refresh(attempt)
    return attempt


def update_challenge(session: Session, challenge_id: int,
                     status: ChallengeStatus | None = None,
                     flag: str | None = None) -> Challenge:
    challenge = session.get(Challenge, challenge_id)
    if challenge is None:
        raise LookupError(f"challenge {challenge_id} not found")
    if status is not None:
        challenge.status = status
        if status is ChallengeStatus.solved and challenge.solved_at is None:
            challenge.solved_at = datetime.now()
    if flag is not None:
        challenge.flag = flag
    session.add(challenge)
    session.commit()
    session.refresh(challenge)
    return challenge


def list_attempts(session: Session, challenge_id: int) -> list[Attempt]:
    query = select(Attempt).where(Attempt.challenge_id == challenge_id).order_by(Attempt.timestamp)
    return list(session.exec(query).all())


def challenge_summary(session: Session, challenge: Challenge) -> dict:
    from sqlmodel import func
    count = session.exec(
        select(func.count()).select_from(Attempt).where(Attempt.challenge_id == challenge.id)
    ).one()
    active_seconds: float | None = None
    if challenge.first_interaction is not None:
        end = challenge.solved_at or datetime.now()
        active_seconds = (end - challenge.first_interaction).total_seconds()
    return {"attempt_count": count, "active_seconds": active_seconds}
```

- [ ] **Step 4: Run all tests to verify they pass**

Run: `python -m pytest tests/ -v`
Expected: all previous tests plus `7 passed` from this file.

- [ ] **Step 5: Commit**

```bash
git add app/services/tracking.py tests/test_tracking_attempts.py
git commit -m "feat: attempts, challenge finalization and derived metrics"
```

---

### Task 5: TrackingService — versioned notes

**Files:**
- Modify: `app/services/tracking.py` (append functions)
- Test: `tests/test_tracking_notes.py`

**Interfaces:**
- Consumes: Task 4 `record_attempt`-style error contract (`LookupError`).
- Produces:
  - `add_note(session, challenge_id: int, content: str) -> Note` — INSERT only; also stamps `first_interaction` if unset.
  - `note_history(session, challenge_id: int) -> list[Note]` — newest first.
  - `current_note(session, challenge_id: int) -> Note | None` — `note_history(...)[0]`.

- [ ] **Step 1: Write failing test tests/test_tracking_notes.py**

```python
import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.services import tracking


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def challenge(session):
    event = tracking.create_event(session, "e")
    return tracking.create_challenge(session, event.id, "n1")


def test_notes_are_append_only(session, challenge):
    first = tracking.add_note(session, challenge.id, "# version 1")
    second = tracking.add_note(session, challenge.id, "# version 2\nwith more detail")
    assert first.id != second.id
    history = tracking.note_history(session, challenge.id)
    assert [n.content for n in history] == ["# version 2\nwith more detail", "# version 1"]
    assert history[0].timestamp >= history[1].timestamp


def test_current_note_returns_latest(session, challenge):
    tracking.add_note(session, challenge.id, "old")
    latest = tracking.add_note(session, challenge.id, "newest")
    assert tracking.current_note(session, challenge.id).id == latest.id


def test_current_note_none_when_empty(session, challenge):
    assert tracking.current_note(session, challenge.id) is None


def test_add_note_sets_first_interaction(session, challenge):
    tracking.add_note(session, challenge.id, "hello")
    assert tracking.get_challenge(session, challenge.id).first_interaction is not None


def test_add_note_missing_challenge_raises(session):
    with pytest.raises(LookupError):
        tracking.add_note(session, 999, "x")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tracking_notes.py -v`
Expected: FAIL with `AttributeError: ... no attribute 'add_note'`.

- [ ] **Step 3: Append to app/services/tracking.py**

```python
def add_note(session: Session, challenge_id: int, content: str) -> Note:
    challenge = session.get(Challenge, challenge_id)
    if challenge is None:
        raise LookupError(f"challenge {challenge_id} not found")
    if challenge.first_interaction is None:
        challenge.first_interaction = datetime.now()
        session.add(challenge)
    note = Note(challenge_id=challenge_id, content=content)
    session.add(note)
    session.commit()
    session.refresh(note)
    return note


def note_history(session: Session, challenge_id: int) -> list[Note]:
    query = select(Note).where(Note.challenge_id == challenge_id).order_by(Note.timestamp.desc(), Note.id.desc())
    return list(session.exec(query).all())


def current_note(session: Session, challenge_id: int) -> Note | None:
    history = note_history(session, challenge_id)
    return history[0] if history else None
```

- [ ] **Step 4: Run all tests to verify they pass**

Run: `python -m pytest tests/ -v`
Expected: all pass, including `5 passed` from this file.

- [ ] **Step 5: Commit**

```bash
git add app/services/tracking.py tests/test_tracking_notes.py
git commit -m "feat: append-only versioned notes"
```

---

### Task 6: StorageService — attachments on disk

**Files:**
- Create: `app/services/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: filesystem.
- Produces (used by Task 8 API and future ToolRunner output capture):
  - `class StorageService: def __init__(self, storage_dir: Path)`
  - `StorageService.save(challenge_id: int, filename: str, data: bytes) -> Path` — writes to `<storage_dir>/<challenge_id>/<8hex>_<sanitized_name>`, sanitizing the filename to its basename (no traversal), returns the written path.

- [ ] **Step 1: Write failing test tests/test_storage.py**

```python
from app.services.storage import StorageService


def test_save_writes_file_and_sanitizes_name(tmp_path):
    storage = StorageService(tmp_path / "storage")
    path = storage.save(7, "../../evil/../report.pdf", b"%PDF-1.4 fake")
    assert path.exists()
    assert path.parent == (tmp_path / "storage" / "7")
    assert path.name.endswith("report.pdf")
    assert ".." not in path.name
    assert path.read_bytes() == b"%PDF-1.4 fake"


def test_save_avoids_collisions(tmp_path):
    storage = StorageService(tmp_path / "storage")
    a = storage.save(1, "same.txt", b"a")
    b = storage.save(1, "same.txt", b"b")
    assert a != b
    assert b.read_bytes() == b"b"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_storage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.storage'`.

- [ ] **Step 3: Write app/services/storage.py**

```python
import uuid
from pathlib import Path


class StorageService:
    def __init__(self, storage_dir: Path) -> None:
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def save(self, challenge_id: int, filename: str, data: bytes) -> Path:
        safe_name = Path(filename.replace("\\", "/")).name or "unnamed"
        target_dir = self.storage_dir / str(challenge_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
        path.write_bytes(data)
        return path
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_storage.py -v`
Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add app/services/storage.py tests/test_storage.py
git commit -m "feat: storage service for challenge attachments"
```

---

### Task 7: REST API — events and challenges

**Files:**
- Create: `app/api/__init__.py` (empty)
- Create: `app/api/events.py`
- Create: `app/api/challenges.py`
- Modify: `app/main.py` (include routers + storage injection)
- Test: `tests/test_api_events.py`, `tests/test_api_challenges.py`

**Interfaces:**
- Consumes: `tracking` (Tasks 3–4), `get_session` dependency.
- Produces (REST contract used by Task 9–10 UI and future CTFd importer):
  - `POST /api/events` body `{name, url?, starts_at?, ends_at?}` → 200 Event JSON; 404 if body event_id missing (n/a)
  - `GET /api/events` → list[Event]
  - `PATCH /api/events/{event_id}/status` body `{"status": "active"}` → Event; 404 unknown id
  - `POST /api/events/{event_id}/challenges` body `{title, category?, points?, difficulty?, description?, target?, tags?}` → 200 Challenge; 404 unknown event
  - `GET /api/events/{event_id}/challenges?category=&status=&tag=` → list[Challenge]
  - `GET /api/challenges/{challenge_id}` → `{challenge, summary, attempts, notes}` (summary from Task 4)
  - `PATCH /api/challenges/{challenge_id}` body `{status?, flag?}` → Challenge; 404 unknown id
  - `app.state.storage: StorageService` available to all routers (Task 8 uses it).

- [ ] **Step 1: Write failing test tests/test_api_events.py**

```python
def test_create_and_list_event(client):
    r = client.post("/api/events", json={"name": "picoCTF", "url": "https://picoctf.org"})
    assert r.status_code == 200
    assert r.json()["name"] == "picoCTF"
    lst = client.get("/api/events").json()
    assert len(lst) == 1


def test_patch_event_status(client):
    event = client.post("/api/events", json={"name": "e"}).json()
    r = client.patch(f"/api/events/{event['id']}/status", json={"status": "active"})
    assert r.status_code == 200
    assert r.json()["status"] == "active"


def test_patch_event_status_unknown_returns_404(client):
    r = client.patch("/api/events/999/status", json={"status": "active"})
    assert r.status_code == 404
```

- [ ] **Step 2: Write failing test tests/test_api_challenges.py**

```python
def _make_event(client):
    return client.post("/api/events", json={"name": "e"}).json()


def test_create_and_filter_challenges(client):
    event = _make_event(client)
    r = client.post(f"/api/events/{event['id']}/challenges",
                    json={"title": "web1", "category": "web", "points": 100, "tags": "ssti"})
    assert r.status_code == 200
    assert r.json()["category"] == "web"
    filtered = client.get(f"/api/events/{event['id']}/challenges", params={"category": "web"}).json()
    assert len(filtered) == 1


def test_create_challenge_unknown_event_404(client):
    r = client.post("/api/events/999/challenges", json={"title": "x"})
    assert r.status_code == 404


def test_challenge_detail_includes_summary(client):
    event = _make_event(client)
    ch = client.post(f"/api/events/{event['id']}/challenges", json={"title": "c1"}).json()
    client.post(f"/api/challenges/{ch['id']}/attempts",
                json={"type": "manual", "description": "tried x", "result": "failed"})
    detail = client.get(f"/api/challenges/{ch['id']}").json()
    assert detail["summary"]["attempt_count"] == 1
    assert len(detail["attempts"]) == 1


def test_patch_challenge_solves(client):
    event = _make_event(client)
    ch = client.post(f"/api/events/{event['id']}/challenges", json={"title": "c1"}).json()
    r = client.patch(f"/api/challenges/{ch['id']}", json={"status": "solved", "flag": "flag{ok}"})
    assert r.json()["status"] == "solved"
    assert r.json()["flag"] == "flag{ok}"
    assert r.json()["solved_at"] is not None


def test_patch_challenge_unknown_404(client):
    r = client.patch("/api/challenges/999", json={"flag": "f"})
    assert r.status_code == 404
```

Note: `test_challenge_detail_includes_summary` and `test_patch_challenge_solves` depend on Task 8's `/attempts` endpoint — it is fine if those two fail now; the others must pass after this task.

- [ ] **Step 3: Run tests to verify failures**

Run: `python -m pytest tests/test_api_events.py tests/test_api_challenges.py -v`
Expected: FAIL (`404 Not Found` on `/api/events` POST).

- [ ] **Step 4: Write app/api/events.py**

```python
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from ..database import get_session
from ..models import Event, EventStatus
from ..services import tracking

router = APIRouter()


class EventCreate(BaseModel):
    name: str
    url: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class StatusUpdate(BaseModel):
    status: EventStatus


@router.post("", response_model=Event)
def create_event(body: EventCreate, session: Session = Depends(get_session)):
    return tracking.create_event(session, body.name, url=body.url,
                                 starts_at=body.starts_at, ends_at=body.ends_at)


@router.get("", response_model=list[Event])
def list_events(session: Session = Depends(get_session)):
    return tracking.list_events(session)


@router.patch("/{event_id}/status", response_model=Event)
def set_status(event_id: int, body: StatusUpdate, session: Session = Depends(get_session)):
    try:
        return tracking.set_event_status(session, event_id, body.status)
    except LookupError:
        raise HTTPException(status_code=404, detail="event not found")
```

- [ ] **Step 5: Write app/api/challenges.py**

```python
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session

from ..database import get_session
from ..models import Attempt, Challenge, ChallengeStatus, Difficulty, Note
from ..services import tracking

router = APIRouter()


class ChallengeCreate(BaseModel):
    title: str
    category: str = "misc"
    points: int = 0
    difficulty: Difficulty | None = None
    description: str = ""
    target: str | None = None
    tags: str = ""


class ChallengeUpdate(BaseModel):
    status: ChallengeStatus | None = None
    flag: str | None = None


@router.post("/events/{event_id}/challenges", response_model=Challenge)
def create_challenge(event_id: int, body: ChallengeCreate,
                     session: Session = Depends(get_session)):
    try:
        return tracking.create_challenge(session, event_id, body.title,
                                         category=body.category, points=body.points,
                                         difficulty=body.difficulty,
                                         description=body.description,
                                         target=body.target, tags=body.tags)
    except LookupError:
        raise HTTPException(status_code=404, detail="event not found")


@router.get("/events/{event_id}/challenges", response_model=list[Challenge])
def list_challenges(event_id: int, category: str | None = None,
                    status: ChallengeStatus | None = None, tag: str | None = None,
                    session: Session = Depends(get_session)):
    return tracking.list_challenges(session, event_id=event_id, category=category,
                                    status=status, tag=tag)


@router.get("/challenges/{challenge_id}")
def challenge_detail(challenge_id: int, session: Session = Depends(get_session)):
    challenge = tracking.get_challenge(session, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    return {
        "challenge": challenge,
        "summary": tracking.challenge_summary(session, challenge),
        "attempts": tracking.list_attempts(session, challenge_id),
        "notes": tracking.note_history(session, challenge_id),
    }


@router.patch("/challenges/{challenge_id}", response_model=Challenge)
def update_challenge(challenge_id: int, body: ChallengeUpdate,
                     session: Session = Depends(get_session)):
    try:
        return tracking.update_challenge(session, challenge_id,
                                         status=body.status, flag=body.flag)
    except LookupError:
        raise HTTPException(status_code=404, detail="challenge not found")
```

- [ ] **Step 6: Modify app/main.py — register routers and app.state.storage**

In `app/main.py`, add imports at top:

```python
from pathlib import Path
```

Inside `create_app`, after the health endpoint, add:

```python
    from .services.storage import StorageService

    app.state.storage = StorageService(storage_dir or __import__("app").config.settings.storage_dir)
    app.include_router(events_router)
    app.include_router(challenges_router)
```

and at the top of the file:

```python
from .api import challenges as challenges_router_module
from .api import events as events_router_module
```

with `events_router = events_router_module.router` / `challenges_router = challenges_router_module.router` referenced in `include_router` calls:

```python
    app.include_router(events_router_module.router, prefix="/api/events", tags=["events"])
    app.include_router(challenges_router_module.router, prefix="/api", tags=["challenges"])
```

Note: the events router paths are relative (`""`, `"/{event_id}/status"`), hence prefix `/api/events`; the challenges router declares full paths (`/events/{event_id}/challenges`, `/challenges/{id}`) under `/api`.

- [ ] **Step 7: Run tests**

Run: `python -m pytest tests/test_api_events.py tests/test_api_challenges.py -v`
Expected: all pass except `test_challenge_detail_includes_summary` and `test_patch_challenge_solves` may still fail (Task 8 dependency) — the two must fail ONLY due to missing `/attempts` route (404), not due to events/challenges behavior.

- [ ] **Step 8: Commit**

```bash
git add app/api app/main.py tests/test_api_events.py tests/test_api_challenges.py
git commit -m "feat: REST API for events and challenges"
```

---

### Task 8: REST API — attempts, notes, attachments

**Files:**
- Modify: `app/api/challenges.py` (append routers/endpoints)
- Test: `tests/test_api_content.py`

**Interfaces:**
- Consumes: Task 4–6 services; `app.state.storage: StorageService` (Task 7).
- Produces:
  - `POST /api/challenges/{id}/attempts` body `{type, description, result, tool_command?, learning?, flag?}` → Attempt; 404 unknown challenge
  - `POST /api/challenges/{id}/notes` body `{content}` → Note
  - `GET /api/challenges/{id}/notes` → list[Note] (newest first)
  - `POST /api/challenges/{id}/attachments` multipart `file` + form field `type` (default `download`) → Attachment
  - `GET /api/challenges/{id}/attachments` → list[Attachment]

- [ ] **Step 1: Write failing test tests/test_api_content.py**

```python
def _challenge(client):
    event = client.post("/api/events", json={"name": "e"}).json()
    return client.post(f"/api/events/{event['id']}/challenges", json={"title": "c1"}).json()


def test_post_attempt(client):
    ch = _challenge(client)
    r = client.post(f"/api/challenges/{ch['id']}/attempts",
                    json={"type": "tool", "description": "ran nmap", "result": "breakthrough",
                          "tool_command": "nmap -p 1337 chal"})
    assert r.status_code == 200
    assert r.json()["result"] == "breakthrough"
    detail = client.get(f"/api/challenges/{ch['id']}").json()
    assert detail["summary"]["attempt_count"] == 1


def test_post_attempt_unknown_challenge_404(client):
    r = client.post("/api/challenges/999/attempts",
                    json={"type": "manual", "description": "x", "result": "failed"})
    assert r.status_code == 404


def test_notes_versions(client):
    ch = _challenge(client)
    client.post(f"/api/challenges/{ch['id']}/notes", json={"content": "v1"})
    client.post(f"/api/challenges/{ch['id']}/notes", json={"content": "v2"})
    notes = client.get(f"/api/challenges/{ch['id']}/notes").json()
    assert [n["content"] for n in notes] == ["v2", "v1"]


def test_attachment_upload(client):
    ch = _challenge(client)
    r = client.post(f"/api/challenges/{ch['id']}/attachments",
                    files={"file": ("shot.png", b"\x89PNG fake", "image/png")},
                    data={"type": "screenshot"})
    assert r.status_code == 200
    assert r.json()["type"] == "screenshot"
    lst = client.get(f"/api/challenges/{ch['id']}/attachments").json()
    assert len(lst) == 1
    assert lst[0]["size"] == len(b"\x89PNG fake")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_api_content.py -v`
Expected: FAIL with 404 on the new routes.

- [ ] **Step 3: Append to app/api/challenges.py**

Add imports at top of file:

```python
from fastapi import UploadFile, File, Form
from ..models import Attachment, AttachmentType, AttemptType, AttemptResult
from ..services.storage import StorageService


class AttemptCreate(BaseModel):
    type: AttemptType
    description: str
    result: AttemptResult
    tool_command: str | None = None
    learning: str = ""
    flag: str | None = None


class NoteCreate(BaseModel):
    content: str
```

Append endpoints:

```python
@router.post("/challenges/{challenge_id}/attempts", response_model=Attempt)
def post_attempt(challenge_id: int, body: AttemptCreate,
                 session: Session = Depends(get_session)):
    try:
        return tracking.record_attempt(session, challenge_id, body.type,
                                       body.description, body.result,
                                       tool_command=body.tool_command,
                                       learning=body.learning, flag=body.flag)
    except LookupError:
        raise HTTPException(status_code=404, detail="challenge not found")


@router.post("/challenges/{challenge_id}/notes", response_model=Note)
def post_note(challenge_id: int, body: NoteCreate,
              session: Session = Depends(get_session)):
    try:
        return tracking.add_note(session, challenge_id, body.content)
    except LookupError:
        raise HTTPException(status_code=404, detail="challenge not found")


@router.get("/challenges/{challenge_id}/notes", response_model=list[Note])
def get_notes(challenge_id: int, session: Session = Depends(get_session)):
    return tracking.note_history(session, challenge_id)


@router.post("/challenges/{challenge_id}/attachments", response_model=Attachment)
def upload_attachment(challenge_id: int, request: Request,
                      file: UploadFile = File(...), type: AttachmentType = Form(AttachmentType.download),
                      session: Session = Depends(get_session)):
    challenge = tracking.get_challenge(session, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    data = file.file.read()
    storage: StorageService = request.app.state.storage
    path = storage.save(challenge_id, file.filename, data)
    attachment = Attachment(challenge_id=challenge_id, name=file.filename,
                            path=str(path.relative_to(storage.storage_dir)),
                            mime=file.content_type or "application/octet-stream",
                            size=len(data), type=type)
    session.add(attachment)
    session.commit()
    session.refresh(attachment)
    return attachment


@router.get("/challenges/{challenge_id}/attachments", response_model=list[Attachment])
def list_attachments(challenge_id: int, session: Session = Depends(get_session)):
    from sqlmodel import select
    return list(session.exec(
        select(Attachment).where(Attachment.challenge_id == challenge_id)
    ).all())
```

- [ ] **Step 4: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: every test passes, including the two deferred from Task 7.

- [ ] **Step 5: Commit**

```bash
git add app/api/challenges.py tests/test_api_content.py
git commit -m "feat: REST API for attempts, versioned notes and attachments"
```

---

### Task 9: Web UI — base template, theme tokens, dashboard

**Files:**
- Create: `app/web/__init__.py` (empty)
- Create: `app/web/routes.py`
- Create: `app/web/templates/base.html`
- Create: `app/web/templates/dashboard.html`
- Create: `app/static/theme.css`
- Modify: `app/main.py` (mount static + include web router)
- Test: `tests/test_ui_dashboard.py`

**Interfaces:**
- Consumes: tracking services.
- Produces:
  - `GET /` → dashboard page (events with progress, open challenges, recent attempts feed)
  - `templates/base.html` provides `{% block content %}`; theme tokens defined in `app/static/theme.css` as CSS custom properties (`--bg-0`, `--bg-1`, `--accent`, `--accent-dim`, `--text`, `--muted`, `--solved`, `--flag-ok`).

- [ ] **Step 1: Write failing smoke test tests/test_ui_dashboard.py**

```python
def test_dashboard_renders(client):
    client.post("/api/events", json={"name": "picoCTF"})
    client.post("/api/events/1/challenges", json={"title": "web1", "category": "web", "points": 100})
    r = client.get("/")
    assert r.status_code == 200
    assert "picoCTF" in r.text
    assert "web1" in r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ui_dashboard.py -v`
Expected: FAIL (`404 Not Found` on `GET /`).

- [ ] **Step 3: Write app/static/theme.css**

```css
:root {
  --bg-0: #0a0a0a;
  --bg-1: #1a0d0d;
  --panel: #12100f;
  --panel-edge: #2a1616;
  --accent: #e01b24;
  --accent-dim: #7a1013;
  --text: #e8e3e1;
  --muted: #8f8886;
  --solved: #4a4442;
  --flag-ok: #2ea043;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  min-height: 100vh;
  background: linear-gradient(160deg, var(--bg-0) 0%, var(--bg-1) 100%) fixed;
  color: var(--text);
  font-family: "Segoe UI", system-ui, sans-serif;
}

code, pre, .mono, .flag, .attempt-output {
  font-family: "Cascadia Code", Consolas, monospace;
}

a { color: var(--accent); text-decoration: none; }
a:hover { color: var(--accent-dim); }

header.topbar {
  display: flex;
  align-items: center;
  gap: 1rem;
  padding: 0.75rem 1.5rem;
  border-bottom: 1px solid var(--panel-edge);
}

header.topbar .brand {
  color: var(--accent);
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

main { max-width: 1100px; margin: 0 auto; padding: 1.5rem; }

.card {
  background: var(--panel);
  border: 1px solid var(--panel-edge);
  border-radius: 8px;
  padding: 1rem 1.25rem;
  margin-bottom: 1rem;
}

.card h2, .card h3 { margin-top: 0; color: var(--text); }

.badge {
  display: inline-block;
  padding: 0.1rem 0.6rem;
  border-radius: 999px;
  font-size: 0.78rem;
  border: 1px solid var(--accent-dim);
  color: var(--accent);
}

.badge.solved { border-color: var(--solved); color: var(--muted); }
.badge.flag-ok { border-color: var(--flag-ok); color: var(--flag-ok); }

.btn {
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: 6px;
  padding: 0.5rem 1rem;
  font-weight: 600;
  cursor: pointer;
}
.btn:hover { background: var(--accent-dim); }
.btn.ghost {
  background: transparent;
  border: 1px solid var(--accent-dim);
  color: var(--accent);
}

table.list { width: 100%; border-collapse: collapse; }
table.list th, table.list td {
  text-align: left;
  padding: 0.4rem 0.6rem;
  border-bottom: 1px solid var(--panel-edge);
}
table.list th { color: var(--muted); font-size: 0.8rem; text-transform: uppercase; }

.meter {
  height: 6px;
  border-radius: 999px;
  background: var(--panel-edge);
  overflow: hidden;
}
.meter > span { display: block; height: 100%; background: var(--accent); }

input, textarea, select {
  background: var(--bg-0);
  border: 1px solid var(--panel-edge);
  color: var(--text);
  border-radius: 6px;
  padding: 0.45rem 0.6rem;
  width: 100%;
}
```

- [ ] **Step 4: Write app/web/templates/base.html**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}CTF Orchestrator{% endblock %}</title>
  <link rel="stylesheet" href="/static/theme.css">
  <script src="https://unpkg.com/htmx.org@2.0.4"></script>
</head>
<body>
  <header class="topbar">
    <a href="/" class="brand">CTF Orchestrator</a>
    <span style="color: var(--muted); font-size: 0.85rem;">solo / local</span>
  </header>
  <main>
    {% block content %}{% endblock %}
  </main>
</body>
</html>
```

- [ ] **Step 5: Write app/web/templates/dashboard.html**

```html
{% extends "base.html" %}
{% block title %}Dashboard — CTF Orchestrator{% endblock %}
{% block content %}
<h1>Dashboard</h1>

<section class="card">
  <h2>Eventos</h2>
  {% for event, solved, total in events %}
    <div>
      <a href="/events/{{ event.id }}">{{ event.name }}</a>
      <span class="badge">{{ event.status.value }}</span>
      <div class="meter" style="max-width: 320px;">
        <span style="width: {{ (100 * solved / total) if total else 0 }}%;"></span>
      </div>
      <small style="color: var(--muted);">{{ solved }}/{{ total }} resolvidos</small>
    </div>
  {% else %}
    <p style="color: var(--muted);">Nenhum evento. Crie via API ou form abaixo.</p>
  {% endfor %}
  <form method="post" action="/events" style="display:flex; gap:0.5rem; max-width:480px; margin-top:1rem;">
    <input name="name" placeholder="Nome do CTF" required>
    <button class="btn" type="submit">Criar evento</button>
  </form>
</section>

<section class="card">
  <h2>Challenges abertos</h2>
  <table class="list">
    <tr><th>Título</th><th>Categoria</th><th>Pontos</th><th>Status</th></tr>
    {% for c in open_challenges %}
      <tr>
        <td><a href="/challenges/{{ c.id }}">{{ c.title }}</a></td>
        <td>{{ c.category }}</td>
        <td>{{ c.points }}</td>
        <td><span class="badge">{{ c.status.value }}</span></td>
      </tr>
    {% else %}
      <tr><td colspan="4" style="color: var(--muted);">Nada aberto.</td></tr>
    {% endfor %}
  </table>
</section>

<section class="card">
  <h2>Atividade recente</h2>
  <ul>
    {% for a in recent_attempts %}
      <li class="mono" style="font-size: 0.85rem;">
        [{{ a.timestamp.strftime("%H:%M") }}] {{ a.type.value }} — {{ a.description }} → {{ a.result.value }}
      </li>
    {% else %}
      <li style="color: var(--muted);">Sem tentativas ainda.</li>
    {% endfor %}
  </ul>
</section>
{% endblock %}
```

- [ ] **Step 6: Write app/web/routes.py (dashboard only for now)**

```python
from datetime import datetime

from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from ..database import get_session
from ..models import Attempt, Challenge, ChallengeStatus, Event
from ..services import tracking
from ..templates import templates

router = APIRouter()


@router.get("/")
def dashboard(request, session: Session = Depends(get_session)):
    events = tracking.list_events(session)
    event_rows = []
    for event in events:
        chs = tracking.list_challenges(session, event_id=event.id)
        solved = sum(1 for c in chs if c.status is ChallengeStatus.solved)
        event_rows.append((event, solved, len(chs)))
    open_challenges = tracking.list_challenges(session, status=ChallengeStatus.open)
    recent = list(session.exec(
        select(Attempt).order_by(Attempt.timestamp.desc()).limit(15)
    ).all())
    return templates.TemplateResponse(request, "dashboard.html", {
        "events": event_rows,
        "open_challenges": open_challenges,
        "recent_attempts": recent,
    })


@router.post("/events")
def create_event_form(request, name: str = Form(...),
                      session: Session = Depends(get_session)):
    tracking.create_event(session, name)
    return RedirectResponse("/", status_code=303)
```

- [ ] **Step 7: Create app/templates.py**

```python
from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "web" / "templates"))
```

- [ ] **Step 8: Modify app/main.py — mount static and web router**

At the top of `app/main.py`, add:

```python
from fastapi.staticfiles import StaticFiles
```

Inside `create_app`, after `app.include_router(challenges_router_module.router, ...)`:

```python
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    from .web import routes as web_routes
    app.include_router(web_routes.router)
```

- [ ] **Step 9: Fix the dashboard route signature**

The route functions need the `Request` for `TemplateResponse(request, ...)`. Update `app/web/routes.py` imports and signatures:

```python
from fastapi import APIRouter, Depends, Form, Request
```

and each handler signature gains `request: Request` as first parameter:

```python
@router.get("/")
def dashboard(request: Request, session: Session = Depends(get_session)):
```

```python
@router.post("/events")
def create_event_form(request: Request, name: str = Form(...),
                      session: Session = Depends(get_session)):
```

- [ ] **Step 10: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: all pass, including `test_dashboard_renders`.

- [ ] **Step 11: Manual visual check (theme)**

Run: `python -m uvicorn app.main:app --port 8000`
Open: `http://localhost:8000/`
Expected: dark gradient background, red accents on brand/buttons/badges, readable cards.

- [ ] **Step 12: Commit**

```bash
git add app/web app/templates.py app/static app/main.py tests/test_ui_dashboard.py
git commit -m "feat: dashboard UI with black/red theme"
```

---

### Task 10: Web UI — event page and challenge page

**Files:**
- Create: `app/web/templates/event.html`
- Create: `app/web/templates/challenge.html`
- Modify: `app/web/routes.py` (append routes)
- Test: `tests/test_ui_pages.py`

**Interfaces:**
- Consumes: all tracking services; Task 9 template infrastructure.
- Produces:
  - `GET /events/{event_id}` → event page: challenge grid with filters (`?category=&status=`) and "new challenge" form
  - `GET /challenges/{challenge_id}` → challenge page: description, target, attachments list, attempts timeline, notes with history count, manual attempt form, note form
  - `POST /challenges/{challenge_id}/attempts` (form) → records manual attempt, redirects back
  - `POST /challenges/{challenge_id}/notes` (form) → appends note version, redirects back

- [ ] **Step 1: Write failing test tests/test_ui_pages.py**

```python
import io


def _seed(client):
    event = client.post("/api/events", json={"name": "HTB"}).json()
    ch = client.post(f"/api/events/{event['id']}/challenges",
                     json={"title": "git_dumper", "category": "web", "points": 250,
                           "description": "Find the source", "target": "10.10.10.1:80",
                           "tags": "git,ssti"}).json()
    return event, ch


def test_event_page_renders_grid(client):
    event, ch = _seed(client)
    r = client.get(f"/events/{event['id']}")
    assert r.status_code == 200
    assert "git_dumper" in r.text


def test_event_page_filters(client):
    event, ch = _seed(client)
    client.post(f"/api/events/{event['id']}/challenges", json={"title": "pwn1", "category": "pwn"})
    r = client.get(f"/events/{event['id']}", params={"category": "pwn"})
    assert "pwn1" in r.text
    assert "git_dumper" not in r.text


def test_challenge_page_renders_all_sections(client):
    event, ch = _seed(client)
    client.post(f"/api/challenges/{ch['id']}/attempts",
                json={"type": "manual", "description": "ran git-dumper", "result": "breakthrough"})
    client.post(f"/api/challenges/{ch['id']}/notes", json={"content": "v1 note"})
    client.post(f"/api/challenges/{ch['id']}/attachments",
                files={"file": ("a.txt", io.BytesIO(b"hello"), "text/plain")},
                data={"type": "download"})
    r = client.get(f"/challenges/{ch['id']}")
    body = r.text
    assert "git_dumper" in body
    assert "ran git-dumper" in body
    assert "v1 note" in body
    assert "a.txt" in body
    assert "10.10.10.1:80" in body


def test_manual_attempt_form_flow(client):
    event, ch = _seed(client)
    r = client.post(f"/challenges/{ch['id']}/attempts",
                    data={"description": "tried dirsearch", "result": "failed",
                          "learning": "no dir listing"}, follow_redirects=False)
    assert r.status_code == 303
    detail = client.get(f"/api/challenges/{ch['id']}").json()
    assert detail["summary"]["attempt_count"] == 1


def test_note_form_flow(client):
    event, ch = _seed(client)
    r = client.post(f"/challenges/{ch['id']}/notes", data={"content": "my notes"}, follow_redirects=False)
    assert r.status_code == 303
    notes = client.get(f"/api/challenges/{ch['id']}/notes").json()
    assert notes[0]["content"] == "my notes"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ui_pages.py -v`
Expected: FAIL (`404 Not Found` on `/events/1`).

- [ ] **Step 3: Write app/web/templates/event.html**

```html
{% extends "base.html" %}
{% block title %}{{ event.name }} — CTF Orchestrator{% endblock %}
{% block content %}
<h1>{{ event.name }} <span class="badge">{{ event.status.value }}</span></h1>

<form method="get" action="/events/{{ event.id }}" style="display:flex; gap:0.5rem; max-width:640px; margin-bottom:1rem;">
  <select name="category">
    <option value="">todas categorias</option>
    {% for cat in ["pwn","web","crypto","reverse","forensics","osint","misc","ai-ml","custom"] %}
      <option value="{{ cat }}" {% if category == cat %}selected{% endif %}>{{ cat }}</option>
    {% endfor %}
  </select>
  <select name="status">
    <option value="">todos status</option>
    {% for st in ["open","solved","gave_up"] %}
      <option value="{{ st }}" {% if status == st %}selected{% endif %}>{{ st }}</option>
    {% endfor %}
  </select>
  <button class="btn ghost" type="submit">Filtrar</button>
</form>

<table class="list">
  <tr><th>Título</th><th>Categoria</th><th>Pontos</th><th>Dificuldade</th><th>Status</th></tr>
  {% for c in challenges %}
    <tr>
      <td><a href="/challenges/{{ c.id }}">{{ c.title }}</a></td>
      <td>{{ c.category }}</td>
      <td>{{ c.points }}</td>
      <td>{{ c.difficulty.value if c.difficulty else "—" }}</td>
      <td><span class="badge {{ 'solved' if c.status.value == 'solved' else '' }}">{{ c.status.value }}</span>
          {% if c.flag %}<span class="badge flag-ok mono">flag ok</span>{% endif %}</td>
    </tr>
  {% else %}
    <tr><td colspan="5" style="color: var(--muted);">Nenhum challenge com esses filtros.</td></tr>
  {% endfor %}
</table>

<details class="card" style="margin-top:1rem;">
  <summary>+ Novo challenge</summary>
  <form method="post" action="/events/{{ event.id }}/challenges" style="display:grid; gap:0.5rem; max-width:640px; margin-top:1rem;">
    <input name="title" placeholder="Título" required>
    <select name="category">
      {% for cat in ["pwn","web","crypto","reverse","forensics","osint","misc","ai-ml","custom"] %}
        <option value="{{ cat }}">{{ cat }}</option>
      {% endfor %}
    </select>
    <input name="points" type="number" placeholder="Pontos" value="100">
    <input name="difficulty" placeholder="Dificuldade (easy/medium/hard/insane)">
    <input name="target" placeholder="host:porta (target autorizado)">
    <input name="tags" placeholder="tags separadas por vírgula">
    <textarea name="description" placeholder="Descrição (Markdown)"></textarea>
    <button class="btn" type="submit">Criar challenge</button>
  </form>
</details>
{% endblock %}
```

- [ ] **Step 4: Write app/web/templates/challenge.html**

```html
{% extends "base.html" %}
{% block title %}{{ challenge.title }} — CTF Orchestrator{% endblock %}
{% block content %}
<h1>{{ challenge.title }}
  <span class="badge {{ 'solved' if challenge.status.value == 'solved' else '' }}">{{ challenge.status.value }}</span>
  {% if challenge.flag %}<span class="badge flag-ok mono">{{ challenge.flag }}</span>{% endif %}
</h1>
<p class="mono" style="color: var(--muted);">
  {{ challenge.category }} · {{ challenge.points }} pts ·
  {% if challenge.target %}target: {{ challenge.target }} · {% endif %}
  {{ summary.attempt_count }} tentativas
  {% if summary.active_seconds is not none %} · {{ (summary.active_seconds / 60) | round(1) }} min ativos{% endif %}
</p>

<section class="card">
  <h2>Descrição</h2>
  <pre style="white-space: pre-wrap;">{{ challenge.description or "—" }}</pre>
</section>

<section class="card">
  <h2>Anexos</h2>
  <ul>
    {% for a in attachments %}
      <li class="mono">{{ a.name }} <small style="color: var(--muted);">({{ a.type.value }}, {{ a.size }} bytes)</small></li>
    {% else %}
      <li style="color: var(--muted);">Nenhum anexo.</li>
    {% endfor %}
  </ul>
  <form method="post" action="/challenges/{{ challenge.id }}/attachments" enctype="multipart/form-data" style="display:flex; gap:0.5rem; max-width:520px;">
    <input type="file" name="file" required>
    <select name="type" style="width:auto;">
      <option value="screenshot">screenshot</option>
      <option value="download" selected>download</option>
      <option value="output">output</option>
    </select>
    <button class="btn ghost" type="submit">Upload</button>
  </form>
</section>

<section class="card">
  <h2>Timeline de tentativas</h2>
  <ul>
    {% for a in attempts %}
      <li class="mono" style="font-size: 0.85rem;">
        [{{ a.timestamp.strftime("%d/%m %H:%M") }}] <span class="badge">{{ a.result.value }}</span>
        {{ a.description }}
        {% if a.tool_command %}<small style="color: var(--muted);"> · {{ a.tool_command }}</small>{% endif %}
        {% if a.learning %}<br><small style="color: var(--muted);">aprendizado: {{ a.learning }}</small>{% endif %}
      </li>
    {% else %}
      <li style="color: var(--muted);">Nenhuma tentativa ainda.</li>
    {% endfor %}
  </ul>
  <form method="post" action="/challenges/{{ challenge.id }}/attempts" style="display:grid; gap:0.5rem; max-width:640px; margin-top:1rem;">
    <input name="description" placeholder="O que foi tentado" required>
    <input name="tool_command" placeholder="Comando/ferramenta (opcional)">
    <select name="result">
      <option value="failed">failed</option>
      <option value="partial">partial</option>
      <option value="breakthrough">breakthrough</option>
      <option value="solved">solved</option>
    </select>
    <input name="learning" placeholder="Aprendizado (opcional)">
    <button class="btn" type="submit">Registrar tentativa</button>
  </form>
</section>

<section class="card">
  <h2>Notas <small style="color: var(--muted);">{{ notes | length }} versões</small></h2>
  {% if notes %}
    <pre style="white-space: pre-wrap;">{{ notes[0].content }}</pre>
  {% else %}
    <p style="color: var(--muted);">Sem notas.</p>
  {% endif %}
  <details {% if not notes %}open{% endif %}>
    <summary style="color: var(--muted); cursor: pointer;">+ Nova versão da nota</summary>
    <form method="post" action="/challenges/{{ challenge.id }}/notes" style="display:grid; gap:0.5rem; max-width:720px; margin-top:0.75rem;">
      <textarea name="content" rows="6" placeholder="Markdown..." required></textarea>
      <button class="btn" type="submit">Salvar nota (nova versão)</button>
    </form>
  </details>
</section>
{% endblock %}
```

- [ ] **Step 5: Append routes to app/web/routes.py**

Add imports at top:

```python
from fastapi import HTTPException, Request, UploadFile, File, Form
from ..models import AttachmentType
from ..services.storage import StorageService
```

Append:

```python
@router.get("/events/{event_id}")
def event_page(request: Request, event_id: int, category: str | None = None,
               status: str | None = None, session: Session = Depends(get_session)):
    event = session.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    challenge_status = ChallengeStatus(status) if status else None
    challenges = tracking.list_challenges(session, event_id=event_id,
                                          category=category, status=challenge_status)
    return templates.TemplateResponse(request, "event.html", {
        "event": event,
        "challenges": challenges,
        "category": category,
        "status": status,
    })


@router.post("/events/{event_id}/challenges")
def create_challenge_form(request: Request, event_id: int, title: str = Form(...),
                          category: str = Form("misc"), points: int = Form(100),
                          difficulty: str = Form(""),
                          target: str | None = Form(None),
                          tags: str = Form(""), description: str = Form(""),
                          session: Session = Depends(get_session)):
    diff = difficulty.strip() or None
    tracking.create_challenge(session, event_id, title, category=category,
                              points=points, difficulty=diff, description=description,
                              target=target, tags=tags)
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@router.get("/challenges/{challenge_id}")
def challenge_page(request: Request, challenge_id: int,
                   session: Session = Depends(get_session)):
    challenge = tracking.get_challenge(session, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    from sqlmodel import select as _select
    attachments = list(session.exec(
        _select(Attachment).where(Attachment.challenge_id == challenge_id)
    ).all())
    return templates.TemplateResponse(request, "challenge.html", {
        "challenge": challenge,
        "summary": tracking.challenge_summary(session, challenge),
        "attempts": tracking.list_attempts(session, challenge_id),
        "notes": tracking.note_history(session, challenge_id),
        "attachments": attachments,
    })


@router.post("/challenges/{challenge_id}/attempts")
def record_attempt_form(request: Request, challenge_id: int,
                        description: str = Form(...), tool_command: str = Form(""),
                        result: str = Form("failed"), learning: str = Form(""),
                        session: Session = Depends(get_session)):
    tracking.record_attempt(session, challenge_id, AttemptType.manual, description,
                            AttemptResult(result), tool_command=tool_command or None,
                            learning=learning)
    return RedirectResponse(f"/challenges/{challenge_id}", status_code=303)


@router.post("/challenges/{challenge_id}/notes")
def add_note_form(request: Request, challenge_id: int, content: str = Form(...),
                  session: Session = Depends(get_session)):
    tracking.add_note(session, challenge_id, content)
    return RedirectResponse(f"/challenges/{challenge_id}", status_code=303)


@router.post("/challenges/{challenge_id}/attachments")
def upload_attachment_form(request: Request, challenge_id: int,
                           file: UploadFile = File(...),
                           type: str = Form("download"),
                           session: Session = Depends(get_session)):
    challenge = tracking.get_challenge(session, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    data = file.file.read()
    storage: StorageService = request.app.state.storage
    path = storage.save(challenge_id, file.filename, data)
    session.add(Attachment(challenge_id=challenge_id, name=file.filename,
                           path=str(path.relative_to(storage.storage_dir)),
                           mime=file.content_type or "application/octet-stream",
                           size=len(data), type=AttachmentType(type)))
    session.commit()
    return RedirectResponse(f"/challenges/{challenge_id}", status_code=303)
```

And update the top import line of `app/web/routes.py` so that the models import covers everything used:

```python
from ..models import Attachment, AttachmentType, Attempt, AttemptResult, AttemptType, ChallengeStatus, Event
```

- [ ] **Step 6: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: every test passes, including the 5 new UI tests.

- [ ] **Step 7: Full manual smoke run**

Run: `python -m uvicorn app.main:app --port 8000`
Manual flow:
1. Open `http://localhost:8000/` → create event via form
2. Event page → create challenge via form
3. Challenge page → upload an attachment, record a manual attempt (result `solved`), save a note
4. Expected: badge turns gray `solved`, green `flag ok` badge only if flag set, note version count increments, timeline shows the attempt

- [ ] **Step 8: Commit**

```bash
git add app/web tests/test_ui_pages.py
git commit -m "feat: event and challenge pages with manual tracking flows"
```

---

## Self-Review

**Spec coverage (sections 4, 5, 9 of the spec):**
- §4 monolith FastAPI + services layer → Tasks 1, 3–6 ✅
- §5 data model (all five entities, enums, derived metrics computed not stored, append-only notes, target field) → Tasks 2–5 ✅
- §6 ToolRunner → explicitly out of scope (Task-level note in Global Constraints) ✅
- §9 UI dashboard/event/challenge + theme tokens → Tasks 9–10 ✅ (Painel do Agente e Configurações pertencem aos subsistemas 4–5, fora deste plano)
- §10 tests (services, API, UI smoke) → every task is TDD ✅
- Flag format global (`settings.flag_format`) — present in config (Task 1); UI Configurations page belongs to a later subsystem, accepted gap documented here.

**Placeholder scan:** no TBD/TODO; all code steps contain full code; deferred tests (Task 7 step 7) are explicitly named, not vague.

**Type consistency:** `record_attempt(session, challenge_id, type, description, result, ...)` signature identical in Task 4 service, Task 7 test, and Task 8 API usage. `challenge_summary` keys `attempt_count` / `active_seconds` consistent across Tasks 4, 7, 10. `StorageService.save(challenge_id, filename, data)` identical in Tasks 6, 8, 10. `LookupError` 404 contract consistent across Tasks 3–8.
