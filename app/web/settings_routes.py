"""Rotas web de configurações (/settings): provedores, modelos, limites, sandbox, MCP e arsenal."""
from pathlib import Path
import shlex

import yaml
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from ..agents.cli_providers import (
    CLI_CATALOG_BY_NAME,
    check_cli_provider_connection,
    cli_provider_status_rows,
)
from ..agents.config import load_agents, load_models
from ..agents.providers import (
    CATALOG_BY_NAME,
    MODEL_PROFILES,
    ROLE_LABELS,
    apply_model_profile,
    delete_provider_key,
    detect_active_profile,
    fetch_provider_models,
    import_opencode_keys,
    opencode_auth_path,
    provider_status_rows,
    save_provider_key,
    test_provider_connection,
    update_model_roles,
)
from ..database import get_session
from ..templates import templates
from ..tooling.config import load_tools_config
from ..tooling.doctor import inventory
from ..tooling.mcp_servers import (
    add_mcp_server,
    delete_mcp_server,
    koth_mcp_client_snippet,
    mcp_status_rows,
    toggle_mcp_server,
)
from ..tooling.sandbox import (
    build_sandbox_image,
    current_sandbox_mode,
    docker_status,
    reset_persistent_containers,
    switch_tools_mode,
)
from . import routes as web_routes
from .theme import load_active_theme, save_active_theme, theme_options

router = APIRouter()


def _models_for_role(
    provider_name: str,
    role: str,
    storage_dir: Path,
    *,
    fetch_live: bool = False,
) -> tuple[list[str], str]:
    if not provider_name:
        return [], ""
    if fetch_live:
        return fetch_provider_models(provider_name, storage_dir, models_path=web_routes.MODELS_PATH)
    if provider_name in CLI_CATALOG_BY_NAME:
        return list(CLI_CATALOG_BY_NAME[provider_name].models), "cli"
    preset = CATALOG_BY_NAME.get(provider_name)
    return (list(preset.models) if preset else []), "presets"


@router.get("/settings")
def settings_page(
    request: Request,
    saved: str | None = None,
    tab: str = "providers",
    session: Session = Depends(get_session),
):
    from ..knowledge.index import DEFAULT_KNOWLEDGE_ROOT, KnowledgeIndex

    ctx = web_routes.nav_context(session, active_nav="settings")
    models_cfg = load_models(web_routes.MODELS_PATH)
    agents_cfg = load_agents(web_routes.AGENTS_PATH)
    tools_cfg = load_tools_config(web_routes.TOOLS_PATH)
    tools_rows = inventory(tools_cfg)
    mcp_rows = mcp_status_rows(web_routes.MCP_PATH)
    mcp_snippet = koth_mcp_client_snippet()
    sandbox_mode = current_sandbox_mode(web_routes.TOOLS_PATH)
    storage_dir = request.app.state.storage.storage_dir
    providers_status = provider_status_rows(storage_dir, models_path=web_routes.MODELS_PATH)
    cli_providers = cli_provider_status_rows()
    knowledge_count = KnowledgeIndex(session, knowledge_root=DEFAULT_KNOWLEDGE_ROOT).count()
    strong_fb = models_cfg.fallbacks.get("strong", [])
    fast_fb = models_cfg.fallbacks.get("fast", [])
    strong_ref = models_cfg.roles.get("strong")
    fast_ref = models_cfg.roles.get("fast")

    strong_models, strong_src = _models_for_role(
        strong_ref.provider if strong_ref else "", "strong", storage_dir
    )
    fast_models, fast_src = _models_for_role(
        fast_ref.provider if fast_ref else "", "fast", storage_dir
    )
    fb_models, fb_src = _models_for_role(
        strong_fb[0].provider if strong_fb else "", "fallback", storage_dir
    )
    fast_fb_models, fast_fb_src = _models_for_role(
        fast_fb[0].provider if fast_fb else "", "fast_fallback", storage_dir
    )
    dk_info = docker_status(tools_cfg.defaults.docker_image)
    active_profile = detect_active_profile(models_cfg, agents_cfg)
    role_routing_rows = []
    for r_key, r_label in ROLE_LABELS.items():
        if r_key not in agents_cfg.roles:
            continue
        tier = agents_cfg.roles[r_key].model
        ref = models_cfg.roles.get(tier)
        role_routing_rows.append({
            "role": r_key,
            "label": r_label,
            "tier": tier,
            "provider": ref.provider if ref else "—",
            "model_name": ref.name if ref else "—",
        })

    ctx.update({
        "models_cfg": models_cfg,
        "model_profiles": MODEL_PROFILES,
        "active_profile": active_profile,
        "role_routing_rows": role_routing_rows,
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
def settings_model_options(
    request: Request,
    role: str = "strong",
    provider: str = "",
    strong_provider: str = "",
    fast_provider: str = "",
    fallback_provider: str = "",
    fast_fallback_provider: str = "",
    current: str = "",
):
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
def settings_add_provider(
    request: Request,
    provider_name: str = Form(...),
    api_key: str = Form(""),
    base_url: str = Form(""),
    activate_now: bool = Form(False),
):
    save_provider_key(
        request.app.state.storage.storage_dir,
        provider_name,
        api_key,
        base_url=base_url if base_url.strip() or provider_name in ("nine_router", "ollama") else None,
        models_path=web_routes.MODELS_PATH,
    )
    if activate_now and provider_name in CATALOG_BY_NAME:
        preset = CATALOG_BY_NAME[provider_name]
        models_cfg = load_models(web_routes.MODELS_PATH)
        strong_fb = models_cfg.fallbacks.get("strong", [])
        update_model_roles(
            preset.name,
            preset.strong_default,
            preset.name,
            preset.fast_default,
            fallback_provider=strong_fb[0].provider if strong_fb else None,
            fallback_name=strong_fb[0].name if strong_fb else None,
            models_path=web_routes.MODELS_PATH,
        )
    from ..agents.api import reload_orchestrator
    reload_orchestrator(request.app)
    return RedirectResponse("/settings?tab=providers&saved=provider", status_code=303)


@router.post("/settings/cli-providers/{provider_name}/test")
def settings_test_cli_provider(request: Request, provider_name: str):
    ok, msg = check_cli_provider_connection(provider_name)
    return templates.TemplateResponse(request, "_provider_test.html", {"ok": ok, "msg": msg})


@router.post("/settings/cli-providers/{provider_name}/activate")
def settings_activate_cli_provider(
    request: Request,
    provider_name: str,
    scope: str = Form("strong"),
):
    spec = CLI_CATALOG_BY_NAME.get(provider_name)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown CLI provider")
    models_cfg = load_models(web_routes.MODELS_PATH)
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
        models_path=web_routes.MODELS_PATH,
    )
    from ..agents.api import reload_orchestrator
    reload_orchestrator(request.app)
    return RedirectResponse("/settings?tab=models&saved=models", status_code=303)


@router.post("/settings/providers/import-opencode")
def settings_import_opencode(request: Request):
    import_opencode_keys(request.app.state.storage.storage_dir, models_path=web_routes.MODELS_PATH)
    from ..agents.api import reload_orchestrator
    reload_orchestrator(request.app)
    return RedirectResponse("/settings?tab=providers&saved=imported", status_code=303)


@router.post("/settings/providers/{provider_name}/save")
def settings_save_provider(
    request: Request,
    provider_name: str,
    api_key: str = Form(""),
    base_url: str = Form(""),
):
    save_provider_key(
        request.app.state.storage.storage_dir,
        provider_name,
        api_key,
        base_url=base_url if base_url.strip() or provider_name in ("nine_router", "ollama") else None,
        models_path=web_routes.MODELS_PATH,
    )
    from ..agents.api import reload_orchestrator
    reload_orchestrator(request.app)
    return RedirectResponse("/settings?tab=providers&saved=provider", status_code=303)


@router.post("/settings/providers/{provider_name}/delete")
def settings_delete_provider(request: Request, provider_name: str):
    delete_provider_key(
        request.app.state.storage.storage_dir,
        provider_name,
        models_path=web_routes.MODELS_PATH,
    )
    from ..agents.api import reload_orchestrator
    reload_orchestrator(request.app)
    return RedirectResponse("/settings?tab=providers&saved=removed", status_code=303)


@router.post("/settings/providers/{provider_name}/activate")
def settings_activate_provider(request: Request, provider_name: str):
    preset = CATALOG_BY_NAME.get(provider_name)
    if preset is None:
        raise HTTPException(status_code=404, detail="unknown provider preset")
    models_cfg = load_models(web_routes.MODELS_PATH)
    strong_fb = models_cfg.fallbacks.get("strong", [])
    update_model_roles(
        preset.name,
        preset.strong_default,
        preset.name,
        preset.fast_default,
        fallback_provider=strong_fb[0].provider if strong_fb else None,
        fallback_name=strong_fb[0].name if strong_fb else None,
        models_path=web_routes.MODELS_PATH,
    )
    from ..agents.api import reload_orchestrator
    reload_orchestrator(request.app)
    return RedirectResponse("/settings?tab=models&saved=models", status_code=303)


@router.post("/settings/providers/{provider_name}/test")
def settings_test_provider(request: Request, provider_name: str):
    ok, msg = test_provider_connection(
        provider_name,
        request.app.state.storage.storage_dir,
        models_path=web_routes.MODELS_PATH,
    )
    return templates.TemplateResponse(request, "_provider_test.html", {"ok": ok, "msg": msg})


@router.post("/settings/models")
def settings_save_models(
    request: Request,
    strong_provider: str = Form(...),
    strong_name: str = Form(...),
    fast_provider: str = Form(...),
    fast_name: str = Form(...),
    fallback_provider: str = Form(""),
    fallback_name: str = Form(""),
    fast_fallback_provider: str = Form(""),
    fast_fallback_name: str = Form(""),
):
    update_model_roles(
        strong_provider,
        strong_name,
        fast_provider,
        fast_name,
        fallback_provider=fallback_provider.strip() or None,
        fallback_name=fallback_name.strip() or None,
        fast_fallback_provider=fast_fallback_provider.strip() or None,
        fast_fallback_name=fast_fallback_name.strip() or None,
        models_path=web_routes.MODELS_PATH,
    )
    from ..agents.api import reload_orchestrator
    reload_orchestrator(request.app)
    return RedirectResponse("/settings?tab=models&saved=models", status_code=303)


@router.post("/settings/profiles/{profile_name}/activate")
def settings_activate_profile(request: Request, profile_name: str):
    try:
        apply_model_profile(
            profile_name,
            models_path=web_routes.MODELS_PATH,
            agents_path=web_routes.AGENTS_PATH,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown model profile: {profile_name}")
    from ..agents.api import reload_orchestrator
    reload_orchestrator(request.app)
    return RedirectResponse("/settings?tab=models&saved=profile", status_code=303)


@router.post("/settings/limits")
def update_limits(
    request: Request,
    max_attempts: int = Form(...),
    max_minutes: int = Form(...),
    allow_parallel_racing: str | None = Form(None),
    max_race_branches: int | None = Form(None),
):
    with open(web_routes.AGENTS_PATH, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    raw.setdefault("limits", {})
    raw["limits"]["max_attempts"] = max(1, int(max_attempts))
    raw["limits"]["max_minutes"] = max(1, int(max_minutes))
    if allow_parallel_racing is not None:
        raw["limits"]["allow_parallel_racing"] = str(allow_parallel_racing).lower() in ("1", "true", "on", "yes")
    if max_race_branches is not None:
        raw["limits"]["max_race_branches"] = max(1, min(int(max_race_branches), 2))
    with open(web_routes.AGENTS_PATH, "w", encoding="utf-8") as fh:
        yaml.safe_dump(raw, fh, sort_keys=False, allow_unicode=True)
    orch = getattr(request.app.state, "_orchestrator", None)
    if orch is not None:
        orch.limits = dict(raw["limits"])
    return RedirectResponse("/settings?tab=runtime&saved=limits", status_code=303)


@router.post("/settings/sandbox/mode")
def settings_sandbox_mode(
    request: Request,
    mode: str = Form("native"),
    docker_image: str = Form("koth-sandbox:latest"),
    persistent_docker: bool = Form(False),
):
    chosen = mode if mode in ("docker", "auto", "native") else "native"
    switch_tools_mode(
        chosen,
        docker_image=docker_image,
        persistent_docker=persistent_docker,
        tools_path=web_routes.TOOLS_PATH,
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
    tools_cfg = load_tools_config(web_routes.TOOLS_PATH)
    st = docker_status(tools_cfg.defaults.docker_image)
    ok = bool(st["daemon_running"] and st["image_present"])
    return templates.TemplateResponse(request, "_provider_test.html", {"ok": ok, "msg": st["message"]})


@router.post("/settings/sandbox/build")
def settings_sandbox_build(request: Request):
    tools_cfg = load_tools_config(web_routes.TOOLS_PATH)
    tag = tools_cfg.defaults.docker_image or "koth-sandbox:latest"
    ok, msg = build_sandbox_image(tag=tag)
    return templates.TemplateResponse(request, "_provider_test.html", {"ok": ok, "msg": msg})


@router.post("/settings/mcp/toggle")
def settings_mcp_toggle(request: Request, name: str = Form(...)):
    try:
        toggle_mcp_server(name, path=web_routes.MCP_PATH)
        from ..agents.api import reload_orchestrator
        reload_orchestrator(request.app)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"MCP '{name}' não encontrado")
    return RedirectResponse("/settings?tab=runtime&saved=mcp", status_code=303)


@router.post("/settings/mcp/add")
def settings_mcp_add(
    request: Request,
    name: str = Form(...),
    label: str = Form(""),
    category: str = Form("all"),
    transport: str = Form("stdio"),
    command: str = Form(""),
    args: str = Form(""),
    url: str = Form(""),
    description: str = Form(""),
):
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
            path=web_routes.MCP_PATH,
        )
        from ..agents.api import reload_orchestrator
        reload_orchestrator(request.app)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse("/settings?tab=runtime&saved=mcp", status_code=303)


@router.post("/settings/mcp/delete")
def settings_mcp_delete(request: Request, name: str = Form(...)):
    delete_mcp_server(name, path=web_routes.MCP_PATH)
    from ..agents.api import reload_orchestrator
    reload_orchestrator(request.app)
    return RedirectResponse("/settings?tab=runtime&saved=mcp", status_code=303)


@router.post("/settings/mcp/test")
def settings_mcp_test(request: Request, name: str = Form(...)):
    ok, msg = web_routes.probe_mcp_server(name, path=web_routes.MCP_PATH)
    return templates.TemplateResponse(request, "_provider_test.html", {"ok": ok, "msg": msg})


@router.post("/settings/knowledge/rebuild")
def settings_rebuild_knowledge(request: Request, session: Session = Depends(get_session)):
    from ..knowledge.index import DEFAULT_KNOWLEDGE_ROOT, KnowledgeIndex

    n = KnowledgeIndex(session, knowledge_root=DEFAULT_KNOWLEDGE_ROOT).rebuild(DEFAULT_KNOWLEDGE_ROOT)
    return templates.TemplateResponse(request, "_knowledge_status.html", {"count": n})
