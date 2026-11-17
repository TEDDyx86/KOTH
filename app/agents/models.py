import os

from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from .config import ModelRef, ModelsConfig


class ProviderUnavailable(Exception):
    ...


def _api_key(ref: ModelRef, cfg: ModelsConfig) -> tuple[str, str | None]:
    """Return (provider_name, api_key). Raises ProviderUnavailable if not set."""
    provider_name = ref.provider or ref.name.split("/", 1)[0]
    provider = cfg.providers.get(provider_name)
    env_var = provider.env_var if provider else f"{provider_name.upper()}_API_KEY"
    key = os.environ.get(env_var)
    if not key:
        raise ProviderUnavailable(
            f"provider '{provider_name}' unavailable: env '{env_var}' not set")
    return provider_name, key


def resolve_model(ref: ModelRef, cfg: ModelsConfig) -> Model:
    provider_name, key = _api_key(ref, cfg)
    provider_cfg = cfg.providers.get(provider_name)
    if provider_name == "google":
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider

        return GoogleModel(ref.name, provider=GoogleProvider(api_key=key))
    base_url = provider_cfg.base_url if provider_cfg else None
    provider = OpenAIProvider(base_url=base_url, api_key=key)
    return OpenAIChatModel(ref.name, provider=provider)


def resolve_for_role(role: str, agents_cfg_role: str | None, models_cfg: ModelsConfig) -> Model:
    """Resolve the model for an agent role. `agents_cfg_role` is the model-role key
    ('strong'|'fast'); tries fallbacks in order, first available wins."""
    key = agents_cfg_role or role
    candidates: list[ModelRef] = [models_cfg.roles[key]] if key in models_cfg.roles else []
    candidates += models_cfg.fallbacks.get(key, [])
    last_error: Exception | None = None
    for ref in candidates:
        try:
            return resolve_model(ref, models_cfg)
        except ProviderUnavailable as exc:
            last_error = exc
    raise last_error or ProviderUnavailable(f"no model configured for role '{key}'")
