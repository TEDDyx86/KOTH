def test_dashboard_renders(client):
    client.post("/api/events", json={"name": "picoCTF"})
    client.post("/api/events/1/challenges", json={"title": "web1", "category": "web", "points": 100})
    r = client.get("/")
    assert r.status_code == 200
    assert "picoCTF" in r.text
    assert "web1" in r.text
