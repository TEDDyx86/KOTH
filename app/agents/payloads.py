from typing import Literal

from pydantic import BaseModel, model_validator


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
    next_step: str | None = None
    tools_used: list[str] = []


class JuryDecision(BaseModel):
    """Júri decision after each cycle."""
    action: Literal["DISPATCH", "RECHECK", "SUBMIT", "GIVE_UP", "WAIT_USER"]
    order: TaskOrder | None = None
    flag_candidate: str | None = None
    reason: str
    findings: list[str] = []

    @model_validator(mode="after")
    def _dispatch_needs_order(self) -> "JuryDecision":
        if self.action == "DISPATCH" and self.order is None:
            raise ValueError("DISPATCH requires an order")
        return self


class FinalReport(BaseModel):
    """OP → user: consolidated result."""
    challenge_id: int
    status: Literal["solved", "failed", "gave_up", "awaiting_input"]
    attempts_summary: list[str] = []
    flag_candidate: str | None = None
