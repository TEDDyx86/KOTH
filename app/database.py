from sqlalchemy import event, inspect, text
from sqlmodel import Session, SQLModel, create_engine

from .config import settings
from .models import *  # noqa: F401,F403 — register SQLModel tables


def configure_sqlite_engine(engine) -> None:
    """Enable WAL mode, busy_timeout, and normal sync on every SQLite connection."""
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA busy_timeout=15000;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
        finally:
            cursor.close()


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


_ENGINES: dict[str, object] = {}


def default_engine():
    db_key = str(settings.db_path.resolve())
    cached = _ENGINES.get(db_key)
    if cached is not None:
        return cached
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{settings.db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    ensure_schema(engine)
    _ENGINES[db_key] = engine
    return engine


def get_session():
    engine = default_engine()
    with Session(engine) as session:
        yield session
