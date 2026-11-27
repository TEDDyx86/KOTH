import json
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

from .cli_providers import CLI_CATALOG_BY_NAME, is_cli_provider
from .config import MODELS_PATH, ModelsConfig, load_models


@dataclass(frozen=True)
class ProviderPreset:
    name: str
    label: str
    env_var: str
    base_url: str | None
    requires_key: bool
    strong_default: str
    fast_default: str
    models: tuple[str, ...]


PROVIDER_CATALOG: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        name="nvidia",
        label="NVIDIA NIM",
        env_var="NVIDIA_API_KEY",
        base_url="https://integrate.api.nvidia.com/v1",
        requires_key=True,
        strong_default="nvidia/nemotron-3-ultra-550b-a55b",
        fast_default="nvidia/nemotron-3.5-super-vl-120b-a12b",
        models=(
            "nvidia/nemotron-3-ultra-550b-a55b",
            "nvidia/nemotron-3.5-super-vl-120b-a12b",
            "meta/llama-3.3-70b-instruct",
            "deepseek-ai/deepseek-r1",
        ),
    ),
    ProviderPreset(
        name="openai",
        label="OpenAI",
        env_var="OPENAI_API_KEY",
        base_url="https://api.openai.com/v1",
        requires_key=True,
        strong_default="gpt-4.1",
        fast_default="gpt-4.1-mini",
        models=("gpt-4.1", "gpt-4.1-mini", "gpt-4o", "o3-mini", "o4-mini"),
    ),
    ProviderPreset(
        name="anthropic",
        label="Anthropic (Claude)",
        env_var="ANTHROPIC_API_KEY",
        base_url="https://api.anthropic.com/v1/",
        requires_key=True,
        strong_default="claude-sonnet-4-20250514",
        fast_default="claude-3-5-haiku-latest",
        models=("claude-sonnet-4-20250514", "claude-opus-4-20250514", "claude-3-5-haiku-latest"),
    ),
    ProviderPreset(
        name="google",
        label="Google Gemini",
        env_var="GEMINI_API_KEY",
        base_url=None,
        requires_key=True,
        strong_default="gemini-2.5-pro",
        fast_default="gemini-2.5-flash",
        models=("gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"),
    ),
    ProviderPreset(
        name="openrouter",
        label="OpenRouter",
        env_var="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
        requires_key=True,
        strong_default="anthropic/claude-sonnet-4",
        fast_default="google/gemini-2.5-flash",
        models=(
            "anthropic/claude-sonnet-4",
            "openai/gpt-4.1",
            "google/gemini-2.5-pro",
            "google/gemini-2.5-flash",
            "deepseek/deepseek-r1",
        ),
    ),
    ProviderPreset(
        name="groq",
        label="Groq",
        env_var="GROQ_API_KEY",
        base_url="https://api.groq.com/openai/v1",
        requires_key=True,
        strong_default="llama-3.3-70b-versatile",
        fast_default="llama-3.1-8b-instant",
        models=("llama-3.3-70b-versatile", "llama-3.1-8b-instant", "qwen-qwq-32b"),
    ),
    ProviderPreset(
        name="deepseek",
        label="DeepSeek",
        env_var="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com/v1",
        requires_key=True,
        strong_default="deepseek-reasoner",
        fast_default="deepseek-chat",
        models=("deepseek-reasoner", "deepseek-chat"),
    ),
    ProviderPreset(
        name="ollama",
        label="Ollama (Local)",
        env_var="OLLAMA_API_KEY",
        base_url="http://localhost:11434/v1",
        requires_key=False,
        strong_default="qwen2.5:14b",
        fast_default="llama3.1:8b",
        models=("qwen2.5:14b", "llama3.1:8b", "deepseek-r1:14b"),
    ),
    ProviderPreset(
        name="nine_router",
        label="Custom / 9router",
        env_var="NINE_ROUTER_API_KEY",
        base_url="http://127.0.0.1:4000/v1",
        requires_key=True,
        strong_default="default-strong",
        fast_default="default-fast",
        models=(),
    ),
)

CATALOG_BY_NAME: dict[str, ProviderPreset] = {p.name: p for p in PROVIDER_CATALOG}


def secrets_path(storage_dir: Path) -> Path:
    return Path(storage_dir) / "provider_keys.json"


def load_saved_keys(storage_dir: Path) -> dict[str, str]:
    p = secrets_path(storage_dir)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in raw.items() if v} if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _write_saved_keys(storage_dir: Path, keys: dict[str, str]) -> None:
    p = secrets_path(storage_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(keys, indent=2), encoding="utf-8")


def mask_key(key: str | None) -> str:
    if not key:
        return ""
    if len(key) <= 6:
        return "••••"
    return f"••••{key[-4:]}"


def _env_var_for(provider_name: str, cfg: ModelsConfig | None = None) -> str:
    if cfg and provider_name in cfg.providers and cfg.providers[provider_name].env_var:
        return cfg.providers[provider_name].env_var
    if provider_name in CATALOG_BY_NAME:
        return CATALOG_BY_NAME[provider_name].env_var
    return f"{provider_name.upper()}_API_KEY"


def apply_saved_keys_to_env(storage_dir: Path, models_path: Path | None = None) -> None:
    cfg = load_models(models_path)
    saved = load_saved_keys(storage_dir)
    for prov, val in saved.items():
        env_var = _env_var_for(prov, cfg)
        if val and not os.environ.get(env_var):
            os.environ[env_var] = val


def _load_raw_models_yaml(models_path: Path | None = None) -> dict:
    p = models_path or MODELS_PATH
    with open(p, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _save_raw_models_yaml(raw: dict, models_path: Path | None = None) -> None:
    p = models_path or MODELS_PATH
    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(raw, fh, sort_keys=False, allow_unicode=True)


def _ensure_provider_in_raw(raw: dict, provider_name: str, base_url: str | None = None) -> None:
    providers = raw.setdefault("providers", {})
    if is_cli_provider(provider_name):
        providers.setdefault(provider_name, {"mode": "cli"})
        return
    preset = CATALOG_BY_NAME.get(provider_name)
    entry = dict(providers.get(provider_name) or {})
    if preset:
        entry.setdefault("env_var", preset.env_var)
        if base_url is not None:
            if base_url.strip():
                entry["base_url"] = base_url.strip()
            else:
                entry.pop("base_url", None)
        elif "base_url" not in entry and preset.base_url:
            entry["base_url"] = preset.base_url
    else:
        entry.setdefault("env_var", f"{provider_name.upper()}_API_KEY")
        if base_url and base_url.strip():
            entry["base_url"] = base_url.strip()
    providers[provider_name] = entry


def save_provider_key(
    storage_dir: Path,
    provider_name: str,
    api_key: str,
    *,
    base_url: str | None = None,
    models_path: Path | None = None,
) -> None:
    raw = _load_raw_models_yaml(models_path)
    _ensure_provider_in_raw(raw, provider_name, base_url=base_url)
    _save_raw_models_yaml(raw, models_path)

    cfg = load_models(models_path)
    env_var = _env_var_for(provider_name, cfg)
    cleaned = api_key.strip()
    if cleaned:
        keys = load_saved_keys(storage_dir)
        keys[provider_name] = cleaned
        _write_saved_keys(storage_dir, keys)
        os.environ[env_var] = cleaned


def delete_provider_key(
    storage_dir: Path,
    provider_name: str,
    *,
    models_path: Path | None = None,
) -> None:
    cfg = load_models(models_path)
    env_var = _env_var_for(provider_name, cfg)
    keys = load_saved_keys(storage_dir)
    if provider_name in keys:
        del keys[provider_name]
        _write_saved_keys(storage_dir, keys)
    os.environ.pop(env_var, None)


def update_model_roles(
    strong_provider: str,
    strong_name: str,
    fast_provider: str,
    fast_name: str,
    *,
    fallback_provider: str | None = None,
    fallback_name: str | None = None,
    fast_fallback_provider: str | None = None,
    fast_fallback_name: str | None = None,
    clear_fast_fallback: bool = False,
    models_path: Path | None = None,
) -> None:
    raw = _load_raw_models_yaml(models_path)
    _ensure_provider_in_raw(raw, strong_provider)
    _ensure_provider_in_raw(raw, fast_provider)
    raw.setdefault("roles", {})
    raw["roles"]["strong"] = {"provider": strong_provider, "name": strong_name.strip()}
    raw["roles"]["fast"] = {"provider": fast_provider, "name": fast_name.strip()}

    raw.setdefault("fallbacks", {})
    if fallback_provider and fallback_name and fallback_name.strip():
        _ensure_provider_in_raw(raw, fallback_provider)
        raw["fallbacks"]["strong"] = [
            {"provider": fallback_provider, "name": fallback_name.strip()}
        ]
    else:
        raw["fallbacks"].pop("strong", None)

    if fast_fallback_provider and fast_fallback_name and fast_fallback_name.strip():
        _ensure_provider_in_raw(raw, fast_fallback_provider)
        raw["fallbacks"]["fast"] = [
            {"provider": fast_fallback_provider, "name": fast_fallback_name.strip()}
        ]
    elif clear_fast_fallback:
        raw["fallbacks"].pop("fast", None)
    _save_raw_models_yaml(raw, models_path)


def opencode_auth_path() -> Path:
    return Path.home() / ".local" / "share" / "opencode" / "auth.json"


def import_opencode_keys(
    storage_dir: Path,
    *,
    auth_path: Path | None = None,
    models_path: Path | None = None,
) -> list[str]:
    src = auth_path or opencode_auth_path()
    if not src.exists():
        return []
    raw = json.loads(src.read_text(encoding="utf-8"))
    imported: list[str] = []
    if not isinstance(raw, dict):
        return imported
    for name, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        key = payload.get("key") or payload.get("apiKey") or payload.get("api_key")
        if key and isinstance(key, str):
            save_provider_key(storage_dir, str(name), key, models_path=models_path)
            imported.append(str(name))
    return imported


def provider_status_rows(storage_dir: Path, models_path: Path | None = None) -> list[dict]:
    cfg = load_models(models_path)
    saved = load_saved_keys(storage_dir)
    active_providers = {ref.provider for ref in cfg.roles.values() if ref.provider}
    for refs in cfg.fallbacks.values():
        active_providers.update(ref.provider for ref in refs if ref.provider)

    rows: list[dict] = []
    seen: set[str] = set()

    ordered_names = [p.name for p in PROVIDER_CATALOG] + [
        k for k in cfg.providers if k not in CATALOG_BY_NAME and not is_cli_provider(k)
    ]
    for name in ordered_names:
        if name in seen or is_cli_provider(name):
            continue
        seen.add(name)
        preset = CATALOG_BY_NAME.get(name)
        pcfg = cfg.providers.get(name)
        env_var = (pcfg.env_var if pcfg and pcfg.env_var else (preset.env_var if preset else f"{name.upper()}_API_KEY"))
        base_url = (pcfg.base_url if pcfg and pcfg.base_url else (preset.base_url if preset else None))
        env_val = os.environ.get(env_var) or (os.environ.get("GOOGLE_API_KEY") if name == "google" else None)
        saved_val = saved.get(name)
        effective = saved_val or env_val
        requires_key = preset.requires_key if preset else True
        source = "salva" if saved_val else ("env" if env_val else ("local" if not requires_key else "nenhuma"))
        added = bool(saved_val or env_val or name in active_providers)
        rows.append({
            "name": name,
            "label": preset.label if preset else name,
            "env_var": env_var,
            "base_url": base_url or "",
            "requires_key": requires_key,
            "connected": bool(effective) or not requires_key,
            "added": added,
            "masked": mask_key(effective),
            "source": source,
            "strong_default": preset.strong_default if preset else "",
            "fast_default": preset.fast_default if preset else "",
            "models": list(preset.models) if preset else [],
        })
    return rows


def fetch_provider_models(
    provider_name: str,
    storage_dir: Path,
    *,
    models_path: Path | None = None,
    timeout_s: float = 5.0,
) -> tuple[list[str], str]:
    """Return (available_models, source_label) for an API or CLI provider."""
    from .cli_providers import CLI_CATALOG_BY_NAME

    if is_cli_provider(provider_name):
        spec = CLI_CATALOG_BY_NAME[provider_name]
        return list(spec.models), "cli"

    cfg = load_models(models_path)
    saved = load_saved_keys(storage_dir)
    preset = CATALOG_BY_NAME.get(provider_name)
    pcfg = cfg.providers.get(provider_name)
    env_var = _env_var_for(provider_name, cfg)
    key = saved.get(provider_name) or os.environ.get(env_var)
    if not key and provider_name == "google":
        key = os.environ.get("GOOGLE_API_KEY")
    fallback_list = list(preset.models) if preset else []

    requires_key = preset.requires_key if preset else True
    if requires_key and not key:
        return fallback_list, "presets"

    base_url = (pcfg.base_url if pcfg and pcfg.base_url else (preset.base_url if preset else None))
    try:
        with httpx.Client(timeout=timeout_s) as client:
            if provider_name == "google":
                r = client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params={"key": key},
                )
                if r.status_code == 200:
                    names = [
                        str(m.get("name", "")).removeprefix("models/")
                        for m in (r.json().get("models") or [])
                        if "generateContent" in (m.get("supportedGenerationMethods") or ["generateContent"])
                    ]
                    names = [n for n in names if n]
                    return (names or fallback_list), "live"
                return fallback_list, f"presets (HTTP {r.status_code})"

            if provider_name == "anthropic":
                url = (base_url or "https://api.anthropic.com/v1/").rstrip("/") + "/models"
                r = client.get(
                    url,
                    headers={"x-api-key": key or "", "anthropic-version": "2023-06-01"},
                )
            else:
                if not base_url:
                    return fallback_list, "presets"
                url = base_url.rstrip("/") + "/models"
                headers = {"Authorization": f"Bearer {key}"} if key else {}
                r = client.get(url, headers=headers)

        if r.status_code == 200:
            items = r.json().get("data") or []
            ids = sorted({str(m.get("id")) for m in items if isinstance(m, dict) and m.get("id")})
            return (ids or fallback_list), "live"
        return fallback_list, f"presets (HTTP {r.status_code})"
    except Exception:
        return fallback_list, "presets (offline)"


def test_provider_connection(
    provider_name: str,
    storage_dir: Path,
    *,
    models_path: Path | None = None,
    timeout_s: float = 8.0,
) -> tuple[bool, str]:
    cfg = load_models(models_path)
    saved = load_saved_keys(storage_dir)
    preset = CATALOG_BY_NAME.get(provider_name)
    pcfg = cfg.providers.get(provider_name)
    env_var = _env_var_for(provider_name, cfg)
    key = saved.get(provider_name) or os.environ.get(env_var)
    if not key and provider_name == "google":
        key = os.environ.get("GOOGLE_API_KEY")

    requires_key = preset.requires_key if preset else True
    if requires_key and not key:
        return False, f"chave ausente ({env_var})"

    base_url = (pcfg.base_url if pcfg and pcfg.base_url else (preset.base_url if preset else None))
    try:
        with httpx.Client(timeout=timeout_s) as client:
            if provider_name == "google":
                r = client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params={"key": key},
                )
            elif provider_name == "anthropic":
                url = (base_url or "https://api.anthropic.com/v1/").rstrip("/") + "/models"
                r = client.get(
                    url,
                    headers={"x-api-key": key or "", "anthropic-version": "2023-06-01"},
                )
            else:
                if not base_url:
                    return False, "base_url não configurada"
                url = base_url.rstrip("/") + "/models"
                headers = {"Authorization": f"Bearer {key}"} if key else {}
                r = client.get(url, headers=headers)
        if r.status_code == 200:
            data = r.json()
            items = data.get("data") or data.get("models") or []
            return True, f"conectado ({len(items)} modelos)"
        return False, f"HTTP {r.status_code}"
    except Exception as exc:
        return False, f"falha: {type(exc).__name__}"
