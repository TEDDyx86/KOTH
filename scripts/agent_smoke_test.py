"""Smoke test: the full orchestration loop against the real NVIDIA models.

builds the real Orchestrator (Júri = nemotron-3-ultra-550b, especialistas =
nemotron-3.5-super-vl-120b), seeds a temp challenge, runs start → approve one
cycle. Usage: python scripts/agent_smoke_test.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

AUTH_PATH = Path(os.environ.get("USERPROFILE", "")) / ".local" / "share" / "opencode" / "auth.json"


def load_nvidia_key() -> str:
    data = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
    key = data.get("nvidia", {}).get("key")
    if not key:
        raise SystemExit("no nvidia key in auth.json")
    return key


def main() -> int:
    os.environ["NVIDIA_API_KEY"] = load_nvidia_key()
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

    from sqlmodel import Session, SQLModel, create_engine

    from app.agents.api import _make_orchestrator
    from app.models import Challenge, Event
    from app.services.storage import StorageService

    tmp = Path(tempfile.mkdtemp(prefix="agent-smoke-"))
    engine = create_engine(f"sqlite:///{tmp / 'smoke.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    storage = StorageService(tmp / "storage")

    with Session(engine) as session:
        event = Event(name="smoke-ctf", flag_format=r"CTF\{.*\}")
        session.add(event)
        session.commit()
        session.refresh(event)
        ch = Challenge(event_id=event.id, title="ping me", category="forensics",
                       points=200, target=None,
                       description="Traffic capture (pcap); the flag conversation "
                                   "happened over ICMP only.")
        session.add(ch)
        session.commit()
        session.refresh(ch)

        orch = _make_orchestrator(storage)

        print("[1] start (recon + primeira decisão do Júri)...")
        s0 = orch.start(session, ch.id)
        print(f"    state={s0.state}")
        print(f"    findings={s0.findings}")
        print(f"    pending={s0.pending_decision.model_dump() if s0.pending_decision else None}")

        print("[2] approve (executa UM ciclo e para de novo)...")
        s1 = orch.approve(session, s0.id)
        print(f"    state={s1.state}")
        print(f"    findings={s1.findings}")
        print(f"    pending={s1.pending_decision.model_dump() if s1.pending_decision else None}")
        print(f"    reports={[r.model_dump() for r in s1.reports]}")
    print("[done] loop executado com modelo real")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
