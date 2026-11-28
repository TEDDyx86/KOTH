from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..config import BASE_DIR

MODELS_PATH = BASE_DIR / "config" / "models.yaml"
AGENTS_PATH = BASE_DIR / "config" / "agents.yaml"


@dataclass
class ModelRef:
    provider: str
    name: str


@dataclass
class ProviderCfg:
    name: str
    base_url: str | None = None
    env_var: str = ""


@dataclass
class ModelsConfig:
    providers: dict[str, ProviderCfg] = field(default_factory=dict)
    roles: dict[str, ModelRef] = field(default_factory=dict)
    fallbacks: dict[str, list[ModelRef]] = field(default_factory=dict)
    active_profile: str | None = None


@dataclass
class AgentCfg:
    persona: str
    model: str
    tools: list[str] = field(default_factory=list)


@dataclass
class AgentsConfig:
    roles: dict[str, AgentCfg] = field(default_factory=dict)
    limits: dict[str, object] = field(default_factory=dict)


def _model_ref(raw) -> ModelRef:
    if isinstance(raw, str):
        # shorthand: full model id (e.g. "nvidia/nemotron-..."); provider is
        # derived from the id prefix at resolution time
        return ModelRef(provider="", name=raw)
    return ModelRef(provider=str(raw.get("provider", "")), name=str(raw.get("name", "")))


def load_models(path: Path | None = None) -> ModelsConfig:
    cfg_path = path or MODELS_PATH
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    cfg = ModelsConfig()
    for name, p in (raw.get("providers") or {}).items():
        cfg.providers[str(name)] = ProviderCfg(name=str(name),
                                               base_url=(p or {}).get("base_url"),
                                               env_var=str((p or {}).get("env_var", "")))
    for role, m in (raw.get("roles") or {}).items():
        cfg.roles[str(role)] = _model_ref(m or {})
    for role, refs in (raw.get("fallbacks") or {}).items():
        cfg.fallbacks[str(role)] = [_model_ref(r) for r in refs or []]
    raw_prof = raw.get("active_profile")
    cfg.active_profile = (str(raw_prof).strip() or None) if raw_prof else None
    return cfg


def load_agents(path: Path | None = None) -> AgentsConfig:
    cfg_path = path or AGENTS_PATH
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    cfg = AgentsConfig()
    for role, a in (raw.get("roles") or {}).items():
        cfg.roles[str(role)] = AgentCfg(persona=str((a or {}).get("persona", "")),
                                        model=str((a or {}).get("model", "fast")),
                                        tools=list((a or {}).get("tools") or []))
    cfg.limits = dict(raw.get("limits") or {})
    return cfg
