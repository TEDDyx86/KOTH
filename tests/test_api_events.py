def test_create_and_list_event(client):
    r = client.post("/api/events", json={"name": "picoCTF", "url": "https://picoctf.org"})
    assert r.status_code == 200
    assert r.json()["name"] == "picoCTF"
    lst = client.get("/api/events").json()
    assert len(lst) == 1


def test_patch_event_status(client):
    event = client.post("/api/events", json={"name": "e"}).json()
    r = client.patch(f"/api/events/{event['id']}/status", json={"status": "active"})
    assert r.status_code == 200
    assert r.json()["status"] == "active"


def test_patch_event_status_unknown_returns_404(client):
    r = client.patch("/api/events/999/status", json={"status": "active"})
    assert r.status_code == 404
