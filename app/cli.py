import argparse
import threading
import webbrowser

import uvicorn

from .agents.providers import provider_status_rows
from .agents.cli_providers import cli_provider_status_rows
from .config import settings
from .tooling.config import load_tools_config
from .tooling.doctor import inventory
from .tooling.koth_mcp import run_koth_mcp_server
from .tooling.mcp_servers import mcp_status_rows
from .tooling.sandbox import current_sandbox_mode, docker_status


def cmd_serve(host: str, port: int, open_browser: bool, reload: bool) -> int:
    url = f"http://{host}:{port}/"
    print(f"[KOTH] King Of The Hill — subindo em {url}")
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run("app.main:app", host=host, port=port, reload=reload)
    return 0


def cmd_doctor() -> int:
    tools_cfg = load_tools_config()
    print(f"[KOTH // DOCTOR] Sandbox mode: {current_sandbox_mode()}")
    dk = docker_status(tools_cfg.defaults.docker_image)
    print(f"  Docker: {dk['message']}")
    print("\nFerramentas:")
    for row in inventory(tools_cfg):
        mark = "OK" if row["present"] else "MISSING"
        print(f"  - {row['name']:<10} [{row['mode']}] {mark} ({row['path'] or '-'})")
    print("\nProvedores API conectados:")
    for p in provider_status_rows(settings.storage_dir):
        if p["added"]:
            print(f"  - {p['label']}: {p['source']} {p['masked']}")
    print("\nProvedores CLI:")
    for c in cli_provider_status_rows():
        mark = "OK" if c["installed"] else "MISSING"
        print(f"  - {c['label']}: {mark} (login: {c['login_hint']})")
    print("\nServidores MCP:")
    for m in mcp_status_rows():
        state = "ON" if m["enabled"] else "off"
        ready = "OK" if m["ready"] else "MISSING"
        print(f"  - {m['name']:<12} [{state} | {m['transport']}] {ready} ({m['target']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="koth",
        description="KOTH (King Of The Hill) — CTF Agent Orchestrator",
    )
    sub = parser.add_subparsers(dest="command")

    serve_p = sub.add_parser("serve", help="Inicia o servidor web local do KOTH")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8777)
    serve_p.add_argument("--no-browser", action="store_true")
    serve_p.add_argument("--reload", action="store_true")

    sub.add_parser("doctor", help="Verifica ferramentas, Docker, MCP e provedores de IA")
    sub.add_parser("mcp", help="Inicia o servidor MCP stdio do KOTH para clientes externos")

    args = parser.parse_args(argv)
    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "mcp":
        run_koth_mcp_server()
        return 0

    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8777)
    no_browser = getattr(args, "no_browser", False)
    reload = getattr(args, "reload", False)
    return cmd_serve(host, port, open_browser=not no_browser, reload=reload)


if __name__ == "__main__":
    raise SystemExit(main())
