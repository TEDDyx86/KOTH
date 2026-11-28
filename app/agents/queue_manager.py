from __future__ import annotations

import threading
import uuid
from typing import Callable

from sqlmodel import Session

from ..models import Challenge, ChallengeStatus
from .session_store import AgentSession


class BatchQueueManager:
    """Manages daemon worker execution and bounded concurrency for multi-challenge queues."""

    def __init__(
        self,
        lock: threading.RLock,
        active_workers: dict[str, int],
        save_fn: Callable[[Session | None, AgentSession], None],
        log_live_fn: Callable[[AgentSession, str, str], None],
    ) -> None:
        self._lock = lock
        self._active_workers = active_workers
        self._save = save_fn
        self._log_live = log_live_fn

    def launch_worker(
        self,
        engine,
        sess: AgentSession,
        fn: Callable[[Session], None],
        *,
        semaphore: threading.Semaphore | None = None,
    ) -> None:
        """Run `fn(bg_session)` inside a daemon thread, updating error state and cleaning `_active_workers`."""

        def _worker() -> None:
            ctx = semaphore if semaphore is not None else _null_context()
            with ctx:
                try:
                    with Session(engine) as bg_session:
                        fn(bg_session)
                except Exception as exc:
                    sess.state = "rejected"
                    sess.error = f"{type(exc).__name__}: {exc}"
                    with Session(engine) as err_session:
                        self._save(err_session, sess)
                finally:
                    with self._lock:
                        self._active_workers.pop(sess.id, None)

        threading.Thread(target=_worker, daemon=True).start()

    def start_batch_background(
        self,
        engine,
        session: Session,
        challenge_ids: list[int],
        *,
        mode: str = "auto",
        max_concurrent: int = 3,
        run_challenge_fn: Callable[[Session, int, AgentSession, str], None],
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

            self.launch_worker(
                engine,
                sess,
                lambda bg_s, target_cid=cid, target_sess=sess: run_challenge_fn(
                    bg_s, target_cid, target_sess, mode
                ),
                semaphore=sem,
            )

        return results


class _null_context:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
