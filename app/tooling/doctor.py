import shutil
import subprocess

from .config import ToolSettings
from .sandbox import docker_status


def _docker_which_batch(image: str, names: list[str], *, timeout_s: float = 10.0) -> dict[str, str | None]:
    """Check which tools exist inside `image` in a single docker container run."""
    if not names:
        return {}
    exe = shutil.which("docker")
    if exe is None:
        return {n: None for n in names}

    st = docker_status(image)
    if not (st.get("daemon_running") and st.get("image_present")):
        return {n: None for n in names}

    # Safe literal command names only
    safe_names = [n for n in names if n.replace("-", "").replace("_", "").isalnum()]
    script = "; ".join(
        f'p=$(command -v "{n}" 2>/dev/null || true); echo "{n}=$p"'
        for n in safe_names
    )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [exe, "run", "--rm", "--network", "none", "--entrypoint", "sh", image, "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            creationflags=creationflags,
        )
    except Exception:
        return {n: None for n in names}

    if proc.returncode != 0:
        return {n: None for n in names}

    found: dict[str, str | None] = {n: None for n in names}
    for line in (proc.stdout or "").splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        val = v.strip()
        if k in found and val:
            found[k] = f"docker:{image}:{val}"
    return found


def inventory(settings: ToolSettings) -> list[dict]:
    docker_groups: dict[str, list[str]] = {}
    auto_groups: dict[str, list[str]] = {}
    for name, spec in settings.tools.items():
        if spec.mode == "docker":
            img = spec.image or settings.defaults.docker_image
            docker_groups.setdefault(img, []).append(name)
        elif spec.mode == "auto":
            img = spec.image or settings.defaults.docker_image
            auto_groups.setdefault(img, []).append(name)

    docker_paths: dict[str, str | None] = {}
    for img, names in docker_groups.items():
        docker_paths.update(_docker_which_batch(img, names))

    auto_docker_ready: dict[str, bool] = {}
    for img, names in auto_groups.items():
        st = docker_status(img)
        ready = bool(st.get("daemon_running") and st.get("image_present"))
        auto_docker_ready[img] = ready
        if ready:
            docker_paths.update(_docker_which_batch(img, names))

    rows = []
    for name in sorted(settings.tools):
        spec = settings.tools[name]
        effective_mode = spec.mode
        if spec.mode == "docker":
            path = docker_paths.get(name)
        elif spec.mode == "auto":
            img = spec.image or settings.defaults.docker_image
            if auto_docker_ready.get(img):
                path = docker_paths.get(name) or shutil.which(name)
                effective_mode = "auto→docker" if docker_paths.get(name) else "auto→native"
            else:
                path = shutil.which(name)
                effective_mode = "auto→native"
        else:
            path = shutil.which(name)
        rows.append({
            "name": name,
            "mode": effective_mode,
            "configured_mode": spec.mode,
            "path": path,
            "present": path is not None,
        })
    return rows
