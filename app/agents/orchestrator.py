import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlmodel import Session, select

from ..models import AgentSessionRecord, AttemptResult, AttemptType, Challenge
from ..services import tracking
from .jury import JuryAgent
from .payloads import FinalReport, JuryDecision, SpecialistReport
from .planner import check_stop_criteria, reassess_strategy
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
    mode: str = "supervised"  # supervised | auto
    dispatch_count: int = 0
    final_report: FinalReport | None = None
    error: str | None = None
    stop_requested: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "challenge_id": self.challenge_id,
            "state": self.state,
            "mode": self.mode,
            "dispatch_count": self.dispatch_count,
            "pending_decision": (self.pending_decision.model_dump()
                                 if self.pending_decision else None),
            "findings": self.findings,
            "reports": [r.model_dump() for r in self.reports],
            "final_report": (self.final_report.model_dump()
                             if self.final_report else None),
            "error": self.error,
            "created_at": self.created_at.isoformat(),
        }


def _record_to_session(rec: AgentSessionRecord) -> AgentSession:
    pending = (JuryDecision.model_validate_json(rec.pending_decision_json)
               if rec.pending_decision_json else None)
    findings = json.loads(rec.findings_json or "[]")
    reports = [SpecialistReport.model_validate(r)
               for r in json.loads(rec.reports_json or "[]")]
    final_report = (FinalReport.model_validate_json(rec.final_report_json)
                    if rec.final_report_json else None)
    created = rec.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return AgentSession(
        id=rec.id,
        challenge_id=rec.challenge_id,
        state=rec.state,
        pending_decision=pending,
        findings=findings,
        reports=reports,
        created_at=created,
        mode=rec.mode,
        dispatch_count=rec.dispatch_count,
        final_report=final_report,
        error=rec.error,
    )


class Orchestrator:
    """OP + PL + RC + JU + S1-S6 + SU loop.
    In 'supervised' mode (default), stops at awaiting_approval after each decision.
    In 'auto' mode, executes the loop until solved, gave_up, limits hit, or WAIT_USER."""

    def __init__(self, runner, jury: JuryAgent,
                 specialists: dict[str, object],
                 agents_limits: dict | None = None) -> None:
        self.runner = runner
        self.jury = jury
        self.specialists = specialists  # category -> SpecialistRunner
        self.limits = agents_limits or {}
        self.sessions: dict[str, AgentSession] = {}

    # -- persistence & helpers ---------------------------------------------
    def _save(self, session: Session | None, sess: AgentSession) -> None:
        self.sessions[sess.id] = sess
        if session is None:
            return
        try:
            rec = session.get(AgentSessionRecord, sess.id)
            now = datetime.now(timezone.utc)
            if rec is None:
                rec = AgentSessionRecord(
                    id=sess.id,
                    challenge_id=sess.challenge_id,
                    created_at=sess.created_at,
                )
            rec.state = sess.state
            rec.mode = sess.mode
            rec.dispatch_count = sess.dispatch_count
            rec.pending_decision_json = (
                sess.pending_decision.model_dump_json() if sess.pending_decision else None
            )
            rec.findings_json = json.dumps(sess.findings)
            rec.reports_json = json.dumps([r.model_dump() for r in sess.reports])
            rec.final_report_json = (
                sess.final_report.model_dump_json() if sess.final_report else None
            )
            rec.error = sess.error
            rec.updated_at = now
            session.add(rec)
            session.commit()
        except Exception:
            session.rollback()

    def get_session(self, session: Session | None, session_id: str) -> AgentSession | None:
        if session_id in self.sessions:
            return self.sessions[session_id]
        if session is None:
            return None
        try:
            rec = session.get(AgentSessionRecord, session_id)
        except Exception:
            return None
        if rec is None:
            return None
        sess = _record_to_session(rec)
        self.sessions[sess.id] = sess
        return sess

    def latest_for_challenge(self, session: Session | None, challenge_id: int) -> AgentSession | None:
        matches = [s for s in self.sessions.values() if s.challenge_id == challenge_id]
        if session is not None:
            try:
                recs = list(session.exec(
                    select(AgentSessionRecord).where(
                        AgentSessionRecord.challenge_id == challenge_id
                    )
                ).all())
                for rec in recs:
                    if rec.id not in self.sessions:
                        s = _record_to_session(rec)
                        self.sessions[s.id] = s
                        matches.append(s)
            except Exception:
                pass
        return max(matches, key=lambda s: s.created_at) if matches else None

    def _get(self, session: Session, challenge_id: int) -> Challenge:
        challenge = session.get(Challenge, challenge_id)
        if challenge is None:
            raise LookupError(f"challenge {challenge_id} not found")
        return challenge

    def _accumulate(self, sess: AgentSession, findings: list[str]) -> None:
        for f in findings:
            if f and f not in sess.findings:
                sess.findings.append(f)

    def _build_final_report(self, challenge_id: int, sess: AgentSession,
                            status: str, flag_candidate: str | None = None) -> FinalReport:
        summary = [f"dispatches={sess.dispatch_count}"]
        summary.extend(sess.findings[-10:])
        report = FinalReport(
            challenge_id=challenge_id,
            status=status,  # solved | failed | gave_up | awaiting_input
            attempts_summary=summary,
            flag_candidate=flag_candidate,
        )
        sess.final_report = report
        return report

    def _usage_note(self, agent_obj) -> str:
        usage = getattr(agent_obj, "last_usage", None)
        if not usage:
            return ""
        return f" | tokens(in={usage.get('input_tokens', 0)},out={usage.get('output_tokens', 0)})"

    def _decide(self, session: Session, challenge: Challenge,
                sess: AgentSession, extra_feedback: str | None = None) -> None:
        if sess.stop_requested or sess.state == "rejected":
            sess.state = "rejected"
            sess.pending_decision = None
            self._save(session, sess)
            return

        stop = check_stop_criteria(sess.created_at, sess.dispatch_count, self.limits)
        if stop.stop:
            sess.state = "gave_up"
            sess.pending_decision = None
            self._accumulate(sess, [stop.reason or "planner stop criteria reached"])
            tracking.record_attempt(session, challenge.id, AttemptType.agent,
                                    stop.reason or "planner stop criteria reached",
                                    AttemptResult.failed)
            self._build_final_report(challenge.id, sess, "gave_up")
            self._save(session, sess)
            return

        decision = self.jury.decide(session, challenge, sess.reports,
                                    extra_feedback=extra_feedback)
        if sess.stop_requested or sess.state == "rejected":
            sess.state = "rejected"
            sess.pending_decision = None
            self._save(session, sess)
            return
        sess.pending_decision = decision
        sess.state = "awaiting_approval"
        self._accumulate(sess, list(decision.findings))
        self._save(session, sess)

    def _step(self, session: Session, challenge: Challenge,
              agent_session: AgentSession) -> bool:
        """Execute ONE pending decision. Returns True if session reached a terminal
        or user-input state and should stop auto-looping."""
        decision = agent_session.pending_decision
        if decision is None:
            return True

        if decision.action == "GIVE_UP":
            agent_session.state = "gave_up"
            agent_session.pending_decision = None
            tracking.record_attempt(session, challenge.id, AttemptType.agent,
                                    f"jury gave up: {decision.reason}", AttemptResult.failed)
            self._build_final_report(challenge.id, agent_session, "gave_up")
            self._save(session, agent_session)
            return True

        if decision.action == "DISPATCH":
            agent_session.dispatch_count += 1
            self._dispatch(session, challenge, agent_session, decision)
            action, reason = reassess_strategy(
                agent_session.created_at,
                agent_session.dispatch_count,
                agent_session.reports,
                self.limits,
            )
            if action == "GIVE_UP":
                agent_session.state = "gave_up"
                agent_session.pending_decision = None
                self._accumulate(agent_session, [reason])
                tracking.record_attempt(session, challenge.id, AttemptType.agent,
                                        reason, AttemptResult.failed)
                self._build_final_report(challenge.id, agent_session, "gave_up")
                self._save(session, agent_session)
                return True
            if action == "RECON":
                self._accumulate(agent_session, [reason])
                digest = recon_bundle(self.runner, session, challenge)
                self._accumulate(agent_session, [digest.summary] if digest.summary else [])

        elif decision.action == "RECHECK":
            digest = recon_bundle(self.runner, session, challenge)
            self._accumulate(agent_session, [digest.summary] if digest.summary else [])

        elif decision.action == "SUBMIT":
            candidate = decision.flag_candidate
            if candidate:
                result = validate_flag(session, challenge, candidate)
                if result.accepted:
                    agent_session.state = "solved"
                    agent_session.pending_decision = None
                    self._build_final_report(challenge.id, agent_session, "solved",
                                             flag_candidate=candidate)
                    self._save(session, agent_session)
                    return True
                self._accumulate(agent_session, [f"submission rejected: {result.reason}"])

        elif decision.action == "WAIT_USER":
            if agent_session.mode == "auto":
                agent_session.state = "awaiting_approval"
                self._save(session, agent_session)
                return True

        self._decide(session, challenge, agent_session)
        return agent_session.state in ("solved", "gave_up", "rejected")

    def _run_auto_loop(self, session: Session, challenge: Challenge,
                       sess: AgentSession) -> None:
        while (not sess.stop_requested
               and sess.state == "awaiting_approval"
               and sess.pending_decision is not None):
            if sess.pending_decision.action == "WAIT_USER":
                break
            sess.state = "running"
            self._save(session, sess)
            done = self._step(session, challenge, sess)
            if done or sess.stop_requested:
                break

    # -- public API --------------------------------------------------------
    def start(self, session: Session, challenge_id: int,
              mode: str = "supervised") -> AgentSession:
        challenge = self._get(session, challenge_id)
        digest = recon_bundle(self.runner, session, challenge)
        sess = AgentSession(id=uuid.uuid4().hex, challenge_id=challenge_id,
                            state="running", mode=mode)
        self._save(session, sess)
        self._accumulate(sess, [digest.summary] if digest.summary else [])
        self._decide(session, challenge, sess)
        if mode == "auto" and sess.state == "awaiting_approval":
            self._run_auto_loop(session, challenge, sess)
        return sess

    def approve(self, session: Session, session_id: str,
                mode: str | None = None) -> AgentSession:
        agent_session = self.get_session(session, session_id)
        if agent_session is None:
            raise KeyError(session_id)
        challenge = self._get(session, agent_session.challenge_id)
        decision = agent_session.pending_decision
        if decision is None or agent_session.state not in ("awaiting_approval", "rejected"):
            raise ValueError(f"session {session_id} is not awaiting approval")

        if mode in ("supervised", "auto"):
            agent_session.mode = mode
        agent_session.state = "running"
        self._save(session, agent_session)
        self._step(session, challenge, agent_session)
        if agent_session.mode == "auto" and agent_session.state == "awaiting_approval":
            self._run_auto_loop(session, challenge, agent_session)
        return agent_session

    def reject(self, session: Session, session_id: str, feedback: str) -> AgentSession:
        agent_session = self.get_session(session, session_id)
        if agent_session is None:
            raise KeyError(session_id)
        if agent_session.state != "awaiting_approval":
            raise ValueError(f"session {session_id} is not awaiting approval")
        challenge = self._get(session, agent_session.challenge_id)
        tracking.add_note(session, challenge.id, f"[agent rejected] {feedback}")
        self._decide(session, challenge, agent_session, extra_feedback=feedback)
        return agent_session

    def terminate(self, session: Session, session_id: str,
                  reason: str = "") -> AgentSession:
        """Operator rejection: stop the session for good (no new decision)."""
        agent_session = self.get_session(session, session_id)
        if agent_session is None:
            raise KeyError(session_id)
        agent_session.stop_requested = True
        agent_session.state = "rejected"
        agent_session.pending_decision = None
        self._accumulate(agent_session, [f"terminated by operator: {reason}"])
        self._build_final_report(agent_session.challenge_id, agent_session, "gave_up")
        tracking.add_note(session, agent_session.challenge_id,
                          f"[agent terminated] {reason}")
        self._save(session, agent_session)
        return agent_session

    # -- background execution ----------------------------------------------
    def start_background(self, engine, session: Session, challenge_id: int,
                         mode: str = "supervised") -> AgentSession:
        self._get(session, challenge_id)
        sess = AgentSession(id=uuid.uuid4().hex, challenge_id=challenge_id,
                            state="running", mode=mode)
        self._save(session, sess)

        def _worker():
            try:
                with Session(engine) as bg_session:
                    challenge = self._get(bg_session, challenge_id)
                    digest = recon_bundle(self.runner, bg_session, challenge)
                    self._accumulate(sess, [digest.summary] if digest.summary else [])
                    self._decide(bg_session, challenge, sess)
                    if mode == "auto" and sess.state == "awaiting_approval":
                        self._run_auto_loop(bg_session, challenge, sess)
            except Exception as exc:
                sess.state = "rejected"
                sess.error = f"{type(exc).__name__}: {exc}"
                with Session(engine) as err_session:
                    self._save(err_session, sess)

        threading.Thread(target=_worker, daemon=True).start()
        return sess

    def approve_background(self, engine, session: Session, session_id: str,
                           mode: str | None = None) -> AgentSession:
        agent_session = self.get_session(session, session_id)
        if agent_session is None:
            raise KeyError(session_id)
        if agent_session.pending_decision is None or agent_session.state not in ("awaiting_approval", "rejected"):
            raise ValueError(f"session {session_id} is not awaiting approval")
        if mode in ("supervised", "auto"):
            agent_session.mode = mode
        agent_session.state = "running"
        self._save(session, agent_session)

        def _worker():
            try:
                with Session(engine) as bg_session:
                    challenge = self._get(bg_session, agent_session.challenge_id)
                    self._step(bg_session, challenge, agent_session)
                    if agent_session.mode == "auto" and agent_session.state == "awaiting_approval":
                        self._run_auto_loop(bg_session, challenge, agent_session)
            except Exception as exc:
                agent_session.state = "rejected"
                agent_session.error = f"{type(exc).__name__}: {exc}"
                with Session(engine) as err_session:
                    self._save(err_session, agent_session)

        threading.Thread(target=_worker, daemon=True).start()
        return agent_session

    def _dispatch(self, session: Session, challenge: Challenge,
                  agent_session: AgentSession, decision: JuryDecision) -> None:
        order = decision.order
        specialist = self.specialists.get(order.category.lower())
        usage_suffix = ""
        if specialist is None:
            self._accumulate(agent_session,
                             [f"no specialist for category '{order.category}'"])
            report = SpecialistReport(status="FAILED",
                                      findings=[f"no specialist for {order.category}"])
        else:
            try:
                report = specialist.run(session, challenge, order, runner=self.runner)
                usage_suffix = self._usage_note(specialist)
            except Exception as exc:  # model/tool failure must not kill the loop
                report = SpecialistReport(
                    status="FAILED",
                    findings=[f"specialist crashed: {type(exc).__name__}: {exc}"],
                    next_step="retry with corrected tool calls or a simpler task")
        agent_session.reports.append(report)
        self._accumulate(agent_session, list(report.findings))
        learning = "; ".join(report.findings)[:1900] + usage_suffix
        tracking.record_attempt(
            session, challenge.id, AttemptType.agent,
            f"specialist {order.category}: {report.status}",
            AttemptResult.breakthrough if report.status == "SOLVED" else AttemptResult.partial,
            learning=learning[:2000])
