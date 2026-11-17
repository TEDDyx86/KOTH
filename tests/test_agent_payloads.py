from typing import Literal

import pytest
from pydantic import ValidationError

from app.agents.payloads import (
    FinalReport,
    JuryDecision,
    SpecialistReport,
    TaskOrder,
)


def test_task_order_defaults():
    order = TaskOrder(category="crypto", task="try small-e cube root")
    assert order.hints == []
    assert order.max_tool_runs == 5


def test_specialist_report_statuses():
    for status in ["SOLVED", "NEED_HINT", "FAILED"]:
        r = SpecialistReport(status=status, findings=["x"])
        assert r.status == status
    with pytest.raises(ValidationError):
        SpecialistReport(status="MAYBE", findings=[])


def test_jury_decision_requires_order_for_dispatch():
    d = JuryDecision(action="DISPATCH", order=TaskOrder(category="web", task="sqli"),
                     reason="surface looks sqli", findings=["nmap open 80"])
    assert d.order is not None
    with pytest.raises(ValidationError):
        JuryDecision(action="DISPATCH", order=None, reason="x", findings=[])


def test_final_report():
    r = FinalReport(challenge_id=1, status="solved", attempts_summary=["a"],
                    flag_candidate="CTF{ok}")
    assert r.flag_candidate == "CTF{ok}"
