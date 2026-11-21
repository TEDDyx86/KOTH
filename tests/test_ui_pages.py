import io


def _seed(client):
    event = client.post("/api/events", json={"name": "HTB"}).json()
    ch = client.post(f"/api/events/{event['id']}/challenges",
                     json={"title": "git_dumper", "category": "web", "points": 250,
                           "description": "Find the source", "target": "10.10.10.1:80",
                           "tags": "git,ssti"}).json()
    return event, ch


def test_event_page_renders_grid(client):
    event, ch = _seed(client)
    r = client.get(f"/events/{event['id']}")
    assert r.status_code == 200
    assert "git_dumper" in r.text


def test_event_page_filters(client):
    event, ch = _seed(client)
    client.post(f"/api/events/{event['id']}/challenges", json={"title": "pwn1", "category": "pwn"})
    r = client.get(f"/events/{event['id']}", params={"category": "pwn"})
    assert "pwn1" in r.text
    assert "git_dumper" not in r.text


def test_challenge_page_renders_all_sections(client):
    event, ch = _seed(client)
    client.post(f"/api/challenges/{ch['id']}/attempts",
                json={"type": "manual", "description": "ran git-dumper", "result": "breakthrough"})
    client.post(f"/api/challenges/{ch['id']}/notes", json={"content": "v1 note"})
    client.post(f"/api/challenges/{ch['id']}/attachments",
                files={"file": ("a.txt", io.BytesIO(b"hello"), "text/plain")},
                data={"type": "download"})
    r = client.get(f"/challenges/{ch['id']}")
    body = r.text
    assert "git_dumper" in body
    assert "ran git-dumper" in body
    assert "v1 note" in body
    assert "a.txt" in body
    assert "10.10.10.1:80" in body


def test_manual_attempt_form_flow(client):
    event, ch = _seed(client)
    r = client.post(f"/challenges/{ch['id']}/attempts",
                    data={"description": "tried dirsearch", "result": "failed",
                          "learning": "no dir listing"}, follow_redirects=False)
    assert r.status_code == 303
    detail = client.get(f"/api/challenges/{ch['id']}").json()
    assert detail["summary"]["attempt_count"] == 1


def test_note_form_flow(client):
    event, ch = _seed(client)
    r = client.post(f"/challenges/{ch['id']}/notes", data={"content": "my notes"}, follow_redirects=False)
    assert r.status_code == 303
    notes = client.get(f"/api/challenges/{ch['id']}/notes").json()
    assert notes[0]["content"] == "my notes"


def test_quick_challenge_flow_with_multifile_and_redirect_to_agent(client):
    r = client.post(
        "/challenges/quick",
        data={
            "event_id": "",
            "new_event_name": "IDP CTF",
            "title": "double_wordle",
            "category": "crypto",
            "points": "500",
            "difficulty": "hard",
            "target": "",
            "tags": "rng",
            "description": "crack the seed",
            "go_to": "agent",
        },
        files=[
            ("files", ("a.txt", io.BytesIO(b"part1"), "text/plain")),
            ("files", ("b.bin", io.BytesIO(b"\x01\x02"), "application/octet-stream")),
        ],
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].endswith("/agent")
    ch_id = int(r.headers["location"].split("/")[2])

    atts = client.get(f"/api/challenges/{ch_id}/attachments").json()
    assert [a["name"] for a in atts] == ["a.txt", "b.bin"]

    # Edit challenge from UI
    r_edit = client.post(
        f"/challenges/{ch_id}/edit",
        data={
            "title": "double_wordle_v2",
            "category": "forensics",
            "points": "600",
            "difficulty": "insane",
            "target": "10.10.10.9:1337",
            "tags": "rng,prng",
            "description": "updated desc",
        },
        follow_redirects=False,
    )
    assert r_edit.status_code == 303
    page = client.get(f"/challenges/{ch_id}").text
    assert "double_wordle_v2" in page
    assert "10.10.10.9:1337" in page


def test_event_create_with_flag_format_and_planner_sort(client):
    r = client.post(
        "/events",
        data={"name": "IDP 03", "flag_format": r"IDP\{.*\}"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    ev_url = r.headers["location"]
    ev_id = int(ev_url.split("/")[-1])

    client.post(f"/api/events/{ev_id}/challenges", json={"title": "low", "points": 50, "difficulty": "easy"})
    client.post(f"/api/events/{ev_id}/challenges", json={"title": "high", "points": 500, "difficulty": "hard"})

    page = client.get(ev_url, params={"sort": "planner"}).text
    assert page.index("high") < page.index("low")
    assert r"IDP\{.*\}" in page
