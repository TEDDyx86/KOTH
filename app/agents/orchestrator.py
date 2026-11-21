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
from .writeup import build_challenge_writeup, persist_writeup_note_and_attachment

CATEGORY_ALIASES: dict[str, str] = {
    "reverse": "reversing",
    "rev": "reversing",
    "binary": "reversing",
    "stego": "forensics",
    "pcap": "forensics",
    "network": "forensics",
    "ai": "misc",
    "ai-ml": "misc",
    "custom": "misc",
}


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
    total_tokens: int = 0
    stop_requested: bool = False
    last_recon_sig: tuple[tuple[str, int], ...] | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "challenge_id": self.challenge_id,
            "state": self.state,
            "mode": self.mode,
            "dispatch_count": self.dispatch_count,
            "total_tokens": self.total_tokens,
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
        total_tokens=int(getattr(rec, "total_tokens", 0) or 0),
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
        self._lock = threading.RLock()
        self._active_workers: dict[str, int] = {}  # session_id -> challenge_id

    # -- persistence & helpers ---------------------------------------------
    def _save(self, session: Session | None, sess: AgentSession) -> None:
        with self._lock:
            self.sessions[sess.id] = sess
            snapshot = sess.to_dict()
            pending_json = (
                sess.pending_decision.model_dump_json() if sess.pending_decision else None
            )
            final_json = (
                sess.final_report.model_dump_json() if sess.final_report else None
            )
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
            rec.state = snapshot["state"]
            rec.mode = snapshot["mode"]
            rec.dispatch_count = snapshot["dispatch_count"]
            rec.total_tokens = snapshot["total_tokens"]
            rec.pending_decision_json = pending_json
            rec.findings_json = json.dumps(snapshot["findings"])
            rec.reports_json = json.dumps(snapshot["reports"])
            rec.final_report_json = final_json
            rec.error = snapshot["error"]
            rec.updated_at = now
            session.add(rec)
            session.commit()
        except Exception:
            session.rollback()

    def _recover_if_orphaned(self, session: Session | None, sess: AgentSession) -> AgentSession:
        with self._lock:
            if sess.state == "running" and sess.id not in self._active_workers:
                if sess.pending_decision is not None:
                    sess.state = "awaiting_approval"
                    sess.error = sess.error or "sessão recuperada após reinício (aguardando aprovação)"
                else:
                    sess.state = "rejected"
                    sess.error = sess.error or "sessão interrompida por reinício do servidor"
                self._save(session, sess)
        return sess

    def get_session(self, session: Session | None, session_id: str) -> AgentSession | None:
        with self._lock:
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
        with self._lock:
            self.sessions[sess.id] = sess
        return self._recover_if_orphaned(session, sess)

    def latest_for_challenge(self, session: Session | None, challenge_id: int) -> AgentSession | None:
        with self._lock:
            matches = [s for s in self.sessions.values() if s.challenge_id == challenge_id]
            known_ids = set(self.sessions.keys())
        if session is not None:
            try:
                recs = list(session.exec(
                    select(AgentSessionRecord).where(
                        AgentSessionRecord.challenge_id == challenge_id
                    )
                ).all())
                for rec in recs:
                    if rec.id not in known_ids:
                        s = _record_to_session(rec)
                        with self._lock:
                            self.sessions[s.id] = s
                        self._recover_if_orphaned(session, s)
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
        with self._lock:
            for f in findings:
                if f and f not in sess.findings:
                    sess.findings.append(f)

    def _sandbox_signature(self, challenge: Challenge) -> tuple[tuple[str, int], ...]:
        if self.runner is None or not hasattr(self.runner, "sandbox_workdir"):
            return ()
        try:
            wd = self.runner.sandbox_workdir(challenge)
            return tuple(
                sorted((p.name, p.stat().st_size) for p in wd.iterdir() if p.is_file())
            )
        except Exception:
            return ()

    def _build_final_report(self, challenge_id: int, sess: AgentSession,
                            status: str, flag_candidate: str | None = None,
                            *, session: Session | None = None,
                            challenge: Challenge | None = None) -> FinalReport:
        summary = [f"dispatches={sess.dispatch_count}", f"tokens={sess.total_tokens}"]
        summary.extend(sess.findings[-10:])
        writeup_md: str | None = None
        ch = challenge or (session.get(Challenge, challenge_id) if session is not None else None)
        storage = getattr(self.runner, "storage", None)
        if session is not None and ch is not None and status in ("solved", "gave_up"):
            try:
                writeup_md = build_challenge_writeup(
                    session, ch, agent_session=sess, storage=storage
                )
                persist_writeup_note_and_attachment(
                    session, ch, writeup_md, storage=storage
                )
            except Exception:
                writeup_md = None
        report = FinalReport(
            challenge_id=challenge_id,
            status=status,  # solved | failed | gave_up | awaiting_input
            attempts_summary=summary,
            flag_candidate=flag_candidate,
            writeup_md=writeup_md,
        )
        sess.final_report = report
        return report

    def _accumulate_usage(self, sess: AgentSession, agent_obj) -> str:
        usage = getattr(agent_obj, "last_usage", None)
        if not usage:
            return ""
        inp = int(usage.get("input_tokens", 0) or 0)
        out = int(usage.get("output_tokens", 0) or 0)
        tot = int(usage.get("total_tokens", 0) or (inp + out))
        with self._lock:
            sess.total_tokens += tot
        return f" | tokens(in={inp},out={out})"

    def _resolve_specialist(self, category: str) -> tuple[str, object | None, str | None]:
        raw = (category or "misc").strip().lower()
        if raw in self.specialists:
            return raw, self.specialists[raw], None
        aliased = CATEGORY_ALIASES.get(raw)
        if aliased and aliased in self.specialists:
            return aliased, self.specialists[aliased], f"category '{raw}' routed to '{aliased}'"
        if "misc" in self.specialists:
            return "misc", self.specialists["misc"], f"category '{raw}' routed to fallback 'misc'"
        return raw, None, None

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
            self._build_final_report(challenge.id, sess, "gave_up",
                                     session=session, challenge=challenge)
            self._save(session, sess)
            return

        decision = self.jury.decide(session, challenge, sess.reports,
                                    extra_feedback=extra_feedback)
        self._accumulate_usage(sess, self.jury)
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

        extra_feedback: str | None = None

        if decision.action == "GIVE_UP":
            agent_session.state = "gave_up"
            agent_session.pending_decision = None
            tracking.record_attempt(session, challenge.id, AttemptType.agent,
                                    f"jury gave up: {decision.reason}", AttemptResult.failed)
            self._build_final_report(challenge.id, agent_session, "gave_up",
                                     session=session, challenge=challenge)
            self._save(session, agent_session)
            return True

        if decision.action == "DISPATCH":
            agent_session.dispatch_count += 1
            last_report = self._dispatch(session, challenge, agent_session, decision)

            # Fast-path in auto mode when specialist already produced a valid flag
            cand = (last_report.flag_candidate or "").strip()
            if agent_session.mode == "auto" and last_report.status == "SOLVED" and cand:
                res = validate_flag(session, challenge, cand)
                if res.accepted:
                    agent_session.state = "solved"
                    agent_session.pending_decision = None
                    self._build_final_report(challenge.id, agent_session, "solved",
                                             flag_candidate=cand,
                                             session=session, challenge=challenge)
                    self._save(session, agent_session)
                    return True
                msg = f"specialist candidate {cand!r} rejected: {res.reason}"
                self._accumulate(agent_session, [msg])
                extra_feedback = msg

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
                self._build_final_report(challenge.id, agent_session, "gave_up",
                                         session=session, challenge=challenge)
                self._save(session, agent_session)
                return True
            if action == "RECON":
                self._accumulate(agent_session, [reason])
                cur_sig = self._sandbox_signature(challenge)
                if cur_sig != agent_session.last_recon_sig:
                    digest = recon_bundle(self.runner, session, challenge)
                    agent_session.last_recon_sig = cur_sig
                    self._accumulate(agent_session, [digest.summary] if digest.summary else [])
                pivot_msg = (
                    "2 consecutive specialist dispatches FAILED. Pivot strategy: choose a "
                    "different attack technique/category or check existing sandbox files instead "
                    "of repeating failed commands."
                )
                extra_feedback = f"{extra_feedback}\n{pivot_msg}".strip() if extra_feedback else pivot_msg

        elif decision.action == "RECHECK":
            digest = recon_bundle(self.runner, session, challenge)
            agent_session.last_recon_sig = self._sandbox_signature(challenge)
            self._accumulate(agent_session, [digest.summary] if digest.summary else [])

        elif decision.action == "SUBMIT":
            candidate = (decision.flag_candidate or "").strip()
            if candidate:
                result = validate_flag(session, challenge, candidate)
                if result.accepted:
                    agent_session.state = "solved"
                    agent_session.pending_decision = None
                    self._build_final_report(challenge.id, agent_session, "solved",
                                             flag_candidate=candidate,
                                             session=session, challenge=challenge)
                    self._save(session, agent_session)
                    return True
                msg = f"submission rejected ({candidate}): {result.reason}"
                self._accumulate(agent_session, [msg])
                extra_feedback = msg
            else:
                msg = "Jury chose SUBMIT without flag_candidate — supply a concrete flag or DISPATCH next step."
                self._accumulate(agent_session, [msg])
                extra_feedback = msg

        elif decision.action == "WAIT_USER":
            agent_session.state = "awaiting_approval"
            self._save(session, agent_session)
            return True

        self._decide(session, challenge, agent_session, extra_feedback=extra_feedback)
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
                            state="running", mode=mode,
                            last_recon_sig=self._sandbox_signature(challenge))
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
        if decision.action == "WAIT_USER":
            # User approved continuing after WAIT_USER -> ask Jury for next action
            self._decide(session, challenge, agent_session,
                         extra_feedback="Operator resumed execution after WAIT_USER.")
        else:
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
        with self._lock:
            if challenge_id in self._active_workers.values():
                raise ValueError(f"active background worker already running for challenge {challenge_id}")
            sess = AgentSession(id=uuid.uuid4().hex, challenge_id=challenge_id,
                                state="running", mode=mode)
            self._active_workers[sess.id] = challenge_id
        self._save(session, sess)

        def _worker():
            try:
                with Session(engine) as bg_session:
                    challenge = self._get(bg_session, challenge_id)
                    digest = recon_bundle(self.runner, bg_session, challenge)
                    sess.last_recon_sig = self._sandbox_signature(challenge)
                    self._accumulate(sess, [digest.summary] if digest.summary else [])
                    self._decide(bg_session, challenge, sess)
                    if mode == "auto" and sess.state == "awaiting_approval":
                        self._run_auto_loop(bg_session, challenge, sess)
            except Exception as exc:
                sess.state = "rejected"
                sess.error = f"{type(exc).__name__}: {exc}"
                with Session(engine) as err_session:
                    self._save(err_session, sess)
            finally:
                with self._lock:
                    self._active_workers.pop(sess.id, None)

        threading.Thread(target=_worker, daemon=True).start()
        return sess

    def approve_background(self, engine, session: Session, session_id: str,
                           mode: str | None = None) -> AgentSession:
        agent_session = self.get_session(session, session_id)
        if agent_session is None:
            raise KeyError(session_id)
        if agent_session.pending_decision is None or agent_session.state not in ("awaiting_approval", "rejected"):
            raise ValueError(f"session {session_id} is not awaiting approval")
        with self._lock:
            if agent_session.challenge_id in self._active_workers.values():
                raise ValueError(f"active background worker already running for challenge {agent_session.challenge_id}")
            if mode in ("supervised", "auto"):
                agent_session.mode = mode
            agent_session.state = "running"
            self._active_workers[agent_session.id] = agent_session.challenge_id
        self._save(session, agent_session)

        def _worker():
            try:
                with Session(engine) as bg_session:
                    challenge = self._get(bg_session, agent_session.challenge_id)
                    if agent_session.pending_decision and agent_session.pending_decision.action == "WAIT_USER":
                        self._decide(bg_session, challenge, agent_session,
                                     extra_feedback="Operator resumed execution after WAIT_USER.")
                    else:
                        self._step(bg_session, challenge, agent_session)
                    if agent_session.mode == "auto" and agent_session.state == "awaiting_approval":
                        self._run_auto_loop(bg_session, challenge, agent_session)
            except Exception as exc:
                agent_session.state = "rejected"
                agent_session.error = f"{type(exc).__name__}: {exc}"
                with Session(engine) as err_session:
                    self._save(err_session, agent_session)
            finally:
                with self._lock:
                    self._active_workers.pop(agent_session.id, None)

        threading.Thread(target=_worker, daemon=True).start()
        return agent_session

    def _dispatch(self, session: Session, challenge: Challenge,
                  agent_session: AgentSession, decision: JuryDecision) -> SpecialistReport:
        order = decision.order
        resolved_cat, specialist, route_note = self._resolve_specialist(order.category)
        if resolved_cat in ("reversing", "pwn", "forensics", "crypto") and (order.max_tool_runs or 0) < 8:
            order = order.model_copy(update={"max_tool_runs": 8})
        if route_note:
            self._accumulate(agent_session, [route_note])
        usage_suffix = ""
        if specialist is None:
            self._accumulate(agent_session,
                             [f"no specialist for category '{order.category}'"])
            report = SpecialistReport(status="FAILED",
                                      findings=[f"no specialist for {order.category}"])
        else:
            try:
                report = specialist.run(session, challenge, order, runner=self.runner)
                usage_suffix = self._accumulate_usage(agent_session, specialist)
            except Exception as exc:  # model/tool failure must not kill the loop
                try:
                    session.rollback()
                except Exception:
                    pass
                report = SpecialistReport(
                    status="FAILED",
                    findings=[f"specialist crashed: {type(exc).__name__}: {exc}"],
                    next_step="retry with corrected tool calls or a simpler task")
        with self._lock:
            agent_session.reports.append(report)
        self._accumulate(agent_session, list(report.findings))
        learning = "; ".join(report.findings)[:1900] + usage_suffix
        tracking.record_attempt(
            session, challenge.id, AttemptType.agent,
            f"specialist {resolved_cat}: {report.status}",
            AttemptResult.breakthrough if report.status == "SOLVED" else AttemptResult.partial,
            learning=learning[:2000])
        return report
