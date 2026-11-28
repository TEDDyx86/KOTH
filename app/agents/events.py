from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
from typing import Callable


@dataclass
class _Subscriber:
    loop: asyncio.AbstractEventLoop | None
    queue: object  # asyncio.Queue | queue.Queue


@dataclass
class _SessionChannel:
    next_id: int = 1
    history: deque[dict] = field(default_factory=lambda: deque(maxlen=200))
    subscribers: list[_Subscriber] = field(default_factory=list)


class SessionEventBus:
    """Thread-safe per-session event bus bridging sync agent threads to async SSE clients."""

    def __init__(self, max_history: int = 200) -> None:
        self._max_history = max(20, int(max_history))
        self._lock = threading.RLock()
        self._channels: dict[str, _SessionChannel] = {}

    def _channel(self, session_id: str) -> _SessionChannel:
        ch = self._channels.get(session_id)
        if ch is None:
            ch = _SessionChannel(history=deque(maxlen=self._max_history))
            self._channels[session_id] = ch
        return ch

    def publish(self, session_id: str, event_type: str, data: dict) -> dict:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        with self._lock:
            ch = self._channel(session_id)
            ev_id = ch.next_id
            ch.next_id += 1
            item = {
                "id": ev_id,
                "type": event_type,
                "ts": ts,
                "data": dict(data),
            }
            ch.history.append(item)
            subs = list(ch.subscribers)

        for sub in subs:
            self._deliver(sub, item)
        return item

    @staticmethod
    def _enqueue_nowait(q, item: dict) -> None:
        try:
            q.put_nowait(item)
        except Exception:
            try:
                q.get_nowait()
                q.put_nowait(item)
            except Exception:
                pass

    def _deliver(self, sub: _Subscriber, item: dict) -> None:
        if sub.loop is None:
            self._enqueue_nowait(sub.queue, item)
            return
        try:
            if not sub.loop.is_closed():
                sub.loop.call_soon_threadsafe(self._enqueue_nowait, sub.queue, item)
        except RuntimeError:
            pass

    def subscribe(
        self,
        session_id: str,
        loop: asyncio.AbstractEventLoop | None = None,
        *,
        last_event_id: int | None = None,
        maxsize: int = 256,
    ) -> tuple[list[dict], asyncio.Queue, Callable[[], None]]:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        sub = _Subscriber(loop=loop, queue=q)
        with self._lock:
            ch = self._channel(session_id)
            if last_event_id is not None:
                replay = [e for e in ch.history if int(e["id"]) > int(last_event_id)]
            else:
                replay = []
            ch.subscribers.append(sub)

        def _unsubscribe() -> None:
            with self._lock:
                ch2 = self._channels.get(session_id)
                if ch2 and sub in ch2.subscribers:
                    ch2.subscribers.remove(sub)

        return replay, q, _unsubscribe

    def history_for(self, session_id: str, *, after_id: int = 0) -> list[dict]:
        with self._lock:
            ch = self._channels.get(session_id)
            if ch is None:
                return []
            return [e for e in ch.history if int(e["id"]) > after_id]

    def subscriber_count(self, session_id: str) -> int:
        with self._lock:
            ch = self._channels.get(session_id)
            return len(ch.subscribers) if ch else 0


event_bus = SessionEventBus()
