from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "web" / "templates"))

_BADGE_FOR = {
    "solved": "flag-ok",        # verde — única cor de sucesso
    "awaiting_approval": "warn",
    "running": "warn",
    "gave_up": "neutral",
    "rejected": "err",
}

_STATE_LABELS = {
    "running": "Executando",
    "awaiting_approval": "Aguardando aprovação",
    "solved": "Resolvido",
    "gave_up": "Desistiu",
    "rejected": "Interrompido",
}

_REPORT_STATUS_LABELS = {
    "SOLVED": "Resolvido",
    "NEED_HINT": "Precisa de pista",
    "FAILED": "Falhou",
}


def badge_for(state: str) -> str:
    """Badge CSS class for an agent session state."""
    return _BADGE_FOR.get(state, "neutral")


def state_label(state: str) -> str:
    """Human-readable Portuguese label for an agent session state."""
    return _STATE_LABELS.get(state, state)


def report_status_label(status: str) -> str:
    """Human-readable Portuguese label for a specialist report status."""
    return _REPORT_STATUS_LABELS.get(status, status)


templates.env.filters["badge_for"] = badge_for
templates.env.filters["state_label"] = state_label
templates.env.filters["report_status_label"] = report_status_label
