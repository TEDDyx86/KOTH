# Knowledge Subsystem Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Vendor `ljagiello/ctf-skills` and `Felipebc2/Dispatch` into `knowledge/` pinned by `knowledge.lock`, and expose their content (Arsenal entries + skills) through an FTS5-backed search service and REST endpoints.

**Architecture:** `app/knowledge/` package: frontmatter parser, FTS5 index manager (SQLite virtual table in the main DB), auto-rebuild-on-first-search. `scripts/sync-knowledge.py` clones the two repos shallow and pins commits. REST endpoints in `app/api/knowledge.py`. No UI, no MCP in this phase.

**Tech Stack:** Python 3.12+, git CLI (subprocess), PyYAML, SQLite FTS5, existing FastAPI + SQLModel stack.

## Global Constraints

- Vendoring is **manual**: content changes only when `scripts/sync-knowledge.py` is run. Nothing auto-updates.
- The FTS index lives in the main SQLite DB (`knowledge_fts` virtual table), rebuilt wholesale.
- `search()` must never crash on FTS5 syntax garbage — fallback to LIKE.
- Parser is schema-agnostic: any YAML frontmatter (Arsenal entries, SKILL.md) is stored generically (`title`, `category`, full body indexed).
- Tests run with `python -m pytest tests/ -v`.

---

### Task 1: sync-knowledge.py script + real vendoring

**Files:**
- Create: `scripts/sync-knowledge.py`
- Create (generated): `knowledge/ctf-skills/`, `knowledge/dispatch/`, `knowledge.lock`

**Interfaces:**
- Produces: `sync(repo: str, dest: Path, ref: str = "main") -> dict` (returns `{repo, ref, commit, path}`); CLI `python scripts/sync-knowledge.py [knowledge_dir]` syncing the two default repos and writing `knowledge.lock` (JSON list).

- [ ] **Step 1:** Failing test (`tests/test_sync_knowledge.py`):

```python
import subprocess
from pathlib import Path

from scripts.sync_knowledge import sync


def _make_repo(path: Path, files: dict[str, str]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True, capture_output=True)
    for name, content in files.items():
        p = path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)


def test_sync_local_repo_and_lock(tmp_path):
    src = tmp_path / "src"
    _make_repo(src, {"Arsenal/crypto/rsa.md": "---\ntitle: rsa\n---\nbody", "SKILL.md": "# skill"})
    dest = tmp_path / "knowledge"
    info = sync(str(src), dest / "fake-repo")
    assert (dest / "fake-repo" / "Arsenal" / "crypto" / "rsa.md").exists()
    assert info["commit"] and len(info["commit"]) == 40
```

Run → FAIL (`No module named scripts.sync_knowledge`). Note: `scripts/` needs `__init__.py` for import (`scripts/__init__.py` empty).
- [ ] **Step 2:** Implement `scripts/sync-knowledge.py`:

```python
"""Vendor knowledge repos into knowledge/ and pin them in knowledge.lock."""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_REPOS = [
    ("https://github.com/ljagiello/ctf-skills", "ctf-skills", "main"),
    ("https://github.com/Felipebc2/Dispatch", "dispatch", "main"),
]
BASE_DIR = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = BASE_DIR / "knowledge"


def sync(repo: str, dest: Path, ref: str = "main") -> dict:
    dest = Path(dest)
    if dest.exists():
        subprocess.run(["git", "-C", str(dest), "fetch", "--depth", "1", "origin", ref],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(dest), "checkout", "FETCH_HEAD"],
                       check=True, capture_output=True)
    else:
        subprocess.run(["git", "clone", "--depth", "1", "--branch", ref, repo, str(dest)],
                       check=True, capture_output=True)
    commit = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"],
                            check=True, capture_output=True, text=True).stdout.strip()
    return {"repo": repo, "ref": ref, "commit": commit, "path": str(dest)}


def main() -> int:
    knowledge_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else KNOWLEDGE_DIR
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    lock = []
    for repo, name, ref in DEFAULT_REPOS:
        print(f"syncing {repo} -> {knowledge_dir / name}")
        lock.append(sync(repo, knowledge_dir / name, ref))
    lock_path = knowledge_dir / "knowledge.lock"
    lock_path.write_text(json.dumps({"synced_at": datetime.now(timezone.utc).isoformat(),
                                     "repos": lock}, indent=2), encoding="utf-8")
    print(f"wrote {lock_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3:** Run tests → PASS. Then run the REAL sync: `python scripts/sync-knowledge.py` → verify `knowledge/ctf-skills` and `knowledge/dispatch` exist and `knowledge.lock` written.
- [ ] **Step 4:** Commit: `feat: knowledge vendoring sync script with lock file` (include knowledge/ content and knowledge.lock).

---

### Task 2: Frontmatter parser

**Files:**
- Create: `app/knowledge/__init__.py` (empty), `app/knowledge/parse.py`
- Test: `tests/test_knowledge_parse.py`

**Interfaces:**
- Produces: `Entry` dataclass (`source: str, category: str, title: str, path: str, body: str`); `parse_knowledge_dir(root: Path) -> list[Entry]` — walks `root` for: `Arsenal/<CATEGORY>/*.md` (source="dispatch-arsenal", category=CATEGORY) and `**/SKILL.md` (source="skill", category=parent dir name). Title = frontmatter `title` or first `# heading` or filename stem. Body = frontmatter YAML block + markdown body.

- [ ] **Step 1:** Failing test:

```python
# tests/test_knowledge_parse.py
from app.knowledge.parse import parse_knowledge_dir


def _write(root, rel, content):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_parses_arsenal_and_skills(tmp_path):
    root = tmp_path / "knowledge"
    _write(root, "dispatch/Arsenal/crypto/rsa-small-e.md",
           "---\ntitle: RSA small e\ndescription: cube root attack\n---\nMethod: e=3 cube root.")
    _write(root, "ctf-skills/ctf-crypto/SKILL.md",
           "# ctf-crypto\n\nRSA, AES, ECC techniques.")
    entries = parse_knowledge_dir(root)
    arsenal = [e for e in entries if e.source == "dispatch-arsenal"]
    skills = [e for e in entries if e.source == "skill"]
    assert len(arsenal) == 1 and len(skills) == 1
    assert arsenal[0].category == "crypto"
    assert arsenal[0].title == "RSA small e"
    assert "cube root" in arsenal[0].body
    assert skills[0].category == "ctf-crypto"
    assert skills[0].title == "ctf-crypto"


def test_heading_fallback_title(tmp_path):
    root = tmp_path / "knowledge"
    _write(root, "dispatch/Arsenal/pwn/no-frontmatter.md",
           "# Format String Overwrite\n\nUse %n.")
    entries = [e for e in parse_knowledge_dir(root) if e.source == "dispatch-arsenal"]
    assert entries[0].title == "Format String Overwrite"


def test_ignores_non_md_and_other_dirs(tmp_path):
    root = tmp_path / "knowledge"
    _write(root, "dispatch/docs/readme.txt", "not markdown")
    _write(root, "ctf-skills/scripts/tool.py", "print(1)")
    assert parse_knowledge_dir(root) == []
```

- [ ] **Step 2:** Implement `app/knowledge/parse.py`:

```python
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


@dataclass
class Entry:
    source: str
    category: str
    title: str
    path: str
    body: str


def _split_frontmatter(text: str) -> tuple[dict, str]:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return data, text[match.end():]


def _title(md_path: Path, meta: dict, body: str) -> str:
    if isinstance(meta.get("title"), str) and meta["title"].strip():
        return meta["title"].strip()
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return md_path.stem


def parse_knowledge_dir(root: Path) -> list[Entry]:
    root = Path(root)
    entries: list[Entry] = []
    if not root.exists():
        return entries
    for md_path in sorted(root.rglob("*.md")):
        rel = md_path.relative_to(root)
        parts = rel.parts
        if "SKILL.md" in parts:
            source, category = "skill", parts[-2] if len(parts) >= 2 else ""
        elif "Arsenal" in parts:
            idx = parts.index("Arsenal")
            category = parts[idx + 1] if len(parts) > idx + 1 else ""
            source = "dispatch-arsenal"
        else:
            continue
        text = md_path.read_text(encoding="utf-8", errors="replace")
        meta, body = _split_frontmatter(text)
        entries.append(Entry(source=source, category=category,
                             title=_title(md_path, meta, body), path=str(rel),
                             body=(str(meta) if meta else "") + "\n" + body))
    return entries
```

- [ ] **Step 3:** Tests → PASS. Commit: `feat: knowledge frontmatter parser`.

---

### Task 3: FTS5 index + search service

**Files:**
- Create: `app/knowledge/index.py`
- Test: `tests/test_knowledge_index.py`

**Interfaces:**
- Produces: `KnowledgeIndex(session: Session)` with `rebuild(root: Path | None = None) -> int` (count indexed), `search(query: str, limit: int = 10, category: str | None = None) -> list[dict]` (keys: source, category, title, path, snippet), `count() -> int`. Index auto-rebuilds on first search when count == 0 and knowledge dir exists. FTS garbage query → LIKE fallback.

- [ ] **Step 1:** Failing test:

```python
# tests/test_knowledge_index.py
import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.knowledge.index import KnowledgeIndex


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed_knowledge(tmp_path):
    root = tmp_path / "knowledge"
    (root / "dispatch" / "Arsenal" / "crypto").mkdir(parents=True, exist_ok=True)
    (root / "dispatch" / "Arsenal" / "crypto" / "rsa-small-e.md").write_text(
        "---\ntitle: RSA small e\n---\nCube root attack for e=3.", encoding="utf-8")
    (root / "ctf-skills" / "ctf-pwn" / "SKILL.md").write_text(
        "# ctf-pwn\nROP chains and heap exploitation.", encoding="utf-8")
    return root


def test_rebuild_and_search(tmp_path, session):
    root = _seed_knowledge(tmp_path)
    index = KnowledgeIndex(session)
    n = index.rebuild(root)
    assert n == 2
    hits = index.search("rsa")
    assert hits and hits[0]["title"] == "RSA small e"
    assert "RSA" in hits[0]["snippet"] or "rsa" in hits[0]["snippet"].lower()


def test_auto_rebuild_on_first_search(tmp_path, session):
    root = _seed_knowledge(tmp_path)
    index = KnowledgeIndex(session, knowledge_root=root)
    hits = index.search("ROP")
    assert hits and hits[0]["category"] == "ctf-pwn"


def test_garbage_query_falls_back(tmp_path, session):
    root = _seed_knowledge(tmp_path)
    index = KnowledgeIndex(session)
    index.rebuild(root)
    hits = index.search('("AND OR *')
    assert isinstance(hits, list)  # must not raise


def test_no_match_returns_empty(tmp_path, session):
    root = _seed_knowledge(tmp_path)
    index = KnowledgeIndex(session)
    index.rebuild(root)
    assert index.search("zzznope") == []
```

- [ ] **Step 2:** Implement `app/knowledge/index.py`:

```python
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
        self.session.exec(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5("
            "source, category, title, path, body)"
        ))
        self.session.commit()

    def count(self) -> int:
        row = self.session.exec(text("SELECT COUNT(*) FROM knowledge_fts")).first()
        return int(row[0]) if row else 0

    def rebuild(self, root: Path | None = None) -> int:
        root = root or self.knowledge_root
        entries = parse_knowledge_dir(root)
        self.session.exec(text("DELETE FROM knowledge_fts"))
        for e in entries:
            self.session.exec(text(
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
        if self.count() == 0 and self.knowledge_root.exists():
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
            rows = self.session.exec(sql, params).all()
        except Exception:
            sql = text(
                "SELECT source, category, title, path, "
                "substr(body, 1, 160) AS snip "
                f"FROM knowledge_fts WHERE body LIKE :like {cat_clause} LIMIT :limit"
            )
            rows = self.session.exec(sql, {**params, "like": f"%{query}%"}).all()
        return [{"source": r[0], "category": r[1], "title": r[2],
                 "path": r[3], "snippet": r[4]} for r in rows]
```

- [ ] **Step 3:** Tests → PASS. Commit: `feat: FTS5 knowledge index with auto-rebuild and LIKE fallback`.

---

### Task 4: REST endpoints

**Files:**
- Create: `app/api/knowledge.py`
- Modify: `app/main.py` (include router)
- Test: `tests/test_api_knowledge.py`

**Interfaces:**
- Produces:
  - `POST /api/knowledge/rebuild` → `{"indexed": n}` (root = knowledge dir)
  - `GET /api/knowledge/search?q=...&limit=&category=` → list of hit dicts; empty q → 422 handled by FastAPI (required param)
  - `GET /api/knowledge/skills` → list of `{category, title, path}` for source="skill"
  - Uses per-request session `Depends(get_session)`; index built on the default engine in the real app (test client uses the overridden engine via conftest).

- [ ] **Step 1:** Failing test:

```python
# tests/test_api_knowledge.py
def _seed(tmp_path, monkeypatch):
    import app.knowledge.index as idx_mod
    root = tmp_path / "knowledge"
    (root / "dispatch" / "Arsenal" / "web").mkdir(parents=True, exist_ok=True)
    (root / "dispatch" / "Arsenal" / "web" / "ssti-jinja.md").write_text(
        "---\ntitle: SSTI Jinja2\n---\n{{7*7}} detection and RCE.", encoding="utf-8")
    (root / "ctf-skills" / "ctf-web" / "SKILL.md").write_text(
        "# ctf-web\nSQLi, XSS, SSRF.", encoding="utf-8")
    monkeypatch.setattr(idx_mod, "DEFAULT_KNOWLEDGE_ROOT", root)
    return root


def test_rebuild_and_search(client, tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    r = client.post("/api/knowledge/rebuild")
    assert r.status_code == 200 and r.json()["indexed"] == 2
    s = client.get("/api/knowledge/search", params={"q": "SSTI"})
    assert s.status_code == 200
    hits = s.json()
    assert hits[0]["title"] == "SSTI Jinja2"
    assert hits[0]["source"] == "dispatch-arsenal"


def test_skills_listing(client, tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    client.post("/api/knowledge/rebuild")
    r = client.get("/api/knowledge/skills")
    assert r.status_code == 200
    cats = [s["category"] for s in r.json()]
    assert cats == ["ctf-web"]
```

Note: `_seed` monkeypatches the module attribute — for the client fixture's engine (per-test tmp DB) the KnowledgeIndex default root must point at the seeded dir, so `app/api/knowledge.py` must read `DEFAULT_KNOWLEDGE_ROOT` **at call time** (import inside handler: `from ..knowledge.index import KnowledgeIndex, DEFAULT_KNOWLEDGE_ROOT`).

- [ ] **Step 2:** Implement `app/api/knowledge.py`:

```python
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlmodel import Session

from ..database import get_session

router = APIRouter()


class RebuildOut(BaseModel):
    indexed: int


@router.post("/rebuild", response_model=RebuildOut)
def rebuild(session: Session = Depends(get_session)):
    from ..knowledge.index import KnowledgeIndex, DEFAULT_KNOWLEDGE_ROOT
    n = KnowledgeIndex(session).rebuild(DEFAULT_KNOWLEDGE_ROOT)
    return {"indexed": n}


@router.get("/search")
def search(q: str = Query(...), limit: int = Query(10, ge=1, le=50),
           category: str | None = None,
           session: Session = Depends(get_session)):
    from ..knowledge.index import KnowledgeIndex, DEFAULT_KNOWLEDGE_ROOT
    return KnowledgeIndex(session, knowledge_root=DEFAULT_KNOWLEDGE_ROOT).search(
        q, limit=limit, category=category)


@router.get("/skills")
def skills(session: Session = Depends(get_session)):
    from ..knowledge.index import KnowledgeIndex, DEFAULT_KNOWLEDGE_ROOT
    index = KnowledgeIndex(session, knowledge_root=DEFAULT_KNOWLEDGE_ROOT)
    hits = index.search("", limit=1)  # ensures auto-rebuild if empty
    if not hits and index.count() == 0:
        index.rebuild()
    rows = session.exec(__import__("sqlalchemy", fromlist=["text"]).text(
        "SELECT category, title, path FROM knowledge_fts WHERE source = 'skill' "
        "ORDER BY category")).all()
    return [{"category": r[0], "title": r[1], "path": r[2]} for r in rows]
```

Simplify that last query in implementation as:

```python
    from sqlalchemy import text
    rows = session.exec(text(
        "SELECT category, title, path FROM knowledge_fts WHERE source = 'skill' "
        "ORDER BY category")).all()
```

- [ ] **Step 3:** Modify `app/main.py` — add include:

```python
    from .api import knowledge as knowledge_module
    app.include_router(knowledge_module.router, prefix="/api/knowledge", tags=["knowledge"])
```

- [ ] **Step 4:** Full suite → PASS. Commit: `feat: knowledge REST endpoints (rebuild, search, skills)`.

---

### Task 5: CLI search convenience + final verification

**Files:**
- Modify: `app/main.py` (nothing), Create: `scripts/knowledge-search.py` (thin CLI)

**Interfaces:**
- Produces: `python scripts/knowledge-search.py "query"` → prints hits (title/path/snippet) using a real DB session from `app.database`.

- [ ] **Step 1:** Implement:

```python
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
            print("(index empty — rebuilding...)")
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
```

- [ ] **Step 2:** Run against the REAL vendored content:

```
python scripts/knowledge-search.py "RSA small e"
python scripts/knowledge-search.py "format string"
```

Expected: hits from dispatch-arsenal. Then full suite `python -m pytest tests/ -v` → all PASS.
- [ ] **Step 3:** Commit: `feat: knowledge search CLI`.

---

## Self-Review

- Spec §8 coverage: vendoring script + knowledge.lock (T1), parser schema-agnostic (T2), FTS5 index + knowledge_search (T3), REST endpoints (T4), no UI/MCP (per user decision). Wordlists (PAT/SecLists) remain future work — documented in spec as part of §8 but deferred with the tool runner integration later.
- No placeholders; `KnowledgeIndex` signature consistent across T3/T4/T5 (session, knowledge_root kwarg); monkeypatch pattern requires late import of DEFAULT_KNOWLEDGE_ROOT in api handlers (documented in T4).
