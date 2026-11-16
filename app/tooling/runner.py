import re
import shutil
import subprocess
import time
from dataclasses import dataclass

from ..models import AttachmentType, AttemptResult, AttemptType
from .config import ToolSettings

METACHARS = set(";|&$`\n\r")


class ToolNotAllowed(Exception):
    ...


class UnsafeArg(Exception):
    ...


class TargetNotAllowed(Exception):
    ...


_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_DOMAIN = re.compile(r"\b[a-z0-9][a-z0-9.\-]+\.[a-z]{2,}\b", re.IGNORECASE)
_ALWAYS_OK = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


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


class ToolRunner:
    def __init__(self, settings: ToolSettings, storage) -> None:
        self.settings = settings
        self.storage = storage

    def resolve_executable(self, tool: str) -> str:
        if tool not in self.settings.tools:
            raise ToolNotAllowed(f"tool '{tool}' not in allowlist")
        path = shutil.which(tool)
        if path is None:
            raise ToolNotAllowed(f"tool '{tool}' not found on PATH")
        return path

    def build_argv(self, tool: str, args: list[str]) -> list[str]:
        spec = self.settings.tools.get(tool)
        guard = spec.guard if spec else "strict"
        return [self.resolve_executable(tool), *self.validate_args(None, list(args), guard=guard)]

    def validate_args(self, challenge_target: str | None, args: list[str],
                      guard: str = "strict") -> list[str]:
        if guard == "none":
            return args
        for arg in args:
            if any(ch in METACHARS for ch in arg):
                raise UnsafeArg(f"shell metacharacter in arg: {arg!r}")
        allowed = set(_ALWAYS_OK)
        if challenge_target:
            allowed.add(challenge_target.split(":")[0].lower())
        for arg in args:
            for token in _IPV4.findall(arg) + _DOMAIN.findall(arg):
                if token.lower() not in allowed:
                    raise TargetNotAllowed(f"host '{token}' is not the challenge target")
        return args

    def run_raw(self, challenge, tool: str, args: list[str],
                timeout_s: int | None = None, output_limit_mb: int | None = None) -> ToolRunResult:
        timeout = timeout_s or self.settings.defaults.timeout_s
        limit_mb = output_limit_mb if output_limit_mb is not None else self.settings.defaults.output_limit_mb
        challenge_target = challenge.target
        executable = self.resolve_executable(tool)
        spec = self.settings.tools[tool]
        tail = self.validate_args(challenge_target, list(args), guard=spec.guard)
        argv = [executable, *tail]

        workdir = self.storage.storage_dir / "sandbox" / str(challenge.event_id) / str(challenge.id)
        workdir.mkdir(parents=True, exist_ok=True)

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        start = time.monotonic()
        try:
            proc = subprocess.run(argv, cwd=str(workdir), capture_output=True,
                                  timeout=timeout, creationflags=creationflags)
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

    def run(self, session, challenge, tool: str, args: list[str],
            attempt_result: AttemptResult = AttemptResult.failed,
            timeout_s: int | None = None, output_limit_mb: int | None = None) -> ToolRunResult:
        from ..models import Attachment
        from ..services import tracking

        res = self.run_raw(challenge, tool, args, timeout_s=timeout_s,
                           output_limit_mb=output_limit_mb)
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
        attempt = tracking.record_attempt(
            session, challenge.id, AttemptType.tool,
            f"{tool} run (rc={res.returncode})", attempt_result,
            tool_command=" ".join(res.argv),
            learning=res.stdout[:2000])
        res.attachment_id = attachment.id
        res.attempt_id = attempt.id
        return res
