from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from sqlmodel import Session

from ..models import Attachment, AttachmentType, Challenge, Difficulty
from ..services import tracking
from ..services.storage import StorageService

_TARGET_RE = re.compile(
    r"(https?://[^\s'\"<>]+|(?:nc|ncat|telnet)\s+([a-zA-Z0-9._-]+)\s+(\d{1,5})|([a-zA-Z0-9.-]+\.[a-z]{2,}:\d{1,5}))",
    re.IGNORECASE,
)


def _default_fetch(url: str, headers: dict[str, str]) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "KOTH-CTFd-Importer/1.0", **headers})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def _extract_target(*texts: str | None) -> str | None:
    for raw in texts:
        txt = (raw or "").strip()
        if not txt:
            continue
        m = _TARGET_RE.search(txt)
        if m:
            if m.group(2) and m.group(3):
                return f"{m.group(2)}:{m.group(3)}"
            return (m.group(1) or m.group(0)).strip()
    return None


def _normalize_ctfd_category(raw: str | None) -> str:
    c = (raw or "misc").strip().lower()
    mapping = {
        "reverse engineering": "reverse",
        "reversing": "reverse",
        "rev": "reverse",
        "cryptography": "crypto",
        "binary exploitation": "pwn",
        "web exploitation": "web",
        "forensic": "forensics",
    }
    return mapping.get(c, c or "misc")


def _guess_difficulty(points: int, tags: list[str]) -> Difficulty | None:
    joined = " ".join(tags).lower()
    for d in ("insane", "hard", "medium", "easy"):
        if d in joined:
            return Difficulty(d)
    if points <= 0:
        return None
    if points <= 150:
        return Difficulty.easy
    if points <= 350:
        return Difficulty.medium
    if points <= 600:
        return Difficulty.hard
    return Difficulty.insane


def import_ctfd_challenges(
    session: Session,
    storage: StorageService,
    event_id: int,
    base_url: str,
    *,
    api_token: str = "",
    session_cookie: str = "",
    fetcher: Callable[[str, dict[str, str]], bytes] | None = None,
) -> dict:
    """Import challenges & attachments from a CTFd /api/v1/challenges endpoint."""
    base = (base_url or "").strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ValueError("URL base do CTFd deve iniciar com http:// ou https://")

    headers: dict[str, str] = {"Accept": "application/json"}
    if api_token.strip():
        headers["Authorization"] = f"Token {api_token.strip()}"
    if session_cookie.strip():
        cookie = session_cookie.strip()
        headers["Cookie"] = cookie if "=" in cookie else f"session={cookie}"

    fetch = fetcher or _default_fetch
    list_payload = json.loads(fetch(f"{base}/api/v1/challenges", headers).decode("utf-8"))
    items = list_payload.get("data") if isinstance(list_payload, dict) else list_payload
    if not isinstance(items, list):
        raise ValueError("Resposta inesperada de /api/v1/challenges")

    existing_titles = {
        (c.title or "").strip().lower()
        for c in tracking.list_challenges(session, event_id=event_id)
    }

    imported = 0
    skipped = 0
    files_saved = 0
    created_challenges: list[Challenge] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        cid = item.get("id")
        title = str(item.get("name") or item.get("title") or "").strip()
        if not title:
            continue
        if title.lower() in existing_titles:
            skipped += 1
            continue

        detail = dict(item)
        if cid is not None and ("description" not in detail or "files" not in detail):
            try:
                det_payload = json.loads(fetch(f"{base}/api/v1/challenges/{cid}", headers).decode("utf-8"))
                if isinstance(det_payload, dict) and isinstance(det_payload.get("data"), dict):
                    detail.update(det_payload["data"])
            except Exception:
                pass

        category = _normalize_ctfd_category(detail.get("category"))
        points = int(detail.get("value") or detail.get("points") or 0)
        description = str(detail.get("description") or "").strip()
        conn_info = str(detail.get("connection_info") or "").strip()
        if conn_info and conn_info not in description:
            description = f"{description}\n\nConnection: {conn_info}".strip()
        raw_tags = [str(t) for t in (detail.get("tags") or []) if t]
        target = _extract_target(conn_info, description)

        ch = tracking.create_challenge(
            session,
            event_id,
            title[:120],
            category=category,
            points=points,
            difficulty=_guess_difficulty(points, raw_tags),
            description=description,
            target=target,
            tags=",".join(raw_tags),
        )
        existing_titles.add(title.lower())
        imported += 1
        created_challenges.append(ch)

        for f_ref in detail.get("files") or []:
            if not isinstance(f_ref, str) or not f_ref.strip():
                continue
            file_url = f_ref if f_ref.startswith(("http://", "https://")) else urllib.parse.urljoin(base + "/", f_ref.lstrip("/"))
            fname = Path(urllib.parse.urlparse(file_url).path).name or "artifact.bin"
            try:
                raw_bytes = fetch(file_url, headers)
                dest = storage.save(ch.id, fname, raw_bytes)
                session.add(Attachment(
                    challenge_id=ch.id,
                    name=fname,
                    path=str(dest.relative_to(storage.storage_dir)),
                    mime="application/octet-stream",
                    size=len(raw_bytes),
                    type=AttachmentType.download,
                ))
                files_saved += 1
            except Exception:
                continue
        session.commit()

    return {
        "imported": imported,
        "skipped": skipped,
        "files": files_saved,
        "challenges": created_challenges,
    }
