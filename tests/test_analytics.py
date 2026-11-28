import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.database import configure_sqlite_engine
from app.main import create_app
from app.models import AgentSessionRecord, Challenge, ChallengeStatus, Event
from app.services.analytics import analytics_to_csv, compute_analytics


def _db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'analytics.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    return engine


def test_analytics_empty_db_no_zero_division(tmp_path):
    engine = _db(tmp_path)
    with Session(engine) as session:
        data = compute_analytics(session)
        assert data["overview"]["total_challenges"] == 0
        assert data["overview"]["solve_rate_pct"] == 0.0
        assert data["overview"]["total_cost_usd"] == 0.0
        assert data["by_model"] == []
        assert data["by_category"] == []
        assert data["by_pair"] == []
        csv_text = analytics_to_csv(data)
        assert "section,name" in csv_text


def test_analytics_aggregation_filters_and_routes(tmp_path):
    engine = _db(tmp_path)
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        ev1 = Event(name="AlphaCTF")
        ev2 = Event(name="BetaCTF")
        session.add(ev1)
        session.add(ev2)
        session.commit()
        session.refresh(ev1)
        session.refresh(ev2)

        ch1 = Challenge(
            event_id=ev1.id, title="SQLi1", category="web", points=200,
            status=ChallengeStatus.solved,
            first_interaction=now - timedelta(minutes=12),
            solved_at=now - timedelta(minutes=2),
        )
        ch2 = Challenge(
            event_id=ev2.id, title="Rev1", category="reversing", points=500,
            status=ChallengeStatus.open,
            created_at=now - timedelta(days=20),
        )
        session.add(ch1)
        session.add(ch2)
        session.commit()
        session.refresh(ch1)
        session.refresh(ch2)

        rec1 = AgentSessionRecord(
            id="s_ana_1",
            challenge_id=ch1.id,
            state="solved",
            dispatch_count=2,
            telemetry_json=json.dumps([
                {
                    "cycle": 1, "role": "specialist:web", "model": "gpt-4o-mini",
                    "jury_model": "gpt-4.1",
                    "input_tokens": 1000, "output_tokens": 500,
                    "duration_s": 2.0, "cost_usd": 0.012,
                },
            ]),
        )
        rec2 = AgentSessionRecord(
            id="s_ana_2",
            challenge_id=ch2.id,
            state="gave_up",
            dispatch_count=4,
            created_at=now - timedelta(days=20),
            telemetry_json=json.dumps([
                {
                    "cycle": 1, "role": "specialist:reversing", "model": "claude-sonnet",
                    "jury_model": "claude-opus",
                    "input_tokens": 2000, "output_tokens": 1000,
                    "duration_s": 5.0, "cost_usd": 0.030,
                },
            ]),
        )
        session.add(rec1)
        session.add(rec2)
        session.commit()

        full = compute_analytics(session)
        assert full["overview"]["total_challenges"] == 2
        assert full["overview"]["solved_challenges"] == 1
        assert full["overview"]["solve_rate_pct"] == 50.0
        assert round(full["overview"]["total_cost_usd"], 4) == 0.042
        assert full["overview"]["avg_solve_minutes"] == 10.0
        assert len(full["by_model"]) == 2
        assert any(p["pair"] == "gpt-4.1 × gpt-4o-mini" and p["solve_rate_pct"] == 100.0 for p in full["by_pair"])

        # Filter by 7 days excludes Rev1 (20 days old)
        recent = compute_analytics(session, days=7)
        assert recent["overview"]["total_challenges"] == 1
        assert recent["overview"]["solved_challenges"] == 1

        # Filter by event_id
        ev2_only = compute_analytics(session, event_id=ev2.id)
        assert ev2_only["overview"]["total_challenges"] == 1
        assert ev2_only["by_category"][0]["category"] == "reversing"

    app = create_app(engine=engine, storage_dir=tmp_path / "storage")
    with TestClient(app) as client:
        r_html = client.get("/analytics")
        assert r_html.status_code == 200
        assert "Benchmark &amp; Analytics" in r_html.text
        assert "gpt-4o-mini" in r_html.text

        r_json = client.get("/api/analytics")
        assert r_json.status_code == 200
        payload = r_json.json()
        assert payload["overview"]["total_challenges"] == 2

        r_csv = client.get("/api/analytics?format=csv")
        assert r_csv.status_code == 200
        assert "text/csv" in r_csv.headers.get("content-type", "")
        assert "gpt-4o-mini" in r_csv.text
