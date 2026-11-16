from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session

from ..config import BASE_DIR
from .parse import parse_knowledge_dir

DEFAULT_KNOWLEDGE_ROOT = BASE_DIR / "knowledge"


class KnowledgeIndex:
    def __init__(self, session: Session, knowledge_root: Path | None = None) -> None:
        self.session = session
        self.knowledge_root = knowledge_root or DEFAULT_KNOWLEDGE_ROOT
        self._ensure_table()

    def _ensure_table(self) -> None:
        self.session.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5("
            "source, category, title, path, body)"
        ))
        self.session.commit()

    def count(self) -> int:
        row = self.session.execute(text("SELECT COUNT(*) FROM knowledge_fts")).first()
        return int(row[0]) if row else 0

    def rebuild(self, root: Path | None = None) -> int:
        root = root or self.knowledge_root
        entries = parse_knowledge_dir(root)
        self.session.execute(text("DELETE FROM knowledge_fts"))
        for e in entries:
            self.session.execute(text(
                "INSERT INTO knowledge_fts (source, category, title, path, body) "
                "VALUES (:s, :c, :t, :p, :b)"),
                {"s": e.source, "c": e.category, "t": e.title, "p": e.path, "b": e.body})
        self.session.commit()
        return len(entries)

    def search(self, query: str, limit: int = 10,
               category: str | None = None) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        if self.count() == 0 and Path(self.knowledge_root).exists():
            self.rebuild()
        params: dict = {"q": query, "limit": limit}
        cat_clause = ""
        if category:
            cat_clause = "AND category = :cat"
            params["cat"] = category
        sql = text(
            "SELECT source, category, title, path, "
            "snippet(knowledge_fts, 4, '[[', ']]', '…', 16) AS snip "
            "FROM knowledge_fts WHERE knowledge_fts MATCH :q "
            f"{cat_clause} ORDER BY bm25(knowledge_fts) LIMIT :limit"
        )
        try:
            rows = self.session.execute(sql, params).all()
        except Exception:
            sql = text(
                "SELECT source, category, title, path, "
                "substr(body, 1, 160) AS snip "
                f"FROM knowledge_fts WHERE body LIKE :like {cat_clause} LIMIT :limit"
            )
            rows = self.session.execute(sql, {**params, "like": f"%{query}%"}).all()
        return [{"source": r[0], "category": r[1], "title": r[2],
                 "path": r[3], "snippet": r[4]} for r in rows]
