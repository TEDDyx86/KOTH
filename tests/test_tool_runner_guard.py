import pytest

from app.services.storage import StorageService
from app.tooling.config import load_tools_config
from app.tooling.runner import TargetNotAllowed, ToolNotAllowed, ToolRunner, UnsafeArg


class ToolRunnerFixture:
    def __init__(self, tmp_path):
        self.settings = load_tools_config()
        self.runner = ToolRunner(self.settings, StorageService(tmp_path / "storage"))

    def validate(self, target, args):
        return self.runner.validate_args(target, args)


def test_unknown_tool_raises(tmp_path):
    f = ToolRunnerFixture(tmp_path)
    with pytest.raises(ToolNotAllowed):
        f.runner.build_argv("definitely_unknown_tool_xyz", [])


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


def test_local_filenames_allowed_while_urls_checked(tmp_path):
    f = ToolRunnerFixture(tmp_path)
    assert f.validate(None, ["challenge.bin", "photo.jpg", "capture.pcap"]) == [
        "challenge.bin",
        "photo.jpg",
        "capture.pcap",
    ]
    with pytest.raises(TargetNotAllowed):
        f.validate("chal.local:1337", ["https://evil.py/payload"])


def test_build_argv_ok(tmp_path):
    f = ToolRunnerFixture(tmp_path)
    argv = f.runner.build_argv("python", ["-c", "print(1)"])
    assert any("python" in a.lower() for a in argv)
