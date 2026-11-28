from __future__ import annotations

import concurrent.futures
import threading
import uuid
from datetime import datetime, timezone

from sqlmodel import Session

from ..models import AttemptResult, AttemptType, Challenge, ChallengeStatus
from ..services import tracking
from .costs import estimate_cost_usd
from .events import event_bus
from .jury import JuryAgent
from .payloads import Blackboard, FinalReport, JuryDecision, SpecialistReport, TaskOrder
from .planner import check_stop_criteria, reassess_strategy
from .queue_manager import BatchQueueManager
from .recon import recon_bundle
from .session_store import (
    AgentSession,
    SessionStore,
    _record_to_session,
    apply_target_update_state,
    approve_discovered_target_state,
    build_final_report,
    build_resume_feedback,
    cleanup_persistent_container,
    prepare_approve_state,
    prepare_resume_state,
    queue_feedback_state,
    sandbox_signature,
    terminate_session_state,
)
from .submission import (
    detect_target_unreachable as _detect_target_unreachable,
    has_verified_flag_evidence as _has_verified_flag_evidence,
    is_placeholder_flag,
    validate_flag,
)

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


class Orchestrator:
    """OP + PL + RC + JU + S1-S6 + SU loop.
    In 'supervised' mode (default), stops at awaiting_approval after each decision.
    In 'auto' mode, executes the loop until solved, gave_up, limits hit, or WAIT_USER."""

    def __init__(
        self,
        runner,
        jury: JuryAgent,
        specialists: dict[str, object],
        agents_limits: dict | None = None,
    ) -> None:
        self.runner = runner
        self.jury = jury
        self.specialists = specialists  # category -> SpecialistRunner
        self.limits = agents_limits or {}
        self.sessions: dict[str, AgentSession] = {}
        self._lock = threading.RLock()
        self._db_lock = threading.RLock()
        self._jury_lock = threading.Lock()
        self._spec_locks: dict[str, threading.Lock] = {}
        self._active_workers: dict[str, int] = {}  # session_id -> challenge_id
        self._cancel_events: dict[str, threading.Event] = {}
        self.event_bus = event_bus
        self._last_emitted_state: dict[str, str] = {}
        self._store = SessionStore(self.sessions, self._lock, self._active_workers)
        self._queue = BatchQueueManager(
            self._lock, self._active_workers, self._save, self._log_live
        )

    def _spec_lock(self, key: str) -> threading.Lock:
        with self._lock:
            lk = self._spec_locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._spec_locks[key] = lk
            return lk

    # -- persistence & helpers ---------------------------------------------
    def _save(self, session: Session | None, sess: AgentSession) -> None:
        self._store.save(session, sess)
        with self._lock:
            prev = self._last_emitted_state.get(sess.id)
            cur = sess.state
            changed = prev != cur
            if changed:
                self._last_emitted_state[sess.id] = cur
        if changed and getattr(self, "event_bus", None) is not None:
            self.event_bus.publish(
                sess.id,
                "state_change",
                {"state": cur, "dispatch_count": sess.dispatch_count, "error": sess.error},
            )

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
        return self._store.recover_if_orphaned(session, sess)

    def get_session(self, session: Session | None, session_id: str) -> AgentSession | None:
        return self._store.get_session(session, session_id)

    def list_for_challenge(self, session: Session | None, challenge_id: int) -> list[AgentSession]:
        return self._store.list_for_challenge(session, challenge_id)

    def latest_for_challenge(self, session: Session | None, challenge_id: int) -> AgentSession | None:
        return self._store.latest_for_challenge(session, challenge_id)

    def _log_live(
        self, sess: AgentSession, kind: str, detail: str, *, branch: str | None = None
    ) -> None:
        text = (detail or "").strip()[:140]
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "kind": f"{kind} [{branch}]" if branch else kind,
            "detail": text,
        }
        with self._lock:
            sess.live_events.append(entry)
            if len(sess.live_events) > 15:
                sess.live_events = sess.live_events[-15:]
        if getattr(self, "event_bus", None) is not None:
            self.event_bus.publish(sess.id, "live_event", entry)

    def _emit_stdout(
        self, sess: AgentSession, stream_name: str, chunk: str, *, branch: str | None = None
    ) -> None:
        text = (chunk or "").rstrip("\r\n")
        if not text or getattr(self, "event_bus", None) is None:
            return
        self.event_bus.publish(
            sess.id,
            "stdout_chunk",
            {
                "stream": stream_name,
                "branch": branch,
                "text": text[:300],
            },
        )

    def _cleanup_persistent_container(self, challenge: Challenge | None) -> None:
        cleanup_persistent_container(self.runner, challenge)

    def _get(self, session: Session, challenge_id: int) -> Challenge:
        challenge = session.get(Challenge, challenge_id)
        if challenge is None:
            raise LookupError(f"challenge {challenge_id} not found")
        return challenge

    def _accumulate(
        self,
        sess: AgentSession,
        findings: list[str],
        *,
        dead_ends: list[str] | None = None,
        hypotheses: list[str] | None = None,
        targets: list[str] | None = None,
    ) -> None:
        self._store.accumulate(
            sess, findings, dead_ends=dead_ends, hypotheses=hypotheses, targets=targets
        )

    def _sandbox_signature(self, challenge: Challenge) -> tuple[tuple[str, int], ...]:
        return sandbox_signature(self.runner, challenge)

    def _build_final_report(
        self,
        challenge_id: int,
        sess: AgentSession,
        status: str,
        flag_candidate: str | None = None,
        *,
        session: Session | None = None,
        challenge: Challenge | None = None,
    ) -> FinalReport:
        return build_final_report(
            self._store, self.runner, challenge_id, sess, status,
            flag_candidate=flag_candidate, session=session, challenge=challenge,
        )

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
        hyps = getattr(digest, "hypotheses", None) or []
        self._accumulate(
            sess,
            [digest.summary] if digest.summary else [],
            hypotheses=hyps,
            targets=urls,
        )
        self._log_live(sess, "recon", f"recon concluído ({len(urls)} URL(s) descoberta(s))")

    def _close_gave_up(
        self,
        session: Session,
        challenge: Challenge,
        sess: AgentSession,
        reason: str,
        *,
        record_attempt: bool = True,
    ) -> None:
        if record_attempt:
            tracking.record_attempt(
                session, challenge.id, AttemptType.agent, reason, AttemptResult.failed
            )
        self._build_final_report(
            challenge.id, sess, "gave_up", session=session, challenge=challenge
        )
        sess.pending_decision = None
        sess.state = "gave_up"
        self._cleanup_persistent_container(challenge)
        self._save(session, sess)

    def _mark_solved(
        self,
        session: Session,
        challenge: Challenge,
        sess: AgentSession,
        candidate: str,
    ) -> None:
        self._build_final_report(
            challenge.id, sess, "solved", flag_candidate=candidate,
            session=session, challenge=challenge,
        )
        sess.pending_decision = None
        sess.state = "solved"
        self._save(session, sess)

    def _decide(
        self,
        session: Session,
        challenge: Challenge,
        sess: AgentSession,
        extra_feedback: str | None = None,
    ) -> None:
        if sess.stop_requested or sess.state == "rejected":
            sess.state = "rejected"
            sess.pending_decision = None
            self._save(session, sess)
            return

        stop = check_stop_criteria(
            self._budget_start(sess), sess.dispatch_count, self._effective_limits(sess)
        )
        if stop.stop:
            msg = stop.reason or "planner stop criteria reached"
            self._accumulate(sess, [msg])
            self._close_gave_up(session, challenge, sess, msg)
            return

        with self._lock:
            queued = list(sess.queued_feedback)
            sess.queued_feedback.clear()
        combined_feedback = extra_feedback or ""
        if queued:
            queued_txt = "Operator live hint(s):\n" + "\n".join(f"- {q}" for q in queued)
            combined_feedback = (
                f"{combined_feedback}\n{queued_txt}".strip() if combined_feedback else queued_txt
            )

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

    def _try_accept_flag(
        self,
        session: Session,
        challenge: Challenge,
        agent_session: AgentSession,
        report: SpecialistReport | None,
        candidate: str,
        *,
        is_submit: bool,
    ) -> tuple[bool, str | None]:
        evidenced, reject_reason = _has_verified_flag_evidence(
            session, challenge, report, candidate,
            agent_session.last_dispatch_outputs, is_submit=is_submit,
        )
        if not evidenced:
            prefix = "SUBMIT blocked" if is_submit else "fast-path blocked"
            msg = reject_reason or f"{prefix} for {candidate!r}"
            self._accumulate(agent_session, [msg])
            return False, msg
        res = validate_flag(session, challenge, candidate)
        if res.accepted:
            self._mark_solved(session, challenge, agent_session, candidate)
            return True, None
        label = "submission rejected" if is_submit else "specialist candidate rejected"
        msg = (
            f"submission rejected ({candidate}): {res.reason}"
            if is_submit
            else f"specialist candidate {candidate!r} rejected: {res.reason}"
        )
        self._accumulate(agent_session, [msg])
        return False, msg

    def _step(
        self,
        session: Session,
        challenge: Challenge,
        agent_session: AgentSession,
    ) -> bool:
        """Execute ONE pending decision. Returns True if session reached a terminal
        or user-input state and should stop auto-looping."""
        decision = agent_session.pending_decision
        if decision is None:
            return True

        extra_feedback: str | None = None

        if decision.action == "GIVE_UP":
            self._close_gave_up(
                session, challenge, agent_session, f"jury gave up: {decision.reason}"
            )
            return True

        if decision.action == "DISPATCH":
            agent_session.dispatch_count += 1
            last_report = self._dispatch(session, challenge, agent_session, decision)

            unreach_msg = _detect_target_unreachable(
                challenge, agent_session.last_dispatch_outputs
            )
            if unreach_msg:
                self._accumulate(
                    agent_session,
                    [unreach_msg],
                    dead_ends=[f"unreachable target: {challenge.target}"],
                )
                agent_session.pending_decision = JuryDecision(
                    action="WAIT_USER", reason=unreach_msg, findings=[unreach_msg],
                )
                agent_session.state = "awaiting_approval"
                self._save(session, agent_session)
                return True

            cand = _normalize_flag_candidate(last_report.flag_candidate)
            if agent_session.mode == "auto" and last_report.status == "SOLVED" and cand:
                ok, err_msg = self._try_accept_flag(
                    session, challenge, agent_session, last_report, cand, is_submit=False
                )
                if ok:
                    return True
                extra_feedback = err_msg

            action, reason = reassess_strategy(
                self._budget_start(agent_session),
                agent_session.dispatch_count,
                agent_session.reports,
                self._effective_limits(agent_session),
            )
            if action == "GIVE_UP":
                self._accumulate(agent_session, [reason])
                self._close_gave_up(session, challenge, agent_session, reason)
                return True
            if action == "RECON":
                self._accumulate(agent_session, [reason])
                if self._sandbox_signature(challenge) != agent_session.last_recon_sig:
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
                ok, err_msg = self._try_accept_flag(
                    session, challenge, agent_session, last_rep, candidate, is_submit=True
                )
                if ok:
                    return True
                extra_feedback = err_msg
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

    def _run_auto_loop(
        self, session: Session, challenge: Challenge, sess: AgentSession
    ) -> None:
        while (
            not sess.stop_requested
            and sess.state == "awaiting_approval"
            and sess.pending_decision is not None
        ):
            if sess.pending_decision.action == "WAIT_USER":
                break
            sess.state = "running"
            self._save(session, sess)
            done = self._step(session, challenge, sess)
            if done or sess.stop_requested:
                break

    # -- public API --------------------------------------------------------
    def start(
        self, session: Session, challenge_id: int, mode: str = "supervised"
    ) -> AgentSession:
        challenge = self._get(session, challenge_id)
        sess = AgentSession(
            id=uuid.uuid4().hex, challenge_id=challenge_id, state="running", mode=mode
        )
        self._save(session, sess)
        self._run_recon(session, challenge, sess)
        self._decide(session, challenge, sess)
        if mode == "auto" and sess.state == "awaiting_approval":
            self._run_auto_loop(session, challenge, sess)
        return sess

    def _execute_approve(
        self, session: Session, challenge: Challenge, agent_session: AgentSession, decision: JuryDecision
    ) -> AgentSession:
        if decision.action == "WAIT_USER":
            self._decide(
                session, challenge, agent_session,
                extra_feedback="Operator resumed execution after WAIT_USER.",
            )
        else:
            self._step(session, challenge, agent_session)
        if agent_session.mode == "auto" and agent_session.state == "awaiting_approval":
            self._run_auto_loop(session, challenge, agent_session)
        return agent_session

    def approve(self, session: Session, session_id: str, mode: str | None = None) -> AgentSession:
        agent_session, decision = prepare_approve_state(
            self._store, session, session_id, mode=mode, register_worker=False
        )
        challenge = self._get(session, agent_session.challenge_id)
        return self._execute_approve(session, challenge, agent_session, decision)

    def update_target_and_resume(
        self, session: Session, session_id: str, new_target: str,
        *, engine=None, mode: str | None = None, background: bool = False,
    ) -> AgentSession:
        agent_session = self.get_session(session, session_id)
        if agent_session is None:
            raise KeyError(session_id)
        cleaned = (new_target or "").strip()
        if not cleaned:
            raise ValueError("new target cannot be empty")
        if agent_session.state in ("gave_up", "rejected"):
            if background and engine is not None:
                return self.resume_background(engine, session, session_id, mode=mode, new_target=cleaned, rerun_recon=True)
            return self.resume(session, session_id, mode=mode, new_target=cleaned, rerun_recon=True)
        challenge = apply_target_update_state(self._store, session, agent_session, cleaned)
        self._run_recon(session, challenge, agent_session)
        if agent_session.pending_decision is None:
            agent_session.pending_decision = JuryDecision(
                action="WAIT_USER", reason=f"Target updated to {cleaned}",
                findings=[f"[target updated] Novo target: {cleaned}"],
            )
        agent_session.state = "awaiting_approval"
        self._save(session, agent_session)
        if background and engine is not None:
            return self.approve_background(engine, session, session_id, mode=mode)
        return self.approve(session, session_id, mode=mode)

    def _prepare_resume(
        self, session: Session, session_id: str, *, mode: str | None = None,
        feedback: str | None = None, new_target: str | None = None,
        extra_attempts: int | None = None, clear_dead_ends: bool = False,
        register_worker: bool = False,
    ) -> tuple[AgentSession, Challenge, dict]:
        return prepare_resume_state(
            self._store, session, session_id, self.limits,
            mode=mode, feedback=feedback, new_target=new_target,
            extra_attempts=extra_attempts, clear_dead_ends=clear_dead_ends,
            register_worker=register_worker,
        )

    def _execute_resume(
        self, session: Session, challenge: Challenge, agent_session: AgentSession,
        meta: dict, *, rerun_recon: bool = False,
    ) -> AgentSession:
        if self.runner is not None and hasattr(self.runner, "stage_downloads"):
            try:
                self.runner.stage_downloads(session, challenge)
            except Exception:
                pass
        cur_sig = self._sandbox_signature(challenge)
        if rerun_recon or meta["cleaned_target"] or (
            agent_session.last_recon_sig is not None and cur_sig != agent_session.last_recon_sig
        ):
            self._run_recon(session, challenge, agent_session)
        self._decide(session, challenge, agent_session, extra_feedback=build_resume_feedback(agent_session, meta))
        if agent_session.mode == "auto" and agent_session.state == "awaiting_approval":
            self._run_auto_loop(session, challenge, agent_session)
        return agent_session

    def resume(
        self, session: Session, session_id: str, *, mode: str | None = None,
        feedback: str | None = None, new_target: str | None = None,
        extra_attempts: int | None = None, clear_dead_ends: bool = False,
        rerun_recon: bool = False,
    ) -> AgentSession:
        agent_session, challenge, meta = self._prepare_resume(
            session, session_id, mode=mode, feedback=feedback, new_target=new_target,
            extra_attempts=extra_attempts, clear_dead_ends=clear_dead_ends, register_worker=False,
        )
        return self._execute_resume(session, challenge, agent_session, meta, rerun_recon=rerun_recon)

    def resume_background(
        self, engine, session: Session, session_id: str, *, mode: str | None = None,
        feedback: str | None = None, new_target: str | None = None,
        extra_attempts: int | None = None, clear_dead_ends: bool = False,
        rerun_recon: bool = False,
    ) -> AgentSession:
        agent_session, _challenge, meta = self._prepare_resume(
            session, session_id, mode=mode, feedback=feedback, new_target=new_target,
            extra_attempts=extra_attempts, clear_dead_ends=clear_dead_ends, register_worker=True,
        )
        self._queue.launch_worker(
            engine, agent_session,
            lambda bg_s: self._execute_resume(
                bg_s, self._get(bg_s, agent_session.challenge_id),
                agent_session, meta, rerun_recon=rerun_recon,
            ),
        )
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

    def approve_discovered_target(self, session: Session, session_id: str, target_url: str) -> AgentSession:
        return approve_discovered_target_state(self._store, self.runner, session, session_id, target_url, self._log_live)

    def queue_feedback(self, session: Session, session_id: str, feedback: str) -> AgentSession:
        return queue_feedback_state(self._store, session, session_id, feedback, self._log_live)

    def terminate(self, session: Session, session_id: str, reason: str = "") -> AgentSession:
        """Operator rejection: stop the session for good (no new decision)."""
        with self._lock:
            ev = self._cancel_events.get(session_id)
            if ev is not None:
                ev.set()
        return terminate_session_state(self._store, self.runner, session, session_id, reason)

    # -- background execution ----------------------------------------------
    def _run_challenge_initial(
        self, bg_session: Session, challenge_id: int, sess: AgentSession, mode: str
    ) -> None:
        challenge = self._get(bg_session, challenge_id)
        self._run_recon(bg_session, challenge, sess)
        self._decide(bg_session, challenge, sess)
        if mode == "auto" and sess.state == "awaiting_approval":
            self._run_auto_loop(bg_session, challenge, sess)

    def start_background(
        self, engine, session: Session, challenge_id: int, mode: str = "supervised"
    ) -> AgentSession:
        self._get(session, challenge_id)
        with self._lock:
            if challenge_id in self._active_workers.values():
                raise ValueError(f"active background worker already running for challenge {challenge_id}")
            sess = AgentSession(
                id=uuid.uuid4().hex, challenge_id=challenge_id, state="running", mode=mode
            )
            self._active_workers[sess.id] = challenge_id
        self._save(session, sess)
        self._queue.launch_worker(
            engine, sess, lambda bg_s: self._run_challenge_initial(bg_s, challenge_id, sess, mode)
        )
        return sess

    def start_batch_background(
        self, engine, session: Session, challenge_ids: list[int],
        *, mode: str = "auto", max_concurrent: int = 3,
    ) -> list[dict]:
        return self._queue.start_batch_background(
            engine, session, challenge_ids, mode=mode, max_concurrent=max_concurrent,
            run_challenge_fn=self._run_challenge_initial,
        )

    def approve_background(self, engine, session: Session, session_id: str, mode: str | None = None) -> AgentSession:
        agent_session, decision = prepare_approve_state(
            self._store, session, session_id, mode=mode, register_worker=True
        )
        self._queue.launch_worker(
            engine, agent_session,
            lambda bg_s: self._execute_approve(bg_s, self._get(bg_s, agent_session.challenge_id), agent_session, decision),
        )
        return agent_session

    def _call_specialist(
        self,
        specialist,
        session: Session,
        challenge: Challenge,
        order: TaskOrder,
        *,
        blackboard_dict: dict,
        on_event,
        on_output_chunk,
        cancel_event: threading.Event | None,
        branch_label: str | None,
    ) -> SpecialistReport:
        try:
            return specialist.run(
                session, challenge, order, runner=self.runner,
                blackboard=blackboard_dict,
                on_event=on_event,
                on_output_chunk=on_output_chunk,
                cancel_event=cancel_event,
                branch_label=branch_label,
            )
        except TypeError:
            try:
                return specialist.run(
                    session, challenge, order, runner=self.runner,
                    blackboard=blackboard_dict, on_event=on_event, on_output_chunk=on_output_chunk,
                )
            except TypeError:
                try:
                    return specialist.run(
                        session, challenge, order, runner=self.runner,
                        blackboard=blackboard_dict, on_event=on_event,
                    )
                except TypeError:
                    try:
                        return specialist.run(
                            session, challenge, order, runner=self.runner,
                            blackboard=blackboard_dict,
                        )
                    except TypeError:
                        return specialist.run(session, challenge, order, runner=self.runner)

    def _dispatch_one(
        self,
        session: Session,
        challenge: Challenge,
        agent_session: AgentSession,
        order: TaskOrder,
        *,
        branch_label: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[SpecialistReport, list[str], list[str], str]:
        resolved_cat, specialist, route_note = self._resolve_specialist(order.category)
        if resolved_cat in ("reversing", "pwn", "forensics", "crypto") and (order.max_tool_runs or 0) < 8:
            order = order.model_copy(update={"max_tool_runs": 8})
        if route_note:
            self._accumulate(agent_session, [route_note])
        tag = branch_label or resolved_cat
        self._log_live(
            agent_session, "dispatch",
            f"ciclo {agent_session.dispatch_count}: {resolved_cat} — {order.task[:70]}",
            branch=branch_label,
        )
        usage_suffix = ""
        dispatch_outputs: list[str] = []
        detected_flags: list[str] = []
        if specialist is None:
            self._accumulate(agent_session, [f"no specialist for category '{order.category}'"])
            report = SpecialistReport(
                status="FAILED", findings=[f"no specialist for {order.category}"], branch=branch_label
            )
        else:
            try:
                def _on_ev(kind: str, detail: str) -> None:
                    self._log_live(agent_session, kind, detail, branch=branch_label)

                def _on_chunk(stream_name: str, chunk_txt: str) -> None:
                    self._emit_stdout(agent_session, stream_name, chunk_txt, branch=tag)

                with self._lock:
                    bb_snap = agent_session.blackboard.model_dump()

                with self._spec_lock(resolved_cat):
                    report = self._call_specialist(
                        specialist, session, challenge, order,
                        blackboard_dict=bb_snap,
                        on_event=_on_ev,
                        on_output_chunk=_on_chunk,
                        cancel_event=cancel_event,
                        branch_label=branch_label,
                    )
                    usage_suffix = self._accumulate_usage(agent_session, specialist)
                    dispatch_outputs = list(getattr(specialist, "last_tool_outputs", None) or [])
                    detected_flags = list(getattr(specialist, "last_detected_flags", None) or [])
                    metrics = getattr(specialist, "last_metrics", None)
                if branch_label and not getattr(report, "branch", None):
                    report = report.model_copy(update={"branch": branch_label})
                if isinstance(metrics, dict):
                    cost_usd = estimate_cost_usd(
                        str(metrics.get("model") or ""),
                        int(metrics.get("input_tokens") or 0),
                        int(metrics.get("output_tokens") or 0),
                    )
                    jury_metrics = getattr(self.jury, "last_metrics", None)
                    jury_model = jury_metrics.get("model") if isinstance(jury_metrics, dict) else None
                    with self._lock:
                        tel_row = {
                            "cycle": agent_session.dispatch_count,
                            "category": resolved_cat,
                            "status": report.status,
                            "cost_usd": cost_usd,
                            **metrics,
                        }
                        if jury_model:
                            tel_row["jury_model"] = jury_model
                        if branch_label:
                            tel_row["branch"] = branch_label
                        agent_session.telemetry.append(tel_row)
            except Exception as exc:
                with self._db_lock:
                    try:
                        session.rollback()
                    except Exception:
                        pass
                report = SpecialistReport(
                    status="FAILED",
                    findings=[f"specialist crashed: {type(exc).__name__}: {exc}"],
                    next_step="retry with corrected tool calls or a simpler task",
                    branch=branch_label,
                )
        extra_flag_findings = [f"auto-detected flag in tool output: {fl}" for fl in detected_flags]
        self._accumulate(
            agent_session,
            [*list(report.findings), *extra_flag_findings],
            dead_ends=list(getattr(report, "dead_ends", None) or []),
        )
        self._log_live(
            agent_session, "dispatch", f"ciclo {agent_session.dispatch_count} → {report.status}",
            branch=branch_label,
        )
        learning = "; ".join(report.findings)[:1900] + usage_suffix
        with self._db_lock:
            tracking.record_attempt(
                session, challenge.id, AttemptType.agent,
                f"specialist {resolved_cat}: {report.status}",
                AttemptResult.breakthrough if report.status == "SOLVED" else AttemptResult.partial,
                learning=learning[:2000],
            )
        return report, dispatch_outputs, detected_flags, resolved_cat

    def _dispatch_parallel(
        self,
        session: Session,
        challenge: Challenge,
        agent_session: AgentSession,
        orders: list[TaskOrder],
    ) -> SpecialistReport:
        max_b = max(1, min(int(self.limits.get("max_race_branches", 2) or 2), 2))
        race_orders = list(orders[:max_b])
        cancel_ev = threading.Event()
        with self._lock:
            self._cancel_events[agent_session.id] = cancel_ev

        branch_results: list[tuple[int, SpecialistReport, list[str], list[str]]] = []
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(race_orders)) as pool:
                fut_map = {}
                for idx, ord_item in enumerate(race_orders, 1):
                    resolved_cat, _, _ = self._resolve_specialist(ord_item.category)
                    lbl = f"Ramo {idx}: {resolved_cat}"
                    fut = pool.submit(
                        self._dispatch_one, session, challenge, agent_session, ord_item,
                        branch_label=lbl, cancel_event=cancel_ev,
                    )
                    fut_map[fut] = idx

                for fut in concurrent.futures.as_completed(fut_map):
                    idx = fut_map[fut]
                    rep, outs, det_flags, _cat = fut.result()
                    branch_results.append((idx, rep, outs, det_flags))
                    cand = _normalize_flag_candidate(rep.flag_candidate)
                    solved_hit = (
                        rep.status == "SOLVED" and cand and not is_placeholder_flag(cand)
                    ) or bool(det_flags)
                    if solved_hit or agent_session.stop_requested:
                        cancel_ev.set()
        finally:
            with self._lock:
                self._cancel_events.pop(agent_session.id, None)

        branch_results.sort(key=lambda t: t[0])
        all_outputs: list[str] = []
        all_reports: list[SpecialistReport] = []
        for _idx, rep, outs, _det in branch_results:
            all_outputs.extend(outs)
            all_reports.append(rep)

        with self._lock:
            agent_session.last_dispatch_outputs = all_outputs
            agent_session.reports.extend(all_reports)

        winner = next(
            (r for r in all_reports if r.status == "SOLVED" and _normalize_flag_candidate(r.flag_candidate)),
            None,
        ) or next((r for r in all_reports if r.status == "NEED_HINT"), None) or all_reports[0]
        return winner

    def _dispatch(
        self,
        session: Session,
        challenge: Challenge,
        agent_session: AgentSession,
        decision: JuryDecision,
    ) -> SpecialistReport:
        if self.runner is not None and hasattr(self.runner, "approve_target"):
            for t in agent_session.blackboard.approved_targets:
                self.runner.approve_target(challenge.id, t)

        allow_racing = bool(self.limits.get("allow_parallel_racing", True))
        max_b = max(1, min(int(self.limits.get("max_race_branches", 2) or 2), 2))
        parallel_orders = list(getattr(decision, "parallel_orders", None) or [])
        if allow_racing and max_b >= 2 and len(parallel_orders) >= 2:
            return self._dispatch_parallel(session, challenge, agent_session, parallel_orders)

        report, outs, _det, _cat = self._dispatch_one(
            session, challenge, agent_session, decision.order
        )
        with self._lock:
            agent_session.last_dispatch_outputs = outs
            agent_session.reports.append(report)
        return report
