import base64
import json
import re
import struct
from types import SimpleNamespace

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlmodel import Session, SQLModel, create_engine

from app.agents.jury import JuryAgent
from app.agents.orchestrator import Orchestrator
from app.agents.recon import (
    extract_binary_intel,
    extract_crypto_intel,
    extract_forensics_intel,
    extract_web_intel,
    recon_bundle,
)
from app.database import configure_sqlite_engine
from app.models import Attachment, AttachmentType, Challenge, Event
from app.services.storage import StorageService
from app.tooling.config import ToolSettings, ToolSpec
from app.tooling.runner import ToolRunner


def _jwt_hs256_fake(payload: dict) -> str:
    h = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).decode().rstrip("=")
    p = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{h}.{p}.sig123"


def test_web_intel_extracts_comments_endpoints_jwt_robots_and_stack():
    tok = _jwt_hs256_fake({"user": "guest", "role": "viewer"})
    html_body = (
        '<html><body><!-- debug token: /internal/reset -->'
        '<form action="/login"><input type="hidden" name="csrfmiddlewaretoken" value="x"></form>'
        '<script src="/static/app.js"></script>'
        '<script>fetch("/api/v2/admin/users");</script>'
        '</body></html>'
    )

    def fake_fetch(url: str):
        if url.endswith("/robots.txt"):
            return {}, "User-agent: *\nDisallow: /admin-panel\n"
        if url.endswith("/sitemap.xml"):
            return {}, "<urlset><url><loc>http://target/help</loc></url></urlset>"
        return {"server": "Werkzeug/3.0 Python/3.12", "set-cookie": f"auth={tok}; Path=/"}, html_body

    info = extract_web_intel("http://ctf.local:5000", fetcher=fake_fetch)
    assert "Flask/Werkzeug" in info["stack"]
    assert "Django" in info["stack"]
    assert "/api/v2/admin/users" in info["endpoints"]
    assert "/admin-panel" in info["robots_paths"]
    assert info["comments"] and "debug token" in info["comments"][0]
    assert "csrfmiddlewaretoken" in info["hidden_inputs"]
    assert info["jwts"] and info["jwts"][0]["payload"]["role"] == "viewer"
    assert any("endpoints" in h.lower() or "/api/v2/admin/users" in h for h in info["hypotheses"])


def test_crypto_intel_rsa_nested_base64_and_hashes():
    layer1 = base64.b64encode(b"secret_key=alpha_beta_42").decode()
    layer2 = base64.b64encode(layer1.encode()).decode()
    blob = (
        f"n = 123456789012345678901\n"
        f"e = 3\n"
        f"c = 987654321\n"
        f"token = {layer2}\n"
        f"hash = {'a' * 64}\n"
    )
    info = extract_crypto_intel(blob)
    assert info["rsa_params"]["e"] == "3"
    assert info["rsa_params"]["n"].startswith("12345")
    assert any(h["type"] == "sha256" for h in info["hashes"])
    assert any("secret_key=alpha_beta_42" in d["decoded"] for d in info["decoded_blobs"])
    assert any("e=3" in h for h in info["hypotheses"])


def test_binary_intel_elf_header_and_dangerous_strings(tmp_path):
    # Minimal 64-bit LE ET_DYN (PIE) x86_64 ELF header
    hdr = bytearray(64)
    hdr[0:4] = b"\x7fELF"
    hdr[4] = 2  # 64-bit
    hdr[5] = 1  # little-endian
    struct.pack_into("<HH", hdr, 16, 3, 0x3E)  # ET_DYN, x86_64
    bin_path = tmp_path / "chal.elf"
    bin_path.write_bytes(bytes(hdr) + b"\x00" * 64)

    info = extract_binary_intel(bin_path, ["puts", "gets", "system", "/flag.txt", "AES_encrypt"])
    assert info["format"] == "ELF"
    assert info["arch"] == "x86_64"
    assert info["bits"] == 64
    assert info["pie"] is True
    assert "gets" in info["dangerous_apis"]
    assert "system" in info["dangerous_apis"]
    assert "/flag.txt" in info["paths_or_fmt"]


def test_forensics_intel_png_trailing_payload_and_pcap(tmp_path):
    # PNG with tEXt chunk + IEND + hidden payload
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + b"\x00\x00\x00\x00"
    txt_data = b"Author\x00hidden_hint"
    txt_chunk = struct.pack(">I", len(txt_data)) + b"tEXt" + txt_data + b"\x00\x00\x00\x00"
    iend = struct.pack(">I", 0) + b"IEND" + b"\xae\x42\x60\x82"
    png_path = tmp_path / "chal.png"
    png_path.write_bytes(b"\x89PNG\r\n\x1a\n" + ihdr + txt_chunk + iend + b"PK_SECRET_TRAILING")

    png_info = extract_forensics_intel(png_path, "PNG image")
    assert png_info["trailing_bytes"] == len(b"PK_SECRET_TRAILING")
    assert "PK_SECRET_TRAILING" in png_info["trailing_preview"]
    assert png_info["png_text"] and "Author=hidden_hint" in png_info["png_text"][0]

    # Minimal valid LE PCAP with 1 packet carrying an HTTP request
    global_hdr = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    payload = b"GET /secret-flag HTTP/1.1\r\nHost: koth.local\r\n"
    pkt_hdr = struct.pack("<IIII", 1, 0, len(payload), len(payload))
    pcap_path = tmp_path / "cap.pcap"
    pcap_path.write_bytes(global_hdr + pkt_hdr + payload)

    pcap_info = extract_forensics_intel(pcap_path, "PCAP capture (little-endian)")
    assert pcap_info["pcap_packets"] == 1
    assert any("GET /secret-flag HTTP/1.1" in a for a in pcap_info["pcap_artifacts"])


def test_recon_bundle_and_orchestrator_seed_blackboard(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'recon3.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    storage = StorageService(tmp_path / "storage")

    with Session(engine) as session:
        ev = Event(name="Recon3", flag_format=r"CTF\{.*\}")
        session.add(ev)
        session.commit()
        session.refresh(ev)
        ch = Challenge(event_id=ev.id, title="NotesRSA", category="crypto", points=200)
        session.add(ch)
        session.commit()
        session.refresh(ch)

        saved = storage.save(ch.id, "params.txt", b"n = 999999\ne = 3\nc = 12345\n")
        session.add(Attachment(
            challenge_id=ch.id,
            name="params.txt",
            path=str(saved.relative_to(storage.storage_dir)),
            mime="text/plain",
            size=saved.stat().st_size,
            type=AttachmentType.download,
        ))
        session.commit()

        runner = ToolRunner(ToolSettings(tools={"strings": ToolSpec(mode="native", guard="none")}), storage)
        # Stub strings output cleanly without relying on host binary
        runner.run_raw = lambda _c, tool, args, **kw: SimpleNamespace(stdout="n = 999999\ne = 3\n", stderr="", returncode=0)

        digest = recon_bundle(runner, session, ch)
        assert digest.structured["crypto"]
        assert any("e=3" in h for h in digest.hypotheses)

        # Verify Orchestrator._run_recon seeds Blackboard active_hypotheses cleanly
        def fn(messages, info):
            blob = "\n".join(str(p) for m in messages for p in (m.parts or []))
            match = re.search(r"['\"](\w*result\w*)['\"]", blob)
            name = match.group(1) if match else "final_result"
            return ModelResponse(parts=[ToolCallPart(
                tool_name=name,
                args=json.dumps({"action": "WAIT_USER", "reason": "ok", "findings": []}),
            )])

        orch = Orchestrator(runner=runner, jury=JuryAgent(FunctionModel(fn), "jury"), specialists={})
        sess = orch.start(session, ch.id, mode="supervised")
        assert any("e=3" in h for h in sess.blackboard.active_hypotheses)
