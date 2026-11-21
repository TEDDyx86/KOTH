import shutil
import subprocess
from pathlib import Path
from typing import Literal

import yaml

from ..config import BASE_DIR
from .config import DEFAULT_PATH, load_tools_config


def docker_status(image: str, *, timeout_s: float = 4.0) -> dict:
    """Inspect whether Docker CLI, daemon, and sandbox image are available."""
    exe = shutil.which("docker")
    if exe is None:
        return {
            "cli_present": False,
            "daemon_running": False,
            "image_present": False,
            "image": image,
            "message": "docker não encontrado no PATH",
        }

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        info_proc = subprocess.run(
            [exe, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            creationflags=creationflags,
        )
    except Exception as exc:
        return {
            "cli_present": True,
            "daemon_running": False,
            "image_present": False,
            "image": image,
            "message": f"erro ao consultar daemon Docker ({type(exc).__name__})",
        }

    if info_proc.returncode != 0:
        return {
            "cli_present": True,
            "daemon_running": False,
            "image_present": False,
            "image": image,
            "message": "Docker daemon parado — inicie o Docker Desktop/service",
        }

    img_proc = subprocess.run(
        [exe, "image", "inspect", image],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        creationflags=creationflags,
    )
    image_present = img_proc.returncode == 0
    msg = (
        f"Docker ativo · imagem '{image}' pronta"
        if image_present
        else f"Docker ativo · imagem '{image}' ainda não construída"
    )
    return {
        "cli_present": True,
        "daemon_running": True,
        "image_present": image_present,
        "image": image,
        "message": msg,
    }


def switch_tools_mode(
    mode: Literal["native", "docker"],
    *,
    docker_image: str | None = None,
    tools_path: Path | None = None,
) -> None:
    """Update all allowlisted tools in config/tools.yaml to native or docker mode."""
    p = tools_path or DEFAULT_PATH
    with open(p, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    defaults = raw.setdefault("defaults", {})
    if docker_image and docker_image.strip():
        defaults["docker_image"] = docker_image.strip()

    tools = raw.setdefault("tools", {})
    for name, spec in list(tools.items()):
        entry = dict(spec or {})
        entry["mode"] = mode
        tools[name] = entry

    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(raw, fh, sort_keys=False, allow_unicode=True)


def current_sandbox_mode(tools_path: Path | None = None) -> str:
    cfg = load_tools_config(tools_path)
    modes = {spec.mode for spec in cfg.tools.values()}
    if modes == {"docker"}:
        return "docker"
    if "docker" in modes:
        return "mixed"
    return "native"


def build_sandbox_image(
    tag: str = "koth-sandbox:latest",
    *,
    project_dir: Path | None = None,
    timeout_s: int = 600,
) -> tuple[bool, str]:
    exe = shutil.which("docker")
    if exe is None:
        return False, "docker não encontrado no PATH"
    root = project_dir or BASE_DIR
    dockerfile = root / "Dockerfile.sandbox"
    if not dockerfile.exists():
        return False, "Dockerfile.sandbox não encontrado"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [exe, "build", "-t", tag, "-f", str(dockerfile), str(root)],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            creationflags=creationflags,
        )
    except Exception as exc:
        return False, f"falha no build: {type(exc).__name__}"
    if proc.returncode == 0:
        return True, f"imagem '{tag}' construída com sucesso"
    err = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip().splitlines()
    return False, (err[-1][:140] if err else "erro no docker build")
