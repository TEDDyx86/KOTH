import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlmodel import Session

from ..models import Challenge
from ..services import tracking
from .jury import JuryAgent
from .payloads import JuryDecision, SpecialistReport
from .recon import recon_bundle
from .submission import validate_flag


@dataclass
class AgentSession:
    id: str
    challenge_id: int
    state: str  # awaiting_approval | running | solved | gave_up | rejected
    pending_decision: JuryDecision | None = None
    findings: list[str] = field(default_factory=list)
    reports: list[SpecialistReport] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "challenge_id": self.challenge_id,
            "state": self.state,
            "pending_decision": (self.pending_decision.model_dump()
                                 if self.pending_decision else None),
            "findings": self.findings,
            "reports": [r.model_dump() for r in self.reports],
            "created_at": self.created_at.isoformat(),
        }


class Orchestrator:
    """OP + checkpoint loop. Starts a session (PL/RC/JU), then stops at
    awaiting_approval. Each approve() executes ONE decision and stops again —
    no attack step runs without the operator seeing the findings."""

    def __init__(self, runner, jury: JuryAgent,
                 specialists: dict[str, object],
                 agents_limits: dict | None = None) -> None:
        self.runner = runner
        self.jury = jury
        self.specialists = specialists  # category -> SpecialistRunner
        self.limits = agents_limits or {}
        self.sessions: dict[str, AgentSession] = {}

    # -- helpers -----------------------------------------------------------
    def _get(self, session: Session, challenge_id: int) -> Challenge:
        challenge = session.get(Challenge, challenge_id)
        if challenge is None:
            raise LookupError(f"challenge {challenge_id} not found")
        return challenge

    def _accumulate(self, sess: AgentSession, findings: list[str]) -> None:
        for f in findings:
            if f and f not in sess.findings:
                sess.findings.append(f)

    def _decide(self, session: Session, challenge: Challenge,
                sess: AgentSession, extra_feedback: str | None = None) -> None:
        decision = self.jury.decide(session, challenge, sess.reports,
                                    extra_feedback=extra_feedback)
        sess.pending_decision = decision
        sess.state = "awaiting_approval"
        self._accumulate(sess, list(decision.findings))

    # -- public API --------------------------------------------------------
    def start(self, session: Session, challenge_id: int) -> AgentSession:
        challenge = self._get(session, challenge_id)
        digest = recon_bundle(self.runner, session, challenge)
        sess = AgentSession(id=uuid.uuid4().hex, challenge_id=challenge_id,
                            state="running")
        self.sessions[sess.id] = sess
        self._accumulate(sess, [digest.summary] if digest.summary else [])
        self._decide(session, challenge, sess)
        return sess

    def approve(self, session: Session, session_id: str) -> AgentSession:
        agent_session = self.sessions[session_id]
        challenge = self._get(session, agent_session.challenge_id)
        decision = agent_session.pending_decision
        if decision is None or agent_session.state not in ("awaiting_approval", "rejected"):
            raise ValueError(f"session {session_id} is not awaiting approval")

        if decision.action == "GIVE_UP":
            agent_session.state = "gave_up"
            tracking.record_attempt(session, challenge.id, "agent",
                                    f"jury gave up: {decision.reason}", "failed")
            return agent_session

        if decision.action == "DISPATCH":
            self._dispatch(session, challenge, agent_session, decision)

        if decision.action == "SUBMIT":
            candidate = decision.flag_candidate
            if candidate:
                result = validate_flag(session, challenge, candidate)
                if result.accepted:
                    agent_session.state = "solved"
                    return agent_session
                self._accumulate(agent_session, [f"submission rejected: {result.reason}"])

        # RECHECK / WAIT_USER / post-dispatch / rejected submission → decide again
        self._decide(session, challenge, agent_session)
        return agent_session

    def reject(self, session: Session, session_id: str, feedback: str) -> AgentSession:
        agent_session = self.sessions[session_id]
        challenge = self._get(session, agent_session.challenge_id)
        tracking.add_note(session, challenge.id, f"[agent rejected] {feedback}")
        self._decide(session, challenge, agent_session, extra_feedback=feedback)
        return agent_session

    def _dispatch(self, session: Session, challenge: Challenge,
                  agent_session: AgentSession, decision: JuryDecision) -> None:
        order = decision.order
        specialist = self.specialists.get(order.category.lower())
        if specialist is None:
            self._accumulate(agent_session,
                             [f"no specialist for category '{order.category}'"])
            report = SpecialistReport(status="FAILED",
                                      findings=[f"no specialist for {order.category}"])
        else:
            try:
                report = specialist.run(session, challenge, order, runner=self.runner)
            except Exception as exc:  # model/tool failure must not kill the loop
                report = SpecialistReport(
                    status="FAILED",
                    findings=[f"specialist crashed: {type(exc).__name__}: {exc}"],
                    next_step="retry with corrected tool calls or a simpler task")
        agent_session.reports.append(report)
        self._accumulate(agent_session, list(report.findings))
        tracking.record_attempt(
            session, challenge.id, "agent",
            f"specialist {order.category}: {report.status}", "breakthrough"
            if report.status == "SOLVED" else "partial",
            learning="; ".join(report.findings)[:2000])
