from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..config import BASE_DIR

DEFAULT_PATH = BASE_DIR / "config" / "tools.yaml"


@dataclass
class ToolSpec:
    mode: str = "native"
    guard: str = "strict"  # strict = host/metachar guards; none = free args (code runners)
    image: str | None = None


@dataclass
class Defaults:
    timeout_s: int = 300
    output_limit_mb: int = 10
    docker_image: str = "kalilinux/kali-rolling"
    persistent_docker: bool = False


@dataclass
class ToolSettings:
    tools: dict[str, ToolSpec] = field(default_factory=dict)
    defaults: Defaults = field(default_factory=Defaults)


def load_tools_config(path: Path | None = None) -> ToolSettings:
    cfg_path = path or DEFAULT_PATH
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    settings = ToolSettings()
    for name, spec in (raw.get("tools") or {}).items():
        s = spec or {}
        settings.tools[str(name)] = ToolSpec(
            mode=s.get("mode", "native"),
            guard=s.get("guard", "strict"),
            image=s.get("image"),
        )
    d = raw.get("defaults") or {}
    settings.defaults = Defaults(
        timeout_s=int(d.get("timeout_s", 300)),
        output_limit_mb=int(d.get("output_limit_mb", 10)),
        docker_image=str(d.get("docker_image", "kalilinux/kali-rolling")),
        persistent_docker=bool(d.get("persistent_docker", False)),
    )
    return settings
