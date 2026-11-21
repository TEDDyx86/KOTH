from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import Session, select

from ..models import Attachment, AttachmentType, AttemptResult, AttemptType, Challenge
from ..services import tracking
from ..services.storage import StorageService

RECON_TOOLS = ("strings", "nmap")

_MAGIC_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\x7fELF", "ELF binary"),
    (b"MZ", "PE/DOS executable (MZ)"),
    (b"\x89PNG\r\n\x1a\n", "PNG image"),
    (b"\xff\xd8\xff", "JPEG image"),
    (b"GIF87a", "GIF image"),
    (b"GIF89a", "GIF image"),
    (b"%PDF-", "PDF document"),
    (b"PK\x03\x04", "ZIP archive"),
    (b"\x1f\x8b\x08", "GZIP compressed"),
    (b"SQLite format 3\x00", "SQLite 3 database"),
    (b"\xd4\xc3\xb2\xa1", "PCAP capture (little-endian)"),
    (b"\xa1\xb2\xc3\xd4", "PCAP capture (big-endian)"),
    (b"\x0a\x0d\x0d\x0a", "PCAPNG capture"),
]


def detect_file_type(path: Path) -> str:
    """Deterministic file type detection from magic bytes / text sniff."""
    try:
        head = path.read_bytes()[:512]
    except OSError as exc:
        return f"unreadable ({exc})"
    if not head:
        return "empty file"
    for magic, label in _MAGIC_SIGNATURES:
        if head.startswith(magic):
            return label
    if len(head) >= 265 and head[257:262] == b"ustar":
        return "TAR archive"
    try:
        head.decode("utf-8")
        return "UTF-8/ASCII text"
    except UnicodeDecodeError:
        return "binary data"


@dataclass
class ReconDigest:
    target: str | None
    files: list[dict] = field(default_factory=list)
    summary: str = ""


def recon_bundle(runner, session: Session, challenge: Challenge,
                 storage: StorageService | None = None) -> ReconDigest:
    """Deterministic recon: file type + strings on each download artifact, nmap top
    ports when the challenge has a target. Uses ToolRunner (guards enforced)."""
    digests: list[dict] = []
    summaries: list[str] = []

    effective_storage = storage or getattr(runner, "storage", None)
    if runner is not None and hasattr(runner, "stage_downloads"):
        runner.stage_downloads(session, challenge)

    artifacts = list(session.exec(select(Attachment).where(
        Attachment.challenge_id == challenge.id,
        Attachment.type == AttachmentType.download,
    )).all())

    for att in artifacts:
        base_dir = effective_storage.storage_dir if effective_storage else Path("storage")
        path = base_dir / att.path
        if not path.exists():
            continue
        ftype = detect_file_type(path)
        summaries.append(f"file {att.name}: {ftype} ({att.size} bytes)")
        entry: dict = {"name": att.name, "file_type": ftype, "strings": []}
        try:
            res = runner.run_raw(challenge, "strings",
                                 [str(path)], timeout_s=30, output_limit_mb=1)
        except Exception as exc:  # tool missing / guard — recon must not die
            summaries.append(f"strings {att.name}: skipped ({exc})")
            digests.append(entry)
            continue
        out = (res.stdout or "").strip()
        interesting = [ln for ln in out.splitlines() if ln.strip()][:40]
        entry["strings"] = interesting[:40]
        digests.append(entry)
        if interesting:
            summaries.append(f"strings {att.name}: " + " | ".join(interesting[:8]))

    target = challenge.target
    if target:
        try:
            res = runner.run_raw(challenge, "nmap",
                                 ["--top-ports", "100", target.split(":")[0]],
                                 timeout_s=120, output_limit_mb=1)
            open_lines = [ln for ln in res.stdout.splitlines() if "/tcp" in ln][:20]
            summaries.append(f"nmap {target}: " + ("; ".join(open_lines) if open_lines
                            else (res.stdout or "")[:200]))
            digests.append({"name": "nmap", "ports": open_lines})
        except Exception as exc:
            summaries.append(f"nmap {target}: skipped ({exc})")
    else:
        summaries.append("no target set — nmap skipped")

    result_kind = (
        AttemptResult.breakthrough
        if any(d.get("strings") or d.get("ports") for d in digests)
        else AttemptResult.partial
    )
    tracking.record_attempt(
        session, challenge.id, AttemptType.agent, "recon bundle (file/strings/nmap)",
        result_kind,
        tool_command="recon_bundle",
        learning="\n".join(summaries)[:2000])

    return ReconDigest(target=target, files=digests,
                       summary="\n".join(summaries))
