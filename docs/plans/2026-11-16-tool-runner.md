# Tool Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Internal tool-execution library: allowlisted tools run sandboxed against the challenge target only, with captured/truncated output persisted as Attachment + Attempt.

**Architecture:** `app/tooling/` package — YAML config loader, `ToolRunner` (subprocess, no shell), and `inventory()` doctor. Pure library: no routes, no UI in this phase. Consumed later by UI form and Agent Runtime.

**Tech Stack:** Python 3.12+, subprocess (list argv, no shell), PyYAML, existing StorageService + tracking service.

## Global Constraints

- **Never** build commands via shell string; argv lists only (`subprocess.run` with `shell=False`).
- Target enforcement: args may only reference hosts `challenge.target` (host part), `localhost`, `127.0.0.1`, `::1`, `0.0.0.0`. Violation → `TargetNotAllowed`.
- Shell metacharacters in any arg (`;`, `|`, `&`, `$`, `` ` ``, newline) → `UnsafeArg` (defense in depth).
- Unknown tool (not in allowlist) → `ToolNotAllowed`.
- Windows: `subprocess.CREATE_NO_WINDOW` when available.
- Output: truncate to `defaults.output_limit_mb`; mark `truncated`.
- Every successful run (returncode recorded either way) persists: Attachment (`type=output`) with captured stdout+stderr, and Attempt (`type=tool`) with the full argv as `tool_command`.
- Seed `config/tools.yaml`: nmap, ncat, curl, strings, gdb, objdump, sqlmap, python — all `mode: native` (present on host machine per 2026-11-16 inventory).
- Tests run with `python -m pytest tests/ -v`.

---

### Task 1: Tools config loader + seed file

**Files:**
- Create: `app/tooling/__init__.py` (empty), `app/tooling/config.py`, `config/tools.yaml`
- Modify: `pyproject.toml` (add `pyyaml>=6.0`)
- Test: `tests/test_tool_config.py`

**Interfaces:**
- Produces: `ToolSettings`, `ToolSpec` dataclasses; `load_tools_config(path: Path | None = None) -> ToolSettings`; `ToolSettings.defaults.timeout_s: int`, `.output_limit_mb: int`; `ToolSettings.tools: dict[str, ToolSpec]`, `ToolSpec.mode: str`.

- [ ] **Step 1:** Add `pyyaml>=6.0` to `[project.dependencies]` in pyproject.toml; run `python -m pip install -e ".[dev]" --quiet`.
- [ ] **Step 2:** Write failing test:

```python
# tests/test_tool_config.py
from app.tooling.config import load_tools_config


def test_load_seed_config():
    settings = load_tools_config()
    for name in ["nmap", "ncat", "curl", "strings", "gdb", "objdump", "sqlmap", "python"]:
        assert name in settings.tools
        assert settings.tools[name].mode == "native"
    assert settings.defaults.timeout_s == 300
    assert settings.defaults.output_limit_mb == 10


def test_load_custom_path(tmp_path):
    cfg = tmp_path / "tools.yaml"
    cfg.write_text("tools:\n  curl:\n    mode: wsl\ndefaults:\n  timeout_s: 5\n  output_limit_mb: 1\n")
    settings = load_tools_config(cfg)
    assert settings.tools["curl"].mode == "wsl"
    assert settings.defaults.timeout_s == 5
```

Run: `python -m pytest tests/test_tool_config.py -v` → FAIL (no module).
- [ ] **Step 3:** Write `config/tools.yaml`:

```yaml
defaults:
  timeout_s: 300
  output_limit_mb: 10
tools:
  nmap:    {mode: native}
  ncat:    {mode: native}
  curl:    {mode: native}
  strings: {mode: native}
  gdb:     {mode: native}
  objdump: {mode: native}
  sqlmap:  {mode: native}
  python:  {mode: native}
```

- [ ] **Step 4:** Write `app/tooling/config.py`:

```python
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..config import BASE_DIR

DEFAULT_PATH = BASE_DIR / "config" / "tools.yaml"


@dataclass
class ToolSpec:
    mode: str = "native"


@dataclass
class Defaults:
    timeout_s: int = 300
    output_limit_mb: int = 10


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
        settings.tools[str(name)] = ToolSpec(mode=(spec or {}).get("mode", "native"))
    d = raw.get("defaults") or {}
    settings.defaults = Defaults(timeout_s=int(d.get("timeout_s", 300)),
                                 output_limit_mb=int(d.get("output_limit_mb", 10)))
    return settings
```

Run tests → PASS. Commit: `feat: tool config loader with seeded allowlist`.

---

### Task 2: ToolRunner — allowlist, arg sanitization, target enforcement

**Files:**
- Create: `app/tooling/runner.py`
- Test: `tests/test_tool_runner_guard.py`

**Interfaces:**
- Produces: errors `ToolNotAllowed`, `UnsafeArg`, `TargetNotAllowed` (all `Exception` subclasses); `ToolRunner(settings: ToolSettings, storage: StorageService)`; internal helpers `ToolRunner.validate_args(challenge_target: str | None, args: list[str]) -> list[str]` (returns argv tail after tool name) used by Task 3/4.

Host-allow logic: extract host part of `challenge_target` (`target.split(":")[0]`); allowed set = {that host (when set), "localhost", "127.0.0.1", "::1", "0.0.0.0"}. IPv4 regex `\b(?:\d{1,3}\.){3}\d{1,3}\b` and domain regex `\b[a-z0-9][a-z0-9.\-]+\.[a-z]{2,}\b` (case-insensitive) scanned over every arg; any match not in the allowed set → `TargetNotAllowed`.

- [ ] **Step 1:** Failing test:

```python
# tests/test_tool_runner_guard.py
import pytest

from app.models import Challenge
from app.tooling.config import load_tools_config
from app.tooling.runner import TargetNotAllowed, ToolNotAllowed, UnsafeArg
from app.services.storage import StorageService


@pytest.fixture
def runner(tmp_path):
    return ToolRunnerFixture(tmp_path)


class ToolRunnerFixture:
    def __init__(self, tmp_path):
        from app.tooling.runner import ToolRunner
        self.settings = load_tools_config()
        self.runner = ToolRunner(self.settings, StorageService(tmp_path / "storage"))

    def validate(self, target, args):
        return self.runner.validate_args(target, args)


def test_unknown_tool_raises(tmp_path):
    f = ToolRunnerFixture(tmp_path)
    with pytest.raises(ToolNotAllowed):
        f.runner.build_argv("binwalk", [])


def test_shell_metachar_rejected(tmp_path):
    f = ToolRunnerFixture(tmp_path)
    with pytest.raises(UnsafeArg):
        f.validate("chal.local:1337", ["-p", "1; rm -rf /"])


def test_foreign_host_rejected(tmp_path):
    f = ToolRunnerFixture(tmp_path)
    with pytest.raises(TargetNotAllowed):
        f.validate("chal.local:1337", ["-p", "1337", "10.0.0.5"])


def test_own_target_and_localhost_allowed(tmp_path):
    f = ToolRunnerFixture(tmp_path)
    argv_tail = f.validate("chal.local:1337", ["-p", "1337", "chal.local", "localhost"])
    assert argv_tail == ["-p", "1337", "chal.local", "localhost"]


def test_no_target_args_without_hosts_pass(tmp_path):
    f = ToolRunnerFixture(tmp_path)
    assert f.validate(None, ["-c", "print(1+1)"]) == ["-c", "print(1+1)"]


def test_build_argv_ok(tmp_path):
    f = ToolRunnerFixture(tmp_path)
    argv = f.runner.build_argv("python", ["-c", "print(1)"])
    assert argv[0].lower().endswith("python.exe") or argv[0].endswith("python")
```

- [ ] **Step 2:** Implement `app/tooling/runner.py` (guard portion; `run` arrives in Task 3):

```python
import re
import shutil
import subprocess
import time
from dataclasses import dataclass

from ..config import settings as app_settings
from ..models import Attachment, Attempt, AttemptType, AttemptResult, Challenge
from ..services.storage import StorageService
from .config import ToolSettings

METACHARS = set(";|&$`\n\r")


class ToolNotAllowed(Exception): ...
class UnsafeArg(Exception): ...
class TargetNotAllowed(Exception): ...


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
    def __init__(self, settings: ToolSettings, storage: StorageService) -> None:
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
```

- [ ] **Step 3:** Tests → PASS. Commit: `feat: tool runner guards (allowlist, metachars, target enforcement)`.

---

### Task 3: ToolRunner — execution, workdir, timeout, truncation

**Files:**
- Modify: `app/tooling/runner.py` (append `run` core)
- Test: `tests/test_tool_runner_exec.py`

**Interfaces:**
- Produces: `ToolRunner.run_raw(challenge: Challenge, tool: str, args: list[str], timeout_s: int | None = None, output_limit_mb: int | None = None) -> ToolRunResult` — creates workdir `storage.storage_dir/sandbox/<event_id>/<challenge_id>`, runs argv, truncates, returns result WITHOUT persistence (persistence is Task 4's `run`).
- Timeout → `returncode=None`, partial stdout captured, `duration_s >= timeout`.

- [ ] **Step 1:** Failing test:

```python
# tests/test_tool_runner_exec.py
import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models import Challenge, Event
from app.services.storage import StorageService
from app.tooling.config import load_tools_config
from app.tooling.runner import ToolRunner


@pytest.fixture
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        event = Event(name="e")
        session.add(event); session.commit(); session.refresh(event)
        ch = Challenge(event_id=event.id, title="c", target=None)
        session.add(ch); session.commit(); session.refresh(ch)
        yield session, ch


def _runner(tmp_path):
    return ToolRunner(load_tools_config(), StorageService(tmp_path / "storage"))


def test_run_python_captures_output(tmp_path, env):
    session, ch = env
    res = _runner(tmp_path).run_raw(ch, "python", ["-c", "print('hello-ctf')"])
    assert res.returncode == 0
    assert "hello-ctf" in res.stdout
    assert res.truncated is False


def test_run_timeout(tmp_path, env):
    session, ch = env
    res = _runner(tmp_path).run_raw(ch, "python", ["-c", "import time; time.sleep(10)"], timeout_s=2)
    assert res.returncode is None
    assert res.duration_s >= 2


def test_run_truncates_output(tmp_path, env):
    session, ch = env
    res = _runner(tmp_path).run_raw(ch, "python", ["-c", "print('x' * 100000)"], output_limit_mb=0)
    # 0 MB limit -> keep at least 0 but not full; runner treats <=0 as 1KB guard
    assert len(res.stdout) < 100000
    assert res.truncated is True


def test_workdir_created_under_sandbox(tmp_path, env):
    session, ch = env
    _runner(tmp_path).run_raw(ch, "python", ["-c", "print(1)"])
    assert (tmp_path / "storage" / "sandbox" / str(ch.event_id) / str(ch.id)).exists()
```

- [ ] **Step 2:** Append to `app/tooling/runner.py`:

```python
class ToolRunner:  # (extend the existing class — add these methods inside it)
    def run_raw(self, challenge: Challenge, tool: str, args: list[str],
                timeout_s: int | None = None, output_limit_mb: int | None = None) -> ToolRunResult:
        timeout = timeout_s or self.settings.defaults.timeout_s
        limit_mb = output_limit_mb if output_limit_mb is not None else self.settings.defaults.output_limit_mb
        challenge_target = challenge.target
        executable = self.resolve_executable(tool)
        tail = self.validate_args(challenge_target, list(args))
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
            returncode = proc.returncode
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start
            stdout, stderr = exc.stdout or b"", exc.stderr or b""
            returncode = None

        limit_bytes = max(limit_mb, 0) * 1024 * 1024
        truncated = False
        if limit_bytes and len(stdout) + len(stderr) > limit_bytes:
            keep = max(limit_bytes, 1024)
            stdout, stderr, truncated = stdout[:keep], b"", True

        return ToolRunResult(tool=tool, argv=argv, returncode=returncode,
                             stdout=stdout.decode("utf-8", "replace"),
                             stderr=stderr.decode("utf-8", "replace"),
                             duration_s=duration, truncated=truncated)
```

(Note for implementer: place these methods **inside** the existing `ToolRunner` class from Task 2, do not redeclare the class.)

- [ ] **Step 3:** Tests → PASS. Commit: `feat: tool runner execution with workdir, timeout and truncation`.

---

### Task 4: ToolRunner — persistence (Attachment + Attempt)

**Files:**
- Modify: `app/tooling/runner.py` (append `run`)
- Test: `tests/test_tool_runner_track.py`

**Interfaces:**
- Produces: `ToolRunner.run(session: Session, challenge: Challenge, tool: str, args: list[str], attempt_result: AttemptResult = AttemptResult.failed, timeout_s=None, output_limit_mb=None) -> ToolRunResult` — calls `run_raw`, saves output as Attachment (`type=output`, name `{tool}_output.txt`, content `=== argv ===\n{argv}\n=== returncode ===\n{rc}\n\n{stdout}\n--- stderr ---\n{stderr}`), then records Attempt (`type=tool`, description `"{tool} run (rc={rc})"`, tool_command = full argv joined, result = attempt_result, learning = last 2000 chars of stdout). Fills `attachment_id`/`attempt_id` on the result.

- [ ] **Step 1:** Failing test:

```python
# tests/test_tool_runner_track.py
import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models import Attempt, AttemptResult, Attachment, Challenge, Event
from app.services import tracking
from app.services.storage import StorageService
from app.tooling.config import load_tools_config
from app.tooling.runner import ToolRunner


@pytest.fixture
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        event = Event(name="e")
        session.add(event); session.commit(); session.refresh(event)
        ch = Challenge(event_id=event.id, title="c")
        session.add(ch); session.commit(); session.refresh(ch)
        yield session, ch


def test_run_persists_attempt_and_attachment(tmp_path, env):
    session, ch = env
    runner = ToolRunner(load_tools_config(), StorageService(tmp_path / "storage"))
    res = runner.run(session, ch, "python", ["-c", "print('tracked')"])
    assert res.attachment_id is not None and res.attempt_id is not None

    att = session.get(Attachment, res.attachment_id)
    assert att.type.value == "output"
    assert att.name == "python_output.txt"

    attempt = session.get(Attempt, res.attempt_id)
    assert attempt.type.value == "tool"
    assert "tracked" in attempt.learning

    summary = tracking.challenge_summary(session, ch)
    assert summary["attempt_count"] == 1
```

- [ ] **Step 2:** Append to `app/tooling/runner.py` (inside class):

```python
    def run(self, session, challenge: Challenge, tool: str, args: list[str],
            attempt_result: AttemptResult = AttemptResult.failed,
            timeout_s: int | None = None, output_limit_mb: int | None = None) -> ToolRunResult:
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
```

(Add `AttachmentType` to the models import line at top of the file.)

- [ ] **Step 3:** Tests → PASS. Commit: `feat: tool runner persists output attachment and tool attempt`.

---

### Task 5: Tool Doctor — inventory

**Files:**
- Create: `app/tooling/doctor.py`
- Test: `tests/test_tool_doctor.py`

**Interfaces:**
- Produces: `inventory(settings: ToolSettings) -> list[dict]` — one dict per allowlisted tool: `{name, mode, path (str|None), present (bool)}`. Uses `shutil.which`; no subprocess version calls (can hang).

- [ ] **Step 1:** Failing test:

```python
# tests/test_tool_doctor.py
from app.tooling.config import load_tools_config
from app.tooling.doctor import inventory


def test_inventory_marks_present_tools():
    rows = inventory(load_tools_config())
    by_name = {r["name"]: r for r in rows}
    assert by_name["python"]["present"] is True
    assert by_name["python"]["path"] is not None
    assert by_name["nmap"]["present"] is True


def test_inventory_marks_missing_tools(tmp_path):
    cfg = tmp_path / "tools.yaml"
    cfg.write_text("tools:\n  definitely_not_a_real_tool_xyz:\n    mode: native\n")
    rows = inventory(load_tools_config(cfg))
    assert rows[0]["name"] == "definitely_not_a_real_tool_xyz"
    assert rows[0]["present"] is False
    assert rows[0]["path"] is None
```

- [ ] **Step 2:** Implement `app/tooling/doctor.py`:

```python
import shutil

from .config import ToolSettings


def inventory(settings: ToolSettings) -> list[dict]:
    rows = []
    for name in sorted(settings.tools):
        path = shutil.which(name)
        rows.append({"name": name, "mode": settings.tools[name].mode,
                     "path": path, "present": path is not None})
    return rows
```

- [ ] **Step 3:** Full suite `python -m pytest tests/ -v` → all PASS. Commit: `feat: tool doctor inventory`.

---

## Self-Review

- Spec §6 coverage: allowlist (T1), target enforcement (T2), metachars/no-shell (T2), workdir sandbox (T3), timeout+truncation (T3), output→Attachment + Attempt→tracking (T4), Tool Doctor inventory (T5), Windows CREATE_NO_WINDOW (T3). `exploit`-tag approval flow deferred to Agent Runtime (per spec §2 item 5). UI manual run explicitly deferred (user decision).
- No placeholders; signatures consistent across tasks (`run_raw`, `run`, `validate_args`, `build_argv`, `ToolRunResult` fields).
- Types: `AttachmentType.output` used in T4 (imported); `AttemptType.tool` matches model enum.
