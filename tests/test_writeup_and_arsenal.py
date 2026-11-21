import io
import json
import re
import zipfile

from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlmodel import Session, SQLModel, create_engine

from app.agents.jury import JuryAgent
from app.agents.orchestrator import Orchestrator
from app.agents.specialists import SpecialistRunner
from app.knowledge.index import KnowledgeIndex
from app.main import create_app
from app.models import Attachment, Challenge, Event
from app.services import tracking
from app.services.storage import StorageService
from app.tooling.config import ToolSettings, ToolSpec
from app.tooling.runner import ToolRunner


def _scripted(payloads: list[dict]):
    state = {"n": 0}

    def fn(messages, info):
        idx = state["n"]
        state["n"] += 1
        payload = payloads[min(idx, len(payloads) - 1)]
        if "tool_name" in payload:
            return ModelResponse(parts=[
                ToolCallPart(tool_name=payload["tool_name"], args=json.dumps(payload["tool_args"]))
            ])
        blob = "\n".join(str(p) for m in messages for p in (m.parts or []))
        match = re.search(r"['\"](\w*result\w*)['\"]", blob)
        name = match.group(1) if match else "final_result"
        return ModelResponse(parts=[ToolCallPart(tool_name=name, args=json.dumps(payload))])

    return FunctionModel(fn)


def test_orchestrator_auto_generates_writeup_and_exports_zip_and_arsenal(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'w.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    storage = StorageService(tmp_path / "storage")

    import app.agents.api as agent_api
    import app.knowledge.index as idx_mod

    k_root = tmp_path / "knowledge"
    (k_root / "dispatch" / "Arsenal" / "CRYPTO").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(idx_mod, "DEFAULT_KNOWLEDGE_ROOT", k_root)

    def _fake_orch(_storage_dir):
        runner = ToolRunner(ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")}), storage)
        jury = JuryAgent(_scripted([
            {"action": "DISPATCH", "reason": "run solver",
             "order": {"category": "crypto", "task": "invert xor"}, "findings": ["xor header detected"]},
        ]), persona_text="jury")
        spec = SpecialistRunner(_scripted([
            {
                "tool_name": "run_python_script",
                "tool_args": {"filename": "solve.py", "code": "print('CTF{proof_writeup_ok}')"},
            },
            {
                "status": "SOLVED",
                "flag_candidate": "CTF{proof_writeup_ok}",
                "findings": ["decoded xor key=0x42"],
                "tools_used": ["python"],
            },
        ]), persona_text="spec", category="crypto")
        return Orchestrator(runner=runner, jury=jury, specialists={"crypto": spec})

    monkeypatch.setattr(agent_api, "_make_orchestrator", _fake_orch)

    app = create_app(engine=engine, storage_dir=tmp_path / "storage")
    with TestClient(app) as c:
        ev = c.post("/api/events", json={"name": "WriteupCTF", "flag_format": r"CTF\{.*\}"}).json()
        ch = c.post(
            f"/api/events/{ev['id']}/challenges",
            json={"title": "XorBaby", "category": "crypto", "points": 250, "tags": "xor,python"},
        ).json()
        cid = ch["id"]

        # Run full auto via Orchestrator
        with Session(engine) as s:
            orch = _fake_orch(storage)
            sess = orch.start(s, cid, mode="auto")
            assert sess.state == "solved"
            assert sess.final_report is not None
            assert sess.final_report.writeup_md is not None
            assert "CTF{proof_writeup_ok}" in sess.final_report.writeup_md
            assert "solve.py" in sess.final_report.writeup_md
            assert "print('CTF{proof_writeup_ok}')" in sess.final_report.writeup_md

            note = tracking.current_note(s, cid)
            assert note is not None
            assert "# Writeup — XorBaby" in note.content

        # Download challenge writeup.md
        r_md = c.get(f"/challenges/{cid}/writeup.md")
        assert r_md.status_code == 200
        assert "XorBaby" in r_md.text
        assert "decoded xor key=0x42" in r_md.text or "CTF{proof_writeup_ok}" in r_md.text

        # Download proof-bundle.zip and verify contents
        r_zip = c.get(f"/challenges/{cid}/proof-bundle.zip")
        assert r_zip.status_code == 200
        with zipfile.ZipFile(io.BytesIO(r_zip.content)) as zf:
            names = set(zf.namelist())
            assert "writeup.md" in names
            assert "attempts.json" in names
            assert "sandbox/solve.py" in names
            assert b"CTF{proof_writeup_ok}" in zf.read("sandbox/solve.py")

        # Export event writeup.md
        r_ev_md = c.get(f"/events/{ev['id']}/writeup.md?only_solved=true")
        assert r_ev_md.status_code == 200
        assert "# Writeups — WriteupCTF" in r_ev_md.text
        assert "XorBaby" in r_ev_md.text

        # Save technique to FTS5 Arsenal and search it back
        r_ars = c.post(f"/challenges/{cid}/arsenal/save")
        assert r_ars.status_code == 200
        assert "FTS5 reindexado" in r_ars.text
        local_md = k_root / "local" / "crypto" / f"{cid}-xorbaby.md"
        assert local_md.exists()

        with Session(engine) as s:
            hits = KnowledgeIndex(s, knowledge_root=k_root).search("xorbaby", category="crypto")
            assert len(hits) == 1
            assert hits[0]["source"] == "koth-local"

        # UI cards present on challenge & event pages
        assert "Writeup &amp; Provas" in c.get(f"/challenges/{cid}").text
        assert "Exportar Writeups (.md)" in c.get(f"/events/{ev['id']}").text
