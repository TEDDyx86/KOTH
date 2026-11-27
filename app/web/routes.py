import os
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlmodel import Session, select

from ..agents.cli_op import list_cli_messages, process_cli_turn
from ..agents.cli_providers import (
    CLI_CATALOG_BY_NAME,
    check_cli_provider_connection,
    cli_provider_status_rows,
)
from ..agents.config import AGENTS_PATH, MODELS_PATH, load_agents, load_models
from ..agents.planner import planner_rank
from ..agents.writeup import (
    build_arsenal_digest,
    build_challenge_writeup,
    build_event_writeup,
    build_proof_bundle_zip,
    persist_writeup_note_and_attachment,
    save_writeup_to_arsenal,
)
from ..agents.providers import (
    CATALOG_BY_NAME,
    delete_provider_key,
    fetch_provider_models,
    import_opencode_keys,
    opencode_auth_path,
    provider_status_rows,
    save_provider_key,
    test_provider_connection,
    update_model_roles,
)
from ..database import get_session
from ..models import (
    Attachment, AttachmentType, Attempt, AttemptResult, AttemptType,
    ChallengeStatus, Difficulty, Event, EventStatus,
)
from ..services import tracking
from ..services.storage import StorageService
from ..templates import templates
from ..tooling.config import DEFAULT_PATH as TOOLS_PATH, load_tools_config
from ..tooling.doctor import inventory
from ..tooling.mcp_servers import (
    DEFAULT_MCP_PATH as MCP_PATH,
    add_mcp_server,
    delete_mcp_server,
    koth_mcp_client_snippet,
    mcp_status_rows,
    probe_mcp_server,
    toggle_mcp_server,
)
from ..tooling.sandbox import (
    build_sandbox_image,
    current_sandbox_mode,
    docker_status,
    reset_persistent_containers,
    switch_tools_mode,
)
from .theme import load_active_theme, save_active_theme, theme_options

router = APIRouter()


def nav_context(session: Session, active_nav: str = "") -> dict:
    """Shared sidebar navigation data across all KOTH pages."""
    events = tracking.list_events(session)
    event_rows = []
    for ev in events:
        chs = tracking.list_challenges(session, event_id=ev.id)
        solved = sum(1 for c in chs if c.status is ChallengeStatus.solved)
        event_rows.append((ev, solved, len(chs)))
    open_challenges = tracking.list_challenges(session, status=ChallengeStatus.open)
    return {
        "active_nav": active_nav,
        "nav_events": event_rows,
        "nav_open_challenges": open_challenges,
    }


def _safe_orchestrator(request: Request):
    try:
        from ..agents.api import get_orchestrator

        return get_orchestrator(request)
    except Exception:
        return None


def _cli_feed_ctx(request: Request, session: Session) -> dict:
    messages = list_cli_messages(session)
    orch = _safe_orchestrator(request)
    sessions_by_ch: dict[int, dict] = {}
    has_running = False
    challenge_map: dict[int, object] = {}
    for m in messages:
        if m.challenge_id and m.challenge_id not in challenge_map:
            ch = tracking.get_challenge(session, m.challenge_id)
            if ch:
                challenge_map[m.challenge_id] = ch
        if m.challenge_id and orch is not None and m.challenge_id not in sessions_by_ch:
            sess = (orch.latest_for_challenge(session, m.challenge_id)
                    if hasattr(orch, "latest_for_challenge") else None)
            if sess is not None:
                d = sess.to_dict()
                sessions_by_ch[m.challenge_id] = d
                if d["state"] == "running":
                    has_running = True
    return {
        "cli_messages": messages,
        "cli_sessions": sessions_by_ch,
        "cli_challenges": challenge_map,
        "cli_has_running": has_running,
    }


def _parse_difficulty(raw: str) -> Difficulty | None:
    cleaned = raw.strip().lower()
    if not cleaned:
        return None
    try:
        return Difficulty(cleaned)
    except ValueError:
        return None


def _save_uploads(session: Session, storage: StorageService, challenge_id: int,
                  uploads: list[UploadFile], attachment_type: AttachmentType = AttachmentType.download) -> None:
    for upload in uploads:
        if not upload or not upload.filename:
            continue
        data = upload.file.read()
        if not data and not upload.filename:
            continue
        path = storage.save(challenge_id, upload.filename, data)
        session.add(Attachment(
            challenge_id=challenge_id,
            name=upload.filename,
            path=str(path.relative_to(storage.storage_dir)),
            mime=upload.content_type or "application/octet-stream",
            size=len(data),
            type=attachment_type,
        ))
    session.commit()


def _challenge_redirect(request: Request, session: Session, challenge_id: int, go_to: str) -> str:
    if go_to == "agent_auto":
        try:
            from ..agents.api import get_orchestrator

            orch = get_orchestrator(request)
            if hasattr(orch, "start_background"):
                orch.start_background(session.get_bind(), session, challenge_id, mode="auto")
            else:
                orch.start(session, challenge_id, mode="auto")
        except Exception:
            pass  # panel will surface 503/idle cleanly if runtime is unavailable
        return f"/challenges/{challenge_id}/agent"
    if go_to == "agent":
        return f"/challenges/{challenge_id}/agent"
    return f"/challenges/{challenge_id}"


@router.get("/")
def cli_home(request: Request, session: Session = Depends(get_session)):
    ctx = nav_context(session, active_nav="cli")
    ctx.update(_cli_feed_ctx(request, session))
    events = tracking.list_events(session)
    ctx["event_list"] = events
    ctx["active_event"] = events[-1] if events else None
    return templates.TemplateResponse(request, "cli.html", ctx)


@router.get("/cli/feed")
def cli_feed(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "_cli_feed.html", _cli_feed_ctx(request, session))


@router.post("/cli/message")
def cli_message(request: Request,
                message: str = Form(""),
                event_id: str = Form(""),
                mode: str = Form("auto"),
                force_launch: bool = Form(False),
                files: list[UploadFile] = File(default=[]),
                session: Session = Depends(get_session)):
    eid = int(event_id) if event_id.strip().isdigit() else None
    orch = _safe_orchestrator(request)
    process_cli_turn(
        session,
        request.app.state.storage,
        orch,
        message,
        event_id=eid,
        mode=mode if mode in ("auto", "supervised") else "auto",
        force_launch=force_launch,
        uploads=files,
    )
    return templates.TemplateResponse(request, "_cli_feed.html", _cli_feed_ctx(request, session))


@router.get("/dashboard")
def dashboard(request: Request, session: Session = Depends(get_session)):
    ctx = nav_context(session, active_nav="dashboard")
    events = tracking.list_events(session)
    all_challenges = tracking.list_challenges(session)
    challenge_map = {c.id: c for c in all_challenges}
    recent = list(session.exec(
        select(Attempt).order_by(Attempt.timestamp.desc()).limit(15)
    ).all())

    orch = _safe_orchestrator(request)
    challenge_states: dict[int, dict] = {}
    running_count = 0
    awaiting_count = 0
    total_cost_usd = 0.0
    for ch in ctx["nav_open_challenges"]:
        if orch is not None and hasattr(orch, "latest_for_challenge"):
            sess = orch.latest_for_challenge(session, ch.id)
            if sess is not None:
                st = sess.state
                sdict = sess.to_dict() if hasattr(sess, "to_dict") else {}
                cost = float(sdict.get("estimated_cost_usd", 0.0) or 0.0)
                total_cost_usd += cost
                challenge_states[ch.id] = {
                    "state": st,
                    "dispatch_count": sess.dispatch_count,
                    "cost_usd": cost,
                }
                if st == "running":
                    running_count += 1
                elif st == "awaiting_approval":
                    awaiting_count += 1

    event_cards = []
    for ev in events:
        chs = [c for c in all_challenges if c.event_id == ev.id]
        solved_chs = [c for c in chs if c.status is ChallengeStatus.solved]
        total_pts = sum(int(c.points or 0) for c in chs)
        earned_pts = sum(int(c.points or 0) for c in solved_chs)
        pct = round(100 * len(solved_chs) / len(chs)) if chs else 0
        event_cards.append({
            "event": ev,
            "solved": len(solved_chs),
            "total": len(chs),
            "open_count": len(chs) - len(solved_chs),
            "earned_pts": earned_pts,
            "total_pts": total_pts,
            "pct": pct,
        })

    solved_total = sum(1 for c in all_challenges if c.status is ChallengeStatus.solved)
    earned_total_pts = sum(
        int(c.points or 0) for c in all_challenges if c.status is ChallengeStatus.solved
    )
    total_pts_all = sum(int(c.points or 0) for c in all_challenges)
    open_categories = sorted({
        (c.category or "misc").strip().lower() for c in ctx["nav_open_challenges"]
    })

    ctx.update({
        "events": ctx["nav_events"],
        "event_cards": event_cards,
        "event_list": events,
        "open_challenges": ctx["nav_open_challenges"],
        "challenge_states": challenge_states,
        "challenge_map": challenge_map,
        "open_categories": open_categories,
        "recent_attempts": recent,
        "sandbox_mode": current_sandbox_mode(TOOLS_PATH),
        "dash_stats": {
            "open_count": len(ctx["nav_open_challenges"]),
            "solved_count": solved_total,
            "total_count": len(all_challenges),
            "earned_pts": earned_total_pts,
            "total_pts": total_pts_all,
            "running_count": running_count,
            "awaiting_count": awaiting_count,
            "events_count": len(events),
            "total_cost_usd": round(total_cost_usd, 4),
        },
    })
    return templates.TemplateResponse(request, "dashboard.html", ctx)


def _models_for_role(provider_name: str, role: str, storage_dir: Path,
                     *, fetch_live: bool = False) -> tuple[list[str], str]:
    if not provider_name:
        return [], ""
    if fetch_live:
        return fetch_provider_models(provider_name, storage_dir, models_path=MODELS_PATH)
    if provider_name in CLI_CATALOG_BY_NAME:
        return list(CLI_CATALOG_BY_NAME[provider_name].models), "cli"
    preset = CATALOG_BY_NAME.get(provider_name)
    return (list(preset.models) if preset else []), "presets"


@router.get("/settings")
def settings_page(request: Request, saved: str | None = None,
                  tab: str = "providers",
                  session: Session = Depends(get_session)):
    from ..knowledge.index import DEFAULT_KNOWLEDGE_ROOT, KnowledgeIndex

    ctx = nav_context(session, active_nav="settings")
    models_cfg = load_models(MODELS_PATH)
    agents_cfg = load_agents(AGENTS_PATH)
    tools_cfg = load_tools_config(TOOLS_PATH)
    tools_rows = inventory(tools_cfg)
    mcp_rows = mcp_status_rows(MCP_PATH)
    mcp_snippet = koth_mcp_client_snippet()
    sandbox_mode = current_sandbox_mode(TOOLS_PATH)
    storage_dir = request.app.state.storage.storage_dir
    providers_status = provider_status_rows(storage_dir, models_path=MODELS_PATH)
    cli_providers = cli_provider_status_rows()
    knowledge_count = KnowledgeIndex(session, knowledge_root=DEFAULT_KNOWLEDGE_ROOT).count()
    strong_fb = models_cfg.fallbacks.get("strong", [])
    fast_fb = models_cfg.fallbacks.get("fast", [])
    strong_ref = models_cfg.roles.get("strong")
    fast_ref = models_cfg.roles.get("fast")

    strong_models, strong_src = _models_for_role(
        strong_ref.provider if strong_ref else "", "strong", storage_dir)
    fast_models, fast_src = _models_for_role(
        fast_ref.provider if fast_ref else "", "fast", storage_dir)
    fb_models, fb_src = _models_for_role(
        strong_fb[0].provider if strong_fb else "", "fallback", storage_dir)
    fast_fb_models, fast_fb_src = _models_for_role(
        fast_fb[0].provider if fast_fb else "", "fast_fallback", storage_dir)
    dk_info = docker_status(tools_cfg.defaults.docker_image)

    ctx.update({
        "models_cfg": models_cfg,
        "strong_fallback": strong_fb[0] if strong_fb else None,
        "fast_fallback": fast_fb[0] if fast_fb else None,
        "strong_models": strong_models,
        "strong_src": strong_src,
        "fast_models": fast_models,
        "fast_src": fast_src,
        "fallback_models": fb_models,
        "fallback_src": fb_src,
        "fast_fallback_models": fast_fb_models,
        "fast_fallback_src": fast_fb_src,
        "agents_cfg": agents_cfg,
        "tools_rows": tools_rows,
        "tools_cfg": tools_cfg,
        "missing_tools_count": sum(1 for r in tools_rows if not r["present"]),
        "docker_info": dk_info,
        "mcp_rows": mcp_rows,
        "mcp_snippet": mcp_snippet,
        "sandbox_mode": sandbox_mode,
        "providers_status": providers_status,
        "added_providers": [p for p in providers_status if p["added"]],
        "cli_providers": cli_providers,
        "has_opencode_auth": opencode_auth_path().exists(),
        "knowledge_count": knowledge_count,
        "ui_themes": theme_options(),
        "active_theme": getattr(request.app.state, "ui_theme", None) or load_active_theme(storage_dir),
        "saved": saved,
        "active_tab": tab if tab in ("providers", "models", "runtime", "brand") else "providers",
    })
    return templates.TemplateResponse(request, "settings.html", ctx)


@router.post("/settings/theme")
def settings_set_theme(request: Request, theme: str = Form("crimson")):
    storage_dir = request.app.state.storage.storage_dir
    try:
        chosen = save_active_theme(theme, storage_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    request.app.state.ui_theme = chosen
    return RedirectResponse("/settings?tab=brand&saved=theme", status_code=303)


@router.get("/settings/models/options")
def settings_model_options(request: Request,
                           role: str = "strong",
                           provider: str = "",
                           strong_provider: str = "",
                           fast_provider: str = "",
                           fallback_provider: str = "",
                           fast_fallback_provider: str = "",
                           current: str = ""):
    chosen = provider or (
        strong_provider if role == "strong"
        else fast_provider if role == "fast"
        else fast_fallback_provider if role == "fast_fallback"
        else fallback_provider
    )
    storage_dir = request.app.state.storage.storage_dir
    models, source = _models_for_role(chosen, role, storage_dir, fetch_live=True)
    field_name = f"{role}_name"
    return templates.TemplateResponse(request, "_model_options.html", {
        "field_name": field_name,
        "role": role,
        "provider": chosen,
        "models": models,
        "source": source,
        "current": current,
    })


@router.post("/settings/providers/add")
def settings_add_provider(request: Request,
                          provider_name: str = Form(...),
                          api_key: str = Form(""),
                          base_url: str = Form(""),
                          activate_now: bool = Form(False)):
    save_provider_key(
        request.app.state.storage.storage_dir,
        provider_name,
        api_key,
        base_url=base_url if base_url.strip() or provider_name in ("nine_router", "ollama") else None,
        models_path=MODELS_PATH,
    )
    if activate_now and provider_name in CATALOG_BY_NAME:
        preset = CATALOG_BY_NAME[provider_name]
        models_cfg = load_models(MODELS_PATH)
        strong_fb = models_cfg.fallbacks.get("strong", [])
        update_model_roles(
            preset.name,
            preset.strong_default,
            preset.name,
            preset.fast_default,
            fallback_provider=strong_fb[0].provider if strong_fb else None,
            fallback_name=strong_fb[0].name if strong_fb else None,
            models_path=MODELS_PATH,
        )
    from ..agents.api import reload_orchestrator
    reload_orchestrator(request.app)
    return RedirectResponse("/settings?tab=providers&saved=provider", status_code=303)


@router.post("/settings/cli-providers/{provider_name}/test")
def settings_test_cli_provider(request: Request, provider_name: str):
    ok, msg = check_cli_provider_connection(provider_name)
    return templates.TemplateResponse(request, "_provider_test.html", {"ok": ok, "msg": msg})


@router.post("/settings/cli-providers/{provider_name}/activate")
def settings_activate_cli_provider(request: Request, provider_name: str,
                                   scope: str = Form("strong")):
    spec = CLI_CATALOG_BY_NAME.get(provider_name)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown CLI provider")
    models_cfg = load_models(MODELS_PATH)
    fast_ref = models_cfg.roles.get("fast")
    strong_fb = models_cfg.fallbacks.get("strong", [])
    if scope == "all" or fast_ref is None:
        fast_prov, fast_name = spec.name, spec.fast_default
    else:
        fast_prov, fast_name = fast_ref.provider, fast_ref.name
    update_model_roles(
        spec.name,
        spec.strong_default,
        fast_prov,
        fast_name,
        fallback_provider=strong_fb[0].provider if strong_fb else None,
        fallback_name=strong_fb[0].name if strong_fb else None,
        models_path=MODELS_PATH,
    )
    from ..agents.api import reload_orchestrator
    reload_orchestrator(request.app)
    return RedirectResponse("/settings?tab=models&saved=models", status_code=303)


@router.post("/settings/providers/import-opencode")
def settings_import_opencode(request: Request):
    import_opencode_keys(request.app.state.storage.storage_dir, models_path=MODELS_PATH)
    from ..agents.api import reload_orchestrator
    reload_orchestrator(request.app)
    return RedirectResponse("/settings?tab=providers&saved=imported", status_code=303)


@router.post("/settings/providers/{provider_name}/save")
def settings_save_provider(request: Request, provider_name: str,
                           api_key: str = Form(""),
                           base_url: str = Form("")):
    save_provider_key(
        request.app.state.storage.storage_dir,
        provider_name,
        api_key,
        base_url=base_url if base_url.strip() or provider_name in ("nine_router", "ollama") else None,
        models_path=MODELS_PATH,
    )
    from ..agents.api import reload_orchestrator
    reload_orchestrator(request.app)
    return RedirectResponse("/settings?tab=providers&saved=provider", status_code=303)


@router.post("/settings/providers/{provider_name}/delete")
def settings_delete_provider(request: Request, provider_name: str):
    delete_provider_key(
        request.app.state.storage.storage_dir,
        provider_name,
        models_path=MODELS_PATH,
    )
    from ..agents.api import reload_orchestrator
    reload_orchestrator(request.app)
    return RedirectResponse("/settings?tab=providers&saved=removed", status_code=303)


@router.post("/settings/providers/{provider_name}/activate")
def settings_activate_provider(request: Request, provider_name: str):
    preset = CATALOG_BY_NAME.get(provider_name)
    if preset is None:
        raise HTTPException(status_code=404, detail="unknown provider preset")
    models_cfg = load_models(MODELS_PATH)
    strong_fb = models_cfg.fallbacks.get("strong", [])
    update_model_roles(
        preset.name,
        preset.strong_default,
        preset.name,
        preset.fast_default,
        fallback_provider=strong_fb[0].provider if strong_fb else None,
        fallback_name=strong_fb[0].name if strong_fb else None,
        models_path=MODELS_PATH,
    )
    from ..agents.api import reload_orchestrator
    reload_orchestrator(request.app)
    return RedirectResponse("/settings?tab=models&saved=models", status_code=303)


@router.post("/settings/providers/{provider_name}/test")
def settings_test_provider(request: Request, provider_name: str):
    ok, msg = test_provider_connection(
        provider_name,
        request.app.state.storage.storage_dir,
        models_path=MODELS_PATH,
    )
    return templates.TemplateResponse(request, "_provider_test.html", {"ok": ok, "msg": msg})


@router.post("/settings/models")
def settings_save_models(request: Request,
                         strong_provider: str = Form(...),
                         strong_name: str = Form(...),
                         fast_provider: str = Form(...),
                         fast_name: str = Form(...),
                         fallback_provider: str = Form(""),
                         fallback_name: str = Form(""),
                         fast_fallback_provider: str = Form(""),
                         fast_fallback_name: str = Form("")):
    update_model_roles(
        strong_provider,
        strong_name,
        fast_provider,
        fast_name,
        fallback_provider=fallback_provider.strip() or None,
        fallback_name=fallback_name.strip() or None,
        fast_fallback_provider=fast_fallback_provider.strip() or None,
        fast_fallback_name=fast_fallback_name.strip() or None,
        models_path=MODELS_PATH,
    )
    from ..agents.api import reload_orchestrator
    reload_orchestrator(request.app)
    return RedirectResponse("/settings?tab=models&saved=models", status_code=303)


@router.post("/settings/limits")
def update_limits(request: Request,
                  max_attempts: int = Form(...),
                  max_minutes: int = Form(...)):
    with open(AGENTS_PATH, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    raw.setdefault("limits", {})
    raw["limits"]["max_attempts"] = max(1, int(max_attempts))
    raw["limits"]["max_minutes"] = max(1, int(max_minutes))
    with open(AGENTS_PATH, "w", encoding="utf-8") as fh:
        yaml.safe_dump(raw, fh, sort_keys=False, allow_unicode=True)
    orch = getattr(request.app.state, "_orchestrator", None)
    if orch is not None:
        orch.limits = dict(raw["limits"])
    return RedirectResponse("/settings?tab=runtime&saved=limits", status_code=303)


@router.post("/settings/sandbox/mode")
def settings_sandbox_mode(request: Request,
                          mode: str = Form("native"),
                          docker_image: str = Form("koth-sandbox:latest"),
                          persistent_docker: bool = Form(False)):
    chosen = mode if mode in ("docker", "auto", "native") else "native"
    switch_tools_mode(
        chosen,
        docker_image=docker_image,
        persistent_docker=persistent_docker,
        tools_path=TOOLS_PATH,
    )
    from ..agents.api import reload_orchestrator
    reload_orchestrator(request.app)
    return RedirectResponse("/settings?tab=runtime&saved=sandbox", status_code=303)


@router.post("/settings/sandbox/reset-containers")
def settings_sandbox_reset_containers(request: Request):
    stopped = reset_persistent_containers()
    return templates.TemplateResponse(
        request,
        "_provider_test.html",
        {"ok": True, "msg": f"{stopped} container(s) persistente(s) encerrado(s)"},
    )


@router.post("/settings/sandbox/test")
def settings_sandbox_test(request: Request):
    tools_cfg = load_tools_config(TOOLS_PATH)
    st = docker_status(tools_cfg.defaults.docker_image)
    ok = bool(st["daemon_running"] and st["image_present"])
    return templates.TemplateResponse(request, "_provider_test.html", {"ok": ok, "msg": st["message"]})


@router.post("/settings/sandbox/build")
def settings_sandbox_build(request: Request):
    tools_cfg = load_tools_config(TOOLS_PATH)
    tag = tools_cfg.defaults.docker_image or "koth-sandbox:latest"
    ok, msg = build_sandbox_image(tag=tag)
    return templates.TemplateResponse(request, "_provider_test.html", {"ok": ok, "msg": msg})


@router.post("/settings/mcp/toggle")
def settings_mcp_toggle(request: Request, name: str = Form(...)):
    try:
        toggle_mcp_server(name, path=MCP_PATH)
        from ..agents.api import reload_orchestrator
        reload_orchestrator(request.app)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"MCP '{name}' não encontrado")
    return RedirectResponse("/settings?tab=runtime&saved=mcp", status_code=303)


@router.post("/settings/mcp/add")
def settings_mcp_add(request: Request,
                     name: str = Form(...),
                     label: str = Form(""),
                     category: str = Form("all"),
                     transport: str = Form("stdio"),
                     command: str = Form(""),
                     args: str = Form(""),
                     url: str = Form(""),
                     description: str = Form("")):
    import shlex
    parsed_args = shlex.split(args) if args.strip() else []
    try:
        add_mcp_server(
            name,
            label=label,
            category=category,
            transport=transport,
            command=command,
            args=parsed_args,
            url=url,
            description=description,
            enabled=True,
            path=MCP_PATH,
        )
        from ..agents.api import reload_orchestrator
        reload_orchestrator(request.app)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse("/settings?tab=runtime&saved=mcp", status_code=303)


@router.post("/settings/mcp/delete")
def settings_mcp_delete(request: Request, name: str = Form(...)):
    delete_mcp_server(name, path=MCP_PATH)
    from ..agents.api import reload_orchestrator
    reload_orchestrator(request.app)
    return RedirectResponse("/settings?tab=runtime&saved=mcp", status_code=303)


@router.post("/settings/mcp/test")
def settings_mcp_test(request: Request, name: str = Form(...)):
    ok, msg = probe_mcp_server(name, path=MCP_PATH)
    return templates.TemplateResponse(request, "_provider_test.html", {"ok": ok, "msg": msg})


@router.post("/settings/knowledge/rebuild")
def settings_rebuild_knowledge(request: Request, session: Session = Depends(get_session)):
    from ..knowledge.index import DEFAULT_KNOWLEDGE_ROOT, KnowledgeIndex

    n = KnowledgeIndex(session, knowledge_root=DEFAULT_KNOWLEDGE_ROOT).rebuild(DEFAULT_KNOWLEDGE_ROOT)
    return templates.TemplateResponse(request, "_knowledge_status.html", {"count": n})


@router.post("/events")
def create_event_form(request: Request, name: str = Form(...),
                      flag_format: str = Form(""),
                      session: Session = Depends(get_session)):
    event = tracking.create_event(session, name.strip(),
                                  flag_format=flag_format.strip() or None)
    return RedirectResponse(f"/events/{event.id}", status_code=303)


def _safe_next(next_url: str | None, fallback: str) -> str:
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return fallback


@router.post("/events/{event_id}/edit")
def edit_event_form(event_id: int,
                    name: str = Form(...),
                    url: str = Form(""),
                    flag_format: str = Form(""),
                    status: str = Form("upcoming"),
                    next: str = Form(""),
                    session: Session = Depends(get_session)):
    try:
        ev_status = EventStatus(status)
    except ValueError:
        ev_status = EventStatus.upcoming
    try:
        tracking.update_event(
            session, event_id,
            name=name.strip(),
            url=url,
            flag_format=flag_format,
            status=ev_status,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="event not found")
    return RedirectResponse(_safe_next(next, f"/events/{event_id}"), status_code=303)


@router.post("/events/{event_id}/delete")
def delete_event_form(request: Request, event_id: int,
                      next: str = Form(""),
                      session: Session = Depends(get_session)):
    storage = getattr(request.app.state, "storage", None)
    try:
        tracking.delete_event(session, event_id, storage=storage)
    except LookupError:
        raise HTTPException(status_code=404, detail="event not found")
    return RedirectResponse(_safe_next(next, "/dashboard"), status_code=303)


@router.post("/challenges/quick")
def create_challenge_quick(request: Request,
                           event_id: str = Form(""),
                           new_event_name: str = Form(""),
                           title: str = Form(...),
                           category: str = Form("misc"),
                           points: int = Form(100),
                           difficulty: str = Form(""),
                           target: str | None = Form(None),
                           tags: str = Form(""),
                           description: str = Form(""),
                           go_to: str = Form("agent"),
                           files: list[UploadFile] = File(default=[]),
                           session: Session = Depends(get_session)):
    eid: int | None = int(event_id) if event_id.strip().isdigit() else None
    if eid is None:
        events = tracking.list_events(session)
        if new_event_name.strip():
            ev = tracking.create_event(session, new_event_name.strip())
            eid = ev.id
        elif events:
            eid = events[-1].id
        else:
            ev = tracking.create_event(session, "CTF Local")
            eid = ev.id

    ch = tracking.create_challenge(
        session, eid, title.strip(), category=category, points=points,
        difficulty=_parse_difficulty(difficulty), description=description,
        target=(target or "").strip() or None, tags=tags,
    )
    _save_uploads(session, request.app.state.storage, ch.id, files, AttachmentType.download)
    return RedirectResponse(_challenge_redirect(request, session, ch.id, go_to), status_code=303)


@router.get("/events/{event_id}")
def event_page(request: Request, event_id: int, category: str | None = None,
               status: str | None = None, sort: str | None = None,
               imported: str | None = None, skipped: str | None = None,
               session: Session = Depends(get_session)):
    event = session.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    challenge_status = ChallengeStatus(status) if status else None
    challenges = tracking.list_challenges(session, event_id=event_id,
                                          category=category, status=challenge_status)
    scores = {ch.id: round(score, 2) for ch, score in planner_rank(session, event)}
    if sort == "planner":
        challenges = sorted(challenges, key=lambda c: scores.get(c.id, 0.0), reverse=True)
    ctx = nav_context(session, active_nav=f"event-{event_id}")
    ctx.update({
        "event": event,
        "challenges": challenges,
        "scores": scores,
        "category": category,
        "status": status,
        "sort": sort,
        "imported": imported,
        "skipped": skipped,
    })
    return templates.TemplateResponse(request, "event.html", ctx)


@router.post("/events/{event_id}/import/ctfd")
def import_ctfd_form(request: Request, event_id: int,
                     base_url: str = Form(...),
                     api_token: str = Form(""),
                     session_cookie: str = Form(""),
                     session: Session = Depends(get_session)):
    from ..services.importer import import_ctfd_challenges

    event = session.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    try:
        res = import_ctfd_challenges(
            session,
            request.app.state.storage,
            event_id,
            base_url,
            api_token=api_token,
            session_cookie=session_cookie,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Falha ao importar CTFd: {exc}")
    return RedirectResponse(
        f"/events/{event_id}?imported={res['imported']}&skipped={res['skipped']}",
        status_code=303,
    )


@router.post("/events/{event_id}/challenges")
def create_challenge_form(request: Request, event_id: int, title: str = Form(...),
                          category: str = Form("misc"), points: int = Form(100),
                          difficulty: str = Form(""),
                          target: str | None = Form(None),
                          tags: str = Form(""), description: str = Form(""),
                          go_to: str = Form("challenge"),
                          files: list[UploadFile] = File(default=[]),
                          session: Session = Depends(get_session)):
    ch = tracking.create_challenge(
        session, event_id, title.strip(), category=category,
        points=points, difficulty=_parse_difficulty(difficulty),
        description=description, target=(target or "").strip() or None, tags=tags,
    )
    _save_uploads(session, request.app.state.storage, ch.id, files, AttachmentType.download)
    return RedirectResponse(_challenge_redirect(request, session, ch.id, go_to), status_code=303)


@router.get("/challenges/{challenge_id}")
def challenge_page(request: Request, challenge_id: int,
                   session: Session = Depends(get_session)):
    challenge = tracking.get_challenge(session, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    attachments = list(session.exec(
        select(Attachment).where(Attachment.challenge_id == challenge_id)
    ).all())
    ctx = nav_context(session, active_nav=f"challenge-{challenge_id}")
    ctx.update({
        "challenge": challenge,
        "summary": tracking.challenge_summary(session, challenge),
        "attempts": tracking.list_attempts(session, challenge_id),
        "notes": tracking.note_history(session, challenge_id),
        "attachments": attachments,
    })
    return templates.TemplateResponse(request, "challenge.html", ctx)


@router.post("/challenges/{challenge_id}/edit")
def edit_challenge_form(challenge_id: int,
                        title: str = Form(...),
                        category: str = Form("misc"),
                        points: int = Form(100),
                        difficulty: str = Form(""),
                        status: str = Form(""),
                        flag: str | None = Form(None),
                        target: str = Form(""),
                        tags: str = Form(""),
                        description: str = Form(""),
                        session: Session = Depends(get_session)):
    parsed_diff = _parse_difficulty(difficulty)
    parsed_status = None
    if status.strip():
        try:
            parsed_status = ChallengeStatus(status.strip())
        except ValueError:
            parsed_status = None
    try:
        tracking.update_challenge(
            session, challenge_id,
            title=title.strip(),
            category=category,
            points=points,
            difficulty=parsed_diff,
            clear_difficulty=(parsed_diff is None),
            status=parsed_status,
            flag=flag,
            target=target.strip(),
            tags=tags,
            description=description,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="challenge not found")
    return RedirectResponse(f"/challenges/{challenge_id}", status_code=303)


@router.post("/challenges/{challenge_id}/delete")
def delete_challenge_form(request: Request, challenge_id: int,
                          next: str = Form(""),
                          session: Session = Depends(get_session)):
    ch = tracking.get_challenge(session, challenge_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    fallback = f"/events/{ch.event_id}"
    storage = getattr(request.app.state, "storage", None)
    tracking.delete_challenge(session, challenge_id, storage=storage)
    return RedirectResponse(_safe_next(next, fallback), status_code=303)


@router.post("/challenges/{challenge_id}/attempts")
def record_attempt_form(request: Request, challenge_id: int,
                        description: str = Form(...), tool_command: str = Form(""),
                        result: str = Form("failed"), learning: str = Form(""),
                        session: Session = Depends(get_session)):
    tracking.record_attempt(session, challenge_id, AttemptType.manual, description,
                            AttemptResult(result), tool_command=tool_command or None,
                            learning=learning)
    return RedirectResponse(f"/challenges/{challenge_id}", status_code=303)


@router.post("/challenges/{challenge_id}/notes")
def add_note_form(request: Request, challenge_id: int, content: str = Form(...),
                  session: Session = Depends(get_session)):
    tracking.add_note(session, challenge_id, content)
    return RedirectResponse(f"/challenges/{challenge_id}", status_code=303)


@router.post("/challenges/{challenge_id}/attachments")
def upload_attachment_form(request: Request, challenge_id: int,
                           file: UploadFile | None = File(default=None),
                           files: list[UploadFile] = File(default=[]),
                           type: str = Form("download"),
                           session: Session = Depends(get_session)):
    challenge = tracking.get_challenge(session, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    uploads = [f for f in [file, *files] if f is not None and f.filename]
    _save_uploads(session, request.app.state.storage, challenge_id, uploads, AttachmentType(type))
    return RedirectResponse(f"/challenges/{challenge_id}", status_code=303)


def _latest_session_for(request: Request, session: Session, challenge_id: int):
    orch = _safe_orchestrator(request)
    if orch is not None and hasattr(orch, "latest_for_challenge"):
        return orch.latest_for_challenge(session, challenge_id)
    return None


@router.get("/challenges/{challenge_id}/writeup.md")
def download_challenge_writeup(request: Request, challenge_id: int,
                               session: Session = Depends(get_session)):
    ch = tracking.get_challenge(session, challenge_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    storage = getattr(request.app.state, "storage", None)
    sess = _latest_session_for(request, session, challenge_id)
    md = build_challenge_writeup(session, ch, agent_session=sess, storage=storage)
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in ch.title.lower()).strip("-") or f"challenge-{challenge_id}"
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{slug}-writeup.md"'},
    )


@router.get("/challenges/{challenge_id}/proof-bundle.zip")
def download_proof_bundle(request: Request, challenge_id: int,
                          session: Session = Depends(get_session)):
    ch = tracking.get_challenge(session, challenge_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    storage = getattr(request.app.state, "storage", None)
    sess = _latest_session_for(request, session, challenge_id)
    zip_bytes = build_proof_bundle_zip(session, ch, agent_session=sess, storage=storage)
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in ch.title.lower()).strip("-") or f"challenge-{challenge_id}"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{slug}-proofs.zip"'},
    )


@router.post("/challenges/{challenge_id}/writeup/generate")
def generate_challenge_writeup(request: Request, challenge_id: int,
                               next: str = Form(""),
                               session: Session = Depends(get_session)):
    ch = tracking.get_challenge(session, challenge_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    storage = getattr(request.app.state, "storage", None)
    sess = _latest_session_for(request, session, challenge_id)
    md = build_challenge_writeup(session, ch, agent_session=sess, storage=storage)
    persist_writeup_note_and_attachment(session, ch, md, storage=storage)
    return RedirectResponse(_safe_next(next, f"/challenges/{challenge_id}"), status_code=303)


@router.post("/challenges/{challenge_id}/arsenal/save")
def save_challenge_to_arsenal(request: Request, challenge_id: int,
                              session: Session = Depends(get_session)):
    from ..knowledge.index import DEFAULT_KNOWLEDGE_ROOT

    ch = tracking.get_challenge(session, challenge_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    storage = getattr(request.app.state, "storage", None)
    sess = _latest_session_for(request, session, challenge_id)
    md = build_arsenal_digest(session, ch, agent_session=sess, storage=storage)
    dest, count = save_writeup_to_arsenal(session, ch, md, knowledge_root=DEFAULT_KNOWLEDGE_ROOT)
    return templates.TemplateResponse(
        request,
        "_provider_test.html",
        {"ok": True, "msg": f"Técnica salva em {dest.name} · FTS5 reindexado ({count} docs)"},
    )


@router.get("/events/{event_id}/writeup.md")
def download_event_writeup(request: Request, event_id: int,
                           only_solved: bool = False,
                           session: Session = Depends(get_session)):
    event = session.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    storage = getattr(request.app.state, "storage", None)
    md = build_event_writeup(session, event, storage=storage, only_solved=only_solved)
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in event.name.lower()).strip("-") or f"event-{event_id}"
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{slug}-writeups.md"'},
    )
