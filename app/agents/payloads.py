from typing import Literal

from pydantic import BaseModel, model_validator


def _dedupe_cap(items: list[str], additions: list[str], cap: int) -> list[str]:
    out = list(items)
    seen = {x.strip().lower() for x in out if x and x.strip()}
    for item in additions:
        cleaned = (item or "").strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out[-cap:] if len(out) > cap else out


class Blackboard(BaseModel):
    """Structured per-session memory preventing hypothesis loops and dead-end retries."""
    confirmed_facts: list[str] = []
    dead_ends: list[str] = []
    active_hypotheses: list[str] = []
    discovered_targets: list[str] = []
    approved_targets: list[str] = []

    def merge(
        self,
        *,
        facts: list[str] | None = None,
        dead_ends: list[str] | None = None,
        hypotheses: list[str] | None = None,
        targets: list[str] | None = None,
        approved_targets: list[str] | None = None,
    ) -> None:
        if facts:
            self.confirmed_facts = _dedupe_cap(self.confirmed_facts, facts, 30)
        if dead_ends:
            self.dead_ends = _dedupe_cap(self.dead_ends, dead_ends, 25)
        if hypotheses:
            self.active_hypotheses = _dedupe_cap(self.active_hypotheses, hypotheses, 12)
        if targets:
            self.discovered_targets = _dedupe_cap(self.discovered_targets, targets, 10)
        if approved_targets:
            self.approved_targets = _dedupe_cap(self.approved_targets, approved_targets, 10)


class TaskOrder(BaseModel):
    """Júri → specialist: the task order for one dispatch."""
    category: str
    task: str
    hints: list[str] = []
    max_tool_runs: int = 5


class SpecialistReport(BaseModel):
    """Specialist → Júri: structured result of one dispatch."""
    status: Literal["SOLVED", "NEED_HINT", "FAILED"]
    flag_candidate: str | None = None
    findings: list[str] = []
    dead_ends: list[str] = []
    next_step: str | None = None
    tools_used: list[str] = []
    branch: str | None = None


class JuryDecision(BaseModel):
    """Júri decision after each cycle."""
    action: Literal["DISPATCH", "RECHECK", "SUBMIT", "GIVE_UP", "WAIT_USER"]
    order: TaskOrder | None = None
    parallel_orders: list[TaskOrder] = []
    flag_candidate: str | None = None
    reason: str
    findings: list[str] = []
    dead_ends: list[str] = []
    hypotheses: list[str] = []

    @model_validator(mode="after")
    def _dispatch_needs_order(self) -> "JuryDecision":
        if self.parallel_orders:
            self.parallel_orders = list(self.parallel_orders[:2])
        if self.action == "DISPATCH":
            if self.order is None and not self.parallel_orders:
                raise ValueError("DISPATCH requires an order or parallel_orders")
            if self.order is None and self.parallel_orders:
                self.order = self.parallel_orders[0]
        return self


class FinalReport(BaseModel):
    """OP → user: consolidated result."""
    challenge_id: int
    status: Literal["solved", "failed", "gave_up", "awaiting_input"]
    attempts_summary: list[str] = []
    flag_candidate: str | None = None
    writeup_md: str | None = None
