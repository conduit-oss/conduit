"""Companion DEPENDENCY_ADD/REMOVE, tomlkit pyproject, lockfile add/remove."""

from __future__ import annotations

import json
from pathlib import Path

from conduit.detect.lockfile_diff import diff_versions
from conduit.detect.models import ChangeSignal
from conduit.packet.synthesize import fold_companion_rules, packet_from_signals
from conduit.packet.validate import validate_packet
from conduit.patcher import apply_packet
from conduit.patcher.dependency_update import (
    apply_dependency_rule,
    bump_requirements_txt,
    dependency_packages,
    format_caret_version,
    format_go_version,
    format_pip_requirement,
)
from conduit.prune.grep_imports import prune_by_imports
from conduit.run_summary import build_run_summary, format_run_summary
from conduit.test_runner import TestResult as RunnerResult


def _packet(rules: list[dict], **extra) -> dict:
    data = {
        "packet_id": "t",
        "package": "langchain",
        "ecosystem": "pypi",
        "from_version": "0.0.1",
        "to_version": "0.1.0",
        "rules": rules,
    }
    data.update(extra)
    return data


def _tests() -> RunnerResult:
    return RunnerResult(
        runner="pytest",
        passed=True,
        returncode=0,
        stdout="",
        stderr="",
        command=["pytest", "-q"],
    )


def test_format_pins_per_ecosystem():
    assert format_pip_requirement("httpx", "1.2.3") == "httpx==1.2.3"
    assert format_pip_requirement("httpx", "==1.2.3") == "httpx==1.2.3"
    assert format_caret_version("1.2.3") == "^1.2.3"
    assert format_caret_version("^1.2.3") == "^1.2.3"
    assert format_go_version("1.2.3") == "v1.2.3"
    assert format_go_version("v1.2.3") == "v1.2.3"


def test_add_remove_requirements_and_package_json(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("langchain==0.0.1\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"langchain": "0.0.1"}}) + "\n",
        encoding="utf-8",
    )
    add = {
        "type": "DEPENDENCY_ADD",
        "package": "langchain-core",
        "to_version": "0.2.0",
        "ecosystems": ["pip"],
    }
    packet = _packet(
        [
            add,
            {
                "type": "DEPENDENCY_ADD",
                "package": "langchain-core",
                "to_version": "0.2.0",
                "scope": "dev",
                "ecosystems": ["npm"],
            },
        ]
    )
    assert validate_packet(packet) == []
    report = apply_packet(tmp_path, packet, dry_run=False, require_context=False)
    req = (tmp_path / "requirements.txt").read_text(encoding="utf-8")
    assert "langchain-core==0.2.0" in req
    npm = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    assert npm["devDependencies"]["langchain-core"] == "^0.2.0"
    assert "dependencies" in npm
    assert "langchain-core" not in npm.get("dependencies", {})

    remove = _packet(
        [
            {
                "type": "DEPENDENCY_REMOVE",
                "package": "langchain-core",
                "ecosystems": ["pip"],
            }
        ]
    )
    apply_packet(tmp_path, remove, dry_run=False, require_context=False)
    req2 = (tmp_path / "requirements.txt").read_text(encoding="utf-8")
    assert "langchain-core" not in req2
    assert report.files_modified


def test_pep621_tomlkit_preserves_comment(tmp_path: Path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\n"
        'name = "demo"\n'
        "dependencies = [\n"
        '  "openai==0.28.1",  # keep me\n'
        "]\n",
        encoding="utf-8",
    )
    apply_dependency_rule(
        tmp_path,
        {
            "type": "DEPENDENCY_ADD",
            "package": "httpx",
            "to_version": "0.27.0",
            "ecosystems": ["pyproject"],
        },
        dry_run=False,
    )
    text = pyproject.read_text(encoding="utf-8")
    assert "keep me" in text
    assert "httpx==0.27.0" in text
    apply_dependency_rule(
        tmp_path,
        {
            "type": "DEPENDENCY_REMOVE",
            "package": "httpx",
            "ecosystems": ["pyproject"],
        },
        dry_run=False,
    )
    text2 = pyproject.read_text(encoding="utf-8")
    assert "httpx" not in text2
    assert "keep me" in text2


def test_poetry_map_add_does_not_become_array(tmp_path: Path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.poetry.dependencies]\n"
        'python = "^3.10"\n'
        'openai = "0.28.1"\n',
        encoding="utf-8",
    )
    apply_dependency_rule(
        tmp_path,
        {
            "type": "DEPENDENCY_ADD",
            "package": "httpx",
            "to_version": "0.27.0",
            "ecosystems": ["pyproject"],
        },
        dry_run=False,
    )
    text = pyproject.read_text(encoding="utf-8")
    assert "httpx" in text
    assert "^0.27.0" in text
    assert "dependencies = [" not in text
    assert 'python = "^3.10"' in text


def test_go_add_uses_v_prefix(tmp_path: Path):
    gomod = tmp_path / "go.mod"
    gomod.write_text("module example.com/app\n\ngo 1.22\n", encoding="utf-8")
    apply_dependency_rule(
        tmp_path,
        {
            "type": "DEPENDENCY_ADD",
            "package": "github.com/foo/bar",
            "to_version": "1.2.3",
            "ecosystems": ["go"],
        },
        dry_run=False,
    )
    text = gomod.read_text(encoding="utf-8")
    assert "require github.com/foo/bar v1.2.3" in text


def test_skip_missing_dev_table(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "demo"\n'
        'dependencies = ["openai==1.0.0"]\n',
        encoding="utf-8",
    )
    result = apply_dependency_rule(
        tmp_path,
        {
            "type": "DEPENDENCY_ADD",
            "package": "pytest",
            "to_version": "8.0.0",
            "scope": "dev",
            "ecosystems": ["pyproject"],
        },
        dry_run=False,
    )
    assert result.changed == []
    assert any("no dev dependency table" in s for s in result.skips)


def test_prune_includes_companion_import(tmp_path: Path):
    (tmp_path / "app.py").write_text("import langchain\n", encoding="utf-8")
    (tmp_path / "extra.py").write_text("import httpx\n", encoding="utf-8")
    packet = _packet(
        [
            {
                "type": "DEPENDENCY_ADD",
                "package": "httpx",
                "to_version": "0.27.0",
            }
        ]
    )
    files = prune_by_imports(tmp_path, dependency_packages(packet))
    rels = {p.name for p in files}
    assert "app.py" in rels
    assert "extra.py" in rels


def test_nested_requirements_and_skip_vendor(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("openai==0.28.1\n", encoding="utf-8")
    svc = tmp_path / "services" / "desk"
    svc.mkdir(parents=True)
    (svc / "requirements.txt").write_text("openai==0.28.1\n", encoding="utf-8")
    (tmp_path / "constraints.txt").write_text("openai==0.28.1\n", encoding="utf-8")
    vendor = tmp_path / "vendor" / "legacy"
    vendor.mkdir(parents=True)
    (vendor / "requirements.txt").write_text("openai==0.28.1\n", encoding="utf-8")
    result = apply_dependency_rule(
        tmp_path,
        {
            "type": "DEPENDENCY_BUMP",
            "package": "openai",
            "from_version": "0.28.1",
            "to_version": "3.3.0",
            "ecosystems": ["pip"],
        },
        dry_run=False,
    )
    rels = {p.replace("\\", "/") for p in result.changed}
    assert "requirements.txt" in rels
    assert "services/desk/requirements.txt" in rels
    assert "constraints.txt" in rels
    assert not any(r.startswith("vendor/") for r in rels)
    assert (vendor / "requirements.txt").read_text(encoding="utf-8") == "openai==0.28.1\n"
    assert "openai==3.3.0" in (svc / "requirements.txt").read_text(encoding="utf-8")


def test_diff_versions_add_and_remove():
    old = "openai==0.28.1\nrequests==2.0.0\n"
    new = "openai==0.28.1\nhttpx==0.27.0\n"
    jumps = diff_versions(old, new, filename="requirements.txt")
    kinds = {j.name: j.kind for j in jumps}
    assert kinds["httpx"] == "add"
    assert kinds["requests"] == "remove"
    assert "openai" not in kinds


def test_fold_companion_only_when_named():
    primary = ChangeSignal(
        source="lockfile",
        package="langchain",
        change_type="SDK_MAJOR_BUMP",
        suggested_rules=[
            {
                "type": "DEPENDENCY_BUMP",
                "package": "langchain",
                "from_version": "0.0.1",
                "to_version": "0.1.0",
            },
            {
                "type": "DEPENDENCY_ADD",
                "package": "langchain-core",
                "to_version": "0.2.0",
            },
        ],
    )
    companion = ChangeSignal(
        source="lockfile",
        package="langchain-core",
        change_type="PACKAGE_ADDED",
        suggested_rules=[
            {
                "type": "DEPENDENCY_ADD",
                "package": "langchain-core",
                "to_version": "0.2.0",
                "scope": "main",
            }
        ],
    )
    unrelated = ChangeSignal(
        source="lockfile",
        package="left-pad",
        change_type="PACKAGE_ADDED",
        suggested_rules=[
            {
                "type": "DEPENDENCY_ADD",
                "package": "left-pad",
                "to_version": "1.0.0",
            }
        ],
    )
    packet = packet_from_signals(
        [primary],
        package="langchain",
        companion_signals=[companion, unrelated],
    )
    pkgs = {r.get("package") for r in packet["rules"] if r.get("type") in {
        "DEPENDENCY_ADD",
        "DEPENDENCY_BUMP",
    }}
    assert "langchain-core" in pkgs
    assert "left-pad" not in pkgs
    folded = fold_companion_rules(
        [{"type": "DEPENDENCY_BUMP", "package": "langchain"}],
        [unrelated],
        package="langchain",
    )
    assert not any(r.get("package") == "left-pad" for r in folded)


def test_run_summary_lockfile_regen_and_leftover():
    packet = _packet(
        [
            {
                "type": "DEPENDENCY_ADD",
                "package": "httpx",
                "to_version": "0.27.0",
            }
        ]
    )
    from conduit.patcher.engine import PatchReport

    leftover = ChangeSignal(
        source="lockfile",
        package="left-pad",
        change_type="PACKAGE_ADDED",
    )
    summary = build_run_summary(
        packet=packet,
        report=PatchReport(skips=["Skipped pyproject.toml: no dev dependency table"]),
        test_result=_tests(),
        detected_signals=[leftover],
    )
    text = format_run_summary(summary)
    assert "Regenerate the lockfile" in text
    assert "left-pad" in text
    assert "no dev dependency table" in text


def test_bump_requirements_refreshes_hashes_from_pypi(monkeypatch, tmp_path: Path):
    h1 = "9652df7fdf8ee6f5bd58e0a12f2b1d414a18e0f06bb7a9a57c8643a5f5469bd3"
    h2 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    monkeypatch.setattr(
        "conduit.patcher.dependency_update.fetch_pypi_sha256s",
        lambda pkg, ver: [h1, h2],
    )
    monkeypatch.setattr(
        "conduit.patcher.dependency_update.fetch_pypi_requires_dist",
        lambda pkg, ver: [],
    )
    req = tmp_path / "requirements.txt"
    req.write_text(
        "aiohttp==3.9.0 \\\n"
        "    --hash=sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc\n"
        "openai==1.40.0 ; python_version >= \"3.10\" and python_version < \"4.0\" \\\n"
        "    --hash=sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd \\\n"
        "    --hash=sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee\n"
        "portend==3.2.1 \\\n"
        "    --hash=sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff\n",
        encoding="utf-8",
    )
    assert bump_requirements_txt(req, "openai", "3.3.1", dry_run=False)
    text = req.read_text(encoding="utf-8")
    assert (
        "openai==3.3.1 ; python_version >= \"3.10\" and python_version < \"4.0\" \\"
        in text
    )
    assert f"--hash=sha256:{h1}" in text
    assert f"--hash=sha256:{h2}" in text
    assert "dddddddd" not in text
    assert "aiohttp==3.9.0" in text
    assert "portend==3.2.1" in text


def test_bump_requirements_adds_hashes_when_file_require_hashes(
    monkeypatch, tmp_path: Path
):
    """Bare openai pin in a hashed lockfile must gain hashes (pip require-hashes)."""
    h1 = "9652df7fdf8ee6f5bd58e0a12f2b1d414a18e0f06bb7a9a57c8643a5f5469bd3"
    monkeypatch.setattr(
        "conduit.patcher.dependency_update.fetch_pypi_sha256s",
        lambda pkg, ver: [h1],
    )
    monkeypatch.setattr(
        "conduit.patcher.dependency_update.fetch_pypi_requires_dist",
        lambda pkg, ver: [],
    )
    req = tmp_path / "requirements.txt"
    req.write_text(
        "aiohttp==3.9.0 \\\n"
        "    --hash=sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc\n"
        "openai==1.40.0\n",
        encoding="utf-8",
    )
    assert bump_requirements_txt(req, "openai", "3.3.1", dry_run=False)
    text = req.read_text(encoding="utf-8")
    assert "openai==3.3.1 \\" in text
    assert f"--hash=sha256:{h1}" in text


def test_bump_requirements_strips_hashes_when_pypi_fetch_fails(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(
        "conduit.patcher.dependency_update.fetch_pypi_sha256s",
        lambda pkg, ver: [],
    )
    monkeypatch.setattr(
        "conduit.patcher.dependency_update.fetch_pypi_requires_dist",
        lambda pkg, ver: [],
    )
    req = tmp_path / "requirements.txt"
    req.write_text(
        "openai==1.0.0 --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
        encoding="utf-8",
    )
    assert bump_requirements_txt(req, "openai", "3.3.1", dry_run=False)
    assert req.read_text(encoding="utf-8").strip() == "openai==3.3.1"


def test_bump_adds_missing_transitive_hashed_pins(monkeypatch, tmp_path: Path):
    """Hashed lockfiles need new openai transitives (e.g. httpx2) pinned + hashed."""
    openai_h = "9652df7fdf8ee6f5bd58e0a12f2b1d414a18e0f06bb7a9a57c8643a5f5469bd3"
    httpx_h = "c895411ba65569158e862368917aaf56597f3e5fa3b91b0502919638465a08f3"

    def _sha(pkg, ver):
        if pkg == "openai":
            return [openai_h]
        if pkg == "httpx2":
            return [httpx_h]
        return []

    monkeypatch.setattr(
        "conduit.patcher.dependency_update.fetch_pypi_sha256s", _sha
    )
    monkeypatch.setattr(
        "conduit.patcher.dependency_update.fetch_pypi_requires_dist",
        lambda pkg, ver: (
            ["httpx2<3,>=2.7.0", "anyio>=3"] if pkg == "openai" else []
        ),
    )
    monkeypatch.setattr(
        "conduit.patcher.dependency_update.pick_pypi_version",
        lambda pkg, spec="": {"httpx2": "2.12.0", "anyio": "4.0.0"}.get(pkg),
    )
    # anyio already pinned — should not be re-added; httpx2 is missing.
    req = tmp_path / "requirements.txt"
    req.write_text(
        "anyio==4.0.0 \\\n"
        "    --hash=sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd\n"
        "openai==1.40.0 \\\n"
        "    --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
        encoding="utf-8",
    )
    assert bump_requirements_txt(req, "openai", "3.3.1", dry_run=False)
    text = req.read_text(encoding="utf-8")
    assert "openai==3.3.1" in text
    assert f"--hash=sha256:{openai_h}" in text
    assert "httpx2==2.12.0" in text
    assert f"--hash=sha256:{httpx_h}" in text
    assert text.count("anyio==4.0.0") == 1


def test_bump_requirements_no_hashes_when_file_unhashed(tmp_path: Path):
    req = tmp_path / "requirements.txt"
    req.write_text("openai==1.0.0\n", encoding="utf-8")
    assert bump_requirements_txt(req, "openai", "3.3.1", dry_run=False)
    assert req.read_text(encoding="utf-8").strip() == "openai==3.3.1"


def test_poetry_hashed_bump_exports_lock(monkeypatch, tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.poetry]\nname = \"demo\"\nversion = \"0.1.0\"\n"
        "[tool.poetry.dependencies]\npython = \"^3.10\"\nopenai = \"^1.0.0\"\n",
        encoding="utf-8",
    )
    (tmp_path / "poetry.lock").write_text("# lock\n", encoding="utf-8")
    req = tmp_path / "requirements.txt"
    req.write_text(
        "openai==1.0.0 \\\n"
        "    --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        if "export" in cmd:
            out = Path(cmd[cmd.index("--output") + 1])
            out.write_text(
                "httpx2==2.12.0 ; python_version >= \"3.10\" \\\n"
                "    --hash=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
                "openai==3.3.1 ; python_version >= \"3.10\" \\\n"
                "    --hash=sha256:9652df7fdf8ee6f5bd58e0a12f2b1d414a18e0f06bb7a9a57c8643a5f5469bd3\n",
                encoding="utf-8",
            )
        return _P()

    monkeypatch.setattr(
        "conduit.patcher.dependency_update.shutil.which",
        lambda name: "poetry" if name == "poetry" else None,
    )
    monkeypatch.setattr(
        "conduit.patcher.dependency_update.subprocess.run", _fake_run
    )

    result = apply_dependency_rule(
        tmp_path,
        {
            "type": "DEPENDENCY_BUMP",
            "package": "openai",
            "to_version": "3.3.1",
            "ecosystems": ["pip", "pyproject"],
        },
        dry_run=False,
    )
    assert "requirements.txt" in result.changed
    assert "poetry.lock" in result.changed
    text = req.read_text(encoding="utf-8")
    assert "httpx2==2.12.0" in text
    assert "openai==3.3.1" in text
    assert any("lock" in c for c in calls)
    assert any("export" in c for c in calls)
    # Direct requirements edit with PyPI hash fetch should be skipped.
    assert not any(
        "falling back to direct requirements.txt edit" in s for s in result.skips
    )


def test_poetry_export_failure_falls_back_to_req_edit(monkeypatch, tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.poetry]\nname = \"demo\"\nversion = \"0.1.0\"\n"
        "[tool.poetry.dependencies]\npython = \"^3.10\"\nopenai = \"^1.0.0\"\n",
        encoding="utf-8",
    )
    req = tmp_path / "requirements.txt"
    req.write_text(
        "openai==1.0.0 \\\n"
        "    --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
        encoding="utf-8",
    )
    h1 = "9652df7fdf8ee6f5bd58e0a12f2b1d414a18e0f06bb7a9a57c8643a5f5469bd3"
    monkeypatch.setattr(
        "conduit.patcher.dependency_update.shutil.which",
        lambda name: "poetry" if name == "poetry" else None,
    )

    class _P:
        returncode = 1
        stdout = ""
        stderr = "poetry-plugin-export not installed"

    monkeypatch.setattr(
        "conduit.patcher.dependency_update.subprocess.run",
        lambda *a, **k: _P(),
    )
    monkeypatch.setattr(
        "conduit.patcher.dependency_update.fetch_pypi_sha256s",
        lambda pkg, ver: [h1],
    )
    monkeypatch.setattr(
        "conduit.patcher.dependency_update.fetch_pypi_requires_dist",
        lambda pkg, ver: [],
    )
    result = apply_dependency_rule(
        tmp_path,
        {
            "type": "DEPENDENCY_BUMP",
            "package": "openai",
            "to_version": "3.3.1",
            "ecosystems": ["pip", "pyproject"],
        },
        dry_run=False,
    )
    assert any("poetry lock/export failed" in s for s in result.skips)
    text = req.read_text(encoding="utf-8")
    assert "openai==3.3.1" in text
    assert f"--hash=sha256:{h1}" in text


def test_remove_requirements_drops_hash_block(tmp_path: Path):
    req = tmp_path / "requirements.txt"
    req.write_text(
        "keep==1.0\n"
        "openai==1.40.0 \\\n"
        "    --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "after==2.0\n",
        encoding="utf-8",
    )
    result = apply_dependency_rule(
        tmp_path,
        {"type": "DEPENDENCY_REMOVE", "package": "openai", "ecosystems": ["pip"]},
        dry_run=False,
    )
    assert result.changed
    text = req.read_text(encoding="utf-8")
    assert "openai" not in text
    assert "--hash=" not in text
    assert "keep==1.0" in text
    assert "after==2.0" in text
