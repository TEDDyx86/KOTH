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
