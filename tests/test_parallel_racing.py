import json
import re
import threading
import time

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlmodel import Session, SQLModel, create_engine

from app.agents.jury import JuryAgent
from app.agents.orchestrator import Orchestrator
from app.agents.payloads import JuryDecision, SpecialistReport, TaskOrder
from app.database import configure_sqlite_engine
from app.models import Challenge, Event
from app.services.storage import StorageService
from app.tooling.config import ToolSettings
from app.tooling.runner import ToolRunner


def _jury_model(decisions: list[dict]):
    state = {"n": 0}

    def fn(messages, info):
        idx = state["n"]
        state["n"] += 1
        payload = decisions[min(idx, len(decisions) - 1)]
        blob = "\n".join(str(p) for m in messages for p in (m.parts or []))
        match = re.search(r"['\"](\w*result\w*)['\"]", blob)
        name = match.group(1) if match else "final_result"
        return ModelResponse(parts=[ToolCallPart(tool_name=name, args=json.dumps(payload))])

    return FunctionModel(fn)


def test_jury_decision_parallel_orders_validation():
    d = JuryDecision(
        action="DISPATCH",
        parallel_orders=[
            TaskOrder(category="web", task="try sqli"),
            TaskOrder(category="crypto", task="try rsa"),
            TaskOrder(category="pwn", task="ignored third"),
        ],
        reason="hybrid chal",
    )
    assert len(d.parallel_orders) == 2
    assert d.order is not None
    assert d.order.category == "web"


def test_parallel_racing_runs_concurrently_cancels_loser_and_sums_telemetry(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'race.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    storage = StorageService(tmp_path / "storage")

    with Session(engine) as session:
        ev = Event(name="RaceEvent", flag_format=r"CTF\{[^}]+\}")
        session.add(ev)
        session.commit()
        session.refresh(ev)
        ch = Challenge(event_id=ev.id, title="HybridRace", category="web", points=400)
        session.add(ch)
        session.commit()
        session.refresh(ch)

        barrier = threading.Barrier(2, timeout=2.0)
        slow_cancelled = {"hit": False}

        class FastWebSpec:
            def __init__(self):
                self.last_usage = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
                self.last_tool_outputs = ["HTTP/1.1 200 OK\nCTF{parallel_win}"]
                self.last_detected_flags = ["CTF{parallel_win}"]
                self.last_metrics = {
                    "role": "specialist:web",
                    "model": "gpt-4o-mini",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "total_tokens": 150,
                    "duration_s": 0.1,
                    "tool_calls": 1,
                    "mcp_calls": 0,
                    "cache_hits": 0,
                }

            def run(self, session, challenge, order, runner=None, *, blackboard=None,
                    on_event=None, on_output_chunk=None, cancel_event=None, branch_label=None):
                barrier.wait()  # proves both branches are executing concurrently!
                return SpecialistReport(
                    status="SOLVED",
                    flag_candidate="CTF{parallel_win}",
                    findings=["sql bypass leaked CTF{parallel_win}"],
                    branch=branch_label,
                )

        class SlowCryptoSpec:
            def __init__(self):
                self.last_usage = {"input_tokens": 80, "output_tokens": 20, "total_tokens": 100}
                self.last_tool_outputs = []
                self.last_detected_flags = []
                self.last_metrics = {
                    "role": "specialist:crypto",
                    "model": "gpt-4o-mini",
                    "input_tokens": 80,
                    "output_tokens": 20,
                    "total_tokens": 100,
                    "duration_s": 0.2,
                    "tool_calls": 1,
                    "mcp_calls": 0,
                    "cache_hits": 0,
                }

            def run(self, session, challenge, order, runner=None, *, blackboard=None,
                    on_event=None, on_output_chunk=None, cancel_event=None, branch_label=None):
                barrier.wait()
                for _ in range(40):
                    if cancel_event is not None and cancel_event.is_set():
                        slow_cancelled["hit"] = True
                        break
                    time.sleep(0.02)
                return SpecialistReport(
                    status="FAILED",
                    findings=["stopped early"] if slow_cancelled["hit"] else ["finished"],
                    dead_ends=["brute force"],
                    branch=branch_label,
                )

        runner = ToolRunner(ToolSettings(), storage)
        jury = JuryAgent(
            _jury_model([{
                "action": "DISPATCH",
                "reason": "race web vs crypto",
                "order": {"category": "web", "task": "check sqli"},
                "parallel_orders": [
                    {"category": "web", "task": "check sqli"},
                    {"category": "crypto", "task": "check weak rsa"},
                ],
                "findings": [],
            }]),
            "jury",
        )
        orch = Orchestrator(
            runner=runner,
            jury=jury,
            specialists={"web": FastWebSpec(), "crypto": SlowCryptoSpec()},
            agents_limits={"max_attempts": 10, "max_minutes": 30, "allow_parallel_racing": True, "max_race_branches": 2},
        )
        sess = orch.start(session, ch.id, mode="auto")
        assert sess.state == "solved"
        assert slow_cancelled["hit"] is True
        assert sess.final_report is not None
        assert sess.final_report.flag_candidate == "CTF{parallel_win}"
        assert len(sess.reports) == 2
        assert {r.branch for r in sess.reports} == {"Ramo 1: web", "Ramo 2: crypto"}
        assert "brute force" in sess.blackboard.dead_ends
        assert len(sess.telemetry) == 2
        assert sess.total_tokens >= 250


def test_parallel_racing_disabled_runs_only_primary_order(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'norace.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    storage = StorageService(tmp_path / "storage")

    with Session(engine) as session:
        ev = Event(name="NoRace", flag_format=r"CTF\{[^}]+\}")
        session.add(ev)
        session.commit()
        session.refresh(ev)
        ch = Challenge(event_id=ev.id, title="SequentialOnly", category="web", points=100)
        session.add(ch)
        session.commit()
        session.refresh(ch)

        called = []

        class StubSpec:
            def __init__(self, label):
                self.label = label
                self.last_usage = None
                self.last_tool_outputs = ["CTF{seq_ok}"]
                self.last_detected_flags = []
                self.last_metrics = None

            def run(self, *args, **kwargs):
                called.append(self.label)
                return SpecialistReport(status="SOLVED", flag_candidate="CTF{seq_ok}", findings=["CTF{seq_ok}"])

        jury = JuryAgent(
            _jury_model([{
                "action": "DISPATCH",
                "reason": "try primary only",
                "order": {"category": "web", "task": "main"},
                "parallel_orders": [
                    {"category": "web", "task": "main"},
                    {"category": "crypto", "task": "secondary"},
                ],
                "findings": [],
            }]),
            "jury",
        )
        orch = Orchestrator(
            runner=ToolRunner(ToolSettings(), storage),
            jury=jury,
            specialists={"web": StubSpec("web"), "crypto": StubSpec("crypto")},
            agents_limits={"max_attempts": 10, "max_minutes": 30, "allow_parallel_racing": False},
        )
        sess = orch.start(session, ch.id, mode="auto")
        assert sess.state == "solved"
        assert called == ["web"]
