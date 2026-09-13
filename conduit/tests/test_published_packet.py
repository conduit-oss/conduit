"""Published packet file/URL: skip vendor scrape, still scan the client."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from conduit.detect.orchestrator import run_detect
from conduit.main import app
from conduit.packet.fetch import PacketFetchError, fetch_packet_url


def _disable_llm(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_BASE_URL", raising=False)


def _packet() -> dict:
    return {
        "packet_id": "openai-pypi-1.0.0",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0",
        "to_version": "1.0.0",
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "target_files": ["*.py"],
                "match": "gpt-4-0613",
                "replace": "gpt-4o",
            }
        ],
    }


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, body: bytes = b"", headers=None):
        self.status_code = status_code
        self.content = body
        self.headers = headers or {}


def test_scan_client_without_vendor_modules(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
    (tmp_path / "app.py").write_text(
        "import openai\nmodel = 'gpt-4-0613'\n", encoding="utf-8"
    )
    (tmp_path / "requirements.txt").write_text("openai==0.28.1\n", encoding="utf-8")

    def boom(self, ctx):
        raise AssertionError("vendor module should not run")

    monkeypatch.setattr("conduit.detect.modules.openai.OpenAIModule.run", boom)
    result = run_detect(
        tmp_path,
        skip_modules=True,
        skip_lockfile=True,
        scan_client=True,
        scan_packages=["openai"],
        demo=True,
    )
    assert "openai" in result.package_states
    assert result.signals == []


def test_fetch_packet_url_caches(tmp_path: Path, monkeypatch):
    payload = json.dumps(_packet()).encode("utf-8")
    calls = {"n": 0}

    def fake_get(self, url, **kwargs):
        calls["n"] += 1
        return _FakeResponse(body=payload)

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    dest = fetch_packet_url(
        "https://example.com/packets/openai-pypi-1.0.0.json",
        root=tmp_path,
    )
    assert dest.name == "openai-pypi-1.0.0.json"
    assert json.loads(dest.read_text(encoding="utf-8"))["package"] == "openai"
    dest2 = fetch_packet_url(
        "https://example.com/packets/openai-pypi-1.0.0.json",
        root=tmp_path,
    )
    assert dest2 == dest
    assert calls["n"] == 1


def test_fetch_packet_url_rejects_non_json(tmp_path: Path, monkeypatch):
    def fake_get(self, url, **kwargs):
        return _FakeResponse(body=b"not-json")

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    try:
        fetch_packet_url("https://example.com/bad.json", root=tmp_path)
        assert False, "expected PacketFetchError"
    except PacketFetchError as exc:
        assert "JSON" in str(exc)


def test_fetch_packet_url_http_error(tmp_path: Path, monkeypatch):
    def fake_get(self, url, **kwargs):
        return _FakeResponse(status_code=404, body=b"{}")

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    try:
        fetch_packet_url("https://example.com/missing.json", root=tmp_path)
        assert False, "expected PacketFetchError"
    except PacketFetchError as exc:
        assert "404" in str(exc)


def test_catalog_url_for_name(monkeypatch):
    from conduit.packet.fetch import catalog_url_for_name

    monkeypatch.delenv("CONDUIT_PACKET_CATALOG_BASE", raising=False)
    assert catalog_url_for_name("example-sdk-pypi-1.0.0") is None

    monkeypatch.setenv(
        "CONDUIT_PACKET_CATALOG_BASE",
        "https://raw.githubusercontent.com/conduit-oss/conduit-packets/main/",
    )
    assert (
        catalog_url_for_name("example-sdk-pypi-1.0.0")
        == "https://raw.githubusercontent.com/conduit-oss/conduit-packets/main/by-package/example-sdk/pypi/example-sdk-pypi-1.0.0.json"
    )
    assert catalog_url_for_name("https://example.com/x.json") is None
    assert catalog_url_for_name("openai") == (
        "https://raw.githubusercontent.com/conduit-oss/conduit-packets/main/openai.json"
    )


def test_run_file_packet_skips_vendor_and_writes_source(tmp_path: Path, monkeypatch):
    from typer.testing import CliRunner

    _disable_llm(monkeypatch)
    (tmp_path / "app.py").write_text(
        "import openai\nmodel = 'gpt-4-0613'\n", encoding="utf-8"
    )
    (tmp_path / "requirements.txt").write_text("openai==0.28.1\n", encoding="utf-8")
    pkt = tmp_path / "hop.json"
    pkt.write_text(json.dumps(_packet()), encoding="utf-8")

    ran = {"n": 0}

    def boom(self, ctx):
        ran["n"] += 1
        return []

    monkeypatch.setattr("conduit.detect.modules.openai.OpenAIModule.run", boom)

    result = CliRunner().invoke(
        app,
        [
            "run",
            "--path",
            str(tmp_path),
            "--packet",
            str(pkt),
            "--skip-pr",
            "--skip-tests",
            "--skip-export-delta",
            "--skip-lockfile",
        ],
    )
    assert result.exit_code == 0, result.output
    assert ran["n"] == 0
    assert "vendor detect scrape skipped" in result.output
    sources = list((tmp_path / ".conduit" / "source-packets").glob("*.json"))
    assert sources, result.output
    text = (tmp_path / "app.py").read_text(encoding="utf-8")
    assert "gpt-4o" in text


def test_run_packet_url(tmp_path: Path, monkeypatch):
    from typer.testing import CliRunner

    _disable_llm(monkeypatch)
    (tmp_path / "app.py").write_text(
        "import openai\nmodel = 'gpt-4-0613'\n", encoding="utf-8"
    )
    payload = json.dumps(_packet()).encode("utf-8")

    def fake_get(self, url, **kwargs):
        return _FakeResponse(body=payload)

    monkeypatch.setattr(httpx.Client, "get", fake_get)

    result = CliRunner().invoke(
        app,
        [
            "run",
            "--path",
            str(tmp_path),
            "--packet",
            "https://cdn.example.com/openai-pypi-1.0.0.json",
            "--skip-pr",
            "--skip-tests",
            "--skip-export-delta",
            "--skip-lockfile",
        ],
    )
    assert result.exit_code == 0, result.output
    cached = tmp_path / ".conduit" / "packets" / "openai-pypi-1.0.0.json"
    assert cached.is_file()
    assert "gpt-4o" in (tmp_path / "app.py").read_text(encoding="utf-8")


def test_cli_invalid_packet_url(tmp_path: Path, monkeypatch):
    from typer.testing import CliRunner

    _disable_llm(monkeypatch)

    def fake_get(self, url, **kwargs):
        return _FakeResponse(body=b"nope")

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    result = CliRunner().invoke(
        app,
        [
            "apply",
            "--path",
            str(tmp_path),
            "--packet",
            "https://example.com/not-a-packet.json",
        ],
    )
    assert result.exit_code == 2
    assert "JSON" in result.output
