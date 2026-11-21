from __future__ import annotations

import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from sqlmodel import Session, select

from ..agents.recon import detect_file_type
from ..config import settings
from ..database import default_engine
from ..knowledge.index import DEFAULT_KNOWLEDGE_ROOT, KnowledgeIndex
from ..models import Challenge
from ..services.storage import StorageService
from .config import load_tools_config
from .doctor import inventory
from .runner import ToolRunner
from .sandbox import current_sandbox_mode, docker_status


def safe_sandbox_file(workdir: Path, filename: str) -> Path:
    """Resolve `filename` inside `workdir`, rejecting path traversal."""
    clean = (filename or "").replace("\\", "/").strip().lstrip("/")
    if not clean:
        raise ValueError("filename não pode ser vazio")
    target = (workdir / clean).resolve()
    root = workdir.resolve()
    if root not in target.parents and target != root:
        raise ValueError(f"caminho inválido fora do sandbox: {filename!r}")
    return target


def koth_list_tools_data(tools_path: Path | None = None) -> dict:
    cfg = load_tools_config(tools_path)
    return {
        "sandbox_mode": current_sandbox_mode(tools_path),
        "docker": docker_status(cfg.defaults.docker_image),
        "tools": inventory(cfg),
    }


def koth_search_arsenal_data(session: Session, query: str, category: str = "", limit: int = 5) -> list[dict]:
    index = KnowledgeIndex(session, knowledge_root=DEFAULT_KNOWLEDGE_ROOT)
    cat = category.strip().lower() or None
    return index.search(query.strip(), limit=max(1, min(limit, 15)), category=cat)


def koth_list_challenges_data(session: Session, event_id: int = 0) -> list[dict]:
    stmt = select(Challenge).order_by(Challenge.id.desc())
    if event_id > 0:
        stmt = stmt.where(Challenge.event_id == event_id)
    rows = list(session.exec(stmt).all())
    return [
        {
            "id": ch.id,
            "event_id": ch.event_id,
            "title": ch.title,
            "category": ch.category,
            "status": ch.status.value if hasattr(ch.status, "value") else str(ch.status),
            "target": ch.target,
            "points": ch.points,
        }
        for ch in rows
    ]


def koth_recon_file_data(filepath: Path) -> dict:
    if not filepath.exists():
        return {"exists": False, "path": str(filepath)}
    return {
        "exists": True,
        "name": filepath.name,
        "size": filepath.stat().st_size,
        "file_type": detect_file_type(filepath),
    }


def build_koth_mcp_server() -> FastMCP:
    mcp = FastMCP(
        "koth-ctf",
        instructions=(
            "KOTH (King Of The Hill) CTF Sandbox & Arsenal MCP Server. "
            "Use koth_list_tools, koth_search_arsenal, koth_run_tool e koth_run_python "
            "para investigar e explorar desafios com tracking automático."
        ),
    )

    @mcp.tool()
    def koth_list_tools() -> str:
        """Lista todas as ferramentas allowlisted no KOTH, modo de sandbox e status do Docker."""
        return json.dumps(koth_list_tools_data(), ensure_ascii=False, indent=2)

    @mcp.tool()
    def koth_search_arsenal(query: str, category: str = "", limit: int = 5) -> str:
        """Busca técnicas de CTF na base FTS5 do KOTH (ctf-skills + dispatch)."""
        with Session(default_engine()) as session:
            hits = koth_search_arsenal_data(session, query, category=category, limit=limit)
        return json.dumps(hits, ensure_ascii=False, indent=2)

    @mcp.tool()
    def koth_list_challenges(event_id: int = 0) -> str:
        """Lista os challenges cadastrados no banco SQLite do KOTH."""
        with Session(default_engine()) as session:
            rows = koth_list_challenges_data(session, event_id=event_id)
        return json.dumps(rows, ensure_ascii=False, indent=2)

    @mcp.tool()
    def koth_run_tool(challenge_id: int, tool: str, args: list[str]) -> str:
        """Executa uma ferramenta allowlisted do KOTH (com target guard) e registra no tracking."""
        storage = StorageService(settings.storage_dir)
        runner = ToolRunner(load_tools_config(), storage)
        with Session(default_engine()) as session:
            ch = session.get(Challenge, challenge_id)
            if ch is None:
                return f"Challenge {challenge_id} não encontrado."
            res = runner.run(session, ch, tool, args)
        return (
            f"rc={res.returncode} truncated={res.truncated} duration={res.duration_s:.2f}s\n"
            f"--- stdout ---\n{res.stdout[:6000]}\n"
            f"--- stderr ---\n{res.stderr[:1500]}"
        )

    @mcp.tool()
    def koth_run_python(challenge_id: int, filename: str, code: str) -> str:
        """Salva um script Python no sandbox do challenge e executa via ToolRunner (pwntools, z3, pycryptodome)."""
        storage = StorageService(settings.storage_dir)
        runner = ToolRunner(load_tools_config(), storage)
        with Session(default_engine()) as session:
            ch = session.get(Challenge, challenge_id)
            if ch is None:
                return f"Challenge {challenge_id} não encontrado."
            workdir = runner.sandbox_workdir(ch)
            target = safe_sandbox_file(workdir, filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(code, encoding="utf-8")
            rel = str(target.relative_to(workdir)).replace("\\", "/")
            res = runner.run(session, ch, "python", [rel])
        return (
            f"script={rel} rc={res.returncode} truncated={res.truncated}\n"
            f"--- stdout ---\n{res.stdout[:6000]}\n"
            f"--- stderr ---\n{res.stderr[:1500]}"
        )

    @mcp.tool()
    def koth_recon_file(challenge_id: int, filename: str) -> str:
        """Inspeciona magic bytes e tamanho de um arquivo no sandbox do challenge."""
        storage = StorageService(settings.storage_dir)
        runner = ToolRunner(load_tools_config(), storage)
        with Session(default_engine()) as session:
            ch = session.get(Challenge, challenge_id)
            if ch is None:
                return json.dumps({"error": f"Challenge {challenge_id} não encontrado"})
            runner.stage_downloads(session, ch)
            workdir = runner.sandbox_workdir(ch)
            target = safe_sandbox_file(workdir, filename)
        return json.dumps(koth_recon_file_data(target), ensure_ascii=False, indent=2)

    return mcp


def run_koth_mcp_server() -> None:
    build_koth_mcp_server().run(transport="stdio")
