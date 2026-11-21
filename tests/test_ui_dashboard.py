def test_dashboard_renders(client):
    client.post("/api/events", json={"name": "picoCTF"})
    client.post("/api/events/1/challenges", json={"title": "web1", "category": "web", "points": 100})
    r = client.get("/")
    assert r.status_code == 200
    assert "picoCTF" in r.text
    assert "web1" in r.text


def test_dashboard_arena_stats_and_feed(client):
    ev = client.post("/api/events", json={"name": "SummitCTF", "flag_format": r"CTF\{.*\}"}).json()
    ch1 = client.post(
        f"/api/events/{ev['id']}/challenges",
        json={"title": "rsa-baby", "category": "crypto", "points": 250, "target": "10.0.0.2:1337"},
    ).json()
    ch2 = client.post(
        f"/api/events/{ev['id']}/challenges",
        json={"title": "sqli-login", "category": "web", "points": 300},
    ).json()
    client.patch(f"/api/challenges/{ch2['id']}", json={"status": "solved"})
    client.post(
        f"/api/challenges/{ch1['id']}/attempts",
        json={"type": "tool", "description": "nmap top ports", "result": "breakthrough"},
    )

    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "1/2" in r.text
    assert "300" in r.text
    assert "rsa-baby" in r.text
    assert "nmap top ports" in r.text
    assert "breakthrough" in r.text
    assert "Challenges abertos" in r.text
    assert "Novo challenge" in r.text
