import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

from app.main import create_app
from app.web.theme import THEME_CATALOG, load_active_theme, save_active_theme


def test_theme_persistence_and_catalog(tmp_path):
    assert [t.id for t in THEME_CATALOG] == ["crimson", "emerald", "cobalt", "violet", "amber"]
    storage_dir = tmp_path / "storage"
    assert load_active_theme(storage_dir) == "crimson"
    assert save_active_theme("cobalt", storage_dir) == "cobalt"
    assert load_active_theme(storage_dir) == "cobalt"
    with pytest.raises(ValueError):
        save_active_theme("unknown-color", storage_dir)
    with pytest.raises(ValueError):
        save_active_theme("", storage_dir)


def test_theme_switching_applies_across_all_pages_and_restarts(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'theme.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    storage_dir = tmp_path / "storage"

    app = create_app(engine=engine, storage_dir=storage_dir)
    with TestClient(app) as c:
        ev = c.post("/api/events", json={"name": "ThemeCTF"}).json()
        ch = c.post(
            f"/api/events/{ev['id']}/challenges",
            json={"title": "ColorChal", "category": "web", "points": 100},
        ).json()

        r_brand = c.get("/settings?tab=brand")
        assert r_brand.status_code == 200
        assert "brand-grid" not in r_brand.text
        assert "koth_hero_2.jpg" not in r_brand.text
        for tid in ("crimson", "emerald", "cobalt", "violet", "amber"):
            assert f'value="{tid}"' in r_brand.text

        # Invalid theme fails cleanly with 400
        assert c.post("/settings/theme", data={"theme": "neon-pink"}).status_code == 400

        # Switch through all non-default themes and confirm every UI route adopts data-theme
        for theme_id in ("emerald", "cobalt", "violet", "amber", "crimson"):
            r_set = c.post("/settings/theme", data={"theme": theme_id}, follow_redirects=False)
            assert r_set.status_code == 303
            assert r_set.headers["location"] == "/settings?tab=brand&saved=theme"

            for url in (
                "/",
                "/dashboard",
                f"/events/{ev['id']}",
                f"/challenges/{ch['id']}",
                f"/challenges/{ch['id']}/agent",
                "/settings?tab=brand",
            ):
                page = c.get(url)
                assert page.status_code == 200
                assert f'data-theme="{theme_id}"' in page.text

        # Leave on violet and simulate server restart
        c.post("/settings/theme", data={"theme": "violet"}, follow_redirects=False)

    app2 = create_app(engine=engine, storage_dir=storage_dir)
    with TestClient(app2) as c2:
        assert 'data-theme="violet"' in c2.get("/dashboard").text
        assert 'data-theme="violet"' in c2.get("/").text
