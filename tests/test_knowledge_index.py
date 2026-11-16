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
    (root / "ctf-skills" / "ctf-pwn").mkdir(parents=True, exist_ok=True)
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
    assert isinstance(hits, list)


def test_no_match_returns_empty(tmp_path, session):
    root = _seed_knowledge(tmp_path)
    index = KnowledgeIndex(session)
    index.rebuild(root)
    assert index.search("zzznope") == []
