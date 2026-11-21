import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel


@dataclass(frozen=True)
class CliProviderSpec:
    name: str
    label: str
    executable: str
    login_hint: str
    strong_default: str
    fast_default: str
    models: tuple[str, ...]


CLI_PROVIDER_CATALOG: tuple[CliProviderSpec, ...] = (
    CliProviderSpec(
        name="cli_claude",
        label="Claude Code CLI (Claude Pro / Max)",
        executable="claude",
        login_hint="claude auth login",
        strong_default="sonnet",
        fast_default="haiku",
        models=("sonnet", "opus", "haiku", "default"),
    ),
    CliProviderSpec(
        name="cli_codex",
        label="OpenAI Codex CLI (ChatGPT Plus / Pro)",
        executable="codex",
        login_hint="codex login",
        strong_default="default",
        fast_default="default",
        models=("default", "o3", "o4-mini", "gpt-4.1"),
    ),
    CliProviderSpec(
        name="cli_gemini",
        label="Google Gemini CLI (Conta Google / AI Pro)",
        executable="gemini",
        login_hint="gemini",
        strong_default="gemini-2.5-pro",
        fast_default="gemini-2.5-flash",
        models=("gemini-2.5-pro", "gemini-2.5-flash", "default"),
    ),
)

CLI_CATALOG_BY_NAME: dict[str, CliProviderSpec] = {c.name: c for c in CLI_PROVIDER_CATALOG}


def is_cli_provider(provider_name: str) -> bool:
    return provider_name in CLI_CATALOG_BY_NAME


def build_cli_command(
    provider_name: str,
    exe_path: str,
    model_name: str,
    *,
    output_file: Path | None = None,
) -> list[str]:
    model = (model_name or "").strip()
    use_model = model and model != "default"

    if provider_name == "cli_claude":
        cmd = [
            exe_path,
            "-p",
            "--output-format", "text",
            "--no-session-persistence",
            "--tools", "",
        ]
        if use_model:
            cmd.extend(["--model", model])
        return cmd

    if provider_name == "cli_codex":
        cmd = [exe_path, "exec", "--skip-git-repo-check", "-s", "read-only"]
        if use_model:
            cmd.extend(["-m", model])
        if output_file is not None:
            cmd.extend(["-o", str(output_file)])
        cmd.append("-")
        return cmd

    if provider_name == "cli_gemini":
        cmd = [exe_path, "-p", " "]
        if use_model:
            cmd.extend(["-m", model])
        return cmd

    raise ValueError(f"unknown CLI provider: {provider_name}")


def _run_cli_prompt(
    provider_name: str,
    model_name: str,
    prompt: str,
    *,
    timeout_s: int = 180,
) -> str:
    spec = CLI_PROVIDER_CATALOG[0]
    spec = CLI_CATALOG_BY_NAME[provider_name]
    exe = shutil.which(spec.executable)
    if exe is None:
        from .models import ProviderUnavailable

        raise ProviderUnavailable(
            f"CLI '{spec.executable}' não encontrado no PATH (instale e rode '{spec.login_hint}')"
        )

    out_path: Path | None = None
    if provider_name == "cli_codex":
        fd, raw_path = tempfile.mkstemp(prefix="koth_codex_", suffix=".txt")
        os.close(fd)
        out_path = Path(raw_path)

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        cmd = build_cli_command(provider_name, exe, model_name, output_file=out_path)
        proc = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_s,
            creationflags=creationflags,
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if out_path is not None and out_path.exists():
            file_txt = out_path.read_text(encoding="utf-8", errors="replace").strip()
            if file_txt:
                stdout = file_txt

        if proc.returncode != 0:
            err_msg = (stderr or stdout or f"exit code {proc.returncode}")[:400]
            raise RuntimeError(f"{spec.executable} CLI failed: {err_msg}")
        return stdout
    finally:
        if out_path is not None and out_path.exists():
            try:
                out_path.unlink()
            except OSError:
                pass


def _extract_json_object(text: str) -> dict | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            stripped = "\n".join(lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:]).strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text, idx)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def _render_cli_prompt(messages: list[ModelMessage], info: AgentInfo) -> str:
    sections: list[str] = []
    if info.instructions:
        sections.append(f"## System Instructions\n{info.instructions}")

    for msg in messages:
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, SystemPromptPart):
                    sections.append(f"## System\n{part.content}")
                elif isinstance(part, UserPromptPart):
                    if isinstance(part.content, str):
                        sections.append(f"## User\n{part.content}")
                    else:
                        txt = [str(item) for item in part.content if isinstance(item, str)]
                        if txt:
                            sections.append("## User\n" + "\n".join(txt))
                elif isinstance(part, ToolReturnPart):
                    sections.append(f"## Tool Result ({part.tool_name})\n{part.content}")
                elif isinstance(part, RetryPromptPart):
                    sections.append(f"## Retry Required\n{part.content}")
        elif isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, TextPart):
                    sections.append(f"## Assistant\n{part.content}")
                elif isinstance(part, ToolCallPart):
                    sections.append(f"## Assistant Tool Call ({part.tool_name})\n{part.args}")

    if info.function_tools and info.output_tools:
        out_tool = info.output_tools[0]
        tool_schemas = [
            {"name": t.name, "description": t.description, "parameters": t.parameters_json_schema}
            for t in info.function_tools
        ]
        sections.append(
            "## Response Format (STRICT JSON ONLY)\n"
            "Do NOT use any built-in shell/file tools. You may ONLY respond with a single JSON object:\n"
            f"1) To call a framework tool: {{\"tool\": \"<tool_name>\", \"args\": {{...}}}}\n"
            f"   Available tools: {json.dumps(tool_schemas)}\n"
            f"2) When finished, return the final result: {{\"output\": <matches schema>}}\n"
            f"   Output schema: {json.dumps(out_tool.parameters_json_schema)}"
        )
    elif info.output_tools:
        out_tool = info.output_tools[0]
        sections.append(
            "## Response Format (STRICT JSON ONLY)\n"
            "Return ONLY a single valid JSON object (no markdown fences, no commentary) matching this JSON Schema:\n"
            f"{json.dumps(out_tool.parameters_json_schema)}"
        )

    return "\n\n".join(sections)


def make_cli_model(provider_name: str, model_name: str, timeout_s: int = 180) -> FunctionModel:
    """Create a pydantic-ai FunctionModel backed by an authenticated local CLI."""
    spec = CLI_CATALOG_BY_NAME[provider_name]
    if shutil.which(spec.executable) is None:
        from .models import ProviderUnavailable

        raise ProviderUnavailable(
            f"CLI '{spec.executable}' não encontrado no PATH (instale e rode '{spec.login_hint}')"
        )

    def _fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = _render_cli_prompt(messages, info)
        raw = _run_cli_prompt(provider_name, model_name, prompt, timeout_s=timeout_s)

        if info.function_tools and info.output_tools:
            parsed = _extract_json_object(raw) or {}
            if "tool" in parsed and "args" in parsed:
                return ModelResponse(parts=[
                    ToolCallPart(tool_name=str(parsed["tool"]), args=json.dumps(parsed["args"]))
                ])
            out_payload = parsed.get("output", parsed)
            return ModelResponse(parts=[
                ToolCallPart(tool_name=info.output_tools[0].name, args=json.dumps(out_payload))
            ])

        if info.output_tools:
            parsed = _extract_json_object(raw)
            payload_str = json.dumps(parsed) if parsed is not None else raw
            return ModelResponse(parts=[
                ToolCallPart(tool_name=info.output_tools[0].name, args=payload_str)
            ])

        return ModelResponse(parts=[TextPart(content=raw)])

    return FunctionModel(_fn, model_name=f"{provider_name}:{model_name or spec.strong_default}")


def cli_provider_status_rows() -> list[dict]:
    rows: list[dict] = []
    for spec in CLI_PROVIDER_CATALOG:
        path = shutil.which(spec.executable)
        rows.append({
            "name": spec.name,
            "label": spec.label,
            "executable": spec.executable,
            "installed": path is not None,
            "path": path or "",
            "login_hint": spec.login_hint,
            "strong_default": spec.strong_default,
            "fast_default": spec.fast_default,
            "models": list(spec.models),
        })
    return rows


def check_cli_provider_connection(provider_name: str, timeout_s: int = 30) -> tuple[bool, str]:
    spec = CLI_CATALOG_BY_NAME.get(provider_name)
    if spec is None:
        return False, "provedor CLI desconhecido"
    exe = shutil.which(spec.executable)
    if exe is None:
        return False, f"'{spec.executable}' não encontrado no PATH"
    try:
        out = _run_cli_prompt(
            provider_name,
            spec.fast_default,
            "Respond with ONLY the single word: PONG",
            timeout_s=timeout_s,
        )
        if "pong" in out.lower():
            return True, f"{spec.executable} autenticado e respondendo"
        return True, f"{spec.executable} respondeu ({out[:40]})"
    except Exception as exc:
        msg = str(exc)
        low = msg.lower()
        if any(k in low for k in ("login", "auth", "unauthorized", "credential", "sign in")):
            return False, f"sem sessão — rode '{spec.login_hint}' no terminal"
        return False, msg[:120]
