"""Hosted skeleton: stored map, one watch pass, flagged call sites, dry-run apply."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from conduit.feeds.client import FeedClient
from conduit.main import app
from conduit.service.worker import add_customer, tick

REPO = Path(__file__).resolve().parents[2]
FEEDS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "feeds"
FIXTURE = REPO / "examples" / "wrapped-openai-fixture"


def _copy_fixture(tmp_path: Path) -> Path:
    dest = tmp_path / "repo"
    shutil.copytree(FIXTURE, dest)
    return dest


def test_service_flags_wrapped_call_sites_and_dry_run_leaves_sources(tmp_path: Path):
    repo = _copy_fixture(tmp_path)
    before = (repo / "llm.py").read_text(encoding="utf-8")
    store = tmp_path / "store"
    add_customer(store, "acme", repo)
    stored = json.loads((store / "customers" / "acme" / "map.json").read_text(encoding="utf-8"))
    assert "openai" in stored["services"]

    client = FeedClient(root=FEEDS_DIR, allow_network=False)
    results = tick(store, client=client, dry_run=True)
    assert results[0].warnings == []
    assert len(results[0].events) == 1
    event = results[0].events[0]
    assert event.apply_exit == 0, event.apply_output
    pr = Path(event.pr_path).read_text(encoding="utf-8")
    assert "app.py" in pr
    assert "llm.py" in pr
    assert "ChatCompletion.create" in pr
    assert "dry-run" in pr
    paths = {row["path"] for row in event.flagged}
    assert "app.py" in paths
    assert "llm.py" in paths
    assert (repo / "llm.py").read_text(encoding="utf-8") == before

    again = tick(store, client=FeedClient(root=FEEDS_DIR, allow_network=False), dry_run=True)
    assert again[0].events == []


def test_service_cli_add_and_tick(tmp_path: Path):
    repo = _copy_fixture(tmp_path)
    store = tmp_path / "store"
    runner = CliRunner()
    added = runner.invoke(
        app,
        ["service", "add", "--customer", "acme", "--path", str(repo), "--store", str(store)],
    )
    assert added.exit_code == 0, added.stdout
    ticked = runner.invoke(
        app,
        [
            "service",
            "tick",
            "--store",
            str(store),
            "--feeds",
            str(FEEDS_DIR),
            "--customer",
            "acme",
        ],
    )
    assert ticked.exit_code == 0, ticked.stdout
    assert "flagged" in ticked.stdout
    prs = list((store / "customers" / "acme" / "prs").glob("*.md"))
    assert prs
    assert "app.py" in prs[0].read_text(encoding="utf-8")
