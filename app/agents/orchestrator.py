import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlmodel import Session, select

from ..models import AgentSessionRecord, Attachment, AttachmentType, AttemptResult, AttemptType, Challenge, ChallengeStatus
from ..services import tracking
from ..tooling.sandbox import sandbox_container_name, stop_persistent_container
from .costs import estimate_cost_usd, session_cost_usd
from .jury import JuryAgent
from .payloads import Blackboard, FinalReport, JuryDecision, SpecialistReport
from .planner import check_stop_criteria, reassess_strategy
from .recon import recon_bundle
from .submission import is_placeholder_flag, validate_flag
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


def _normalize_flag_candidate(val: str | None) -> str | None:
    cleaned = (val or "").strip()
    if not cleaned or cleaned.lower() in ("none", "null", "n/a", "-", "unknown"):
        return None
    return cleaned


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
            "budget_started_at": (self.budget_started_at.isoformat()
                                  if self.budget_started_at else None),
            "pending_decision": (self.pending_decision.model_dump()
                                 if self.pending_decision else None),
            "findings": self.findings,
            "reports": [r.model_dump() for r in self.reports],
            "final_report": (self.final_report.model_dump()
                             if self.final_report else None),
            "blackboard": self.blackboard.model_dump(),
            "telemetry": list(self.telemetry),
            "live_events": list(self.live_events[-15:]),
            "queued_feedback": list(self.queued_feedback),
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


_UNRESOLVED_NEXT_HINTS = (
    "to obtain",
    "to confirm",
    "obtain/confirm",
    "confirm the flag",
    "retrieve the flag",
    "get the flag",
    "submit derived",
    "para obter",
    "confirmar a flag",
    "ainda falta",
)

_CONN_REFUSED_MARKERS = (
    "curl: (7)",
    "rc=7",
    "winerror 10061",
    "connectionrefusederror",
    "connection refused",
    "err_connection_refused",
    "econnrefused",
    "actively refused",
    "recusou ativamente",
    "nenhuma conexão pôde ser feita",
    "nenhuma conexao pode ser feita",
    "could not connect to server",
)
_CONN_SUCCESS_MARKERS = (
    "http/1.",
    "http/2",
    "200 ok",
    "302 found",
    "401 unauthorized",
    "404 not found",
    "banner",
)


def _has_download_attachments(session: Session | None, challenge: Challenge) -> bool:
    if session is None or challenge.id is None:
        return False
    try:
        row = session.exec(
            select(Attachment).where(
                Attachment.challenge_id == challenge.id,
                Attachment.type == AttachmentType.download,
            )
        ).first()
        return row is not None
    except Exception:
        return False


def _has_tool_attempts(session: Session | None, challenge: Challenge) -> bool:
    if session is None or challenge.id is None:
        return False
    try:
        from ..models import Attempt
        row = session.exec(
            select(Attempt).where(
                Attempt.challenge_id == challenge.id,
                Attempt.type == AttemptType.tool,
            )
        ).first()
        return row is not None
    except Exception:
        return False


def _has_verified_flag_evidence(
    session: Session | None,
    challenge: Challenge,
    report: SpecialistReport | None,
    candidate: str,
    tool_outputs: list[str],
    *,
    is_submit: bool = False,
) -> tuple[bool, str | None]:
    """Guard SOLVED/SUBMIT against fabricated or placeholder flags."""
    prefix = "submit blocked" if is_submit else "fast-path blocked"
    if is_placeholder_flag(candidate):
        return False, f"{prefix} for {candidate!r}: placeholder/empty value is not a real flag"

    if report is not None:
        next_low = (report.next_step or "").strip().lower()
        if next_low and any(hint in next_low for hint in _UNRESOLVED_NEXT_HINTS):
            return (
                False,
                f"{prefix} for {candidate!r}: report.next_step still indicates obtaining/confirming the flag",
            )

    has_downloads = _has_download_attachments(session, challenge)
    has_tool_runs = bool(tool_outputs) or _has_tool_attempts(session, challenge)
    requires_evidence = (
        (has_tool_runs and (bool(challenge.target) or has_downloads))
        if is_submit
        else (bool(tool_outputs) or bool(challenge.target) or has_downloads)
    )
    if requires_evidence:
        haystack_parts = list(tool_outputs)
        if session is not None and challenge.id is not None:
            try:
                for att in tracking.list_attempts(session, challenge.id):
                    if att.type != AttemptType.submission and att.learning:
                        haystack_parts.append(att.learning)
            except Exception:
                pass
        if candidate not in "\n".join(haystack_parts):
            return (
                False,
                f"{prefix} for {candidate!r}: flag literal was not observed in any tool/API output "
                "(do not wrap derived keys or hashes in flag format; execute/verify until the server or tool prints the literal flag)",
            )
    return True, None


def _detect_target_unreachable(challenge: Challenge, outputs: list[str]) -> str | None:
    if not challenge.target or not outputs:
        return None
    refused = 0
    succeeded = 0
    for out in outputs:
        low = (out or "").lower()
        if any(m in low for m in _CONN_REFUSED_MARKERS):
            refused += 1
        elif any(m in low for m in _CONN_SUCCESS_MARKERS):
            succeeded += 1
    if refused >= 3 and succeeded == 0:
        return (
            f"Target {challenge.target} appears unreachable ({refused} connection-refused errors, "
            "0 successful responses). Update the target port/URL and resume when the instance is back."
        )
    return None


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
        self._jury_lock = threading.Lock()
        self._spec_locks: dict[str, threading.Lock] = {}
        self._active_workers: dict[str, int] = {}  # session_id -> challenge_id

    def _spec_lock(self, key: str) -> threading.Lock:
        with self._lock:
            lk = self._spec_locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._spec_locks[key] = lk
            return lk

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

    def _effective_limits(self, sess: AgentSession) -> dict:
        if not self.limits:
            return {}
        out = dict(self.limits)
        base_max = int(out.get("max_attempts", 0) or 0)
        if base_max > 0 and sess.extra_attempts > 0:
            out["max_attempts"] = base_max + sess.extra_attempts
        return out

    def _budget_start(self, sess: AgentSession) -> datetime:
        return sess.budget_started_at or sess.created_at

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

    def list_for_challenge(self, session: Session | None, challenge_id: int) -> list[AgentSession]:
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
        matches.sort(key=lambda s: s.created_at, reverse=True)
        return matches

    def latest_for_challenge(self, session: Session | None, challenge_id: int) -> AgentSession | None:
        matches = self.list_for_challenge(session, challenge_id)
        return matches[0] if matches else None

    def _log_live(self, sess: AgentSession, kind: str, detail: str) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "kind": kind,
            "detail": (detail or "").strip()[:140],
        }
        with self._lock:
            sess.live_events.append(entry)
            if len(sess.live_events) > 15:
                sess.live_events = sess.live_events[-15:]

    def _cleanup_persistent_container(self, challenge: Challenge | None) -> None:
        if challenge is None or self.runner is None:
            return
        settings = getattr(self.runner, "settings", None)
        defaults = getattr(settings, "defaults", None) if settings is not None else None
        if not getattr(defaults, "persistent_docker", False):
            return
        try:
            cname = sandbox_container_name(challenge.event_id, challenge.id)
            stop_persistent_container(cname)
        except Exception:
            pass

    def _get(self, session: Session, challenge_id: int) -> Challenge:
        challenge = session.get(Challenge, challenge_id)
        if challenge is None:
            raise LookupError(f"challenge {challenge_id} not found")
        return challenge

    def _accumulate(self, sess: AgentSession, findings: list[str],
                    *, dead_ends: list[str] | None = None,
                    hypotheses: list[str] | None = None,
                    targets: list[str] | None = None) -> None:
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
        if status in ("solved", "gave_up"):
            self._cleanup_persistent_container(ch)
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

    def _run_recon(self, session: Session, challenge: Challenge, sess: AgentSession) -> None:
        self._log_live(sess, "recon", "executando recon inicial (arquivos, strings, nmap)")
        digest = recon_bundle(self.runner, session, challenge)
        sess.last_recon_sig = self._sandbox_signature(challenge)
        urls = getattr(digest, "discovered_urls", None) or []
        self._accumulate(
            sess,
            [digest.summary] if digest.summary else [],
            targets=urls,
        )
        self._log_live(sess, "recon", f"recon concluído ({len(urls)} URL(s) descoberta(s))")

    def _decide(self, session: Session, challenge: Challenge,
                sess: AgentSession, extra_feedback: str | None = None) -> None:
        if sess.stop_requested or sess.state == "rejected":
            sess.state = "rejected"
            sess.pending_decision = None
            self._save(session, sess)
            return

        stop = check_stop_criteria(
            self._budget_start(sess),
            sess.dispatch_count,
            self._effective_limits(sess),
        )
        if stop.stop:
            self._accumulate(sess, [stop.reason or "planner stop criteria reached"])
            tracking.record_attempt(session, challenge.id, AttemptType.agent,
                                    stop.reason or "planner stop criteria reached",
                                    AttemptResult.failed)
            self._build_final_report(challenge.id, sess, "gave_up",
                                     session=session, challenge=challenge)
            sess.pending_decision = None
            sess.state = "gave_up"
            self._cleanup_persistent_container(challenge)
            self._save(session, sess)
            return

        with self._lock:
            queued = list(sess.queued_feedback)
            sess.queued_feedback.clear()
        combined_feedback = extra_feedback or ""
        if queued:
            queued_txt = "Operator live hint(s):\n" + "\n".join(f"- {q}" for q in queued)
            combined_feedback = f"{combined_feedback}\n{queued_txt}".strip() if combined_feedback else queued_txt

        self._log_live(sess, "jury", "Júri avaliando evidências…")
        with self._jury_lock:
            try:
                decision = self.jury.decide(
                    session, challenge, sess.reports,
                    extra_feedback=combined_feedback or None,
                    blackboard=sess.blackboard.model_dump(),
                )
            except TypeError:
                decision = self.jury.decide(
                    session, challenge, sess.reports,
                    extra_feedback=combined_feedback or None,
                )
            self._accumulate_usage(sess, self.jury)
        if sess.stop_requested or sess.state == "rejected":
            sess.state = "rejected"
            sess.pending_decision = None
            self._save(session, sess)
            return
        sess.pending_decision = decision
        sess.state = "awaiting_approval"
        self._log_live(sess, "jury", f"decisão: {decision.action}")
        self._accumulate(
            sess,
            list(decision.findings),
            dead_ends=list(getattr(decision, "dead_ends", None) or []),
            hypotheses=list(getattr(decision, "hypotheses", None) or []),
        )
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
            tracking.record_attempt(session, challenge.id, AttemptType.agent,
                                    f"jury gave up: {decision.reason}", AttemptResult.failed)
            self._build_final_report(challenge.id, agent_session, "gave_up",
                                     session=session, challenge=challenge)
            agent_session.pending_decision = None
            agent_session.state = "gave_up"
            self._save(session, agent_session)
            return True

        if decision.action == "DISPATCH":
            agent_session.dispatch_count += 1
            last_report = self._dispatch(session, challenge, agent_session, decision)

            # Circuit-breaker: if target refused >=3 connections with 0 successes, pause for operator
            unreach_msg = _detect_target_unreachable(challenge, agent_session.last_dispatch_outputs)
            if unreach_msg:
                self._accumulate(agent_session, [unreach_msg], dead_ends=[f"unreachable target: {challenge.target}"])
                agent_session.pending_decision = JuryDecision(
                    action="WAIT_USER",
                    reason=unreach_msg,
                    findings=[unreach_msg],
                )
                agent_session.state = "awaiting_approval"
                self._save(session, agent_session)
                return True

            # Fast-path in auto mode when specialist already produced a valid, evidenced flag
            cand = _normalize_flag_candidate(last_report.flag_candidate)
            if agent_session.mode == "auto" and last_report.status == "SOLVED" and cand:
                evidenced, reject_reason = _has_verified_flag_evidence(
                    session, challenge, last_report, cand, agent_session.last_dispatch_outputs, is_submit=False
                )
                if not evidenced:
                    msg = reject_reason or f"fast-path blocked for {cand!r}"
                    self._accumulate(agent_session, [msg])
                    extra_feedback = msg
                else:
                    res = validate_flag(session, challenge, cand)
                    if res.accepted:
                        self._build_final_report(challenge.id, agent_session, "solved",
                                                 flag_candidate=cand,
                                                 session=session, challenge=challenge)
                        agent_session.pending_decision = None
                        agent_session.state = "solved"
                        self._save(session, agent_session)
                        return True
                    msg = f"specialist candidate {cand!r} rejected: {res.reason}"
                    self._accumulate(agent_session, [msg])
                    extra_feedback = msg

            action, reason = reassess_strategy(
                self._budget_start(agent_session),
                agent_session.dispatch_count,
                agent_session.reports,
                self._effective_limits(agent_session),
            )
            if action == "GIVE_UP":
                self._accumulate(agent_session, [reason])
                tracking.record_attempt(session, challenge.id, AttemptType.agent,
                                        reason, AttemptResult.failed)
                self._build_final_report(challenge.id, agent_session, "gave_up",
                                         session=session, challenge=challenge)
                agent_session.pending_decision = None
                agent_session.state = "gave_up"
                self._save(session, agent_session)
                return True
            if action == "RECON":
                self._accumulate(agent_session, [reason])
                cur_sig = self._sandbox_signature(challenge)
                if cur_sig != agent_session.last_recon_sig:
                    self._run_recon(session, challenge, agent_session)
                pivot_msg = (
                    "2 consecutive specialist dispatches FAILED. Pivot strategy: choose a "
                    "different attack technique/category or check existing sandbox files instead "
                    "of repeating failed commands."
                )
                extra_feedback = f"{extra_feedback}\n{pivot_msg}".strip() if extra_feedback else pivot_msg

        elif decision.action == "RECHECK":
            self._run_recon(session, challenge, agent_session)

        elif decision.action == "SUBMIT":
            candidate = _normalize_flag_candidate(decision.flag_candidate)
            if candidate:
                last_rep = agent_session.reports[-1] if agent_session.reports else None
                evidenced, reject_reason = _has_verified_flag_evidence(
                    session, challenge, last_rep, candidate, agent_session.last_dispatch_outputs, is_submit=True
                )
                if not evidenced:
                    msg = reject_reason or f"SUBMIT blocked for {candidate!r}"
                    self._accumulate(agent_session, [msg])
                    extra_feedback = msg
                else:
                    result = validate_flag(session, challenge, candidate)
                    if result.accepted:
                        self._build_final_report(challenge.id, agent_session, "solved",
                                                 flag_candidate=candidate,
                                                 session=session, challenge=challenge)
                        agent_session.pending_decision = None
                        agent_session.state = "solved"
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
        sess = AgentSession(id=uuid.uuid4().hex, challenge_id=challenge_id,
                            state="running", mode=mode)
        self._save(session, sess)
        self._run_recon(session, challenge, sess)
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
        agent_session.stop_requested = False
        agent_session.error = None
        agent_session.state = "running"
        self._save(session, agent_session)
        if decision.action == "WAIT_USER":
            self._decide(session, challenge, agent_session,
                         extra_feedback="Operator resumed execution after WAIT_USER.")
        else:
            self._step(session, challenge, agent_session)
        if agent_session.mode == "auto" and agent_session.state == "awaiting_approval":
            self._run_auto_loop(session, challenge, agent_session)
        return agent_session

    def update_target_and_resume(
        self,
        session: Session,
        session_id: str,
        new_target: str,
        *,
        engine=None,
        mode: str | None = None,
        background: bool = False,
    ) -> AgentSession:
        agent_session = self.get_session(session, session_id)
        if agent_session is None:
            raise KeyError(session_id)
        cleaned = (new_target or "").strip()
        if not cleaned:
            raise ValueError("new target cannot be empty")
        if agent_session.state in ("gave_up", "rejected"):
            if background and engine is not None:
                return self.resume_background(
                    engine, session, session_id,
                    mode=mode, new_target=cleaned, rerun_recon=True,
                )
            return self.resume(
                session, session_id,
                mode=mode, new_target=cleaned, rerun_recon=True,
            )
        challenge = tracking.update_challenge(session, agent_session.challenge_id, target=cleaned)
        note = f"[target updated] Novo target: {cleaned}. Erros de conexão do target anterior são obsoletos."
        tracking.add_note(session, challenge.id, note)
        with self._lock:
            agent_session.error = None
            agent_session.last_recon_sig = None
            agent_session.last_dispatch_outputs = []
            agent_session.blackboard.dead_ends = [
                d for d in agent_session.blackboard.dead_ends
                if "unreachable target" not in d.lower()
            ]
            agent_session.blackboard.merge(targets=[cleaned])
        self._run_recon(session, challenge, agent_session)
        if agent_session.pending_decision is None:
            agent_session.pending_decision = JuryDecision(
                action="WAIT_USER",
                reason=f"Target updated to {cleaned}",
                findings=[note],
            )
        agent_session.state = "awaiting_approval"
        self._save(session, agent_session)
        if background and engine is not None:
            return self.approve_background(engine, session, session_id, mode=mode)
        return self.approve(session, session_id, mode=mode)

    def _prepare_resume(
        self,
        session: Session,
        session_id: str,
        *,
        mode: str | None = None,
        feedback: str | None = None,
        new_target: str | None = None,
        extra_attempts: int | None = None,
        clear_dead_ends: bool = False,
        register_worker: bool = False,
    ) -> tuple[AgentSession, Challenge, dict]:
        agent_session = self.get_session(session, session_id)
        if agent_session is None:
            raise KeyError(session_id)
        if agent_session.state == "solved":
            raise ValueError(f"session {session_id} is already solved")
        if agent_session.state not in ("gave_up", "rejected"):
            raise ValueError(f"session {session_id} cannot be resumed from state '{agent_session.state}'")

        cleaned_target = (new_target or "").strip() or None
        cleaned_feedback = (feedback or "").strip() or None

        with self._lock:
            if register_worker and agent_session.challenge_id in self._active_workers.values():
                raise ValueError(
                    f"active background worker already running for challenge {agent_session.challenge_id}"
                )
            challenge = self._get(session, agent_session.challenge_id)
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

            base_max = int(self.limits.get("max_attempts", 0) or 0) if self.limits else 0
            if base_max > 0:
                grant = max(1, min(base_max, 10)) if extra_attempts is None else max(0, int(extra_attempts))
                min_new_cap = agent_session.dispatch_count + max(1, grant)
                agent_session.extra_attempts = max(
                    agent_session.extra_attempts + grant,
                    min_new_cap - base_max,
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
                self._active_workers[agent_session.id] = agent_session.challenge_id

        summary_parts = [f"from={prev_state}"]
        if cleaned_target:
            summary_parts.append(f"target={cleaned_target}")
        if clear_dead_ends:
            summary_parts.append("dead_ends_cleared")
        if cleaned_feedback:
            summary_parts.append(f"hint={cleaned_feedback}")
        note_text = f"[agent resumed #{agent_session.resume_count}] " + " | ".join(summary_parts)
        tracking.add_note(session, challenge.id, note_text)
        self._accumulate(agent_session, [note_text])
        self._save(session, agent_session)
        return agent_session, challenge, {
            "prev_state": prev_state,
            "prev_error": prev_error,
            "cleaned_target": cleaned_target,
            "cleaned_feedback": cleaned_feedback,
            "clear_dead_ends": clear_dead_ends,
            "remaining_dispatches": remaining_dispatches,
        }

    def _execute_resume(
        self,
        session: Session,
        challenge: Challenge,
        agent_session: AgentSession,
        meta: dict,
        *,
        rerun_recon: bool = False,
    ) -> AgentSession:
        if self.runner is not None and hasattr(self.runner, "stage_downloads"):
            try:
                self.runner.stage_downloads(session, challenge)
            except Exception:
                pass
        cur_sig = self._sandbox_signature(challenge)
        sig_changed = (
            agent_session.last_recon_sig is not None
            and cur_sig != agent_session.last_recon_sig
        )
        if rerun_recon or meta["cleaned_target"] or sig_changed:
            self._run_recon(session, challenge, agent_session)

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
        resume_feedback = "\n".join(lines)

        self._decide(session, challenge, agent_session, extra_feedback=resume_feedback)
        if agent_session.mode == "auto" and agent_session.state == "awaiting_approval":
            self._run_auto_loop(session, challenge, agent_session)
        return agent_session

    def resume(
        self,
        session: Session,
        session_id: str,
        *,
        mode: str | None = None,
        feedback: str | None = None,
        new_target: str | None = None,
        extra_attempts: int | None = None,
        clear_dead_ends: bool = False,
        rerun_recon: bool = False,
    ) -> AgentSession:
        agent_session, challenge, meta = self._prepare_resume(
            session,
            session_id,
            mode=mode,
            feedback=feedback,
            new_target=new_target,
            extra_attempts=extra_attempts,
            clear_dead_ends=clear_dead_ends,
            register_worker=False,
        )
        return self._execute_resume(
            session, challenge, agent_session, meta, rerun_recon=rerun_recon,
        )

    def resume_background(
        self,
        engine,
        session: Session,
        session_id: str,
        *,
        mode: str | None = None,
        feedback: str | None = None,
        new_target: str | None = None,
        extra_attempts: int | None = None,
        clear_dead_ends: bool = False,
        rerun_recon: bool = False,
    ) -> AgentSession:
        agent_session, _challenge, meta = self._prepare_resume(
            session,
            session_id,
            mode=mode,
            feedback=feedback,
            new_target=new_target,
            extra_attempts=extra_attempts,
            clear_dead_ends=clear_dead_ends,
            register_worker=True,
        )

        def _worker():
            try:
                with Session(engine) as bg_session:
                    challenge = self._get(bg_session, agent_session.challenge_id)
                    self._execute_resume(
                        bg_session, challenge, agent_session, meta, rerun_recon=rerun_recon,
                    )
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

    def reject(self, session: Session, session_id: str, feedback: str) -> AgentSession:
        agent_session = self.get_session(session, session_id)
        if agent_session is None:
            raise KeyError(session_id)
        if agent_session.state != "awaiting_approval":
            raise ValueError(f"session {session_id} is not awaiting approval")
        challenge = self._get(session, agent_session.challenge_id)
        agent_session.error = None
        tracking.add_note(session, challenge.id, f"[agent rejected] {feedback}")
        self._decide(session, challenge, agent_session, extra_feedback=feedback)
        return agent_session

    def approve_discovered_target(
        self,
        session: Session,
        session_id: str,
        target_url: str,
    ) -> AgentSession:
        agent_session = self.get_session(session, session_id)
        if agent_session is None:
            raise KeyError(session_id)
        cleaned = (target_url or "").strip()
        if not cleaned:
            raise ValueError("target cannot be empty")
        challenge = self._get(session, agent_session.challenge_id)
        with self._lock:
            agent_session.blackboard.merge(targets=[cleaned], approved_targets=[cleaned])
        if self.runner is not None and hasattr(self.runner, "approve_target"):
            self.runner.approve_target(challenge.id, cleaned)
        tracking.add_note(session, challenge.id, f"[target approved] {cleaned}")
        self._accumulate(agent_session, [f"approved discovered target: {cleaned}"])
        self._log_live(agent_session, "target", f"target liberado: {cleaned}")
        self._save(session, agent_session)
        return agent_session

    def queue_feedback(
        self,
        session: Session,
        session_id: str,
        feedback: str,
    ) -> AgentSession:
        agent_session = self.get_session(session, session_id)
        if agent_session is None:
            raise KeyError(session_id)
        cleaned = (feedback or "").strip()
        if not cleaned:
            raise ValueError("feedback cannot be empty")
        challenge = self._get(session, agent_session.challenge_id)
        with self._lock:
            agent_session.queued_feedback.append(cleaned)
        tracking.add_note(session, challenge.id, f"[live hint] {cleaned}")
        self._log_live(agent_session, "hint", f"dica enfileirada: {cleaned[:80]}")
        self._save(session, agent_session)
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
        ch = session.get(Challenge, agent_session.challenge_id) if session is not None else None
        self._build_final_report(agent_session.challenge_id, agent_session, "gave_up",
                                 session=session, challenge=ch)
        self._cleanup_persistent_container(ch)
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
                    self._run_recon(bg_session, challenge, sess)
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

    def start_batch_background(
        self,
        engine,
        session: Session,
        challenge_ids: list[int],
        *,
        mode: str = "auto",
        max_concurrent: int = 3,
    ) -> list[dict]:
        """Launch multiple challenges concurrently with a bounded Semaphore."""
        sem = threading.Semaphore(max(1, int(max_concurrent)))
        results: list[dict] = []

        for cid in challenge_ids:
            ch = session.get(Challenge, cid)
            if ch is None:
                results.append({"challenge_id": cid, "started": False, "reason": "not found"})
                continue
            if ch.status == ChallengeStatus.solved:
                results.append({"challenge_id": cid, "started": False, "reason": "already solved"})
                continue
            with self._lock:
                if cid in self._active_workers.values():
                    results.append({"challenge_id": cid, "started": False, "reason": "already running"})
                    continue
                sess = AgentSession(
                    id=uuid.uuid4().hex,
                    challenge_id=cid,
                    state="running",
                    mode=mode,
                )
                self._active_workers[sess.id] = cid
            self._save(session, sess)
            self._log_live(sess, "batch", f"na fila paralela (concorrência={max_concurrent})")
            results.append({"challenge_id": cid, "started": True, "session_id": sess.id})

            def _batch_worker(target_cid=cid, target_sess=sess):
                with sem:
                    try:
                        with Session(engine) as bg_session:
                            challenge = self._get(bg_session, target_cid)
                            self._run_recon(bg_session, challenge, target_sess)
                            self._decide(bg_session, challenge, target_sess)
                            if mode == "auto" and target_sess.state == "awaiting_approval":
                                self._run_auto_loop(bg_session, challenge, target_sess)
                    except Exception as exc:
                        target_sess.state = "rejected"
                        target_sess.error = f"{type(exc).__name__}: {exc}"
                        with Session(engine) as err_session:
                            self._save(err_session, target_sess)
                    finally:
                        with self._lock:
                            self._active_workers.pop(target_sess.id, None)

            threading.Thread(target=_batch_worker, daemon=True).start()

        return results

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
            agent_session.stop_requested = False
            agent_session.error = None
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
        if self.runner is not None and hasattr(self.runner, "approve_target"):
            for t in agent_session.blackboard.approved_targets:
                self.runner.approve_target(challenge.id, t)
        self._log_live(agent_session, "dispatch", f"ciclo {agent_session.dispatch_count}: {resolved_cat} — {order.task[:70]}")
        usage_suffix = ""
        dispatch_outputs: list[str] = []
        detected_flags: list[str] = []
        if specialist is None:
            self._accumulate(agent_session,
                             [f"no specialist for category '{order.category}'"])
            report = SpecialistReport(status="FAILED",
                                      findings=[f"no specialist for {order.category}"])
        else:
            try:
                def _on_ev(kind: str, detail: str) -> None:
                    self._log_live(agent_session, kind, detail)

                with self._spec_lock(resolved_cat):
                    try:
                        report = specialist.run(
                            session, challenge, order, runner=self.runner,
                            blackboard=agent_session.blackboard.model_dump(),
                            on_event=_on_ev,
                        )
                    except TypeError:
                        try:
                            report = specialist.run(
                                session, challenge, order, runner=self.runner,
                                blackboard=agent_session.blackboard.model_dump(),
                            )
                        except TypeError:
                            report = specialist.run(session, challenge, order, runner=self.runner)
                    usage_suffix = self._accumulate_usage(agent_session, specialist)
                    dispatch_outputs = list(getattr(specialist, "last_tool_outputs", None) or [])
                    detected_flags = list(getattr(specialist, "last_detected_flags", None) or [])
                    metrics = getattr(specialist, "last_metrics", None)
                if isinstance(metrics, dict):
                    cost_usd = estimate_cost_usd(
                        str(metrics.get("model") or ""),
                        int(metrics.get("input_tokens") or 0),
                        int(metrics.get("output_tokens") or 0),
                    )
                    with self._lock:
                        agent_session.telemetry.append({
                            "cycle": agent_session.dispatch_count,
                            "category": resolved_cat,
                            "status": report.status,
                            "cost_usd": cost_usd,
                            **metrics,
                        })
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
            agent_session.last_dispatch_outputs = dispatch_outputs
            agent_session.reports.append(report)
        extra_flag_findings = [f"auto-detected flag in tool output: {fl}" for fl in detected_flags]
        self._accumulate(
            agent_session,
            [*list(report.findings), *extra_flag_findings],
            dead_ends=list(getattr(report, "dead_ends", None) or []),
        )
        self._log_live(agent_session, "dispatch", f"ciclo {agent_session.dispatch_count} → {report.status}")
        learning = "; ".join(report.findings)[:1900] + usage_suffix
        tracking.record_attempt(
            session, challenge.id, AttemptType.agent,
            f"specialist {resolved_cat}: {report.status}",
            AttemptResult.breakthrough if report.status == "SOLVED" else AttemptResult.partial,
            learning=learning[:2000])
        return report
