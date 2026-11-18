from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "web" / "templates"))

_BADGE_FOR = {
    "solved": "flag-ok",        # verde — única cor de sucesso
    "awaiting_approval": "warn",
    "running": "warn",
}


def badge_for(state: str) -> str:
    """Badge CSS class for an agent session state."""
    return _BADGE_FOR.get(state, "solved")  # gave_up/rejected → muted


templates.env.filters["badge_for"] = badge_for
