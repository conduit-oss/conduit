"""packet publish: catalog by-package write, no consumer fan-out."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
from typer.testing import CliRunner

from conduit.main import app
from conduit.packet.publish import (
    PacketPublishError,
    catalog_paths_for_packet,
    publish_packet,
)

REPO = Path(__file__).resolve().parents[2]
SAMPLE_PACKET = REPO / "examples" / "sample-packet" / "conduit-packet.json"


def _init_git_catalog(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("catalog\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "conduit-test@example.com"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "conduit-test"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return root


def test_catalog_paths_prefer_by_package():
    paths = catalog_paths_for_packet(
        {
            "packet_id": "openai-0.28.1-1.0.0",
            "package": "openai",
            "ecosystem": "pypi",
        }
    )
    assert paths.by_package == Path("by-package/openai/pypi/openai-0.28.1-1.0.0.json")
    assert paths.flat_root == Path("openai-0.28.1-1.0.0.json")


def test_catalog_paths_skip_flat_when_eco_in_id():
    paths = catalog_paths_for_packet(
        {
            "packet_id": "openai-pypi-1.0.0",
            "package": "openai",
            "ecosystem": "pypi",
        }
    )
    assert paths.by_package == Path("by-package/openai/pypi/openai-pypi-1.0.0.json")
    assert paths.flat_root is None


def test_catalog_paths_reject_path_traversal():
    try:
        catalog_paths_for_packet(
            {
                "packet_id": "ok-1.0.0",
                "package": "../evil",
                "ecosystem": "pypi",
            }
        )
        assert False, "expected PacketPublishError"
    except PacketPublishError as exc:
        assert "unsafe package" in str(exc).lower()


def test_publish_writes_by_package_layout(tmp_path: Path):
    catalog = _init_git_catalog(tmp_path / "catalog")
    result = publish_packet(SAMPLE_PACKET, str(catalog))
    dest = catalog / "by-package" / "openai" / "pypi" / "openai-0.28.1-1.0.0.json"
    assert dest.is_file()
    assert "by-package/openai/pypi/openai-0.28.1-1.0.0.json" in result.written
    data = json.loads(dest.read_text(encoding="utf-8"))
    assert data["packet_id"] == "openai-0.28.1-1.0.0"
    assert result.committed is True
    assert result.commit_sha
    assert (catalog / "openai-0.28.1-1.0.0.json").is_file()


def test_publish_rejects_invalid_before_write(tmp_path: Path):
    catalog = _init_git_catalog(tmp_path / "catalog")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"packet_id": "x"}), encoding="utf-8")
    try:
        publish_packet(bad, str(catalog))
        assert False, "expected PacketPublishError"
    except PacketPublishError as exc:
        assert "invalid packet" in str(exc).lower()
    assert not (catalog / "by-package").exists()


def test_publish_idempotent_second_run(tmp_path: Path):
    catalog = _init_git_catalog(tmp_path / "catalog")
    first = publish_packet(SAMPLE_PACKET, str(catalog))
    second = publish_packet(SAMPLE_PACKET, str(catalog))
    dest = catalog / "by-package" / "openai" / "pypi" / "openai-0.28.1-1.0.0.json"
    assert dest.is_file()
    assert first.written
    assert not second.written
    assert second.unchanged
    assert second.committed is False


def test_publish_cli_no_consumer_fanout_help():
    result = CliRunner().invoke(app, ["packet", "publish", "--help"])
    assert result.exit_code == 0
    text = (result.stdout or "") + (result.stderr or "")
    assert "catalog" in text.lower()
    assert "consumer" in text.lower()


def test_publish_cli_success_and_fail(tmp_path: Path):
    catalog = _init_git_catalog(tmp_path / "catalog")
    runner = CliRunner()
    ok = runner.invoke(
        app,
        [
            "packet",
            "publish",
            "--packet",
            str(SAMPLE_PACKET),
            "--catalog",
            str(catalog),
        ],
    )
    assert ok.exit_code == 0, ok.stdout + (ok.stderr or "")
    assert (catalog / "by-package" / "openai" / "pypi" / "openai-0.28.1-1.0.0.json").is_file()
    out = (ok.stdout or "").lower()
    assert "consumer" in out

    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    fail = runner.invoke(
        app,
        ["packet", "publish", "--packet", str(bad), "--catalog", str(catalog)],
    )
    assert fail.exit_code != 0


def test_publish_notice_url(tmp_path: Path, monkeypatch):
    catalog = _init_git_catalog(tmp_path / "catalog")
    seen: dict = {}

    class _Resp:
        status_code = 204

    def fake_post(self, url, **kwargs):
        seen["url"] = url
        seen["json"] = kwargs.get("json")
        return _Resp()

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    result = publish_packet(
        SAMPLE_PACKET,
        str(catalog),
        notice_url="https://example.com/hooks/packet",
    )
    assert result.notice_posted is True
    assert seen["url"] == "https://example.com/hooks/packet"
    assert seen["json"]["packet_id"] == "openai-0.28.1-1.0.0"
    assert "openai" in seen["json"]["path"]


def test_publish_source_has_no_consumer_pr_api():
    src = (REPO / "conduit" / "src" / "conduit" / "packet" / "publish.py").read_text(
        encoding="utf-8"
    )
    assert "api.github.com" not in src
    assert "/pulls" not in src
    assert "create_pull_request" not in src
    assert "Does not open consumer PRs" in src
