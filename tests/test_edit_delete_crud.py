import io


def test_api_edit_and_delete_event_and_challenge_cascade(client, tmp_path):
    ev = client.post("/api/events", json={"name": "OldEvent", "flag_format": r"CTF\{.*\}"}).json()
    eid = ev["id"]

    r_up_ev = client.patch(
        f"/api/events/{eid}",
        json={"name": "NewEvent", "url": "https://ctf.local", "flag_format": r"KOTH\{.*\}", "status": "active"},
    )
    assert r_up_ev.status_code == 200
    assert r_up_ev.json()["name"] == "NewEvent"
    assert r_up_ev.json()["status"] == "active"

    ch = client.post(
        f"/api/events/{eid}/challenges",
        json={"title": "pwn1", "category": "pwn", "points": 100},
    ).json()
    cid = ch["id"]

    # Add attempt, note, and attachment
    client.post(f"/api/challenges/{cid}/attempts", json={"type": "manual", "description": "strings", "result": "partial"})
    client.post(f"/api/challenges/{cid}/notes", json={"content": "check stack canary"})
    client.post(
        f"/api/challenges/{cid}/attachments",
        data={"type": "download"},
        files=[("file", ("bin.elf", io.BytesIO(b"\x7fELF"), "application/octet-stream"))],
    )

    r_up_ch = client.patch(
        f"/api/challenges/{cid}",
        json={"title": "pwn1-v2", "points": 250, "target": "10.0.0.8:9000", "tags": "bof,rop", "status": "solved", "flag": "KOTH{ok}"},
    )
    assert r_up_ch.status_code == 200
    data = r_up_ch.json()
    assert data["title"] == "pwn1-v2"
    assert data["points"] == 250
    assert data["status"] == "solved"
    assert data["flag"] == "KOTH{ok}"

    # Delete challenge cleanly
    r_del_ch = client.delete(f"/api/challenges/{cid}")
    assert r_del_ch.status_code == 200
    assert r_del_ch.json()["deleted"] is True
    assert client.get(f"/api/challenges/{cid}").status_code == 404
    assert client.delete(f"/api/challenges/{cid}").status_code == 404

    # Create a second challenge, then delete entire event (cascade)
    ch2 = client.post(
        f"/api/events/{eid}/challenges",
        json={"title": "web2", "category": "web", "points": 150},
    ).json()
    client.post(f"/api/challenges/{ch2['id']}/notes", json={"content": "note2"})

    r_del_ev = client.delete(f"/api/events/{eid}")
    assert r_del_ev.status_code == 200
    assert client.get(f"/api/challenges/{ch2['id']}").status_code == 404
    assert client.delete(f"/api/events/{eid}").status_code == 404


def test_web_edit_and_delete_event_and_challenge(client):
    ev = client.post("/api/events", json={"name": "WebEditCTF"}).json()
    eid = ev["id"]
    ch = client.post(
        f"/api/events/{eid}/challenges",
        json={"title": "crypto1", "category": "crypto", "points": 100, "difficulty": "easy"},
    ).json()
    cid = ch["id"]

    # Check UI renders edit & delete buttons
    assert "Editar evento" in client.get(f"/events/{eid}").text
    assert "Apagar evento" in client.get(f"/events/{eid}").text
    assert "Apagar" in client.get(f"/challenges/{cid}").text
    assert "Editar" in client.get("/dashboard").text

    # Edit event via form
    r_ev = client.post(
        f"/events/{eid}/edit",
        data={"name": "WebEditCTF-Updated", "url": "https://koth.test", "flag_format": r"FLAG\{.*\}", "status": "active"},
        follow_redirects=False,
    )
    assert r_ev.status_code == 303
    assert "WebEditCTF-Updated" in client.get(f"/events/{eid}").text

    # Edit challenge via form (clear difficulty, update status & flag)
    r_ch = client.post(
        f"/challenges/{cid}/edit",
        data={
            "title": "crypto1-fixed",
            "category": "reverse",
            "points": "400",
            "difficulty": "",
            "status": "solved",
            "flag": "FLAG{done}",
            "target": "127.0.0.1:4444",
            "tags": "rsa,z3",
            "description": "updated desc",
        },
        follow_redirects=False,
    )
    assert r_ch.status_code == 303
    detail = client.get(f"/api/challenges/{cid}").json()["challenge"]
    assert detail["title"] == "crypto1-fixed"
    assert detail["category"] == "reverse"
    assert detail["difficulty"] is None
    assert detail["status"] == "solved"
    assert detail["flag"] == "FLAG{done}"

    # Delete challenge from dashboard form with safe redirect & reject unsafe //evil.com
    r_del_c = client.post(
        f"/challenges/{cid}/delete",
        data={"next": "//evil.com"},
        follow_redirects=False,
    )
    assert r_del_c.status_code == 303
    assert r_del_c.headers["location"] == f"/events/{eid}"
    assert client.get(f"/api/challenges/{cid}").status_code == 404

    # Delete event via form
    r_del_e = client.post(
        f"/events/{eid}/delete",
        data={"next": "/dashboard"},
        follow_redirects=False,
    )
    assert r_del_e.status_code == 303
    assert r_del_e.headers["location"] == "/dashboard"
    assert client.get("/api/events").json() == []
