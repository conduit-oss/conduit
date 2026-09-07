from pathlib import Path

from conduit.gitignore import ensure_conduit_gitignore


def test_adds_conduit_when_missing(tmp_path: Path):
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    rel = ensure_conduit_gitignore(tmp_path)
    assert rel == ".gitignore"
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in text
    assert ".conduit/" in text
    assert ensure_conduit_gitignore(tmp_path) is None


def test_creates_gitignore_when_absent(tmp_path: Path):
    rel = ensure_conduit_gitignore(tmp_path)
    assert rel == ".gitignore"
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert text.splitlines()[-1] == ".conduit/"


def test_recognizes_existing_variants(tmp_path: Path):
    (tmp_path / ".gitignore").write_text("**/.conduit/\n", encoding="utf-8")
    assert ensure_conduit_gitignore(tmp_path) is None
