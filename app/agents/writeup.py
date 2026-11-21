from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

from sqlmodel import Session, select

from ..knowledge.index import DEFAULT_KNOWLEDGE_ROOT, KnowledgeIndex
from ..models import Attachment, AttachmentType, Challenge, Event
from ..services import tracking

_SCRIPT_EXTS = {
    ".py": "python",
    ".sh": "bash",
    ".js": "javascript",
    ".c": "c",
    ".rs": "rust",
    ".sql": "sql",
    ".txt": "text",
    ".md": "markdown",
}
_MAX_EMBED_BYTES = 16_000
_MAX_ZIP_FILE_BYTES = 5 * 1024 * 1024


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "challenge"


def _sandbox_scripts(challenge: Challenge, storage, download_names: set[str]) -> list[tuple[str, str, str]]:
    """Return `(rel_path, lang, content)` for generated text/scripts in challenge sandbox."""
    if storage is None:
        return []
    workdir = storage.storage_dir / "sandbox" / str(challenge.event_id) / str(challenge.id)
    if not workdir.exists():
        return []
    found: list[tuple[str, str, str]] = []
    for p in sorted(workdir.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(workdir)).replace("\\", "/")
        if rel in download_names:
            continue
        lang = _SCRIPT_EXTS.get(p.suffix.lower())
        if not lang:
            continue
        if p.stat().st_size > _MAX_EMBED_BYTES:
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if content.strip():
            found.append((rel, lang, content))
        if len(found) >= 6:
            break
    return found


def _output_excerpts(attachments: list[Attachment], storage) -> list[tuple[str, str]]:
    if storage is None:
        return []
    outputs = [a for a in attachments if a.type == AttachmentType.output][-4:]
    snippets: list[tuple[str, str]] = []
    for att in outputs:
        p = storage.storage_dir / att.path
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        snippets.append((att.name, text[:1500].strip()))
    return snippets


def build_challenge_writeup(
    session: Session,
    challenge: Challenge,
    *,
    agent_session=None,
    storage=None,
) -> str:
    """Assemble a reproducible Markdown writeup + execution proof for `challenge`."""
    event = session.get(Event, challenge.event_id)
    summary = tracking.challenge_summary(session, challenge)
    attempts = tracking.list_attempts(session, challenge.id)
    attachments = list(session.exec(
        select(Attachment).where(Attachment.challenge_id == challenge.id)
    ).all())

    status_val = challenge.status.value if hasattr(challenge.status, "value") else str(challenge.status)
    flag_val = challenge.flag or (
        agent_session.final_report.flag_candidate
        if agent_session and getattr(agent_session, "final_report", None)
        else None
    )
    diff_val = challenge.difficulty.value if challenge.difficulty else "—"

    lines: list[str] = [
        f"# Writeup — {challenge.title}",
        "",
        "## Resumo da Missão",
        f"- **Evento:** {event.name if event else challenge.event_id}",
        f"- **Categoria:** `{challenge.category}` | **Pontos:** {challenge.points} | **Dificuldade:** {diff_val}",
        f"- **Target:** `{challenge.target or 'offline / anexo'}`",
        f"- **Status:** `{status_val}`",
    ]
    if flag_val:
        lines.append(f"- **Flag obtida:** `{flag_val}`")
    lines.append(f"- **Tentativas registradas:** {summary['attempt_count']}")
    if summary["active_seconds"] is not None:
        lines.append(f"- **Tempo ativo:** {summary['active_seconds'] / 60:.1f} min")
    if agent_session is not None:
        lines.append(
            f"- **Sessão KOTH:** `{agent_session.id[:8]}` · dispatches={agent_session.dispatch_count} · tokens={getattr(agent_session, 'total_tokens', 0)}"
        )

    if challenge.description:
        lines += ["", "## Enunciado", challenge.description.strip()]

    downloads = [a for a in attachments if a.type == AttachmentType.download]
    if downloads:
        lines += ["", "## Artefatos fornecidos"]
        for a in downloads:
            lines.append(f"- `{a.name}` ({a.size} bytes)")

    if agent_session and getattr(agent_session, "findings", None):
        lines += ["", "## Achados consolidados"]
        for f in agent_session.findings:
            lines.append(f"- {f}")

    if agent_session and getattr(agent_session, "reports", None):
        lines += ["", "## Ciclos dos Especialistas"]
        for idx, rep in enumerate(agent_session.reports, 1):
            tools = ", ".join(rep.tools_used) if rep.tools_used else "—"
            lines.append(f"### Ciclo {idx} (`{rep.status}` · tools: {tools})")
            for f in rep.findings:
                lines.append(f"- {f}")
            if rep.flag_candidate:
                lines.append(f"- Flag candidata: `{rep.flag_candidate}`")

    if attempts:
        lines += ["", "## Prova de Execução (Timeline & Comandos)"]
        for a in attempts:
            ts = a.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
            t_val = a.type.value if hasattr(a.type, "value") else str(a.type)
            r_val = a.result.value if hasattr(a.result, "value") else str(a.result)
            lines.append(f"- **[{ts}]** `{t_val}` → `{r_val}` — {a.description}")
            if a.tool_command:
                lines.append(f"  - Comando/valor: `{a.tool_command}`")
            if a.learning:
                clipped = a.learning.strip().replace("\r\n", "\n")[:500]
                lines.append(f"  - Evidência: `{clipped}`")

    download_names = {a.name for a in downloads}
    scripts = _sandbox_scripts(challenge, storage, download_names)
    if scripts:
        lines += ["", "## Scripts / Exploits gerados no Sandbox (`/work`)"]
        for rel, lang, content in scripts:
            lines += [f"### `{rel}`", f"```{lang}", content.rstrip(), "```"]

    excerpts = _output_excerpts(attachments, storage)
    if excerpts:
        lines += ["", "## Saídas capturadas das ferramentas"]
        for name, snippet in excerpts:
            lines += [f"### `{name}`", "```text", snippet, "```"]

    return "\n".join(lines).strip() + "\n"


def persist_writeup_note_and_attachment(
    session: Session,
    challenge: Challenge,
    writeup_md: str,
    *,
    storage=None,
) -> Path | None:
    """Save `writeup_md` as latest Note version (deduped) and `writeup.md` file."""
    latest = tracking.current_note(session, challenge.id)
    if latest is None or latest.content != writeup_md:
        tracking.add_note(session, challenge.id, writeup_md)

    if storage is None:
        return None
    dest = storage.save(challenge.id, "writeup.md", writeup_md.encode("utf-8"))
    rel = str(dest.relative_to(storage.storage_dir))
    existing = session.exec(
        select(Attachment).where(
            Attachment.challenge_id == challenge.id,
            Attachment.name == "writeup.md",
        )
    ).first()
    if existing is None:
        session.add(Attachment(
            challenge_id=challenge.id,
            name="writeup.md",
            path=rel,
            mime="text/markdown",
            size=len(writeup_md.encode("utf-8")),
            type=AttachmentType.output,
        ))
    else:
        existing.path = rel
        existing.size = len(writeup_md.encode("utf-8"))
        session.add(existing)
    session.commit()
    return dest


def build_proof_bundle_zip(
    session: Session,
    challenge: Challenge,
    *,
    agent_session=None,
    storage=None,
) -> bytes:
    """Package `writeup.md`, `attempts.json`, sandbox files, and tool outputs into a ZIP."""
    writeup_md = build_challenge_writeup(
        session, challenge, agent_session=agent_session, storage=storage
    )
    attempts = tracking.list_attempts(session, challenge.id)
    attempts_payload = [
        {
            "id": a.id,
            "timestamp": a.timestamp.isoformat(),
            "type": a.type.value if hasattr(a.type, "value") else str(a.type),
            "description": a.description,
            "tool_command": a.tool_command,
            "result": a.result.value if hasattr(a.result, "value") else str(a.result),
            "learning": a.learning,
        }
        for a in attempts
    ]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("writeup.md", writeup_md.encode("utf-8"))
        zf.writestr("attempts.json", json.dumps(attempts_payload, ensure_ascii=False, indent=2).encode("utf-8"))

        if storage is not None:
            workdir = storage.storage_dir / "sandbox" / str(challenge.event_id) / str(challenge.id)
            if workdir.exists():
                for p in sorted(workdir.rglob("*")):
                    if p.is_file() and p.stat().st_size <= _MAX_ZIP_FILE_BYTES:
                        rel = str(p.relative_to(workdir)).replace("\\", "/")
                        zf.write(p, f"sandbox/{rel}")

            atts = list(session.exec(
                select(Attachment).where(Attachment.challenge_id == challenge.id)
            ).all())
            for a in atts:
                if a.type == AttachmentType.output:
                    src = storage.storage_dir / a.path
                    if src.exists() and src.stat().st_size <= _MAX_ZIP_FILE_BYTES:
                        zf.write(src, f"outputs/{a.id}_{Path(a.name).name}")
    return buf.getvalue()


def build_event_writeup(
    session: Session,
    event: Event,
    *,
    storage=None,
    only_solved: bool = False,
) -> str:
    challenges = tracking.list_challenges(session, event_id=event.id)
    if only_solved:
        challenges = [c for c in challenges if str(getattr(c.status, "value", c.status)) == "solved"]

    header = [
        f"# Writeups — {event.name}",
        "",
        f"- **Status do evento:** `{event.status.value}`",
        f"- **Total incluído:** {len(challenges)} desafio(s)",
    ]
    if event.flag_format:
        header.append(f"- **Formato de flag:** `{event.flag_format}`")
    if not challenges:
        header += ["", "_Nenhum desafio para exportar._"]
        return "\n".join(header) + "\n"

    sections = ["\n".join(header), "", "---", ""]
    for ch in challenges:
        sections.append(build_challenge_writeup(session, ch, storage=storage))
        sections.append("\n---\n")
    return "\n".join(sections).strip() + "\n"


def save_writeup_to_arsenal(
    session: Session,
    challenge: Challenge,
    writeup_md: str,
    *,
    knowledge_root: Path | None = None,
) -> tuple[Path, int]:
    """Save solved challenge writeup under `knowledge/local/<category>/<slug>.md` and rebuild FTS5."""
    root = knowledge_root or DEFAULT_KNOWLEDGE_ROOT
    cat = _slugify(challenge.category or "misc")
    slug = f"{challenge.id}-{_slugify(challenge.title)}"
    dest = root / "local" / cat / f"{slug}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)

    tags_list = [t.strip() for t in (challenge.tags or "").split(",") if t.strip()]
    fm = (
        "---\n"
        f"title: {json.dumps(f'{challenge.title} ({challenge.category})', ensure_ascii=False)}\n"
        f"category: {cat}\n"
        f"tags: {json.dumps(tags_list, ensure_ascii=False)}\n"
        f"challenge_id: {challenge.id}\n"
        "---\n\n"
    )
    dest.write_text(fm + writeup_md, encoding="utf-8")
    count = KnowledgeIndex(session, knowledge_root=root).rebuild(root)
    return dest, count
