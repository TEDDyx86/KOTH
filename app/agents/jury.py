import time

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
        de = f" dead_ends={'; '.join(r.dead_ends)}" if getattr(r, "dead_ends", None) else ""
        lines.append(
            f"{i}. status={r.status} flag={r.flag_candidate or '-'} "
            f"findings={'; '.join(r.findings) or '-'}{de} next={r.next_step or '-'}"
        )
    return "\n".join(lines)


def _extract_usage(result) -> dict | None:
    u = getattr(result, "usage", None)
    if u is None:
        return None
    if not hasattr(u, "total_tokens") and callable(u):
        try:
            u = u()
        except Exception:
            return None
    if u is None:
        return None
    inp = int(getattr(u, "input_tokens", None) or getattr(u, "request_tokens", 0) or 0)
    out = int(getattr(u, "output_tokens", None) or getattr(u, "response_tokens", 0) or 0)
    tot = int(getattr(u, "total_tokens", 0) or (inp + out))
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": tot,
    }


class JuryAgent:
    def __init__(self, model, persona_text: str,
                 fallback_models: list | None = None,
                 output_retries: int = 4) -> None:
        self.persona_text = persona_text
        self.output_retries = output_retries
        self.models = [model, *(fallback_models or [])]
        self.agent = Agent(model, output_type=JuryDecision,
                           system_prompt=persona_text,
                           retries=output_retries)
        self.last_usage: dict | None = None
        self.last_metrics: dict | None = None

    def decide(self, session, challenge: Challenge,
               reports: list[SpecialistReport],
               extra_feedback: str | None = None,
               *,
               blackboard: dict | None = None) -> JuryDecision:
        prompt = (
            f"{jury_context(session, challenge, blackboard=blackboard)}\n\n"
            f"## Specialist reports so far\n{_render_reports(reports)}\n"
        )
        if extra_feedback:
            prompt += f"\n## Operator feedback (reject/adjust)\n{extra_feedback}\n"

        used_model = getattr(self.models[0], "model_name", str(self.models[0]))
        t0 = time.monotonic()

        def _call(m):
            nonlocal used_model
            used_model = getattr(m, "model_name", str(m))
            ag = self.agent if m is self.models[0] else Agent(
                m, output_type=JuryDecision, system_prompt=self.persona_text,
                retries=self.output_retries)
            return ag.run_sync(prompt)

        result = run_with_fallback(self.models, _call)
        duration_s = round(time.monotonic() - t0, 2)
        self.last_usage = _extract_usage(result)
        u = self.last_usage or {}
        self.last_metrics = {
            "role": "jury",
            "model": used_model,
            "input_tokens": int(u.get("input_tokens", 0)),
            "output_tokens": int(u.get("output_tokens", 0)),
            "total_tokens": int(u.get("total_tokens", 0)),
            "duration_s": duration_s,
        }
        return result.output
