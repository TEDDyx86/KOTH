import shutil
import subprocess
import time
from pathlib import Path
from typing import Literal

import yaml

from ..config import BASE_DIR
from .config import DEFAULT_PATH, load_tools_config

_DOCKER_CACHE: dict[str, tuple[float, dict]] = {}
_DOCKER_CACHE_TTL_S = 15.0


def docker_status_cached(image: str, *, ttl_s: float = _DOCKER_CACHE_TTL_S) -> dict:
    """Cached wrapper around docker_status to avoid spawning `docker info` per tool call."""
    now = time.monotonic()
    cached = _DOCKER_CACHE.get(image)
    if cached is not None and (now - cached[0]) < ttl_s:
        return cached[1]
    st = docker_status(image)
    _DOCKER_CACHE[image] = (now, st)
    return st


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
    mode: Literal["native", "docker", "auto"],
    *,
    docker_image: str | None = None,
    persistent_docker: bool | None = None,
    tools_path: Path | None = None,
) -> None:
    """Update all allowlisted tools in config/tools.yaml to native, docker, or auto mode."""
    p = tools_path or DEFAULT_PATH
    with open(p, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    defaults = raw.setdefault("defaults", {})
    if docker_image and docker_image.strip():
        defaults["docker_image"] = docker_image.strip()
    if persistent_docker is not None:
        defaults["persistent_docker"] = bool(persistent_docker)

    tools = raw.setdefault("tools", {})
    for name, spec in list(tools.items()):
        entry = dict(spec or {})
        entry["mode"] = mode
        tools[name] = entry

    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(raw, fh, sort_keys=False, allow_unicode=True)
    _DOCKER_CACHE.clear()


def sandbox_container_name(event_id: int | None, challenge_id: int | None) -> str:
    ev = str(event_id if event_id is not None else 0)
    ch = str(challenge_id if challenge_id is not None else 0)
    return f"koth-sb-{ev}-{ch}"


def ensure_persistent_container(
    container_name: str,
    image: str,
    mount_dir: Path,
    *,
    has_target: bool = True,
) -> bool:
    """Start a long-lived sandbox container if not already running."""
    exe = shutil.which("docker")
    if exe is None:
        return False
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        insp = subprocess.run(
            [exe, "inspect", "-f", "{{.State.Running}}", container_name],
            capture_output=True, text=True, timeout=5, creationflags=creationflags,
        )
        if insp.returncode == 0 and insp.stdout.strip().lower() == "true":
            return True
        if insp.returncode == 0:
            subprocess.run(
                [exe, "rm", "-f", container_name],
                capture_output=True, text=True, timeout=5, creationflags=creationflags,
            )
    except Exception:
        pass

    net_args = ["--network", "none"] if not has_target else ["--add-host=host.docker.internal:host-gateway"]
    cmd = [
        exe, "run", "-d", "--rm", "--name", container_name,
        "--memory", "1g", "--cpus", "2", "--pids-limit", "256",
        *net_args,
        "-v", f"{mount_dir.resolve()}:/work",
        "-w", "/work",
        image, "sleep", "infinity",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, creationflags=creationflags)
        return proc.returncode == 0
    except Exception:
        return False


def stop_persistent_container(container_name: str) -> bool:
    exe = shutil.which("docker")
    if exe is None:
        return False
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [exe, "rm", "-f", container_name],
            capture_output=True, text=True, timeout=10, creationflags=creationflags,
        )
        return proc.returncode == 0
    except Exception:
        return False


def list_persistent_containers() -> list[str]:
    exe = shutil.which("docker")
    if exe is None:
        return []
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [exe, "ps", "-a", "--filter", "name=koth-sb-", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=5, creationflags=creationflags,
        )
        if proc.returncode != 0:
            return []
        return [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    except Exception:
        return []


def reset_persistent_containers() -> int:
    stopped = 0
    for cname in list_persistent_containers():
        if stop_persistent_container(cname):
            stopped += 1
    return stopped


def current_sandbox_mode(tools_path: Path | None = None) -> str:
    cfg = load_tools_config(tools_path)
    modes = {spec.mode for spec in cfg.tools.values()}
    if modes == {"docker"}:
        return "docker"
    if modes == {"auto"}:
        return "auto"
    if "docker" in modes or "auto" in modes:
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
