def _challenge(client):
    event = client.post("/api/events", json={"name": "e"}).json()
    return client.post(f"/api/events/{event['id']}/challenges", json={"title": "c1"}).json()


def test_post_attempt(client):
    ch = _challenge(client)
    r = client.post(f"/api/challenges/{ch['id']}/attempts",
                    json={"type": "tool", "description": "ran nmap", "result": "breakthrough",
                          "tool_command": "nmap -p 1337 chal"})
    assert r.status_code == 200
    assert r.json()["result"] == "breakthrough"
    detail = client.get(f"/api/challenges/{ch['id']}").json()
    assert detail["summary"]["attempt_count"] == 1


def test_post_attempt_unknown_challenge_404(client):
    r = client.post("/api/challenges/999/attempts",
                    json={"type": "manual", "description": "x", "result": "failed"})
    assert r.status_code == 404


def test_notes_versions(client):
    ch = _challenge(client)
    client.post(f"/api/challenges/{ch['id']}/notes", json={"content": "v1"})
    client.post(f"/api/challenges/{ch['id']}/notes", json={"content": "v2"})
    notes = client.get(f"/api/challenges/{ch['id']}/notes").json()
    assert [n["content"] for n in notes] == ["v2", "v1"]


def test_attachment_upload(client):
    ch = _challenge(client)
    r = client.post(f"/api/challenges/{ch['id']}/attachments",
                    files={"file": ("shot.png", b"\x89PNG fake", "image/png")},
                    data={"type": "screenshot"})
    assert r.status_code == 200
    assert r.json()["type"] == "screenshot"
    lst = client.get(f"/api/challenges/{ch['id']}/attachments").json()
    assert len(lst) == 1
    assert lst[0]["size"] == len(b"\x89PNG fake")
