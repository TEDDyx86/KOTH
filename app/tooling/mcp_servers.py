from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from ..config import BASE_DIR

DEFAULT_MCP_PATH = BASE_DIR / "config" / "mcp.yaml"


@dataclass
class McpServerSpec:
    name: str
    label: str
    category: str = "all"
    transport: str = "stdio"  # stdio | http | sse
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = False
    description: str = ""


def _read_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"servers": {}}
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        return {"servers": {}}
    raw.setdefault("servers", {})
    return raw


def _write_raw(path: Path, raw: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(raw, fh, sort_keys=False, allow_unicode=True)


def load_mcp_config(path: Path | None = None) -> dict[str, McpServerSpec]:
    cfg_path = path or DEFAULT_MCP_PATH
    raw = _read_raw(cfg_path)
    out: dict[str, McpServerSpec] = {}
    for name, entry in (raw.get("servers") or {}).items():
        s = entry or {}
        key = str(name).strip()
        if not key:
            continue
        transport = str(s.get("transport") or ("stdio" if s.get("command") else "http")).lower()
        out[key] = McpServerSpec(
            name=key,
            label=str(s.get("label") or key),
            category=str(s.get("category") or "all").lower(),
            transport=transport,
            command=s.get("command"),
            args=[str(a) for a in (s.get("args") or [])],
            env={str(k): str(v) for k, v in (s.get("env") or {}).items()},
            url=s.get("url"),
            headers={str(k): str(v) for k, v in (s.get("headers") or {}).items()},
            enabled=bool(s.get("enabled", False)),
            description=str(s.get("description") or ""),
        )
    return out


def toggle_mcp_server(name: str, enabled: bool | None = None, path: Path | None = None) -> bool:
    cfg_path = path or DEFAULT_MCP_PATH
    raw = _read_raw(cfg_path)
    servers = raw.setdefault("servers", {})
    if name not in servers:
        raise KeyError(f"MCP server '{name}' not found")
    cur = bool((servers[name] or {}).get("enabled", False))
    new_val = (not cur) if enabled is None else bool(enabled)
    servers[name]["enabled"] = new_val
    _write_raw(cfg_path, raw)
    return new_val


def add_mcp_server(
    name: str,
    *,
    label: str = "",
    category: str = "all",
    transport: str = "stdio",
    command: str = "",
    args: list[str] | None = None,
    url: str = "",
    description: str = "",
    enabled: bool = True,
    path: Path | None = None,
) -> McpServerSpec:
    clean_name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name.strip().lower()).strip("_")
    if not clean_name:
        raise ValueError("Nome de servidor MCP inválido")
    tr = transport.strip().lower()
    if tr not in ("stdio", "http", "sse"):
        raise ValueError(f"Transporte MCP inválido: {transport}")

    entry: dict[str, Any] = {
        "label": (label or clean_name).strip(),
        "category": (category or "all").strip().lower(),
        "transport": tr,
        "enabled": bool(enabled),
        "description": description.strip(),
    }
    if tr == "stdio":
        cmd = command.strip()
        if not cmd:
            raise ValueError("Servidor stdio requer um comando")
        entry["command"] = cmd
        entry["args"] = list(args or [])
        entry["env"] = {}
    else:
        u = url.strip()
        parsed = urlparse(u)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Servidor HTTP/SSE requer URL http(s):// válida")
        entry["url"] = u
        entry["headers"] = {}

    cfg_path = path or DEFAULT_MCP_PATH
    raw = _read_raw(cfg_path)
    raw.setdefault("servers", {})[clean_name] = entry
    _write_raw(cfg_path, raw)
    return load_mcp_config(cfg_path)[clean_name]


def delete_mcp_server(name: str, path: Path | None = None) -> bool:
    cfg_path = path or DEFAULT_MCP_PATH
    raw = _read_raw(cfg_path)
    servers = raw.get("servers") or {}
    if name not in servers:
        return False
    del servers[name]
    _write_raw(cfg_path, raw)
    return True


def mcp_status_rows(path: Path | None = None) -> list[dict[str, Any]]:
    specs = load_mcp_config(path)
    rows: list[dict[str, Any]] = []
    for name in sorted(specs):
        s = specs[name]
        if s.transport == "stdio":
            exe = shutil.which(s.command) if s.command else None
            ready = exe is not None
            target = " ".join([s.command or "", *s.args]).strip()
            detail = exe or f"{s.command} ausente no PATH"
        else:
            parsed = urlparse(s.url or "")
            ready = parsed.scheme in ("http", "https") and bool(parsed.netloc)
            target = s.url or ""
            detail = target or "URL ausente"
        rows.append({
            "name": s.name,
            "label": s.label,
            "category": s.category,
            "transport": s.transport,
            "enabled": s.enabled,
            "ready": ready,
            "target": target,
            "detail": detail,
            "description": s.description,
        })
    return rows


def _make_toolset(spec: McpServerSpec):
    from pydantic_ai.mcp import MCPToolset, SSETransport, StdioTransport, StreamableHttpTransport

    if spec.transport == "stdio":
        if not spec.command or shutil.which(spec.command) is None:
            return None
        merged_env = {**os.environ, **spec.env} if spec.env else None
        transport = StdioTransport(command=spec.command, args=list(spec.args), env=merged_env)
        return MCPToolset(transport, id=spec.name).prefixed(spec.name)

    if not spec.url:
        return None
    if spec.transport == "sse":
        transport = SSETransport(url=spec.url, headers=spec.headers or None)
    else:
        transport = StreamableHttpTransport(url=spec.url, headers=spec.headers or None)
    return MCPToolset(transport, id=spec.name).prefixed(spec.name)


def build_pydantic_toolsets(category: str | None = None, path: Path | None = None) -> list[Any]:
    """Build pydantic-ai MCPToolset instances for enabled MCP servers."""
    specs = load_mcp_config(path)
    cat = (category or "").strip().lower()
    toolsets: list[Any] = []
    for spec in specs.values():
        if not spec.enabled:
            continue
        if cat and spec.category not in ("all", cat):
            continue
        try:
            ts = _make_toolset(spec)
        except Exception:
            ts = None
        if ts is not None:
            toolsets.append(ts)
    return toolsets


async def _probe_async(spec: McpServerSpec, timeout_s: float = 8.0) -> tuple[bool, str]:
    ts = _make_toolset(spec)
    if ts is None:
        hint = f"comando '{spec.command}' não encontrado no PATH" if spec.transport == "stdio" else "URL inválida"
        return False, hint
    try:
        async def _check():
            async with ts:
                tools = await ts.list_tools()
                names = [getattr(t, "name", str(t)) for t in (tools or [])]
                return names

        names = await asyncio.wait_for(_check(), timeout=timeout_s)
        preview = ", ".join(names[:5]) + ("…" if len(names) > 5 else "")
        return True, f"MCP OK ({len(names)} tools: {preview or 'nenhuma'})"
    except Exception as exc:
        return False, f"falha ao conectar MCP '{spec.name}': {type(exc).__name__}: {str(exc)[:140]}"


def probe_mcp_server(name: str, path: Path | None = None, timeout_s: float = 8.0) -> tuple[bool, str]:
    specs = load_mcp_config(path)
    if name not in specs:
        return False, f"Servidor MCP '{name}' não encontrado"
    return asyncio.run(_probe_async(specs[name], timeout_s=timeout_s))


def koth_mcp_client_snippet(project_dir: Path | None = None) -> str:
    root = str((project_dir or BASE_DIR).resolve())
    cfg = {
        "mcpServers": {
            "koth-ctf": {
                "command": "python",
                "args": ["-m", "app.cli", "mcp"],
                "cwd": root,
            }
        }
    }
    return json.dumps(cfg, indent=2, ensure_ascii=False)
