import json
import os
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

from app.agents.config import ModelRef, ModelsConfig, load_models
from app.agents.models import resolve_model
from app.agents.providers import (
    import_opencode_keys,
    load_saved_keys,
    mask_key,
)
from app.main import create_app


def test_provider_auth_save_activate_test_and_delete(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'p.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    tmp_models = tmp_path / "models.yaml"
    tmp_models.write_text(
        "providers:\n"
        "  nvidia:\n"
        "    base_url: https://integrate.api.nvidia.com/v1\n"
        "    env_var: NVIDIA_API_KEY\n"
        "roles:\n"
        "  strong:\n"
        "    provider: nvidia\n"
        "    name: nvidia/nemotron-3-ultra-550b-a55b\n"
        "  fast:\n"
        "    provider: nvidia\n"
        "    name: nvidia/nemotron-3.5-super-vl-120b-a12b\n",
        encoding="utf-8",
    )

    import app.web.routes as web_routes
    monkeypatch.setattr(web_routes, "MODELS_PATH", tmp_models)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    app = create_app(engine=engine, storage_dir=tmp_path / "storage")
    with TestClient(app) as c:
        r_page = c.get("/settings")
        assert r_page.status_code == 200
        assert "OpenAI" in r_page.text
        assert "Anthropic (Claude)" in r_page.text
        assert "Google Gemini" in r_page.text
        assert "Ollama (Local)" in r_page.text

        # Save OpenAI key
        r_save = c.post(
            "/settings/providers/openai/save",
            data={"api_key": "sk-test-12345678abcd", "base_url": "https://api.openai.com/v1"},
            follow_redirects=False,
        )
        assert r_save.status_code == 303
        assert os.environ.get("OPENAI_API_KEY") == "sk-test-12345678abcd"
        saved = load_saved_keys(tmp_path / "storage")
        assert saved["openai"] == "sk-test-12345678abcd"

        # Key is masked in rendered HTML (never leaked in full)
        html = c.get("/settings").text
        assert "••••abcd" in html
        assert "sk-test-12345678abcd" not in html

        # 1-click activate preset
        r_act = c.post("/settings/providers/openai/activate", follow_redirects=False)
        assert r_act.status_code == 303
        cfg = load_models(tmp_models)
        assert cfg.roles["strong"].provider == "openai"
        assert cfg.roles["strong"].name == "gpt-4.1"
        assert cfg.roles["fast"].name == "gpt-4.1-mini"

        # Custom models + fallback
        r_mod = c.post(
            "/settings/models",
            data={
                "strong_provider": "openai",
                "strong_name": "o3-mini",
                "fast_provider": "openai",
                "fast_name": "gpt-4o",
                "fallback_provider": "nvidia",
                "fallback_name": "nvidia/nemotron-3.5-super-vl-120b-a12b",
            },
            follow_redirects=False,
        )
        assert r_mod.status_code == 303
        cfg2 = load_models(tmp_models)
        assert cfg2.roles["strong"].name == "o3-mini"
        assert cfg2.fallbacks["strong"][0].provider == "nvidia"

        # Mocked connection test
        class FakeHttpxClient:
            def __init__(self, timeout=8.0):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url, headers=None, params=None):
                return SimpleNamespace(status_code=200, json=lambda: {"data": [1, 2, 3]})

        monkeypatch.setattr("app.agents.providers.httpx.Client", FakeHttpxClient)
        r_test = c.post("/settings/providers/openai/test")
        assert r_test.status_code == 200
        assert "conectado (3 modelos)" in r_test.text

        # Delete key
        r_del = c.post("/settings/providers/openai/delete", follow_redirects=False)
        assert r_del.status_code == 303
        assert "OPENAI_API_KEY" not in os.environ
        assert "openai" not in load_saved_keys(tmp_path / "storage")


def test_import_opencode_and_extra_providers_resolution(tmp_path, monkeypatch):
    tmp_models = tmp_path / "models.yaml"
    tmp_models.write_text("providers: {}\nroles: {}\n", encoding="utf-8")
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"openrouter": {"key": "or-key-9999"}}), encoding="utf-8")

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    imported = import_opencode_keys(tmp_path / "storage", auth_path=auth_file, models_path=tmp_models)
    assert imported == ["openrouter"]
    assert os.environ.get("OPENROUTER_API_KEY") == "or-key-9999"
    assert mask_key("or-key-9999") == "••••9999"
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    # Ollama resolves without key; Anthropic resolves via OpenAI-compatible fallback
    cfg = ModelsConfig()
    m_ollama = resolve_model(ModelRef(provider="ollama", name="qwen2.5:14b"), cfg)
    assert m_ollama.model_name == "qwen2.5:14b"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    m_ant = resolve_model(ModelRef(provider="anthropic", name="claude-sonnet-4-20250514"), cfg)
    assert m_ant.model_name == "claude-sonnet-4-20250514"


def test_settings_tabs_add_provider_and_dynamic_model_options(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'dyn.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    tmp_models = tmp_path / "models.yaml"
    tmp_models.write_text(
        "providers: {}\nroles:\n  strong:\n    provider: nvidia\n    name: nvidia/nemotron-3-ultra-550b-a55b\n",
        encoding="utf-8",
    )

    import app.web.routes as web_routes
    monkeypatch.setattr(web_routes, "MODELS_PATH", tmp_models)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    app = create_app(engine=engine, storage_dir=tmp_path / "storage")
    with TestClient(app) as c:
        # Clean tabbed settings page
        html = c.get("/settings").text
        assert "Provedores &amp; Auth" in html
        assert "Modelos &amp; IA" in html
        assert "Adicionar provedor" in html

        # Add Groq via the unified "+ Adicionar provedor" form
        r_add = c.post(
            "/settings/providers/add",
            data={
                "provider_name": "groq",
                "api_key": "gsk_test_1234",
                "base_url": "https://api.groq.com/openai/v1",
                "activate_now": "true",
            },
            follow_redirects=False,
        )
        assert r_add.status_code == 303
        cfg = load_models(tmp_models)
        assert cfg.roles["strong"].provider == "groq"

        # Live model discovery on provider change
        class FakeHttpxClient:
            def __init__(self, timeout=5.0):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url, headers=None, params=None):
                return SimpleNamespace(
                    status_code=200,
                    json=lambda: {"data": [{"id": "llama-3.3-70b-versatile"}, {"id": "mixtral-8x7b"}]},
                )

        monkeypatch.setattr("app.agents.providers.httpx.Client", FakeHttpxClient)
        r_opts = c.get("/settings/models/options", params={"role": "strong", "provider": "groq"})
        assert r_opts.status_code == 200
        assert "mixtral-8x7b" in r_opts.text
        assert "origem: live" in r_opts.text

        # CLI provider models returned dynamically too
        r_cli_opts = c.get("/settings/models/options", params={"role": "strong", "provider": "cli_claude"})
        assert r_cli_opts.status_code == 200
        assert "sonnet" in r_cli_opts.text
        assert "origem: cli" in r_cli_opts.text

        monkeypatch.delenv("GROQ_API_KEY", raising=False)
