from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import Session

from ..database import get_session

router = APIRouter()


class RebuildOut(BaseModel):
    indexed: int


@router.post("/rebuild", response_model=RebuildOut)
def rebuild(session: Session = Depends(get_session)):
    from ..knowledge.index import DEFAULT_KNOWLEDGE_ROOT, KnowledgeIndex

    n = KnowledgeIndex(session).rebuild(DEFAULT_KNOWLEDGE_ROOT)
    return {"indexed": n}


@router.get("/search")
def search(q: str = Query(...), limit: int = Query(10, ge=1, le=50),
           category: str | None = None,
           session: Session = Depends(get_session)):
    # imported at call time so tests can monkeypatch DEFAULT_KNOWLEDGE_ROOT
    from ..knowledge.index import DEFAULT_KNOWLEDGE_ROOT, KnowledgeIndex

    return KnowledgeIndex(session, knowledge_root=DEFAULT_KNOWLEDGE_ROOT).search(
        q, limit=limit, category=category)


@router.get("/skills")
def skills(session: Session = Depends(get_session)):
    from ..knowledge.index import DEFAULT_KNOWLEDGE_ROOT, KnowledgeIndex

    index = KnowledgeIndex(session, knowledge_root=DEFAULT_KNOWLEDGE_ROOT)
    if index.count() == 0:
        index.rebuild()
    rows = session.execute(text(
        "SELECT category, title, path FROM knowledge_fts WHERE source = 'skill' "
        "ORDER BY category")).all()
    return [{"category": r[0], "title": r[1], "path": r[2]} for r in rows]
