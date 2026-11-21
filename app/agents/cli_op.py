import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic_ai import Agent
from sqlmodel import Session, select

from ..models import (
    Attachment, AttachmentType, Challenge, ChallengeStatus,
    CliMessageRecord, Difficulty, Event,
)
from ..services import tracking
from ..services.storage import StorageService
from ..tooling.config import load_tools_config
from ..tooling.doctor import inventory
from .config import load_agents, load_models
from .models import ProviderUnavailable, resolve_candidates_for_role, run_with_fallback
from .planner import planner_rank

VALID_CATEGORIES = ("web", "pwn", "crypto", "reverse", "forensics", "osint", "misc", "ai-ml", "custom")


class OpCliAction(BaseModel):
    action: Literal["REPLY", "LAUNCH_CHALLENGE", "RANK", "STATUS"]
    reply: str
    title: str | None = None
    category: str = "misc"
    points: int = 100
    difficulty: Literal["easy", "medium", "hard", "insane"] | None = None
    target: str | None = None
    tags: str = ""
    description: str = ""


def _op_persona() -> str:
    p = Path(__file__).resolve().parent.parent.parent / "agents" / "personas" / "op.md"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return "Você é o Orquestrador Principal do KOTH (King Of The Hill). Terse. Zero disclaimers."


def _normalize_category(cat: str | None) -> str:
    c = (cat or "misc").strip().lower()
    if c == "reversing":
        return "reverse"
    return c if c in VALID_CATEGORIES else "misc"


def _parse_difficulty(raw: str | None) -> Difficulty | None:
    if not raw:
        return None
    try:
        return Difficulty(raw.strip().lower())
    except ValueError:
        return None


def _parse_slash_launch(raw_args: str, file_names: list[str]) -> dict:
    """Parse `/solve Title | cat=web | target=10.10.10.5:80 | pts=500 | diff=hard | description`."""
    parts = [p.strip() for p in raw_args.split("|") if p.strip()]
    title = ""
    cat = "misc"
    target = None
    points = 100
    diff = None
    tags = ""
    desc_parts: list[str] = []

    for idx, part in enumerate(parts):
        m = re.match(r"^(cat|category|target|pts|points|diff|difficulty|tags)\s*=\s*(.+)$", part, re.I)
        if m:
            k, v = m.group(1).lower(), m.group(2).strip()
            if k in ("cat", "category"):
                cat = _normalize_category(v)
            elif k == "target":
                target = v
            elif k in ("pts", "points") and v.isdigit():
                points = int(v)
            elif k in ("diff", "difficulty"):
                diff = v.lower()
            elif k == "tags":
                tags = v
        elif idx == 0 and not title:
            title = part[:80]
        else:
            desc_parts.append(part)

    if not title:
        title = (file_names[0] if file_names else "Desafio CLI")[:60]
    description = "\n".join(desc_parts) if desc_parts else raw_args.strip()
    return {
        "title": title,
        "category": cat,
        "target": target,
        "points": points,
        "difficulty": diff,
        "tags": tags,
        "description": description,
    }


def _heuristic_extract(text: str, file_names: list[str]) -> dict:
    """Offline fallback extractor when no LLM key is configured."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    first_line = lines[0] if lines else (file_names[0] if file_names else "Desafio KOTH")
    title = first_line[:60]
    target_match = re.search(r"\b(?:https?://)?([a-z0-9.-]+\.[a-z]{2,}|(?:\d{1,3}\.){3}\d{1,3})(?::(\d{2,5}))?", text, re.I)
    target = None
    if target_match:
        host = target_match.group(1)
        port = target_match.group(2)
        target = f"{host}:{port}" if port else host

    lower = text.lower()
    cat = "misc"
    for kw, c in [
        ("sqli", "web"), ("xss", "web"), ("http", "web"), ("cookie", "web"), ("jwt", "web"),
        ("rsa", "crypto"), ("aes", "crypto"), ("xor", "crypto"), ("cipher", "crypto"), ("prng", "crypto"),
        ("bof", "pwn"), ("rop", "pwn"), ("libc", "pwn"), ("shellcode", "pwn"), ("elf", "pwn"),
        ("ghidra", "reverse"), ("decompile", "reverse"), ("keygen", "reverse"),
        ("pcap", "forensics"), ("stego", "forensics"), ("exif", "forensics"), ("png", "forensics"),
        ("osint", "osint"), ("geoloc", "osint"),
    ]:
        if kw in lower:
            cat = c
            break
    return {
        "title": title,
        "category": cat,
        "target": target,
        "points": 100,
        "difficulty": None,
        "tags": "",
        "description": text.strip(),
    }


def _format_rank(session: Session, event: Event | None) -> str:
    if event is None:
        return "[KOTH // PL] Nenhum evento cadastrado ainda. Crie um evento ou envie um desafio direto."
    ranked = planner_rank(session, event)
    if not ranked:
        return f"[KOTH // PL] Evento '{event.name}' ainda não tem desafios."
    lines = [f"[KOTH // PL] Priorização para '{event.name}':"]
    for i, (ch, score) in enumerate(ranked[:10], 1):
        st = ch.status.value if hasattr(ch.status, "value") else str(ch.status)
        lines.append(f"  {i}. [{score:.1f}] #{ch.id} {ch.title} ({ch.category}, {ch.points} pts, {st})")
    return "\n".join(lines)


def _format_status(session: Session, active_event: Event | None) -> str:
    tools = inventory(load_tools_config())
    ok_tools = [t["name"] for t in tools if t["present"]]
    miss_tools = [t["name"] for t in tools if not t["present"]]
    open_chs = tracking.list_challenges(session, status=ChallengeStatus.open)
    try:
        models_cfg = load_models()
        agents_cfg = load_agents()
        role_key = agents_cfg.roles["op"].model if "op" in agents_cfg.roles else "strong"
        resolve_candidates_for_role("op", role_key, models_cfg)
        llm_status = f"ONLINE ({models_cfg.roles[role_key].name})"
    except Exception as exc:
        llm_status = f"OFFLINE ({exc})"

    ev_label = f"{active_event.name} (#{active_event.id})" if active_event else "(nenhum)"
    return "\n".join([
        "[KOTH // STATUS]",
        f"  LLM OP: {llm_status}",
        f"  Evento ativo: {ev_label}",
        f"  Desafios abertos: {len(open_chs)}",
        f"  Ferramentas OK ({len(ok_tools)}): {', '.join(ok_tools) or 'nenhuma'}",
        f"  Ausentes ({len(miss_tools)}): {', '.join(miss_tools) or 'nenhuma'}",
    ])


def _help_text() -> str:
    return "\n".join([
        "[KOTH // CLI] Comandos disponíveis:",
        "  • Texto livre + anexos → conversa com o OP ou envia um desafio direto (ex: cole o enunciado)",
        "  • /solve <título> | cat=web | target=host:port | pts=300 | <descrição> → cria e dispara já",
        "  • /rank   → ranking tático do Planner (pontos + dificuldade)",
        "  • /events → lista eventos cadastrados",
        "  • /status → checa LLM, ferramentas (Tool Doctor) e desafios abertos",
        "  • /clear  → limpa o histórico do terminal",
        "  • /help   → mostra esta ajuda",
    ])


def _resolve_event(session: Session, event_id: int | None) -> Event | None:
    if event_id is not None:
        ev = session.get(Event, event_id)
        if ev is not None:
            return ev
    events = tracking.list_events(session)
    return events[-1] if events else None


def _ensure_event(session: Session, event_id: int | None) -> Event:
    ev = _resolve_event(session, event_id)
    if ev is not None:
        return ev
    return tracking.create_event(session, "KOTH Arena", flag_format=r"CTF\{.*\}")


def _call_op_llm(session: Session, text: str, file_names: list[str],
                 active_event: Event | None, recent_msgs: list[CliMessageRecord]) -> OpCliAction:
    models_cfg = load_models()
    agents_cfg = load_agents()
    role_key = agents_cfg.roles["op"].model if "op" in agents_cfg.roles else "strong"
    candidates = resolve_candidates_for_role("op", role_key, models_cfg)
    persona = _op_persona() + (
        "\n\nRetorne OpCliAction:\n"
        "- Use action='LAUNCH_CHALLENGE' sempre que o operador colar um desafio, pedir para atacar/resolver "
        "um target/arquivo, ou enviar anexos com instrução de CTF.\n"
        "- Use action='RANK' se pedir qual desafio fazer primeiro.\n"
        "- Use action='STATUS' se perguntar estado do sistema/ferramentas.\n"
        "- Caso contrário use action='REPLY' respondendo como par técnico."
    )
    open_chs = tracking.list_challenges(session, status=ChallengeStatus.open)
    ch_lines = [f"- #{c.id} {c.title} ({c.category}, {c.points}pts, target={c.target or '-'})"
                for c in open_chs[:12]]
    hist_lines = [f"{m.role}: {m.content[:300]}" for m in recent_msgs[-8:]]
    prompt = "\n".join([
        f"Evento ativo: {active_event.name if active_event else '(nenhum — será criado KOTH Arena)'}",
        f"Arquivos anexados nesta mensagem: {', '.join(file_names) if file_names else '(nenhum)'}",
        "## Desafios abertos",
        *(ch_lines or ["(nenhum)"]),
        "## Histórico recente do chat",
        *(hist_lines or ["(vazio)"]),
        "## Mensagem atual do operador",
        text,
    ])

    def _run(model):
        ag = Agent(model, output_type=OpCliAction, system_prompt=persona)
        return ag.run_sync(prompt)

    res = run_with_fallback(candidates, _run)
    return res.output


def _launch_challenge(
    session: Session,
    storage: StorageService,
    orch,
    data: dict,
    event_id: int | None,
    mode: str,
    uploads: list,
) -> tuple[Challenge, str | None, str | None]:
    ev = _ensure_event(session, event_id)
    ch = tracking.create_challenge(
        session,
        ev.id,
        (data.get("title") or "Desafio KOTH")[:120],
        category=_normalize_category(data.get("category")),
        points=int(data.get("points") or 100),
        difficulty=_parse_difficulty(data.get("difficulty")),
        description=data.get("description") or "",
        target=(data.get("target") or "").strip() or None,
        tags=data.get("tags") or "",
    )
    saved_names: list[str] = []
    for up in uploads:
        if not up or not getattr(up, "filename", None):
            continue
        raw = up.file.read()
        path = storage.save(ch.id, up.filename, raw)
        session.add(Attachment(
            challenge_id=ch.id,
            name=up.filename,
            path=str(path.relative_to(storage.storage_dir)),
            mime=getattr(up, "content_type", None) or "application/octet-stream",
            size=len(raw),
            type=AttachmentType.download,
        ))
        saved_names.append(up.filename)
    session.commit()

    session_id: str | None = None
    note: str | None = None
    if orch is not None:
        try:
            if mode == "auto" and hasattr(orch, "start_background"):
                sess = orch.start_background(session.get_bind(), session, ch.id, mode="auto")
            else:
                sess = orch.start(session, ch.id, mode=mode)
            session_id = sess.id
        except Exception as exc:
            note = f"runtime indisponível ({exc})"
    else:
        note = "runtime indisponível — challenge salvo para início manual"
    return ch, session_id, note


def list_cli_messages(session: Session, limit: int = 80) -> list[CliMessageRecord]:
    rows = list(session.exec(
        select(CliMessageRecord).order_by(CliMessageRecord.id.desc()).limit(limit)
    ).all())
    rows.reverse()
    return rows


def process_cli_turn(
    session: Session,
    storage: StorageService,
    orch,
    text: str,
    *,
    event_id: int | None = None,
    mode: str = "auto",
    force_launch: bool = False,
    uploads: list | None = None,
    op_responder=None,
) -> CliMessageRecord:
    """Process one operator turn from the CLI chat and persist both user and OP records."""
    cleaned = (text or "").strip()
    uploads = [u for u in (uploads or []) if u and getattr(u, "filename", None)]
    file_names = [u.filename for u in uploads]

    if cleaned == "/clear":
        for row in session.exec(select(CliMessageRecord)).all():
            session.delete(row)
        session.commit()
        msg = CliMessageRecord(role="system", content="[KOTH // CLI] Histórico limpo. Pronto para próxima escalada.")
        session.add(msg)
        session.commit()
        session.refresh(msg)
        return msg

    display_user = cleaned or f"[anexos: {', '.join(file_names)}]"
    session.add(CliMessageRecord(role="user", content=display_user))
    session.commit()

    active_event = _resolve_event(session, event_id)
    cmd = cleaned.split(None, 1)[0].lower() if cleaned.startswith("/") else ""

    if cmd == "/help":
        reply_text = _help_text()
        rec = CliMessageRecord(role="op", content=reply_text)
    elif cmd == "/rank":
        rec = CliMessageRecord(role="op", content=_format_rank(session, active_event))
    elif cmd == "/events":
        events = tracking.list_events(session)
        if not events:
            txt = "[KOTH // EVENTS] Nenhum evento ainda. Envie um desafio para criar 'KOTH Arena' automaticamente."
        else:
            lines = ["[KOTH // EVENTS]"]
            for ev in events:
                lines.append(f"  • #{ev.id} {ev.name} ({ev.status.value}) flag={ev.flag_format or r'CTF\{.*\}'}")
            txt = "\n".join(lines)
        rec = CliMessageRecord(role="op", content=txt)
    elif cmd == "/status":
        rec = CliMessageRecord(role="op", content=_format_status(session, active_event))
    elif cmd in ("/solve", "/new") or force_launch:
        raw_arg = cleaned.split(None, 1)[1] if cmd in ("/solve", "/new") and " " in cleaned else cleaned
        data = _parse_slash_launch(raw_arg, file_names) if cmd in ("/solve", "/new") else _heuristic_extract(cleaned, file_names)
        ch, sid, warn = _launch_challenge(session, storage, orch, data, event_id, mode, uploads)
        status_note = f" | aviso: {warn}" if warn else f" | modo={mode} | sessão={(sid or '-')[:8]}"
        rec = CliMessageRecord(
            role="op",
            content=(
                f"[KOTH // OP] Missão lançada → #{ch.id} {ch.title} "
                f"({ch.category}, {ch.points} pts, target={ch.target or 'offline'}){status_note}"
            ),
            challenge_id=ch.id,
            session_id=sid,
        )
    else:
        recent = list_cli_messages(session, limit=12)
        action_obj: OpCliAction | None = None
        if op_responder is not None:
            action_obj = op_responder(session, cleaned, file_names, active_event, recent)
        else:
            try:
                action_obj = _call_op_llm(session, cleaned, file_names, active_event, recent)
            except (ProviderUnavailable, Exception) as exc:
                # Graceful offline fallback: if attachments exist, treat as challenge launch; else inform cleanly
                if uploads:
                    data = _heuristic_extract(cleaned, file_names)
                    ch, sid, warn = _launch_challenge(session, storage, orch, data, event_id, mode, uploads)
                    rec = CliMessageRecord(
                        role="op",
                        content=(
                            f"[KOTH // OP] Anexos recebidos — missão criada #{ch.id} {ch.title} "
                            f"({ch.category}) | {warn or f'sessão {(sid or '')[:8]}'}"
                        ),
                        challenge_id=ch.id,
                        session_id=sid,
                    )
                    session.add(rec)
                    session.commit()
                    session.refresh(rec)
                    return rec
                action_obj = OpCliAction(
                    action="REPLY",
                    reply=(
                        f"[KOTH // OP] LLM offline ({exc}).\n"
                        "Você ainda pode lançar desafios direto com `/solve Título | cat=web | target=host:port | descrição` "
                        "ou marcar 'Lançar como desafio'. Digite `/help` para comandos."
                    ),
                )

        if action_obj.action == "LAUNCH_CHALLENGE":
            ch, sid, warn = _launch_challenge(
                session, storage, orch, action_obj.model_dump(), event_id, mode, uploads,
            )
            extra = f"\nAviso: {warn}" if warn else f"\nEscalada iniciada ({mode}, sessão {(sid or '-')[:8]})."
            rec = CliMessageRecord(
                role="op",
                content=f"{action_obj.reply}\n→ Missão #{ch.id} {ch.title} ({ch.category}, {ch.points} pts){extra}",
                challenge_id=ch.id,
                session_id=sid,
            )
        elif action_obj.action == "RANK":
            rec = CliMessageRecord(role="op", content=f"{action_obj.reply}\n\n{_format_rank(session, active_event)}")
        elif action_obj.action == "STATUS":
            rec = CliMessageRecord(role="op", content=f"{action_obj.reply}\n\n{_format_status(session, active_event)}")
        else:
            rec = CliMessageRecord(role="op", content=action_obj.reply)

    session.add(rec)
    session.commit()
    session.refresh(rec)
    return rec
