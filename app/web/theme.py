from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_THEME = "crimson"


@dataclass(frozen=True)
class ThemeSpec:
    id: str
    name: str
    subtitle: str
    accent: str
    accent_bright: str
    bg_0: str
    bg_2: str


THEME_CATALOG: tuple[ThemeSpec, ...] = (
    ThemeSpec(
        id="crimson",
        name="Crimson Obsidian",
        subtitle="Preto terminal absoluto com carmesim tático (padrão KOTH)",
        accent="#e01b24",
        accent_bright="#ff3b44",
        bg_0="#020203",
        bg_2="#120306",
    ),
    ThemeSpec(
        id="emerald",
        name="Emerald Matrix",
        subtitle="Preto terminal absoluto com verde matrix",
        accent="#10b981",
        accent_bright="#34d399",
        bg_0="#020302",
        bg_2="#04150c",
    ),
    ThemeSpec(
        id="cobalt",
        name="Cobalt Stealth",
        subtitle="Preto terminal absoluto com azul cobalto",
        accent="#2563eb",
        accent_bright="#60a5fa",
        bg_0="#020305",
        bg_2="#061226",
    ),
    ThemeSpec(
        id="violet",
        name="Violet Phantom",
        subtitle="Preto terminal absoluto com roxo neon",
        accent="#8b5cf6",
        accent_bright="#a78bfa",
        bg_0="#020204",
        bg_2="#120824",
    ),
    ThemeSpec(
        id="amber",
        name="Amber Command",
        subtitle="Preto terminal absoluto com âmbar tático",
        accent="#f59e0b",
        accent_bright="#fbbf24",
        bg_0="#030201",
        bg_2="#171004",
    ),
)

VALID_THEME_IDS: set[str] = {t.id for t in THEME_CATALOG}


def _theme_file(storage_dir: Path | None) -> Path | None:
    if storage_dir is None:
        return None
    return Path(storage_dir) / "ui_theme.json"


def load_active_theme(storage_dir: Path | None = None) -> str:
    p = _theme_file(storage_dir)
    if p is None or not p.exists():
        return DEFAULT_THEME
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        theme_id = str(data.get("theme") or DEFAULT_THEME).strip().lower()
        return theme_id if theme_id in VALID_THEME_IDS else DEFAULT_THEME
    except Exception:
        return DEFAULT_THEME


def save_active_theme(theme_id: str, storage_dir: Path | None = None) -> str:
    chosen = (theme_id or "").strip().lower()
    if chosen not in VALID_THEME_IDS:
        raise ValueError(f"Tema inválido: {theme_id!r}")
    p = _theme_file(storage_dir)
    if p is not None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"theme": chosen}, indent=2), encoding="utf-8")
    return chosen


def theme_options() -> list[dict]:
    return [
        {
            "id": t.id,
            "name": t.name,
            "subtitle": t.subtitle,
            "accent": t.accent,
            "accent_bright": t.accent_bright,
            "bg_0": t.bg_0,
            "bg_2": t.bg_2,
        }
        for t in THEME_CATALOG
    ]
