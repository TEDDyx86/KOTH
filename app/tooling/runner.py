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
        return [self.resolve_executable(tool), *self.validate_args(None, list(args))]

    def validate_args(self, challenge_target: str | None, args: list[str]) -> list[str]:
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
