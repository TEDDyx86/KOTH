from dataclasses import dataclass, field
from pathlib import Path
import threading

from pydantic_ai import Agent, BinaryContent, ModelRetry, RunContext
from pydantic_ai.messages import ToolReturnPart
from sqlmodel import select

from ..knowledge.index import DEFAULT_KNOWLEDGE_ROOT, KnowledgeIndex
from ..models import Attachment, AttachmentType
from ..tooling.koth_mcp import safe_sandbox_file
from ..tooling.mcp_servers import build_pydantic_toolsets
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
    lock: threading.RLock = field(default_factory=threading.RLock)


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


def _extract_tool_outputs(result) -> list[str]:
    """Collect serialized ToolReturnPart payloads produced during an agent run."""
    outputs: list[str] = []
    all_msgs = getattr(result, "all_messages", None)
    if not callable(all_msgs):
        return outputs
    try:
        messages = all_msgs()
    except Exception:
        return outputs
    for msg in messages or []:
        for part in getattr(msg, "parts", None) or []:
            if isinstance(part, ToolReturnPart) or getattr(part, "part_kind", None) == "tool-return":
                tname = getattr(part, "tool_name", "") or ""
                if tname == "final_result" or tname.startswith("final_result_"):
                    continue
                try:
                    outputs.append(part.model_response_str())
                except Exception:
                    outputs.append(str(getattr(part, "content", "")))
    return outputs


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


def _build_specialist_agent(
    model,
    persona_text: str,
    max_tool_retries: int,
    *,
    toolsets: list | None = None,
) -> Agent:
    kwargs: dict = {
        "deps_type": RunDeps,
        "output_type": SpecialistReport,
        "system_prompt": persona_text,
        "retries": max(max_tool_retries, 4),
        "max_concurrency": 1,
    }
    if toolsets:
        kwargs["toolsets"] = toolsets
    agent = Agent(model, **kwargs)

    @agent.tool(retries=max_tool_retries)
    def run_tool(ctx: RunContext[RunDeps], tool: str, args: list[str]) -> str:
        """Execute an allowlisted tool against the challenge via the sandbox.
        Returns captured stdout/stderr (truncated)."""
        with ctx.deps.lock:
            limit = max(int(ctx.deps.order.max_tool_runs or 1), 1)
            if ctx.deps.tool_calls >= limit:
                raise ModelRetry(
                    f"tool call budget ({limit}) reached — do not call more tools, "
                    "return your SpecialistReport now."
                )
            ctx.deps.tool_calls += 1
            allowed_list = ", ".join(sorted(ctx.deps.runner.settings.tools))
            try:
                res = ctx.deps.runner.run(ctx.deps.session, ctx.deps.challenge, tool, args)
            except Exception as exc:  # allowlist/guard/target violations
                if ctx.deps.session is not None and hasattr(ctx.deps.session, "rollback"):
                    try:
                        ctx.deps.session.rollback()
                    except Exception:
                        pass
                raise ModelRetry(
                    f"tool run rejected: {type(exc).__name__}: {exc}. "
                    f"Allowed tools: {allowed_list}. Retry with a fixed call."
                )
        return (
            f"rc={res.returncode} truncated={res.truncated}\n"
            f"--- stdout ---\n{res.stdout[:4000]}\n"
            f"--- stderr ---\n{res.stderr[:1000]}"
        )

    @agent.tool(retries=max_tool_retries)
    def run_python_script(ctx: RunContext[RunDeps], filename: str, code: str) -> str:
        """Write a Python script inside the challenge sandbox workdir (/work) and execute it.
        Ideal for pwntools exploits, Z3/SymPy crypto solvers, Scapy PCAP parsing, and HTTP scripts."""
        with ctx.deps.lock:
            limit = max(int(ctx.deps.order.max_tool_runs or 1), 1)
            if ctx.deps.tool_calls >= limit:
                raise ModelRetry(
                    f"tool call budget ({limit}) reached — do not call more tools, "
                    "return your SpecialistReport now."
                )
            ctx.deps.tool_calls += 1
            try:
                workdir = ctx.deps.runner.sandbox_workdir(ctx.deps.challenge)
                target = safe_sandbox_file(workdir, filename or "solve.py")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(code, encoding="utf-8")
                rel = str(target.relative_to(workdir)).replace("\\", "/")
                res = ctx.deps.runner.run(ctx.deps.session, ctx.deps.challenge, "python", [rel])
            except Exception as exc:
                if ctx.deps.session is not None and hasattr(ctx.deps.session, "rollback"):
                    try:
                        ctx.deps.session.rollback()
                    except Exception:
                        pass
                raise ModelRetry(f"run_python_script failed: {type(exc).__name__}: {exc}")
        return (
            f"script={rel} rc={res.returncode} truncated={res.truncated}\n"
            f"--- stdout ---\n{res.stdout[:4000]}\n"
            f"--- stderr ---\n{res.stderr[:1000]}"
        )

    @agent.tool(retries=max_tool_retries)
    def list_sandbox_files(ctx: RunContext[RunDeps]) -> str:
        """List files currently in the challenge sandbox workdir (/work)."""
        with ctx.deps.lock:
            ctx.deps.runner.stage_downloads(ctx.deps.session, ctx.deps.challenge)
            workdir = ctx.deps.runner.sandbox_workdir(ctx.deps.challenge)
        items: list[str] = []
        for p in sorted(workdir.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(workdir)).replace("\\", "/")
                items.append(f"{rel} ({p.stat().st_size} bytes)")
        return "\n".join(items[:80]) if items else "(empty workdir)"

    @agent.tool(retries=max_tool_retries)
    def read_sandbox_file(
        ctx: RunContext[RunDeps], filename: str, offset: int = 0, max_bytes: int = 4000
    ) -> str:
        """Read a slice of text (or hex summary for binary) from a file inside the sandbox workdir."""
        with ctx.deps.lock:
            ctx.deps.runner.stage_downloads(ctx.deps.session, ctx.deps.challenge)
            workdir = ctx.deps.runner.sandbox_workdir(ctx.deps.challenge)
        target = safe_sandbox_file(workdir, filename)
        if not target.exists():
            return f"file not found: {filename}"
        start = max(0, int(offset))
        cap = max(1, min(int(max_bytes), 16000))
        raw = target.read_bytes()
        chunk = raw[start:start + cap]
        try:
            text = chunk.decode("utf-8")
            return f"[text {start}..{start + len(chunk)} / {len(raw)}b]\n{text}"
        except UnicodeDecodeError:
            hex_preview = chunk[:512].hex(" ")
            return f"[binary {start}..{start + len(chunk)} / {len(raw)}b hex]\n{hex_preview}"

    @agent.tool(retries=max_tool_retries)
    def search_arsenal(ctx: RunContext[RunDeps], query: str, category: str = "") -> str:
        """Search the local FTS5 CTF knowledge base (ctf-skills + dispatch) for exploit techniques."""
        if ctx.deps.session is None:
            return "(knowledge unavailable without DB session)"
        with ctx.deps.lock:
            idx = KnowledgeIndex(ctx.deps.session, knowledge_root=DEFAULT_KNOWLEDGE_ROOT)
            cat = (category or ctx.deps.order.category or "").strip().lower() or None
            hits = idx.search(query.strip(), limit=4, category=cat) or idx.search(query.strip(), limit=4)
        if not hits:
            return f"no arsenal hits for {query!r}"
        return "\n".join(f"- {h['title']} ({h['path']}): {h['snippet']}" for h in hits)

    return agent


class SpecialistRunner:
    def __init__(
        self,
        model,
        persona_text: str,
        max_tool_retries: int = 4,
        fallback_models: list | None = None,
        category: str | None = None,
    ) -> None:
        self.persona_text = persona_text
        self.max_tool_retries = max_tool_retries
        self.category = category
        self.models = [model, *(fallback_models or [])]
        self.agent = _build_specialist_agent(model, persona_text, max_tool_retries)
        self.last_usage: dict | None = None
        self.last_tool_outputs: list[str] = []

    def run(self, session, challenge, order: TaskOrder, runner: ToolRunner) -> SpecialistReport:
        self.last_tool_outputs = []
        deps = RunDeps(runner=runner, session=session, challenge=challenge, order=order)
        text_prompt = specialist_context(session, challenge, order)
        allowed_tools = ", ".join(sorted(runner.settings.tools))
        text_prompt += f"\n\n## Available sandbox tools\n{allowed_tools}"
        images = _forensics_images(session, challenge, runner) if order.category.lower() == "forensics" else []
        prompt_input: str | list = [text_prompt, *images] if images else text_prompt

        cat = self.category or order.category
        mcp_toolsets = build_pydantic_toolsets(category=cat)

        def _call(m):
            deps.tool_calls = 0
            if m is self.models[0] and not mcp_toolsets:
                return self.agent.run_sync(prompt_input, deps=deps)
            ag = _build_specialist_agent(
                m,
                self.persona_text,
                self.max_tool_retries,
                toolsets=mcp_toolsets or None,
            )
            try:
                return ag.run_sync(prompt_input, deps=deps)
            except Exception:
                if mcp_toolsets:
                    # Fallback cleanly if an external MCP server is unreachable mid-run
                    fallback_ag = _build_specialist_agent(m, self.persona_text, self.max_tool_retries)
                    return fallback_ag.run_sync(prompt_input, deps=deps)
                raise

        result = run_with_fallback(self.models, _call)
        self.last_usage = _extract_usage(result)
        self.last_tool_outputs = _extract_tool_outputs(result)
        return result.output
