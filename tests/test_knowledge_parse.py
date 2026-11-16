from app.knowledge.parse import parse_knowledge_dir


def _write(root, rel, content):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_parses_arsenal_and_skills(tmp_path):
    root = tmp_path / "knowledge"
    _write(root, "dispatch/Arsenal/crypto/rsa-small-e.md",
           "---\ntitle: RSA small e\ndescription: cube root attack\n---\nMethod: e=3 cube root.")
    _write(root, "ctf-skills/ctf-crypto/SKILL.md",
           "# ctf-crypto\n\nRSA, AES, ECC techniques.")
    entries = parse_knowledge_dir(root)
    arsenal = [e for e in entries if e.source == "dispatch-arsenal"]
    skills = [e for e in entries if e.source == "skill"]
    assert len(arsenal) == 1 and len(skills) == 1
    assert arsenal[0].category == "crypto"
    assert arsenal[0].title == "RSA small e"
    assert "cube root" in arsenal[0].body
    assert skills[0].category == "ctf-crypto"
    assert skills[0].title == "ctf-crypto"


def test_heading_fallback_title(tmp_path):
    root = tmp_path / "knowledge"
    _write(root, "dispatch/Arsenal/pwn/no-frontmatter.md",
           "# Format String Overwrite\n\nUse %n.")
    entries = [e for e in parse_knowledge_dir(root) if e.source == "dispatch-arsenal"]
    assert entries[0].title == "Format String Overwrite"


def test_ignores_non_md_and_other_dirs(tmp_path):
    root = tmp_path / "knowledge"
    _write(root, "dispatch/docs/readme.txt", "not markdown")
    _write(root, "ctf-skills/scripts/tool.py", "print(1)")
    assert parse_knowledge_dir(root) == []
