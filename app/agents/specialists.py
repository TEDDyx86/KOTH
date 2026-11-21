from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import Agent, BinaryContent, ModelRetry, RunContext
from sqlmodel import select

from ..models import Attachment, AttachmentType
from ..tooling.runner import ToolRunner
from .context import specialist_context
from .models import run_with_fallback
from .payloads import SpecialistReport, TaskOrder

_IMAGE_MIMES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_MAX_IMAGE_BYTES = 4 * 1024 * 1024


@dataclass
class RunDeps:
    runner: ToolRunner
    session: object
    challenge: object
    order: TaskOrder
    tool_calls: int = 0


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


def _forensics_images(session, challenge, runner: ToolRunner | None) -> list[BinaryContent]:
    """Load image attachments as BinaryContent for multimodal forensics analysis."""
    storage = getattr(runner, "storage", None)
    if session is None or storage is None:
        return []
    rows = list(session.exec(select(Attachment).where(
        Attachment.challenge_id == challenge.id,
        Attachment.type != AttachmentType.output,
    )).all())
    images: list[BinaryContent] = []
    for att in rows:
        ext = Path(att.name).suffix.lower()
        media_type = _IMAGE_MIMES.get(ext)
        if not media_type:
            continue
        p = storage.storage_dir / att.path
        if not p.exists():
            continue
        data = p.read_bytes()
        if len(data) <= _MAX_IMAGE_BYTES:
            images.append(BinaryContent(data=data, media_type=media_type))
        if len(images) >= 3:
            break
    return images


def _build_specialist_agent(model, persona_text: str, max_tool_retries: int) -> Agent:
    agent = Agent(model, deps_type=RunDeps, output_type=SpecialistReport,
                  system_prompt=persona_text)

    @agent.tool(retries=max_tool_retries)
    def run_tool(ctx: RunContext[RunDeps], tool: str, args: list[str]) -> str:
        """Execute an allowlisted tool against the challenge via the sandbox.
        Returns the captured output (truncated)."""
        limit = max(int(ctx.deps.order.max_tool_runs or 1), 1)
        if ctx.deps.tool_calls >= limit:
            raise ModelRetry(
                f"tool call budget ({limit}) reached — do not call more tools, "
                "return your SpecialistReport now."
            )
        ctx.deps.tool_calls += 1
        allowed_list = ", ".join(sorted(ctx.deps.runner.settings.tools))
        try:
            res = ctx.deps.runner.run(ctx.deps.session, ctx.deps.challenge,
                                      tool, args)
        except Exception as exc:  # allowlist/guard/target violations
            raise ModelRetry(f"tool run rejected: {type(exc).__name__}: {exc}. "
                             f"Allowed tools: {allowed_list}. Retry with a fixed call.")
        return (f"rc={res.returncode} truncated={res.truncated}\n"
                f"--- stdout ---\n{res.stdout[:4000]}\n"
                f"--- stderr ---\n{res.stderr[:1000]}")

    return agent


class SpecialistRunner:
    def __init__(self, model, persona_text: str, max_tool_retries: int = 4,
                 fallback_models: list | None = None) -> None:
        self.persona_text = persona_text
        self.max_tool_retries = max_tool_retries
        self.models = [model, *(fallback_models or [])]
        self.agent = _build_specialist_agent(model, persona_text, max_tool_retries)
        self.last_usage: dict | None = None

    def run(self, session, challenge, order: TaskOrder, runner: ToolRunner) -> SpecialistReport:
        deps = RunDeps(runner=runner, session=session, challenge=challenge, order=order)
        text_prompt = specialist_context(session, challenge, order)
        images = _forensics_images(session, challenge, runner) if order.category.lower() == "forensics" else []
        prompt_input: str | list = [text_prompt, *images] if images else text_prompt

        def _call(m):
            deps.tool_calls = 0
            ag = self.agent if m is self.models[0] else _build_specialist_agent(
                m, self.persona_text, self.max_tool_retries)
            return ag.run_sync(prompt_input, deps=deps)

        result = run_with_fallback(self.models, _call)
        self.last_usage = _extract_usage(result)
        return result.output
