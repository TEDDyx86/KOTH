from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

from app.agents.config import load_agents, load_models
from app.agents.providers import PROFILE_BY_NAME, apply_model_profile, detect_active_profile
from app.database import configure_sqlite_engine
from app.main import create_app


def test_seed_configs_use_combo_profile():
    mcfg = load_models()
    acfg = load_agents()
    assert mcfg.active_profile == "combo"
    assert mcfg.roles["strong"].provider == "cli_agy"
    assert mcfg.roles["strong"].name == "gemini-4-neon-xhigh"
    assert mcfg.roles["fast"].provider == "cli_codex"
    assert mcfg.roles["fast"].name == "gpt-6.1-sol"
    assert detect_active_profile(mcfg, acfg) == "combo"

    for role in ("op", "planner", "jury", "specialist_crypto", "specialist_reversing", "specialist_forensics", "specialist_osint"):
        assert acfg.roles[role].model == "strong"
    for role in ("recon", "submission", "specialist_web", "specialist_pwn", "specialist_misc"):
        assert acfg.roles[role].model == "fast"


def test_apply_model_profile_combo_and_switch(tmp_path):
    models_file = tmp_path / "models.yaml"
    agents_file = tmp_path / "agents.yaml"
    models_file.write_text("providers: {}\nroles:\n  strong: {provider: nvidia, name: a}\n  fast: {provider: nvidia, name: b}\n", encoding="utf-8")
    agents_file.write_text(
        "roles:\n" + "\n".join(
            f"  {r}:\n    persona: personas/{r}.md\n    model: fast"
            for r in PROFILE_BY_NAME["combo"].role_routing
        ) + "\nlimits:\n  max_attempts: 20\n  max_minutes: 90\n",
        encoding="utf-8",
    )

    prof = apply_model_profile("combo", models_path=models_file, agents_path=agents_file)
    assert prof.name == "combo"

    mcfg = load_models(models_file)
    acfg = load_agents(agents_file)
    assert mcfg.active_profile == "combo"
    assert mcfg.roles["strong"].provider == "cli_agy"
    assert mcfg.roles["strong"].name == "gemini-4-neon-xhigh"
    assert mcfg.roles["fast"].provider == "cli_codex"
    assert mcfg.roles["fast"].name == "gpt-6.1-sol"
    assert acfg.roles["specialist_crypto"].model == "strong"
    assert acfg.roles["specialist_reversing"].model == "strong"
    assert acfg.roles["specialist_web"].model == "fast"
    assert acfg.roles["specialist_pwn"].model == "fast"
    assert detect_active_profile(mcfg, acfg) == "combo"

    apply_model_profile("codex_all", models_path=models_file, agents_path=agents_file)
    mcfg2 = load_models(models_file)
    acfg2 = load_agents(agents_file)
    assert mcfg2.active_profile == "codex_all"
    assert mcfg2.roles["strong"].provider == "cli_codex"
    assert mcfg2.roles["fast"].name == "gpt-6-luna"
    assert acfg2.roles["specialist_crypto"].model == "fast"


def test_settings_profile_activation_route(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'prof.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)

    models_file = tmp_path / "models.yaml"
    agents_file = tmp_path / "agents.yaml"
    models_file.write_text("providers: {}\nroles:\n  strong: {provider: nvidia, name: a}\n  fast: {provider: nvidia, name: b}\n", encoding="utf-8")
    agents_file.write_text(
        "roles:\n" + "\n".join(
            f"  {r}:\n    persona: personas/{r}.md\n    model: fast"
            for r in PROFILE_BY_NAME["combo"].role_routing
        ) + "\nlimits:\n  max_attempts: 20\n  max_minutes: 90\n",
        encoding="utf-8",
    )

    import app.web.routes as web_routes
    monkeypatch.setattr(web_routes, "MODELS_PATH", models_file)
    monkeypatch.setattr(web_routes, "AGENTS_PATH", agents_file)

    app = create_app(engine=engine, storage_dir=tmp_path / "storage")
    with TestClient(app) as client:
        r_404 = client.post("/settings/profiles/nonexistent/activate")
        assert r_404.status_code == 404

        r_act = client.post("/settings/profiles/combo/activate", follow_redirects=False)
        assert r_act.status_code == 303
        assert "saved=profile" in r_act.headers["location"]

        html = client.get("/settings?tab=models&saved=profile").text
        assert "Perfis de Modelos (Combos)" in html
        assert "perfil ativo: combo" in html
        assert "gemini-4-neon-xhigh" in html
        assert "gpt-6.1-sol" in html
        assert "Roteamento por Tarefa" in html
