import pytest

from app.agents.config import ModelRef, ProviderCfg, ModelsConfig, load_models
from app.agents.models import ProviderUnavailable, resolve_for_role, resolve_model


@pytest.fixture
def cfg():
    return load_models()


def test_resolve_nvidia_model(cfg, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    ref = ModelRef(provider="nvidia", name="nvidia/nemotron-3-ultra-550b-a55b")
    model = resolve_model(ref, cfg)
    assert "nemotron" in str(model.model_name) or hasattr(model, "model_name")


def test_missing_env_raises(cfg, monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    ref = ModelRef(provider="nvidia", name="nvidia/nemotron-3-ultra-550b-a55b")
    with pytest.raises(ProviderUnavailable):
        resolve_model(ref, cfg)


def test_string_shorthand_provider_derived(cfg, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    ref = ModelRef(provider="", name="nvidia/nemotron-3.5-super-vl-120b-a12b")
    model = resolve_model(ref, cfg)  # provider derived from "nvidia/" prefix
    assert model is not None


def test_resolve_for_role_with_fallback(cfg, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg.providers["anthropic"] = ProviderCfg(name="anthropic", env_var="ANTHROPIC_API_KEY")
    cfg.roles["strong"] = ModelRef(provider="anthropic", name="claude-x")
    cfg.fallbacks["strong"] = [ModelRef(provider="nvidia",
                                        name="nvidia/nemotron-3.5-super-vl-120b-a12b")]
    model = resolve_for_role("jury", agents_cfg_role="strong", models_cfg=cfg)
    assert model is not None


def test_resolve_for_role_all_unavailable_raises(cfg, monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    cfg.roles["strong"] = ModelRef(provider="nvidia", name="nvidia/nemotron-3-ultra-550b-a55b")
    cfg.fallbacks["strong"] = [ModelRef(provider="nvidia", name="nvidia/nemotron-3.5-super-vl-120b-a12b")]
    with pytest.raises(ProviderUnavailable):
        resolve_for_role("jury", agents_cfg_role="strong", models_cfg=cfg)
