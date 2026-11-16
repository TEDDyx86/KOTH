def _seed(tmp_path, monkeypatch):
    import app.knowledge.index as idx_mod
    root = tmp_path / "knowledge"
    (root / "dispatch" / "Arsenal" / "web").mkdir(parents=True, exist_ok=True)
    (root / "dispatch" / "Arsenal" / "web" / "ssti-jinja.md").write_text(
        "---\ntitle: SSTI Jinja2\n---\n{{7*7}} detection and RCE.", encoding="utf-8")
    (root / "ctf-skills" / "ctf-web").mkdir(parents=True, exist_ok=True)
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


def test_search_requires_query(client):
    r = client.get("/api/knowledge/search")
    assert r.status_code == 422
