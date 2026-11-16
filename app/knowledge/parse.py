import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


@dataclass
class Entry:
    source: str
    category: str
    title: str
    path: str
    body: str


def _split_frontmatter(text: str) -> tuple[dict, str]:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return data, text[match.end():]


def _title(md_path: Path, meta: dict, body: str) -> str:
    if isinstance(meta.get("title"), str) and meta["title"].strip():
        return meta["title"].strip()
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return md_path.stem


def parse_knowledge_dir(root: Path) -> list[Entry]:
    root = Path(root)
    entries: list[Entry] = []
    if not root.exists():
        return entries
    for md_path in sorted(root.rglob("*.md")):
        rel = md_path.relative_to(root)
        parts = rel.parts
        if "SKILL.md" in parts:
            source, category = "skill", parts[-2] if len(parts) >= 2 else ""
        elif "Arsenal" in parts:
            idx = parts.index("Arsenal")
            category = parts[idx + 1] if len(parts) > idx + 1 else ""
            source = "dispatch-arsenal"
        else:
            continue
        text = md_path.read_text(encoding="utf-8", errors="replace")
        meta, body = _split_frontmatter(text)
        entries.append(Entry(source=source, category=category,
                             title=_title(md_path, meta, body), path=str(rel),
                             body=(str(meta) if meta else "") + "\n" + body))
    return entries
