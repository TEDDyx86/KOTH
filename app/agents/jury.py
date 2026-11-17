from pydantic_ai import Agent, RunContext

from ..models import Attempt, Challenge
from .context import jury_context, specialist_context
from .payloads import JuryDecision, SpecialistReport, TaskOrder


def _render_reports(reports: list[SpecialistReport]) -> str:
    if not reports:
        return "(none yet)"
    lines = []
    for i, r in enumerate(reports, 1):
        lines.append(f"{i}. status={r.status} flag={r.flag_candidate or '-'} "
                     f"findings={'; '.join(r.findings) or '-'} next={r.next_step or '-'}")
    return "\n".join(lines)


class JuryAgent:
    def __init__(self, model, persona_text: str) -> None:
        self.agent = Agent(model, output_type=JuryDecision, system_prompt=persona_text)

    def decide(self, session, challenge: Challenge,
               reports: list[SpecialistReport],
               extra_feedback: str | None = None) -> JuryDecision:
        prompt = (
            f"{jury_context(session, challenge)}\n\n"
            f"## Specialist reports so far\n{_render_reports(reports)}\n"
        )
        if extra_feedback:
            prompt += f"\n## Operator feedback (reject/adjust)\n{extra_feedback}\n"
        result = self.agent.run_sync(prompt)
        return result.output
