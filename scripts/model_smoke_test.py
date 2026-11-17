"""Smoke test: connect pydantic-ai to NVIDIA NIM (OpenAI-compatible).

Reads the nvidia API key from opencode's auth.json (never prints it).
Usage: python scripts/model_smoke_test.py
"""
import json
import os
import sys
from pathlib import Path

AUTH_PATH = Path(os.environ.get("USERPROFILE", "")) / ".local" / "share" / "opencode" / "auth.json"
BASE_URL = "https://integrate.api.nvidia.com/v1"
MODEL = "nvidia/nemotron-3.5-super-vl-120b-a12b"  # fast role


def load_nvidia_key() -> str:
    data = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
    key = data.get("nvidia", {}).get("key")
    if not key:
        raise SystemExit("no nvidia key in auth.json")
    return key


def main() -> int:
    from pydantic_ai import Agent
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    provider = OpenAIProvider(base_url=BASE_URL, api_key=load_nvidia_key())
    model = OpenAIChatModel(MODEL, provider=provider)

    agent = Agent(
        model,
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
