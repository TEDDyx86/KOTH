import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from sqlmodel import select

from ..models import Attachment, AttachmentType, AttemptResult, AttemptType
from .config import ToolSettings
from .sandbox import docker_status_cached, ensure_persistent_container, sandbox_container_name

METACHARS = set(";|&$`\n\r")
CACHEABLE_READONLY_TOOLS = frozenset({
    "strings", "file", "readelf", "objdump", "checksec",
    "exiftool", "binwalk", "tshark", "nmap", "jq", "whatweb", "ROPgadget",
})


class ToolNotAllowed(Exception):
    ...


class UnsafeArg(Exception):
    ...


class TargetNotAllowed(Exception):
    ...


_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_DOMAIN = re.compile(r"\b[a-z0-9][a-z0-9.\-]+\.[a-z]{2,}\b", re.IGNORECASE)
_ALWAYS_OK = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}
_URL_SCHEME = re.compile(r"^[a-z][a-z0-9+\-.]*://", re.IGNORECASE)
_FILE_SUFFIXES = {
    ".txt", ".bin", ".elf", ".exe", ".dll", ".so", ".o", ".a", ".out", ".raw",
    ".dylib", ".macho", ".ipa", ".plist", ".dex", ".deb", ".rpm", ".dmg", ".pkg",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".docx", ".xlsx", ".pptx", ".ttf", ".otf", ".ovpn",
    ".pcap", ".pcapng", ".cap", ".sql", ".sqlite", ".db", ".csv", ".log",
    ".py", ".c", ".cpp", ".h", ".js", ".ts", ".json", ".yaml", ".yml", ".xml",
    ".key", ".pem", ".pub", ".crt", ".csr", ".der", ".enc", ".img", ".iso",
    ".dmp", ".core", ".wasm", ".apk", ".jar", ".class", ".pyc", ".asm", ".md",
    ".wav", ".mp3", ".mp4", ".sh",
}


@dataclass
class ToolRunResult:
    tool: str
    argv: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    duration_s: float
    truncated: bool
    attachment_id: int | None = None
    attempt_id: int | None = None
    cache_hit: bool = False
    detected_flags: list[str] = field(default_factory=list)


def _extract_host(raw: str | None) -> str:
    txt = (raw or "").strip()
    if not txt:
        return ""
    host = txt.split("://")[-1].split("/")[0].split(":")[0].lower()
    return host


def _scan_output_flags(session, challenge, text: str) -> list[str]:
    if not text:
        return []
    from ..agents.context import effective_flag_format
    from ..agents.submission import is_placeholder_flag

    pattern = effective_flag_format(session, challenge)
    try:
        rx = re.compile(pattern)
    except re.error:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for m in rx.finditer(text):
        cand = m.group(0).strip()
        if not cand or cand in seen or is_placeholder_flag(cand):
            continue
        seen.add(cand)
        found.append(cand)
        if len(found) >= 5:
            break
    return found


class ToolRunner:
    def __init__(self, settings: ToolSettings, storage) -> None:
        self.settings = settings
        self.storage = storage
        self._cmd_cache: dict[tuple, tuple[tuple, ToolRunResult]] = {}
        self._approved_targets: dict[int, set[str]] = {}

    def approve_target(self, challenge_id: int | None, target: str) -> str:
        host = _extract_host(target)
        if not host or challenge_id is None:
            return host
        self._approved_targets.setdefault(int(challenge_id), set()).add(host)
        return host

    def approved_hosts_for(self, challenge_id: int | None) -> set[str]:
        if challenge_id is None:
            return set()
        return set(self._approved_targets.get(int(challenge_id), set()))

    def _effective_mode(self, tool: str) -> str:
        spec = self.settings.tools[tool]
        if spec.mode != "auto":
            return spec.mode
        if shutil.which("docker") is not None:
            image = spec.image or self.settings.defaults.docker_image
            st = docker_status_cached(image)
            if st.get("daemon_running") and st.get("image_present"):
                return "docker"
        return "native"

    def available_tools(self) -> list[str]:
        """Return allowlisted tool names actually runnable in the active runtime."""
        ready: list[str] = []
        for name in sorted(self.settings.tools):
            try:
                self.resolve_executable(name)
                ready.append(name)
            except ToolNotAllowed:
                continue
        return ready

    def runtime_summary(self) -> str:
        modes = {self._effective_mode(t) for t in self.settings.tools}
        if modes == {"docker"}:
            return f"docker ({self.settings.defaults.docker_image})"
        if "docker" in modes:
            return f"hybrid docker+native ({self.settings.defaults.docker_image})"
        return "native (host OS — only locally installed tools and host Python packages are available)"

    def resolve_executable(self, tool: str) -> str:
        if tool not in self.settings.tools:
            raise ToolNotAllowed(f"tool '{tool}' not in allowlist")
        mode = self._effective_mode(tool)
        if mode == "docker":
            path = shutil.which("docker")
            if path is None:
                raise ToolNotAllowed("docker not found on PATH for docker-mode tool")
            return path
        if mode == "wsl":
            path = shutil.which("wsl")
            if path is None:
                raise ToolNotAllowed("wsl not found on PATH for wsl-mode tool")
            return path
        path = shutil.which(tool)
        if path is None:
            raise ToolNotAllowed(f"tool '{tool}' not found on PATH")
        return path

    def sandbox_workdir(self, challenge) -> Path:
        workdir = self.storage.storage_dir / "sandbox" / str(challenge.event_id) / str(challenge.id)
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir

    def stage_downloads(self, session, challenge) -> list[Path]:
        """Copy download attachments into the challenge sandbox workdir so tools
        running with cwd=sandbox can access them by filename directly."""
        if session is None or self.storage is None:
            return []
        workdir = self.sandbox_workdir(challenge)
        rows = list(session.exec(select(Attachment).where(
            Attachment.challenge_id == challenge.id,
            Attachment.type == AttachmentType.download,
        )).all())
        staged: list[Path] = []
        for att in rows:
            src = self.storage.storage_dir / att.path
            if not src.exists():
                continue
            safe_name = Path(att.name).name
            if not safe_name:
                continue
            dst = workdir / safe_name
            if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                shutil.copy2(src, dst)
            staged.append(dst)
        return staged

    def _build_command(self, tool: str, args: list[str], challenge_target: str | None,
                       workdir: Path | None = None,
                       extra_allowed_hosts: set[str] | None = None,
                       container_name: str | None = None) -> list[str]:
        executable = self.resolve_executable(tool)
        spec = self.settings.tools[tool]
        mode = self._effective_mode(tool)
        tail = self.validate_args(
            challenge_target,
            list(args),
            guard=spec.guard,
            extra_allowed_hosts=extra_allowed_hosts,
        )
        if mode == "docker":
            image = spec.image or self.settings.defaults.docker_image
            mount = str(workdir.resolve()) if workdir else str(Path.cwd().resolve())
            if getattr(self.settings.defaults, "persistent_docker", False) and container_name:
                if ensure_persistent_container(
                    container_name, image, workdir or Path.cwd(),
                    has_target=bool(challenge_target or extra_allowed_hosts),
                ):
                    return [executable, "exec", "-w", "/work", container_name, tool, *tail]
            net_args = (
                ["--network", "none"]
                if not (challenge_target or extra_allowed_hosts)
                else ["--add-host=host.docker.internal:host-gateway"]
            )
            return [executable, "run", "--rm", *net_args, "-v", f"{mount}:/work",
                    "-w", "/work", image, tool, *tail]
        if mode == "wsl":
            return [executable, tool, *tail]
        return [executable, *tail]

    def build_argv(self, tool: str, args: list[str]) -> list[str]:
        return self._build_command(tool, args, challenge_target=None)

    def validate_args(self, challenge_target: str | None, args: list[str],
                      guard: str = "strict",
                      extra_allowed_hosts: set[str] | None = None) -> list[str]:
        if guard == "none":
            return args
        for arg in args:
            if any(ch in METACHARS for ch in arg):
                raise UnsafeArg(f"shell metacharacter in arg: {arg!r}")
        allowed = set(_ALWAYS_OK)
        if extra_allowed_hosts:
            allowed.update(h.lower() for h in extra_allowed_hosts if h)
        if challenge_target:
            host = _extract_host(challenge_target)
            if host:
                allowed.add(host)
        for arg in args:
            is_url = bool(_URL_SCHEME.match(arg))
            for token in _IPV4.findall(arg):
                if token.lower() not in allowed:
                    raise TargetNotAllowed(f"host '{token}' is not the challenge target")
            for token in _DOMAIN.findall(arg):
                low = token.lower()
                if low in allowed:
                    continue
                if not is_url and Path(low).suffix in _FILE_SUFFIXES:
                    continue
                raise TargetNotAllowed(f"host '{token}' is not the challenge target")
        return args

    def run_raw(self, challenge, tool: str, args: list[str],
                timeout_s: int | None = None, output_limit_mb: int | None = None) -> ToolRunResult:
        default_t = 20 if tool in ("ncat", "nc") else self.settings.defaults.timeout_s
        timeout = timeout_s or default_t
        limit_mb = output_limit_mb if output_limit_mb is not None else self.settings.defaults.output_limit_mb
        workdir = self.sandbox_workdir(challenge)
        cid = getattr(challenge, "id", None)
        ev_id = getattr(challenge, "event_id", None)
        cname = (
            sandbox_container_name(ev_id, cid)
            if getattr(self.settings.defaults, "persistent_docker", False) and cid is not None
            else None
        )
        argv = self._build_command(
            tool,
            args,
            challenge_target=challenge.target,
            workdir=workdir,
            extra_allowed_hosts=self.approved_hosts_for(cid),
            container_name=cname,
        )

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        start = time.monotonic()
        try:
            proc = subprocess.run(argv, cwd=str(workdir), stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=timeout, creationflags=creationflags)
            duration = time.monotonic() - start
            stdout, stderr = proc.stdout or b"", proc.stderr or b""
            returncode: int | None = proc.returncode
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start
            stdout, stderr = exc.stdout or b"", exc.stderr or b""
            returncode = None

        limit_bytes = max(limit_mb, 0) * 1024 * 1024
        if limit_bytes <= 0:
            limit_bytes = 1024  # <=0 MB is never "unlimited" — 1KB guard instead
        truncated = False
        if len(stdout) + len(stderr) > limit_bytes:
            stdout, stderr, truncated = stdout[:limit_bytes], b"", True

        return ToolRunResult(tool=tool, argv=argv, returncode=returncode,
                             stdout=stdout.decode("utf-8", "replace"),
                             stderr=stderr.decode("utf-8", "replace"),
                             duration_s=duration, truncated=truncated)

    def _workdir_sig(self, workdir: Path) -> tuple[tuple[str, int, int], ...]:
        try:
            items = []
            for p in workdir.iterdir():
                if p.is_file():
                    st = p.stat()
                    items.append((p.name, st.st_size, st.st_mtime_ns))
            return tuple(sorted(items))
        except Exception:
            return ()

    def run(self, session, challenge, tool: str, args: list[str],
            attempt_result: AttemptResult | None = None,
            timeout_s: int | None = None, output_limit_mb: int | None = None,
            use_cache: bool = False) -> ToolRunResult:
        from ..services import tracking

        self.stage_downloads(session, challenge)
        workdir = self.sandbox_workdir(challenge)
        cache_key = (challenge.id, challenge.target or "", tool, tuple(args))
        sig = self._workdir_sig(workdir) if use_cache else ()
        if use_cache and tool in CACHEABLE_READONLY_TOOLS:
            cached = self._cmd_cache.get(cache_key)
            if cached is not None and cached[0] == sig:
                prev = cached[1]
                prefix = "[cached — identical command already executed on unchanged sandbox; do not repeat]\n"
                return replace(
                    prev,
                    stdout=(prefix + prev.stdout) if prev.stdout else prefix.strip(),
                    cache_hit=True,
                )

        res = self.run_raw(challenge, tool, args, timeout_s=timeout_s,
                           output_limit_mb=output_limit_mb)
        detected = _scan_output_flags(session, challenge, f"{res.stdout}\n{res.stderr}")
        res.detected_flags = detected
        effective_result = (
            attempt_result
            if attempt_result is not None
            else (AttemptResult.partial if res.returncode == 0 else AttemptResult.failed)
        )
        content = (f"=== argv ===\n{' '.join(res.argv)}\n"
                   f"=== returncode ===\n{res.returncode}\n\n"
                   f"{res.stdout}\n--- stderr ---\n{res.stderr}")
        path = self.storage.save(challenge.id, f"{tool}_output.txt", content.encode("utf-8"))
        attachment = Attachment(challenge_id=challenge.id, name=f"{tool}_output.txt",
                                path=str(path.relative_to(self.storage.storage_dir)),
                                mime="text/plain", size=len(content), type=AttachmentType.output)
        session.add(attachment)
        session.commit()
        session.refresh(attachment)
        combined_out = res.stdout.strip()
        if res.stderr.strip():
            err_block = f"[stderr] {res.stderr.strip()}"
            combined_out = f"{combined_out}\n{err_block}".strip() if combined_out else err_block
        if detected:
            flag_hdr = f"[FLAG DETECTED: {', '.join(detected)}]"
            combined_out = f"{flag_hdr}\n{combined_out}".strip()
        attempt = tracking.record_attempt(
            session, challenge.id, AttemptType.tool,
            f"{tool} run (rc={res.returncode})", effective_result,
            tool_command=" ".join(res.argv),
            learning=combined_out[:2000])
        res.attachment_id = attachment.id
        res.attempt_id = attempt.id
        if use_cache and tool in CACHEABLE_READONLY_TOOLS:
            self._cmd_cache[cache_key] = (sig, res)
        return res
