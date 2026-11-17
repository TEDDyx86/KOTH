from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from .config import settings
from .models import *  # noqa: F401,F403 — register SQLModel tables


def ensure_schema(engine) -> None:
    """Lightweight migration: add columns that exist in the models but are
    missing from an older SQLite database (create_all only adds new tables)."""
    inspector = inspect(engine)
    for table in SQLModel.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue  # create_all handles new tables
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            coltype = column.type.compile(engine.dialect)
            with engine.connect() as conn:
                conn.execute(text(
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {coltype}'))
                conn.commit()


def default_engine():
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{settings.db_path}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    ensure_schema(engine)
    return engine


def get_session():
    engine = default_engine()
    with Session(engine) as session:
        yield session
