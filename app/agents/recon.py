from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import Session

from ..models import Attachment, Challenge, ChallengeStatus
from ..services import tracking
from ..services.storage import StorageService

RECON_TOOLS = ("strings", "nmap")


@dataclass
class ReconDigest:
    target: str | None
    files: list[dict] = field(default_factory=list)
    summary: str = ""


def recon_bundle(runner, session: Session, challenge: Challenge,
                 storage: StorageService | None = None) -> ReconDigest:
    """Deterministic recon: strings on each download artifact, nmap top ports when
    the challenge has a target. Uses the ToolRunner (guards enforced there) so every
    run is persisted as Attachment + Attempt by the runner itself."""
    digests: list[dict] = []
    summaries: list[str] = []

    artifacts = session.query(Attachment).filter(
        Attachment.challenge_id == challenge.id,
        Attachment.type == "download").all()

    for att in artifacts:
        path = (storage.storage_dir if storage else Path("storage")) / att.path
        if not path.exists():
            continue
        try:
            res = runner.run_raw(challenge, "strings",
                                 [str(path)], timeout_s=30, output_limit_mb=1)
        except Exception as exc:  # tool missing / guard — recon must not die
            summaries.append(f"strings {att.name}: skipped ({exc})")
            continue
        out = (res.stdout or "").strip()
        interesting = [ln for ln in out.splitlines() if ln.strip()][:40]
        digests.append({"name": att.name, "strings": interesting[:40]})
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

    tracking.record_attempt(
        session, challenge.id, "agent", "recon bundle (strings/nmap)",
        "breakthrough" if any(d.get("strings") or d.get("ports") for d in digests)
        else "partial",
        tool_command="recon_bundle",
        learning="\n".join(summaries)[:2000])

    if challenge.first_interaction is None and challenge.status.value == "open":
        challenge.status = ChallengeStatus.in_progress
        session.add(challenge)
        session.commit()

    return ReconDigest(target=target, files=digests,
                       summary="\n".join(summaries))
