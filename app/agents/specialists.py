from dataclasses import dataclass

from pydantic_ai import Agent, ModelRetry, RunContext

from ..tooling.runner import ToolRunner
from .context import specialist_context
from .payloads import SpecialistReport, TaskOrder


@dataclass
class RunDeps:
    runner: ToolRunner
    session: object
    challenge: object
    order: TaskOrder


class SpecialistRunner:
    def __init__(self, model, persona_text: str) -> None:
        agent = Agent(model, deps_type=RunDeps, output_type=SpecialistReport,
                      system_prompt=persona_text)

        @agent.tool
        def run_tool(ctx: RunContext[RunDeps], tool: str, args: list[str]) -> str:
            """Execute an allowlisted tool against the challenge via the sandbox.
            Returns the captured output (truncated)."""
            try:
                res = ctx.deps.runner.run(ctx.deps.session, ctx.deps.challenge,
                                          tool, args)
            except Exception as exc:  # allowlist/guard/target violations
                raise ModelRetry(f"tool run rejected: {exc}. "
                                 "Use an allowlisted tool and only the challenge target.")
            return (f"rc={res.returncode} truncated={res.truncated}\n"
                    f"--- stdout ---\n{res.stdout[:4000]}\n"
                    f"--- stderr ---\n{res.stderr[:1000]}")

        self.agent = agent

    def run(self, session, challenge, order: TaskOrder, runner: ToolRunner) -> SpecialistReport:
        deps = RunDeps(runner=runner, session=session, challenge=challenge, order=order)
        prompt = specialist_context(session, challenge, order)
        result = self.agent.run_sync(prompt, deps=deps)
        return result.output
