def _make_event(client):
    return client.post("/api/events", json={"name": "e"}).json()


def test_create_and_filter_challenges(client):
    event = _make_event(client)
    r = client.post(f"/api/events/{event['id']}/challenges",
                    json={"title": "web1", "category": "web", "points": 100, "tags": "ssti"})
    assert r.status_code == 200
    assert r.json()["category"] == "web"
    filtered = client.get(f"/api/events/{event['id']}/challenges", params={"category": "web"}).json()
    assert len(filtered) == 1


def test_create_challenge_unknown_event_404(client):
    r = client.post("/api/events/999/challenges", json={"title": "x"})
    assert r.status_code == 404


def test_challenge_detail_includes_summary(client):
    event = _make_event(client)
    ch = client.post(f"/api/events/{event['id']}/challenges", json={"title": "c1"}).json()
    client.post(f"/api/challenges/{ch['id']}/attempts",
                json={"type": "manual", "description": "tried x", "result": "failed"})
    detail = client.get(f"/api/challenges/{ch['id']}").json()
    assert detail["summary"]["attempt_count"] == 1
    assert len(detail["attempts"]) == 1


def test_patch_challenge_solves(client):
    event = _make_event(client)
    ch = client.post(f"/api/events/{event['id']}/challenges", json={"title": "c1"}).json()
    r = client.patch(f"/api/challenges/{ch['id']}", json={"status": "solved", "flag": "flag{ok}"})
    assert r.json()["status"] == "solved"
    assert r.json()["flag"] == "flag{ok}"
    assert r.json()["solved_at"] is not None


def test_patch_challenge_unknown_404(client):
    r = client.patch("/api/challenges/999", json={"flag": "f"})
    assert r.status_code == 404
