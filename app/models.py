from datetime import datetime, timezone
from enum import Enum

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
