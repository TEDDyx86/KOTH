"""CLI: search the vendored knowledge. Usage: python scripts/knowledge-search.py <query>"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session

from app.database import default_engine
from app.knowledge.index import KnowledgeIndex


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python scripts/knowledge-search.py <query>")
        return 2
    query = " ".join(sys.argv[1:])
    with Session(default_engine()) as session:
        index = KnowledgeIndex(session)
        if index.count() == 0:
            print("(index empty - rebuilding...)")
            index.rebuild()
        hits = index.search(query)
        for h in hits:
            print(f"[{h['source']}/{h['category']}] {h['title']}  ({h['path']})")
            print(f"    {h['snippet']}")
        if not hits:
            print("no hits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
