from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

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

    from .config import settings
    from .services.storage import StorageService

    app.state.storage = StorageService(storage_dir or settings.storage_dir)

    from .api import challenges as challenges_module
    from .api import events as events_module
    from .api import knowledge as knowledge_module

    app.include_router(events_module.router, prefix="/api/events", tags=["events"])
    app.include_router(challenges_module.router, prefix="/api", tags=["challenges"])
    app.include_router(knowledge_module.router, prefix="/api/knowledge", tags=["knowledge"])

    from .agents import api as agent_api
    app.include_router(agent_api.router, prefix="/api/agent", tags=["agent"])

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    from .web import routes as web_routes
    app.include_router(web_routes.router)

    from .web import agent_routes
    app.include_router(agent_routes.router)

    return app


app = create_app()
