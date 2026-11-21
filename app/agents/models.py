import os

from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from .cli_providers import is_cli_provider, make_cli_model
from .config import ModelRef, ModelsConfig


class ProviderUnavailable(Exception):
    ...


def _api_key(ref: ModelRef, cfg: ModelsConfig) -> tuple[str, str | None]:
    """Return (provider_name, api_key). Raises ProviderUnavailable if not set."""
    provider_name = ref.provider or ref.name.split("/", 1)[0]
    provider = cfg.providers.get(provider_name)
    env_var = provider.env_var if provider and provider.env_var else f"{provider_name.upper()}_API_KEY"
    key = os.environ.get(env_var)
    if not key and provider_name == "google":
        key = os.environ.get("GOOGLE_API_KEY")
    if not key and provider_name == "ollama":
        return provider_name, "ollama"
    if not key:
        raise ProviderUnavailable(
            f"provider '{provider_name}' unavailable: env '{env_var}' not set")
    return provider_name, key


def resolve_model(ref: ModelRef, cfg: ModelsConfig) -> Model:
    raw_provider = ref.provider or ref.name.split("/", 1)[0]
    if is_cli_provider(raw_provider):
        return make_cli_model(raw_provider, ref.name)
    provider_name, key = _api_key(ref, cfg)
    provider_cfg = cfg.providers.get(provider_name)
    if provider_name == "google":
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider

        return GoogleModel(ref.name, provider=GoogleProvider(api_key=key))
    if provider_name == "anthropic":
        try:
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider

            return AnthropicModel(ref.name, provider=AnthropicProvider(api_key=key))
        except ImportError:
            # Fallback to Anthropic's OpenAI-compatible endpoint when anthropic SDK isn't installed
            pass
    base_url = provider_cfg.base_url if provider_cfg else None
    if not base_url and provider_name == "anthropic":
        base_url = "https://api.anthropic.com/v1/"
    elif not base_url and provider_name == "ollama":
        base_url = "http://localhost:11434/v1"
    provider = OpenAIProvider(base_url=base_url, api_key=key)
    return OpenAIChatModel(ref.name, provider=provider)


def resolve_candidates_for_role(role: str, agents_cfg_role: str | None, models_cfg: ModelsConfig) -> list[Model]:
    """Resolve all available models for a role in priority order (primary + fallbacks)."""
    key = agents_cfg_role or role
    candidates: list[ModelRef] = [models_cfg.roles[key]] if key in models_cfg.roles else []
    candidates += models_cfg.fallbacks.get(key, [])
    resolved: list[Model] = []
    last_error: Exception | None = None
    for ref in candidates:
        try:
            resolved.append(resolve_model(ref, models_cfg))
        except ProviderUnavailable as exc:
            last_error = exc
    if not resolved:
        raise last_error or ProviderUnavailable(f"no model configured for role '{key}'")
    return resolved


def resolve_for_role(role: str, agents_cfg_role: str | None, models_cfg: ModelsConfig) -> Model:
    """Resolve the model for an agent role. `agents_cfg_role` is the model-role key
    ('strong'|'fast'); tries fallbacks in order, first available wins."""
    return resolve_candidates_for_role(role, agents_cfg_role, models_cfg)[0]


_TRANSIENT_HINTS = ("503", "502", "429", "service unavailable", "rate limit", "temporarily unavailable", "timeout", "connection")


def is_transient_provider_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if any(k in name for k in ("timeout", "connection", "serviceunavailable", "ratelimit", "httpstatus")):
        return True
    return any(hint in msg for hint in _TRANSIENT_HINTS)


def run_with_fallback(models: list[Model], fn):
    """Execute `fn(model)` trying fallback models when a transient provider error occurs."""
    last_exc: Exception | None = None
    for idx, model in enumerate(models):
        try:
            return fn(model)
        except Exception as exc:
            last_exc = exc
            has_next = idx + 1 < len(models)
            if not has_next or not is_transient_provider_error(exc):
                raise
    assert last_exc is not None
    raise last_exc
