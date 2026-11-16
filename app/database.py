from sqlmodel import Session, SQLModel, create_engine

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
