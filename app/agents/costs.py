from __future__ import annotations

from pathlib import Path
from typing import Iterable

import yaml

from .config import MODELS_PATH

# USD per 1M tokens: (input_usd, output_usd)
DEFAULT_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus": (15.0, 75.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku": (0.8, 4.0),
    "gpt-5": (1.25, 10.0),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4o": (2.5, 10.0),
    "o3": (2.0, 8.0),
    "o4-mini": (1.1, 4.4),
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.15, 0.6),
    "gemini-2.0-flash": (0.1, 0.4),
    "deepseek-r1": (0.55, 2.19),
    "deepseek-v3": (0.27, 1.1),
    "nemotron": (0.6, 1.8),
    "qwen": (0.4, 1.2),
    "llama": (0.2, 0.6),
}


def load_custom_pricing(models_path: Path | None = None) -> dict[str, tuple[float, float]]:
    merged = dict(DEFAULT_MODEL_PRICING)
    p = models_path or MODELS_PATH
    if not p.exists():
        return merged
    try:
        with open(p, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        for k, v in (raw.get("pricing") or {}).items():
            if isinstance(v, dict):
                inp = float(v.get("input", 0) or 0)
                out = float(v.get("output", 0) or 0)
                merged[str(k).strip().lower()] = (inp, out)
    except Exception:
        pass
    return merged


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    pricing: dict[str, tuple[float, float]] | None = None,
) -> float:
    table = pricing or DEFAULT_MODEL_PRICING
    low = (model or "").strip().lower()
    if not low or (input_tokens <= 0 and output_tokens <= 0):
        return 0.0

    rates: tuple[float, float] | None = table.get(low)
    if rates is None:
        # Longest substring match so gpt-4o-mini wins over gpt-4o
        for key in sorted(table.keys(), key=len, reverse=True):
            if key in low:
                rates = table[key]
                break
    if rates is None:
        return 0.0

    inp_rate, out_rate = rates
    cost = (max(0, int(input_tokens)) * inp_rate + max(0, int(output_tokens)) * out_rate) / 1_000_000.0
    return round(cost, 6)


def session_cost_usd(
    telemetry: Iterable[dict] | None,
    pricing: dict[str, tuple[float, float]] | None = None,
) -> float:
    total = 0.0
    for row in telemetry or []:
        if not isinstance(row, dict):
            continue
        if row.get("cost_usd") is not None:
            total += float(row["cost_usd"] or 0.0)
        else:
            total += estimate_cost_usd(
                str(row.get("model") or ""),
                int(row.get("input_tokens") or 0),
                int(row.get("output_tokens") or 0),
                pricing=pricing,
            )
    return round(total, 6)
