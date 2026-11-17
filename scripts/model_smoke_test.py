"""Smoke test: connect pydantic-ai to Gemini using the key already on this machine.

Reads the google API key from opencode's auth.json (never prints it).
Usage: python scripts/model_smoke_test.py
"""
import json
import os
import sys
from pathlib import Path

AUTH_PATH = Path(os.environ.get("USERPROFILE", "")) / ".local" / "share" / "opencode" / "auth.json"


def load_google_key() -> str:
    data = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
    key = data.get("google", {}).get("key")
    if not key:
        raise SystemExit("no google key in auth.json")
    return key


def main() -> int:
    os.environ["GEMINI_API_KEY"] = load_google_key()

    from pydantic_ai import Agent

    agent = Agent(
        "google:gemini-3-flash-preview",
        system_prompt=(
            "You are a CTF triage assistant. Given a challenge description, "
            "reply in this exact format: CATEGORY: <one of crypto/web/pwn/forensics/misc> | "
            "IDEA: <one sentence>. Keep it terse."
        ),
    )
    result = agent.run_sync(
        "Challenge: We intercepted this traffic capture (pcap) but the flag "
        "conversation happened over ICMP only. Title: 'ping me'."
    )
    print("--- model reply ---")
    print(result.output)
    usage = result.usage
    print(f"--- usage: requests={usage.requests} input={usage.input_tokens} "
          f"output={usage.output_tokens} ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
