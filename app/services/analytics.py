from __future__ import annotations

import csv
import io
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from ..agents.costs import estimate_cost_usd
from ..models import AgentSessionRecord, Challenge, ChallengeStatus, Event


def _aware_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _solve_minutes(ch: Challenge) -> float | None:
    if ch.status != ChallengeStatus.solved or ch.solved_at is None:
        return None
    start = _aware_utc(ch.first_interaction) or _aware_utc(ch.created_at)
    end = _aware_utc(ch.solved_at)
    if start is None or end is None:
        return None
    mins = max(0.0, (end - start).total_seconds() / 60.0)
    return round(mins, 2)


def compute_analytics(
    session: Session,
    *,
    event_id: int | None = None,
    category: str | None = None,
    days: int | None = None,
) -> dict:
    """Aggregate deterministic model, category, and Jury×Specialist benchmark metrics."""
    cutoff: datetime | None = None
    if days is not None and int(days) > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))

    events = list(session.exec(select(Event).order_by(Event.id.desc())).all())
    ch_stmt = select(Challenge)
    if event_id is not None and int(event_id) > 0:
        ch_stmt = ch_stmt.where(Challenge.event_id == int(event_id))
    all_challenges = list(session.exec(ch_stmt).all())
    if category and category.strip():
        cat_clean = category.strip().lower()
        all_challenges = [c for c in all_challenges if (c.category or "misc").strip().lower() == cat_clean]

    if cutoff is not None:
        filtered_ch: list[Challenge] = []
        for c in all_challenges:
            ct = _aware_utc(c.created_at)
            if ct is None or ct >= cutoff:
                filtered_ch.append(c)
        all_challenges = filtered_ch

    ch_map = {c.id: c for c in all_challenges if c.id is not None}
    ch_ids = set(ch_map.keys())

    sess_records = list(session.exec(select(AgentSessionRecord)).all())
    if ch_ids:
        sess_records = [r for r in sess_records if r.challenge_id in ch_ids]
    elif event_id is not None or (category and category.strip()):
        sess_records = []

    if cutoff is not None:
        sess_records = [
            r for r in sess_records
            if (_aware_utc(r.created_at) is None or _aware_utc(r.created_at) >= cutoff)
        ]

    # Overview accumulators
    total_challenges = len(all_challenges)
    solved_challenges = sum(1 for c in all_challenges if c.status == ChallengeStatus.solved)
    solve_times = [m for c in all_challenges if (m := _solve_minutes(c)) is not None]

    total_input_tokens = 0
    total_output_tokens = 0
    total_cost_usd = 0.0

    model_acc: dict[str, dict] = defaultdict(lambda: {
        "calls": 0,
        "jury_calls": 0,
        "specialist_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_cost_usd": 0.0,
        "duration_sum": 0.0,
        "session_ids": set(),
        "solved_session_ids": set(),
    })
    pair_acc: dict[str, dict] = defaultdict(lambda: {
        "sessions": 0,
        "solved": 0,
        "total_cost_usd": 0.0,
    })
    cat_sessions: dict[str, list[AgentSessionRecord]] = defaultdict(list)
    session_cost_map: dict[str, float] = {}

    for rec in sess_records:
        ch = ch_map.get(rec.challenge_id)
        ch_cat = ((ch.category if ch else None) or "misc").strip().lower()
        cat_sessions[ch_cat].append(rec)
        is_solved = rec.state == "solved" or (ch is not None and ch.status == ChallengeStatus.solved)

        try:
            tel = json.loads(rec.telemetry_json or "[]")
            if not isinstance(tel, list):
                tel = []
        except Exception:
            tel = []

        sess_cost = 0.0
        jury_counter: Counter[str] = Counter()
        spec_counter: Counter[str] = Counter()

        for row in tel:
            if not isinstance(row, dict):
                continue
            model = str(row.get("model") or "").strip() or "unknown"
            role = str(row.get("role") or "specialist").lower()
            inp = max(0, int(row.get("input_tokens") or 0))
            out = max(0, int(row.get("output_tokens") or 0))
            dur = float(row.get("duration_s") or 0.0)
            cost = float(
                row.get("cost_usd")
                if row.get("cost_usd") is not None
                else estimate_cost_usd(model, inp, out)
            )
            sess_cost += cost
            total_input_tokens += inp
            total_output_tokens += out
            total_cost_usd += cost

            ma = model_acc[model]
            ma["calls"] += 1
            if role.startswith("jury"):
                ma["jury_calls"] += 1
                jury_counter[model] += 1
            else:
                ma["specialist_calls"] += 1
                spec_counter[model] += 1
            ma["input_tokens"] += inp
            ma["output_tokens"] += out
            ma["total_cost_usd"] += cost
            ma["duration_sum"] += dur
            ma["session_ids"].add(rec.id)
            if is_solved:
                ma["solved_session_ids"].add(rec.id)

            jm = str(row.get("jury_model") or "").strip()
            if jm:
                jury_counter[jm] += 1

        session_cost_map[rec.id] = sess_cost
        if tel:
            top_jury = jury_counter.most_common(1)[0][0] if jury_counter else "—"
            top_spec = spec_counter.most_common(1)[0][0] if spec_counter else "—"
            pair_key = f"{top_jury} × {top_spec}"
            pa = pair_acc[pair_key]
            pa["sessions"] += 1
            if is_solved:
                pa["solved"] += 1
            pa["total_cost_usd"] += sess_cost

    by_model: list[dict] = []
    for m, ma in model_acc.items():
        calls = ma["calls"]
        s_count = len(ma["session_ids"])
        s_solved = len(ma["solved_session_ids"])
        tot_cost = round(ma["total_cost_usd"], 6)
        by_model.append({
            "model": m,
            "calls": calls,
            "jury_calls": ma["jury_calls"],
            "specialist_calls": ma["specialist_calls"],
            "input_tokens": ma["input_tokens"],
            "output_tokens": ma["output_tokens"],
            "total_cost_usd": tot_cost,
            "avg_cost_usd": round(tot_cost / calls, 6) if calls else 0.0,
            "avg_duration_s": round(ma["duration_sum"] / calls, 2) if calls else 0.0,
            "sessions_count": s_count,
            "solved_sessions": s_solved,
            "solve_rate_pct": round(100.0 * s_solved / s_count, 1) if s_count else 0.0,
            "cost_per_solve_usd": round(tot_cost / s_solved, 4) if s_solved else None,
        })
    by_model.sort(key=lambda r: (r["solve_rate_pct"], r["calls"]), reverse=True)

    cat_groups: dict[str, list[Challenge]] = defaultdict(list)
    for c in all_challenges:
        cat_groups[(c.category or "misc").strip().lower()].append(c)

    by_category: list[dict] = []
    for cat_name in sorted(set(cat_groups.keys()) | set(cat_sessions.keys())):
        chs = cat_groups.get(cat_name, [])
        recs = cat_sessions.get(cat_name, [])
        tot_c = len(chs)
        sol_c = sum(1 for c in chs if c.status == ChallengeStatus.solved)
        c_times = [m for c in chs if (m := _solve_minutes(c)) is not None]
        cycles = [int(r.dispatch_count or 0) for r in recs]
        c_costs = [session_cost_map.get(r.id, 0.0) for r in recs]
        by_category.append({
            "category": cat_name,
            "total": tot_c,
            "solved": sol_c,
            "solve_rate_pct": round(100.0 * sol_c / tot_c, 1) if tot_c else 0.0,
            "avg_solve_minutes": round(sum(c_times) / len(c_times), 2) if c_times else None,
            "avg_cycles": round(sum(cycles) / len(cycles), 2) if cycles else 0.0,
            "avg_cost_usd": round(sum(c_costs) / len(c_costs), 4) if c_costs else 0.0,
        })
    by_category.sort(key=lambda r: (r["solve_rate_pct"], r["total"]), reverse=True)

    by_pair: list[dict] = []
    for pair_name, pa in pair_acc.items():
        n_s = pa["sessions"]
        n_sol = pa["solved"]
        tot_c = round(pa["total_cost_usd"], 6)
        by_pair.append({
            "pair": pair_name,
            "sessions": n_s,
            "solved": n_sol,
            "solve_rate_pct": round(100.0 * n_sol / n_s, 1) if n_s else 0.0,
            "total_cost_usd": tot_c,
            "cost_per_solve_usd": round(tot_c / n_sol, 4) if n_sol else None,
        })
    by_pair.sort(key=lambda r: (r["solve_rate_pct"], r["sessions"]), reverse=True)

    overview = {
        "total_challenges": total_challenges,
        "solved_challenges": solved_challenges,
        "solve_rate_pct": round(100.0 * solved_challenges / total_challenges, 1) if total_challenges else 0.0,
        "total_sessions": len(sess_records),
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_tokens": total_input_tokens + total_output_tokens,
        "total_cost_usd": round(total_cost_usd, 4),
        "avg_cost_per_solve_usd": round(total_cost_usd / solved_challenges, 4) if solved_challenges else 0.0,
        "avg_solve_minutes": round(sum(solve_times) / len(solve_times), 2) if solve_times else None,
    }
    return {
        "overview": overview,
        "by_model": by_model,
        "by_category": by_category,
        "by_pair": by_pair,
        "events": [{"id": e.id, "name": e.name} for e in events],
        "categories": sorted({(c.category or "misc").strip().lower() for c in all_challenges}),
    }


def analytics_to_csv(data: dict) -> str:
    """Serialize benchmark rankings into CSV tables."""
    buf = io.StringIO()
    w = csv.writer(buf)

    w.writerow(["section", "name", "calls_or_total", "solved", "solve_rate_pct", "total_cost_usd", "cost_per_solve_usd", "extra"])
    for m in data.get("by_model", []):
        w.writerow([
            "model", m["model"], m["calls"], m["solved_sessions"],
            m["solve_rate_pct"], m["total_cost_usd"],
            m["cost_per_solve_usd"] if m["cost_per_solve_usd"] is not None else "",
            f"tokens={m['input_tokens']}+{m['output_tokens']} dur={m['avg_duration_s']}s",
        ])
    for c in data.get("by_category", []):
        w.writerow([
            "category", c["category"], c["total"], c["solved"],
            c["solve_rate_pct"], c["avg_cost_usd"], "",
            f"avg_cycles={c['avg_cycles']} avg_min={c['avg_solve_minutes']}",
        ])
    for p in data.get("by_pair", []):
        w.writerow([
            "pair", p["pair"], p["sessions"], p["solved"],
            p["solve_rate_pct"], p["total_cost_usd"],
            p["cost_per_solve_usd"] if p["cost_per_solve_usd"] is not None else "",
            "",
        ])
    return buf.getvalue()
