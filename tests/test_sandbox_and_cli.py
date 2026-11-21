from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

from app import cli
from app.main import create_app
from app.tooling.config import load_tools_config
from app.tooling.sandbox import current_sandbox_mode, docker_status, switch_tools_mode


def test_docker_status_and_switch_mode(tmp_path, monkeypatch):
    cfg_file = tmp_path / "tools.yaml"
    cfg_file.write_text(
        "defaults:\n  timeout_s: 300\n  output_limit_mb: 10\n  docker_image: kalilinux/kali-rolling\n"
        "tools:\n  nmap:\n    mode: native\n    guard: strict\n  python:\n    mode: native\n    guard: none\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("app.tooling.sandbox.shutil.which", lambda name: None)
    st0 = docker_status("koth-sandbox:latest")
    assert st0["cli_present"] is False

    monkeypatch.setattr("app.tooling.sandbox.shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        "app.tooling.sandbox.subprocess.run",
        lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="27.0.0", stderr=""),
    )
    st1 = docker_status("koth-sandbox:latest")
    assert st1["daemon_running"] is True
    assert st1["image_present"] is True

    assert current_sandbox_mode(cfg_file) == "native"
    switch_tools_mode("docker", docker_image="koth-sandbox:latest", tools_path=cfg_file)
    assert current_sandbox_mode(cfg_file) == "docker"
    loaded = load_tools_config(cfg_file)
    assert loaded.defaults.docker_image == "koth-sandbox:latest"
    assert loaded.tools["python"].guard == "none"


def test_settings_sandbox_routes_and_cli_entrypoint(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'sb.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    cfg_file = tmp_path / "tools.yaml"
    cfg_file.write_text(
        "defaults:\n  timeout_s: 300\n  output_limit_mb: 10\n  docker_image: koth-sandbox:latest\n"
        "tools:\n  python:\n    mode: native\n    guard: none\n",
        encoding="utf-8",
    )
    import app.web.routes as web_routes
    monkeypatch.setattr(web_routes, "TOOLS_PATH", cfg_file)
    monkeypatch.setattr("app.tooling.sandbox.shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        "app.tooling.sandbox.subprocess.run",
        lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
    )

    app = create_app(engine=engine, storage_dir=tmp_path / "storage")
    with TestClient(app) as c:
        r_test = c.post("/settings/sandbox/test")
        assert r_test.status_code == 200
        assert "pronta" in r_test.text

        r_mode = c.post(
            "/settings/sandbox/mode",
            data={"mode": "docker", "docker_image": "koth-sandbox:latest"},
            follow_redirects=False,
        )
        assert r_mode.status_code == 303
        assert current_sandbox_mode(cfg_file) == "docker"

        r_build = c.post("/settings/sandbox/build")
        assert r_build.status_code == 200
        assert "construída" in r_build.text

    # CLI entrypoint tests
    assert cli.main(["doctor"]) == 0
    called = {}
    monkeypatch.setattr(
        "app.cli.uvicorn.run",
        lambda target, host, port, reload: called.update({"target": target, "host": host, "port": port}),
    )
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "9000", "--no-browser"]) == 0
    assert called == {"target": "app.main:app", "host": "0.0.0.0", "port": 9000}
