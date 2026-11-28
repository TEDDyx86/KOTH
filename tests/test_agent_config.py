from app.agents.config import load_agents, load_models


def test_load_seed_models():
    cfg = load_models()
    assert "nvidia" in cfg.providers
    assert cfg.providers["nvidia"].base_url == "https://integrate.api.nvidia.com/v1"
    assert cfg.providers["nvidia"].env_var == "NVIDIA_API_KEY"
    assert cfg.roles["strong"].provider == "cli_agy"
    assert cfg.roles["strong"].name == "gemini-4-neon-xhigh"
    assert cfg.roles["fast"].provider == "cli_codex"
    assert cfg.roles["fast"].name == "gpt-6.1-sol"
    assert cfg.fallbacks["strong"][0] == cfg.roles["fast"]
    assert cfg.fallbacks["fast"][0] == cfg.roles["strong"]


def test_load_custom_models_path(tmp_path):
    cfg_file = tmp_path / "models.yaml"
    cfg_file.write_text(
        "roles:\n"
        "  strong: {provider: nvidia, name: nvidia/x}\n"
        "  fast: {provider: nvidia, name: nvidia/y}\n"
        "providers:\n"
        "  nvidia: {base_url: https://example.com/v1, env_var: NV_KEY}\n"
        "fallbacks:\n"
        "  strong: [nvidia/y]\n",
        encoding="utf-8")
    cfg = load_models(cfg_file)
    assert cfg.roles["strong"].name == "nvidia/x"
    assert cfg.providers["nvidia"].base_url == "https://example.com/v1"
    assert cfg.fallbacks["strong"][0].name == "nvidia/y"


def test_load_seed_agents():
    cfg = load_agents()
    for role in ["op", "planner", "recon", "jury", "submission",
                 "specialist_web", "specialist_pwn", "specialist_crypto",
                 "specialist_reversing", "specialist_forensics", "specialist_osint"]:
        assert role in cfg.roles, role
    assert cfg.roles["jury"].model == "strong"
    assert cfg.roles["specialist_web"].model == "fast"
    assert cfg.roles["op"].persona.endswith("op.md")
    assert cfg.limits["max_attempts"] >= 20
    assert cfg.limits["max_minutes"] >= 90


def test_load_custom_agents_path(tmp_path):
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        "roles:\n"
        "  jury: {persona: personas/jury.md, model: strong, tools: [nmap]}\n",
        encoding="utf-8")
    cfg = load_agents(cfg_file)
    assert cfg.roles["jury"].tools == ["nmap"]
