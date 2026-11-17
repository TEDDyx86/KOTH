from pathlib import Path

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from app.database import ensure_schema
from app.models import Event


def _old_event_table(engine):
    """Simulate a pre-migration DB: event table without flag_format."""
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE event ("
            " id INTEGER PRIMARY KEY,"
            " name VARCHAR NOT NULL,"
            " url VARCHAR,"
            " starts_at DATETIME,"
            " ends_at DATETIME,"
            " status VARCHAR,"
            " created_at DATETIME)"))
        conn.commit()


def test_ensure_schema_adds_missing_column(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}", connect_args={"check_same_thread": False})
    _old_event_table(engine)
    ensure_schema(engine)
    with Session(engine) as session:
        e = Event(name="x", flag_format="CTF{.*}")
        session.add(e)
        session.commit()
        session.refresh(e)
        assert e.flag_format == "CTF{.*}"


def test_ensure_schema_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'new.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    ensure_schema(engine)  # no-op on a current schema
    ensure_schema(engine)
    with Session(engine) as session:
        session.add(Event(name="y"))
        session.commit()
