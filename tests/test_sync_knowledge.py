import subprocess

from scripts.sync_knowledge import sync


def _make_repo(path, files):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True, capture_output=True)
    for name, content in files.items():
        p = path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)


def test_sync_local_repo_and_lock(tmp_path):
    src = tmp_path / "src"
    _make_repo(src, {"Arsenal/crypto/rsa.md": "---\ntitle: rsa\n---\nbody", "SKILL.md": "# skill"})
    dest = tmp_path / "knowledge"
    info = sync(str(src), dest / "fake-repo")
    assert (dest / "fake-repo" / "Arsenal" / "crypto" / "rsa.md").exists()
    assert info["commit"] and len(info["commit"]) == 40
