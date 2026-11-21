import json
import re
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlmodel import Session, SQLModel, create_engine

from app.agents.context import specialist_context
from app.agents.jury import JuryAgent
from app.agents.orchestrator import Orchestrator
from app.agents.payloads import TaskOrder
from app.agents.recon import recon_bundle
from app.agents.specialists import SpecialistRunner
from app.models import AgentSessionRecord, Attachment, AttachmentType, Challenge, Event
from app.services import tracking
from app.services.storage import StorageService
from app.tooling.config import ToolSettings, ToolSpec, load_tools_config
from app.tooling.runner import ToolRunner


@pytest.fixture
def env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'smart.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    storage = StorageService(tmp_path / "storage")
    with Session(engine) as session:
        ev = Event(name="KOTH", flag_format=r"CTF\{.*\}")
        session.add(ev)
        session.commit()
        session.refresh(ev)
        ch = Challenge(event_id=ev.id, title="SmartChal", category="reverse", points=300)
        session.add(ch)
        session.commit()
        session.refresh(ch)
        yield engine, session, ev, ch, storage


def _scripted(payloads: list[dict], counter: list[str] | None = None):
    state = {"n": 0}

    def fn(messages, info):
        if counter is not None:
            counter.append("\n".join(str(p) for m in messages for p in (m.parts or [])))
        idx = state["n"]
        state["n"] += 1
        blob = "\n".join(str(p) for m in messages for p in (m.parts or []))
        match = re.search(r"['\"](\w*result\w*)['\"]", blob)
        name = match.group(1) if match else "final_result"
        payload = payloads[min(idx, len(payloads) - 1)]
        return ModelResponse(parts=[ToolCallPart(tool_name=name, args=json.dumps(payload))])

    return FunctionModel(fn)


def test_category_alias_and_misc_fallback(env):
    _, session, _, ch, storage = env
    runner = ToolRunner(load_tools_config(), storage)
    jury = JuryAgent(_scripted([
        {"action": "DISPATCH", "reason": "rev", "order": {"category": "reverse", "task": "disassemble"}, "findings": []},
        {"action": "DISPATCH", "reason": "aiml", "order": {"category": "ai-ml", "task": "inspect weights"}, "findings": []},
        {"action": "DISPATCH", "reason": "chain", "order": {"category": "blockchain", "task": "audit contract"}, "findings": []},
        {"action": "GIVE_UP", "reason": "done", "findings": []},
    ]), persona_text="jury")
    rev_spec = SpecialistRunner(_scripted([{"status": "NEED_HINT", "findings": ["rev ran"]}]), persona_text="rev")
    misc_spec = SpecialistRunner(_scripted([
        {"status": "NEED_HINT", "findings": ["misc ran"]},
        {"status": "NEED_HINT", "findings": ["fallback ran"]},
    ]), persona_text="misc")
    orch = Orchestrator(
        runner=runner,
        jury=jury,
        specialists={"reversing": rev_spec, "misc": misc_spec},
    )
    s = orch.start(session, ch.id, mode="auto")
    assert s.dispatch_count == 3
    assert "rev ran" in s.findings
    assert "misc ran" in s.findings
    assert "fallback ran" in s.findings
    assert any("routed to 'reversing'" in f for f in s.findings)
    assert any("routed to 'misc'" in f for f in s.findings)
    assert any("fallback 'misc'" in f for f in s.findings)


def test_specialist_context_includes_recent_attempts(env):
    _, session, _, ch, _ = env
    tracking.record_attempt(
        session, ch.id, "tool", "curl http://127.0.0.1/", "partial",
        tool_command="curl -sI http://127.0.0.1/",
        learning="Server: Werkzeug; X-Hint: /admin",
    )
    ctx = specialist_context(session, ch, TaskOrder(category="web", task="probe admin"))
    assert "Recent attempts & outputs" in ctx
    assert "X-Hint: /admin" in ctx


def test_auto_mode_fast_path_skips_redundant_jury_and_tracks_tokens(env):
    _, session, _, ch, storage = env
    runner = ToolRunner(load_tools_config(), storage)
    jury_prompts: list[str] = []
    jury = JuryAgent(_scripted([
        {"action": "DISPATCH", "reason": "solve", "order": {"category": "crypto", "task": "crack"}, "findings": []},
    ], counter=jury_prompts), persona_text="jury")
    spec = SpecialistRunner(_scripted([
        {"status": "SOLVED", "flag_candidate": "CTF{fast_path_win}", "findings": ["cracked"]},
    ]), persona_text="spec")

    orch = Orchestrator(runner=runner, jury=jury, specialists={"crypto": spec})
    s = orch.start(session, ch.id, mode="auto")
    assert s.state == "solved"
    assert s.final_report.flag_candidate == "CTF{fast_path_win}"
    # Only ONE jury decision needed (the initial DISPATCH); no redundant SUBMIT roundtrip!
    assert len(jury_prompts) == 1
    assert s.total_tokens > 0


def test_smart_pivot_skips_redundant_recon_and_handles_empty_submit(env):
    _, session, _, ch, storage = env
    runner = ToolRunner(load_tools_config(), storage)
    recon_calls = 0

    original_run_raw = runner.run_raw

    def counting_run_raw(c, tool, args, **kw):
        nonlocal recon_calls
        if tool == "strings":
            recon_calls += 1
        return original_run_raw(c, tool, args, **kw)

    runner.run_raw = counting_run_raw
    p = storage.save(ch.id, "chal.txt", b"hello")
    session.add(Attachment(
        challenge_id=ch.id, name="chal.txt",
        path=str(p.relative_to(storage.storage_dir)),
        mime="text/plain", size=5, type=AttachmentType.download,
    ))
    session.commit()

    jury_prompts: list[str] = []
    jury = JuryAgent(_scripted([
        {"action": "DISPATCH", "reason": "try 1", "order": {"category": "misc", "task": "t1"}, "findings": []},
        {"action": "DISPATCH", "reason": "try 2", "order": {"category": "misc", "task": "t2"}, "findings": []},
        {"action": "SUBMIT", "reason": "oops", "flag_candidate": "", "findings": []},
        {"action": "GIVE_UP", "reason": "pivot exhausted", "findings": []},
    ], counter=jury_prompts), persona_text="jury")
    spec = SpecialistRunner(_scripted([
        {"status": "FAILED", "findings": ["fail 1"]},
        {"status": "FAILED", "findings": ["fail 2"]},
    ]), persona_text="spec")

    orch = Orchestrator(runner=runner, jury=jury, specialists={"misc": spec})
    s = orch.start(session, ch.id, mode="auto")
    assert s.state == "gave_up"
    # Initial recon ran ONCE; 2 consecutive failures did NOT repeat identical recon on unchanged sandbox
    assert recon_calls == 1
    assert any("Pivot strategy" in p for p in jury_prompts)
    assert any("without flag_candidate" in p for p in jury_prompts)


def test_adaptive_recon_probes_elf_image_pcap_and_web(env, tmp_path):
    _, session, _, ch, storage = env
    ch.target = "http://127.0.0.1:8080"
    session.add(ch)
    for fname, raw in [
        ("bin.elf", b"\x7fELF\x02\x01\x01\x00"),
        ("pic.png", b"\x89PNG\r\n\x1a\n\x00"),
        ("cap.pcap", b"\xd4\xc3\xb2\xa1\x02\x00"),
    ]:
        p = storage.save(ch.id, fname, raw)
        session.add(Attachment(
            challenge_id=ch.id, name=fname,
            path=str(p.relative_to(storage.storage_dir)),
            mime="application/octet-stream", size=len(raw), type=AttachmentType.download,
        ))
    session.commit()

    called_tools: list[str] = []

    class ProbeRunner:
        def __init__(self):
            self.storage = storage
            self.settings = ToolSettings(tools={
                t: ToolSpec() for t in ("strings", "file", "checksec", "exiftool", "binwalk", "tshark", "nmap", "curl")
            })
            self.real = ToolRunner(self.settings, storage)

        def stage_downloads(self, s, c):
            return self.real.stage_downloads(s, c)

        def run_raw(self, c, tool, args, **kw):
            called_tools.append(tool)
            return SimpleNamespace(stdout=f"{tool}: ok\n", stderr="", returncode=0, truncated=False)

    dig = recon_bundle(ProbeRunner(), session, ch, storage=storage)
    for expected in ("strings", "file", "checksec", "exiftool", "binwalk", "tshark", "nmap", "curl"):
        assert expected in called_tools
    assert "checksec bin.elf" in dig.summary
    assert "exiftool pic.png" in dig.summary
    assert "tshark cap.pcap" in dig.summary
    assert "http-headers" in dig.summary


def test_orphaned_running_session_recovered_on_restart(env):
    engine, session, _, ch, storage = env
    rec = AgentSessionRecord(
        id="orphan-1",
        challenge_id=ch.id,
        state="running",
        mode="auto",
        dispatch_count=2,
        total_tokens=420,
    )
    session.add(rec)
    session.commit()

    orch = Orchestrator(
        runner=ToolRunner(load_tools_config(), storage),
        jury=JuryAgent(_scripted([{"action": "GIVE_UP", "reason": "x", "findings": []}]), persona_text="j"),
        specialists={},
    )
    recovered = orch.get_session(session, "orphan-1")
    assert recovered is not None
    assert recovered.state == "rejected"
    assert "reinício" in (recovered.error or "")
    assert recovered.total_tokens == 420
