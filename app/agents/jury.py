from pydantic_ai import Agent

from ..models import Challenge
from .context import jury_context
from .models import run_with_fallback
from .payloads import JuryDecision, SpecialistReport


def _render_reports(reports: list[SpecialistReport]) -> str:
    if not reports:
        return "(none yet)"
    lines = []
    for i, r in enumerate(reports, 1):
        lines.append(f"{i}. status={r.status} flag={r.flag_candidate or '-'} "
                     f"findings={'; '.join(r.findings) or '-'} next={r.next_step or '-'}")
    return "\n".join(lines)


def _extract_usage(result) -> dict | None:
    try:
        u = result.usage
    except Exception:
        return None
    if u is None:
        return None
    return {
        "input_tokens": getattr(u, "input_tokens", None) or getattr(u, "request_tokens", 0) or 0,
        "output_tokens": getattr(u, "output_tokens", None) or getattr(u, "response_tokens", 0) or 0,
        "total_tokens": getattr(u, "total_tokens", 0) or 0,
    }


class JuryAgent:
    def __init__(self, model, persona_text: str,
                 fallback_models: list | None = None) -> None:
        self.persona_text = persona_text
        self.models = [model, *(fallback_models or [])]
        self.agent = Agent(model, output_type=JuryDecision, system_prompt=persona_text)
        self.last_usage: dict | None = None

    def decide(self, session, challenge: Challenge,
               reports: list[SpecialistReport],
               extra_feedback: str | None = None) -> JuryDecision:
        prompt = (
            f"{jury_context(session, challenge)}\n\n"
            f"## Specialist reports so far\n{_render_reports(reports)}\n"
        )
        if extra_feedback:
            prompt += f"\n## Operator feedback (reject/adjust)\n{extra_feedback}\n"

        def _call(m):
            ag = self.agent if m is self.models[0] else Agent(
                m, output_type=JuryDecision, system_prompt=self.persona_text)
            return ag.run_sync(prompt)

        result = run_with_fallback(self.models, _call)
        self.last_usage = _extract_usage(result)
        return result.output
