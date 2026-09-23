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
    catalog_paths_for_surface_packet,
    publish_packet,
    publish_surface_packet,
    recipe_sibling_path,
)

REPO = Path(__file__).resolve().parents[2]
SAMPLE_PACKET = REPO / "examples" / "sample-packet" / "conduit-packet.json"
SURFACE_PACKET = (
    REPO / "examples" / "surface-packets" / "pydantic-pypi-2.0.0.json"
)


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


def test_catalog_paths_for_surface():
    paths = catalog_paths_for_surface_packet(
        {
            "packet_id": "surface:pypi:pydantic:2.0.0",
            "packet_kind": "surface",
            "package": "pydantic",
            "ecosystem": "pypi",
            "version": "2.0.0",
        }
    )
    assert paths.by_package == Path(
        "by-package/pydantic/pypi/surfaces/2.0.0.json"
    )
    assert paths.flat_root is None


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


def test_publish_surface_writes_surfaces_layout(tmp_path: Path):
    catalog = _init_git_catalog(tmp_path / "catalog")
    result = publish_surface_packet(SURFACE_PACKET, str(catalog), commit=False)
    dest = catalog / "by-package" / "pydantic" / "pypi" / "surfaces" / "2.0.0.json"
    assert dest.is_file()
    assert "by-package/pydantic/pypi/surfaces/2.0.0.json" in result.written
    data = json.loads(dest.read_text(encoding="utf-8"))
    assert data["packet_kind"] == "surface"
    assert data["version"] == "2.0.0"
    assert data["package"] == "pydantic"
    # migration hop path must remain unused
    assert not (catalog / "by-package" / "pydantic" / "pypi" / "2.0.0.json").exists()


def test_publish_packet_detects_surface_kind(tmp_path: Path):
    catalog = _init_git_catalog(tmp_path / "catalog")
    result = publish_packet(SURFACE_PACKET, str(catalog), commit=False)
    dest = catalog / "by-package" / "pydantic" / "pypi" / "surfaces" / "2.0.0.json"
    assert dest.is_file()
    assert result.packet_id == "surface:pypi:pydantic:2.0.0"
    second = publish_packet(SURFACE_PACKET, str(catalog), commit=False)
    assert not second.written
    assert second.unchanged


def test_publish_surface_rejects_invalid_before_write(tmp_path: Path):
    catalog = _init_git_catalog(tmp_path / "catalog")
    bad = tmp_path / "bad-surface.json"
    bad.write_text(
        json.dumps({"packet_kind": "surface", "packet_id": "x"}),
        encoding="utf-8",
    )
    try:
        publish_surface_packet(bad, str(catalog))
        assert False, "expected PacketPublishError"
    except PacketPublishError as exc:
        assert "invalid surface packet" in str(exc).lower()
    assert not (catalog / "by-package").exists()


def test_publish_migration_with_recipe_sibling(tmp_path: Path):
    catalog = _init_git_catalog(tmp_path / "catalog")
    recipe = tmp_path / "reshape.json"
    recipe.write_text(
        json.dumps(
            {
                "package": "openai",
                "from_version": "0.28.1",
                "to_version": "1.0.0",
                "rules": [],
                "side_effects": [],
            }
        ),
        encoding="utf-8",
    )
    result = publish_packet(
        SAMPLE_PACKET, str(catalog), recipe=recipe, commit=False
    )
    hop = catalog / "by-package" / "openai" / "pypi" / "openai-0.28.1-1.0.0.json"
    sibling = (
        catalog / "by-package" / "openai" / "pypi" / "openai-0.28.1-1.0.0.recipe.json"
    )
    assert hop.is_file()
    assert sibling.is_file()
    assert recipe_sibling_path(
        Path("by-package/openai/pypi/openai-0.28.1-1.0.0.json")
    ) == Path("by-package/openai/pypi/openai-0.28.1-1.0.0.recipe.json")
    assert any(p.endswith(".recipe.json") for p in result.written)
    copied = json.loads(sibling.read_text(encoding="utf-8"))
    assert copied["package"] == "openai"


PYDANTIC_SURFACE_HOP = REPO / "examples" / "sample-packet" / "pydantic-surface-hop.json"
PYDANTIC_RECIPE = REPO / "examples" / "reshape-recipes" / "pydantic-1.10.13-2.0.0.json"
PYDANTIC_SURFACE_V1 = REPO / "examples" / "surface-packets" / "pydantic-pypi-1.10.13.json"


def test_publish_pydantic_surface_hop_and_recipe_smoke(tmp_path: Path):
    """Catalog receipt: by-package/pydantic/pypi hop + .recipe.json sibling."""
    catalog = _init_git_catalog(tmp_path / "catalog")
    result = publish_packet(
        PYDANTIC_SURFACE_HOP,
        str(catalog),
        recipe=PYDANTIC_RECIPE,
        commit=False,
    )
    hop = catalog / "by-package" / "pydantic" / "pypi" / "pydantic-1.10.13-2.0.0.json"
    sibling = (
        catalog
        / "by-package"
        / "pydantic"
        / "pypi"
        / "pydantic-1.10.13-2.0.0.recipe.json"
    )
    assert hop.is_file()
    assert sibling.is_file()
    assert hop.as_posix().endswith("by-package/pydantic/pypi/pydantic-1.10.13-2.0.0.json")
    assert "by-package/pydantic/pypi/pydantic-1.10.13-2.0.0.json" in result.written
    assert any(p.endswith("pydantic-1.10.13-2.0.0.recipe.json") for p in result.written)
    hop_data = json.loads(hop.read_text(encoding="utf-8"))
    assert hop_data["packet_id"] == "pydantic-1.10.13-2.0.0"
    assert hop_data["package"] == "pydantic"
    recipe_data = json.loads(sibling.read_text(encoding="utf-8"))
    assert recipe_data["package"] == "pydantic"
    assert recipe_data["from_version"] == "1.10.13"
    assert recipe_data["to_version"] == "2.0.0"
    # flat mirror (packet_id has no -<eco>- token)
    assert (catalog / "pydantic-1.10.13-2.0.0.json").is_file()


def test_publish_pydantic_surface_packet_under_surfaces(tmp_path: Path):
    catalog = _init_git_catalog(tmp_path / "catalog")
    result = publish_surface_packet(SURFACE_PACKET, str(catalog), commit=False)
    dest = catalog / "by-package" / "pydantic" / "pypi" / "surfaces" / "2.0.0.json"
    assert dest.is_file()
    assert "by-package/pydantic/pypi/surfaces/2.0.0.json" in result.written
    v1 = publish_surface_packet(PYDANTIC_SURFACE_V1, str(catalog), commit=False)
    dest_v1 = catalog / "by-package" / "pydantic" / "pypi" / "surfaces" / "1.10.13.json"
    assert dest_v1.is_file()
    assert any("surfaces/1.10.13.json" in p for p in v1.written)


def test_publish_cli_pydantic_hop_recipe_no_commit(tmp_path: Path):
    catalog = _init_git_catalog(tmp_path / "catalog")
    runner = CliRunner()
    ok = runner.invoke(
        app,
        [
            "packet",
            "publish",
            "--packet",
            str(PYDANTIC_SURFACE_HOP),
            "--catalog",
            str(catalog),
            "--recipe",
            str(PYDANTIC_RECIPE),
            "--no-commit",
        ],
    )
    assert ok.exit_code == 0, ok.stdout + (ok.stderr or "")
    assert (
        catalog / "by-package" / "pydantic" / "pypi" / "pydantic-1.10.13-2.0.0.json"
    ).is_file()
    assert (
        catalog
        / "by-package"
        / "pydantic"
        / "pypi"
        / "pydantic-1.10.13-2.0.0.recipe.json"
    ).is_file()


def test_publish_cli_no_consumer_fanout_help():
    result = CliRunner().invoke(app, ["packet", "publish", "--help"])
    assert result.exit_code == 0
    text = (result.stdout or "") + (result.stderr or "")
    assert "catalog" in text.lower()
    assert "consumer" in text.lower()
    assert "recipe" in text.lower()


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
    assert (
        catalog / "by-package" / "openai" / "pypi" / "openai-0.28.1-1.0.0.json"
    ).is_file()
    out = (ok.stdout or "").lower()
    assert "consumer" in out

    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    fail = runner.invoke(
        app,
        ["packet", "publish", "--packet", str(bad), "--catalog", str(catalog)],
    )
    assert fail.exit_code != 0


def test_publish_cli_surface_packet(tmp_path: Path):
    catalog = _init_git_catalog(tmp_path / "catalog")
    runner = CliRunner()
    ok = runner.invoke(
        app,
        [
            "packet",
            "publish",
            "--packet",
            str(SURFACE_PACKET),
            "--catalog",
            str(catalog),
            "--no-commit",
        ],
    )
    assert ok.exit_code == 0, ok.stdout + (ok.stderr or "")
    dest = catalog / "by-package" / "pydantic" / "pypi" / "surfaces" / "2.0.0.json"
    assert dest.is_file()


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
