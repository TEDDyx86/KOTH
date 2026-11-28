from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlmodel import Session, select

from ..models import AgentSessionRecord, Challenge, ChallengeStatus
from ..services import tracking
from ..tooling.sandbox import sandbox_container_name, stop_persistent_container
from .costs import session_cost_usd
from .payloads import Blackboard, FinalReport, JuryDecision, SpecialistReport
from .writeup import build_challenge_writeup, persist_writeup_note_and_attachment


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
    blackboard: Blackboard = field(default_factory=Blackboard)
    telemetry: list[dict] = field(default_factory=list)
    resume_count: int = 0
    extra_attempts: int = 0
    budget_started_at: datetime | None = None
    stop_requested: bool = False
    last_recon_sig: tuple[tuple[str, int], ...] | None = field(default=None, repr=False)
    last_dispatch_outputs: list[str] = field(default_factory=list, repr=False)
    live_events: list[dict] = field(default_factory=list, repr=False)
    queued_feedback: list[str] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "challenge_id": self.challenge_id,
            "state": self.state,
            "mode": self.mode,
            "dispatch_count": self.dispatch_count,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": session_cost_usd(self.telemetry),
            "resume_count": self.resume_count,
            "extra_attempts": self.extra_attempts,
            "budget_started_at": (
                self.budget_started_at.isoformat() if self.budget_started_at else None
            ),
            "pending_decision": (
                self.pending_decision.model_dump() if self.pending_decision else None
            ),
            "findings": self.findings,
            "reports": [r.model_dump() for r in self.reports],
            "final_report": (
                self.final_report.model_dump() if self.final_report else None
            ),
            "blackboard": self.blackboard.model_dump(),
            "telemetry": list(self.telemetry),
            "live_events": list(self.live_events[-15:]),
            "queued_feedback": list(self.queued_feedback),
            "error": self.error,
            "created_at": self.created_at.isoformat(),
        }


def _record_to_session(rec: AgentSessionRecord) -> AgentSession:
    pending = (
        JuryDecision.model_validate_json(rec.pending_decision_json)
        if rec.pending_decision_json
        else None
    )
    findings = json.loads(rec.findings_json or "[]")
    reports = [
        SpecialistReport.model_validate(r)
        for r in json.loads(rec.reports_json or "[]")
    ]
    final_report = (
        FinalReport.model_validate_json(rec.final_report_json)
        if rec.final_report_json
        else None
    )
    raw_bb = getattr(rec, "blackboard_json", None) or "{}"
    try:
        blackboard = Blackboard.model_validate_json(raw_bb)
    except Exception:
        blackboard = Blackboard()
    raw_tel = getattr(rec, "telemetry_json", None) or "[]"
    try:
        telemetry = json.loads(raw_tel)
        if not isinstance(telemetry, list):
            telemetry = []
    except Exception:
        telemetry = []
    created = rec.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    budget_started = getattr(rec, "budget_started_at", None)
    if budget_started is not None and budget_started.tzinfo is None:
        budget_started = budget_started.replace(tzinfo=timezone.utc)
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
        blackboard=blackboard,
        telemetry=telemetry,
        resume_count=int(getattr(rec, "resume_count", 0) or 0),
        extra_attempts=int(getattr(rec, "extra_attempts", 0) or 0),
        budget_started_at=budget_started,
    )


class SessionStore:
    """Thread-safe in-memory + SQLite persistence for AgentSession instances."""

    def __init__(
        self,
        sessions: dict[str, AgentSession],
        lock: threading.RLock,
        active_workers: dict[str, int],
    ) -> None:
        self.sessions = sessions
        self._lock = lock
        self._active_workers = active_workers

    def save(self, session: Session | None, sess: AgentSession) -> None:
        with self._lock:
            self.sessions[sess.id] = sess
            snapshot = sess.to_dict()
            pending_json = (
                sess.pending_decision.model_dump_json() if sess.pending_decision else None
            )
            final_json = (
                sess.final_report.model_dump_json() if sess.final_report else None
            )
            bb_json = sess.blackboard.model_dump_json()
            tel_json = json.dumps(sess.telemetry[-40:], ensure_ascii=False)
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
            rec.blackboard_json = bb_json
            rec.telemetry_json = tel_json
            rec.resume_count = int(snapshot["resume_count"])
            rec.extra_attempts = int(snapshot["extra_attempts"])
            rec.budget_started_at = sess.budget_started_at
            rec.error = snapshot["error"]
            rec.updated_at = now
            session.add(rec)
            session.commit()
        except Exception:
            session.rollback()

    def recover_if_orphaned(self, session: Session | None, sess: AgentSession) -> AgentSession:
        with self._lock:
            if sess.state == "running" and sess.id not in self._active_workers:
                if sess.pending_decision is not None:
                    sess.state = "awaiting_approval"
                    sess.error = sess.error or "sessão recuperada após reinício (aguardando aprovação)"
                else:
                    sess.state = "rejected"
                    sess.error = sess.error or "sessão interrompida por reinício do servidor"
                self.save(session, sess)
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
        return self.recover_if_orphaned(session, sess)

    def list_for_challenge(self, session: Session | None, challenge_id: int) -> list[AgentSession]:
        with self._lock:
            matches = [s for s in self.sessions.values() if s.challenge_id == challenge_id]
            known_ids = set(self.sessions.keys())
        if session is not None:
            try:
                recs = list(
                    session.exec(
                        select(AgentSessionRecord).where(
                            AgentSessionRecord.challenge_id == challenge_id
                        )
                    ).all()
                )
                for rec in recs:
                    if rec.id not in known_ids:
                        s = _record_to_session(rec)
                        with self._lock:
                            self.sessions[s.id] = s
                        self.recover_if_orphaned(session, s)
                        matches.append(s)
            except Exception:
                pass
        matches.sort(key=lambda s: s.created_at, reverse=True)
        return matches

    def latest_for_challenge(self, session: Session | None, challenge_id: int) -> AgentSession | None:
        matches = self.list_for_challenge(session, challenge_id)
        return matches[0] if matches else None

    def accumulate(
        self,
        sess: AgentSession,
        findings: list[str],
        *,
        dead_ends: list[str] | None = None,
        hypotheses: list[str] | None = None,
        targets: list[str] | None = None,
    ) -> None:
        with self._lock:
            for f in findings:
                if f and f not in sess.findings:
                    sess.findings.append(f)
            sess.blackboard.merge(
                facts=findings,
                dead_ends=dead_ends,
                hypotheses=hypotheses,
                targets=targets,
            )


def cleanup_persistent_container(runner, challenge: Challenge | None) -> None:
    if challenge is None or runner is None:
        return
    settings = getattr(runner, "settings", None)
    defaults = getattr(settings, "defaults", None) if settings is not None else None
    if not getattr(defaults, "persistent_docker", False):
        return
    try:
        cname = sandbox_container_name(challenge.event_id, challenge.id)
        stop_persistent_container(cname)
    except Exception:
        pass


def build_final_report(
    store: SessionStore,
    runner,
    challenge_id: int,
    sess: AgentSession,
    status: str,
    flag_candidate: str | None = None,
    *,
    session: Session | None = None,
    challenge: Challenge | None = None,
) -> FinalReport:
    summary = [f"dispatches={sess.dispatch_count}", f"tokens={sess.total_tokens}"]
    summary.extend(sess.findings[-10:])
    writeup_md: str | None = None
    ch = challenge or (session.get(Challenge, challenge_id) if session is not None else None)
    storage = getattr(runner, "storage", None)
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
        status=status,
        attempts_summary=summary,
        flag_candidate=flag_candidate,
        writeup_md=writeup_md,
    )
    sess.final_report = report
    if status in ("solved", "gave_up"):
        cleanup_persistent_container(runner, ch)
    return report


def prepare_resume_state(
    store: SessionStore,
    session: Session,
    session_id: str,
    limits: dict,
    *,
    mode: str | None = None,
    feedback: str | None = None,
    new_target: str | None = None,
    extra_attempts: int | None = None,
    clear_dead_ends: bool = False,
    register_worker: bool = False,
) -> tuple[AgentSession, Challenge, dict]:
    agent_session = store.get_session(session, session_id)
    if agent_session is None:
        raise KeyError(session_id)
    if agent_session.state == "solved":
        raise ValueError(f"session {session_id} is already solved")
    if agent_session.state not in ("gave_up", "rejected"):
        raise ValueError(f"session {session_id} cannot be resumed from state '{agent_session.state}'")

    cleaned_target = (new_target or "").strip() or None
    cleaned_feedback = (feedback or "").strip() or None

    with store._lock:
        if register_worker and agent_session.challenge_id in store._active_workers.values():
            raise ValueError(
                f"active background worker already running for challenge {agent_session.challenge_id}"
            )
        challenge = session.get(Challenge, agent_session.challenge_id)
        if challenge is None:
            raise LookupError(f"challenge {agent_session.challenge_id} not found")
        if challenge.status == ChallengeStatus.gave_up:
            challenge = tracking.update_challenge(session, challenge.id, status=ChallengeStatus.open)
        if cleaned_target:
            challenge = tracking.update_challenge(session, challenge.id, target=cleaned_target)

        prev_state = agent_session.state
        prev_error = agent_session.error
        if mode in ("supervised", "auto"):
            agent_session.mode = mode
        agent_session.stop_requested = False
        agent_session.error = None
        agent_session.pending_decision = None
        agent_session.final_report = None
        agent_session.last_dispatch_outputs = []
        agent_session.resume_count += 1
        agent_session.budget_started_at = datetime.now(timezone.utc)

        base_max = int(limits.get("max_attempts", 0) or 0) if limits else 0
        if base_max > 0:
            grant = max(1, min(base_max, 10)) if extra_attempts is None else max(0, int(extra_attempts))
            min_new_cap = agent_session.dispatch_count + max(1, grant)
            agent_session.extra_attempts = max(
                agent_session.extra_attempts + grant, min_new_cap - base_max
            )
            remaining_dispatches = (base_max + agent_session.extra_attempts) - agent_session.dispatch_count
        else:
            grant = max(0, int(extra_attempts)) if extra_attempts is not None else 0
            agent_session.extra_attempts += grant
            remaining_dispatches = grant

        if clear_dead_ends:
            agent_session.blackboard.dead_ends = []
        if cleaned_target:
            agent_session.last_recon_sig = None
            agent_session.blackboard.dead_ends = [
                d for d in agent_session.blackboard.dead_ends
                if "unreachable target" not in d.lower()
            ]
            agent_session.blackboard.merge(targets=[cleaned_target])

        agent_session.state = "running"
        if register_worker:
            store._active_workers[agent_session.id] = agent_session.challenge_id

    summary_parts = [f"from={prev_state}"]
    if cleaned_target:
        summary_parts.append(f"target={cleaned_target}")
    if clear_dead_ends:
        summary_parts.append("dead_ends_cleared")
    if cleaned_feedback:
        summary_parts.append(f"hint={cleaned_feedback}")
    note_text = f"[agent resumed #{agent_session.resume_count}] " + " | ".join(summary_parts)
    tracking.add_note(session, challenge.id, note_text)
    store.accumulate(agent_session, [note_text])
    store.save(session, agent_session)
    return agent_session, challenge, {
        "prev_state": prev_state,
        "prev_error": prev_error,
        "cleaned_target": cleaned_target,
        "cleaned_feedback": cleaned_feedback,
        "clear_dead_ends": clear_dead_ends,
        "remaining_dispatches": remaining_dispatches,
    }


def build_resume_feedback(agent_session: AgentSession, meta: dict) -> str:
    lines = [
        f"Operator resumed closed session (resume #{agent_session.resume_count}, previous state={meta['prev_state']}).",
        f"Budget refreshed: {meta['remaining_dispatches'] or 'unlimited'} dispatch(es) remaining and fresh time window.",
    ]
    if meta["prev_error"]:
        lines.append(f"Previous error cleared: {meta['prev_error']}")
    if meta["cleaned_target"]:
        lines.append(f"Target updated to: {meta['cleaned_target']} (ignore old connection errors).")
    if meta["clear_dead_ends"]:
        lines.append("Operator cleared prior dead_ends — previous paths may be reconsidered.")
    if meta["cleaned_feedback"]:
        lines.append(f"Operator instruction/hint: {meta['cleaned_feedback']}")
    lines.append(
        "Do NOT immediately return GIVE_UP on this resumed turn; propose a concrete new DISPATCH or RECHECK incorporating the context above."
    )
    return "\n".join(lines)


def apply_target_update_state(
    store: SessionStore,
    session: Session,
    agent_session: AgentSession,
    cleaned_target: str,
) -> Challenge:
    challenge = tracking.update_challenge(session, agent_session.challenge_id, target=cleaned_target)
    note = f"[target updated] Novo target: {cleaned_target}. Erros de conexão do target anterior são obsoletos."
    tracking.add_note(session, challenge.id, note)
    with store._lock:
        agent_session.error = None
        agent_session.last_recon_sig = None
        agent_session.last_dispatch_outputs = []
        agent_session.blackboard.dead_ends = [
            d for d in agent_session.blackboard.dead_ends
            if "unreachable target" not in d.lower()
        ]
        agent_session.blackboard.merge(targets=[cleaned_target])
    return challenge


def approve_discovered_target_state(
    store: SessionStore,
    runner,
    session: Session,
    session_id: str,
    target_url: str,
    log_live,
) -> AgentSession:
    agent_session = store.get_session(session, session_id)
    if agent_session is None:
        raise KeyError(session_id)
    cleaned = (target_url or "").strip()
    if not cleaned:
        raise ValueError("target cannot be empty")
    challenge = session.get(Challenge, agent_session.challenge_id)
    if challenge is None:
        raise LookupError(f"challenge {agent_session.challenge_id} not found")
    with store._lock:
        agent_session.blackboard.merge(targets=[cleaned], approved_targets=[cleaned])
    if runner is not None and hasattr(runner, "approve_target"):
        runner.approve_target(challenge.id, cleaned)
    tracking.add_note(session, challenge.id, f"[target approved] {cleaned}")
    store.accumulate(agent_session, [f"approved discovered target: {cleaned}"])
    log_live(agent_session, "target", f"target liberado: {cleaned}")
    store.save(session, agent_session)
    return agent_session


def queue_feedback_state(
    store: SessionStore,
    session: Session,
    session_id: str,
    feedback: str,
    log_live,
) -> AgentSession:
    agent_session = store.get_session(session, session_id)
    if agent_session is None:
        raise KeyError(session_id)
    cleaned = (feedback or "").strip()
    if not cleaned:
        raise ValueError("feedback cannot be empty")
    challenge = session.get(Challenge, agent_session.challenge_id)
    if challenge is None:
        raise LookupError(f"challenge {agent_session.challenge_id} not found")
    with store._lock:
        agent_session.queued_feedback.append(cleaned)
    tracking.add_note(session, challenge.id, f"[live hint] {cleaned}")
    log_live(agent_session, "hint", f"dica enfileirada: {cleaned[:80]}")
    store.save(session, agent_session)
    return agent_session


def terminate_session_state(
    store: SessionStore,
    runner,
    session: Session,
    session_id: str,
    reason: str = "",
) -> AgentSession:
    agent_session = store.get_session(session, session_id)
    if agent_session is None:
        raise KeyError(session_id)
    agent_session.stop_requested = True
    agent_session.state = "rejected"
    agent_session.pending_decision = None
    store.accumulate(agent_session, [f"terminated by operator: {reason}"])
    ch = session.get(Challenge, agent_session.challenge_id) if session is not None else None
    build_final_report(store, runner, agent_session.challenge_id, agent_session, "gave_up", session=session, challenge=ch)
    cleanup_persistent_container(runner, ch)
    tracking.add_note(session, agent_session.challenge_id, f"[agent terminated] {reason}")
    store.save(session, agent_session)
    return agent_session


def prepare_approve_state(
    store: SessionStore,
    session: Session,
    session_id: str,
    *,
    mode: str | None = None,
    register_worker: bool = False,
) -> tuple[AgentSession, JuryDecision]:
    agent_session = store.get_session(session, session_id)
    if agent_session is None:
        raise KeyError(session_id)
    decision = agent_session.pending_decision
    if decision is None or agent_session.state not in ("awaiting_approval", "rejected"):
        raise ValueError(f"session {session_id} is not awaiting approval")
    with store._lock:
        if register_worker and agent_session.challenge_id in store._active_workers.values():
            raise ValueError(
                f"active background worker already running for challenge {agent_session.challenge_id}"
            )
        if mode in ("supervised", "auto"):
            agent_session.mode = mode
        agent_session.stop_requested = False
        agent_session.error = None
        agent_session.state = "running"
        if register_worker:
            store._active_workers[agent_session.id] = agent_session.challenge_id
    store.save(session, agent_session)
    return agent_session, decision


def sandbox_signature(runner, challenge: Challenge) -> tuple[tuple[str, int], ...]:
    if runner is None or not hasattr(runner, "sandbox_workdir"):
        return ()
    try:
        wd = runner.sandbox_workdir(challenge)
        return tuple(sorted((p.name, p.stat().st_size) for p in wd.iterdir() if p.is_file()))
    except Exception:
        return ()



