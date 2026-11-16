"""Vendor knowledge repos into knowledge/ and pin them in knowledge.lock."""
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_REPOS = [
    ("https://github.com/ljagiello/ctf-skills", "ctf-skills", "main"),
    ("https://github.com/Felipebc2/Dispatch", "dispatch", "main"),
]
BASE_DIR = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = BASE_DIR / "knowledge"


def sync(repo: str, dest: Path, ref: str = "main") -> dict:
    dest = Path(dest)
    if dest.exists() and (dest / ".git").exists():
        subprocess.run(["git", "-C", str(dest), "fetch", "--depth", "1", "origin", ref],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(dest), "checkout", "FETCH_HEAD"],
                       check=True, capture_output=True)
    else:
        if dest.exists():
            shutil.rmtree(dest)
        subprocess.run(["git", "clone", "--depth", "1", "--branch", ref, repo, str(dest)],
                       check=True, capture_output=True)
    commit = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"],
                            check=True, capture_output=True, text=True).stdout.strip()
    # vendored copy is content-only: strip the embedded repo metadata
    shutil.rmtree(dest / ".git", ignore_errors=True)
    return {"repo": repo, "ref": ref, "commit": commit, "path": str(dest)}


def main() -> int:
    knowledge_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else KNOWLEDGE_DIR
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    lock = []
    for repo, name, ref in DEFAULT_REPOS:
        print(f"syncing {repo} -> {knowledge_dir / name}")
        lock.append(sync(repo, knowledge_dir / name, ref))
    lock_path = knowledge_dir / "knowledge.lock"
    lock_path.write_text(json.dumps({"synced_at": datetime.now(timezone.utc).isoformat(),
                                     "repos": lock}, indent=2), encoding="utf-8")
    print(f"wrote {lock_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
