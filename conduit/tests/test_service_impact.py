"""Line-by-line impact for outside services. Offline fixtures only."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from typer.testing import CliRunner

from conduit.feeds.client import FeedClient
from conduit.feeds.watch import watch_outside
from conduit.impact.api import diff_wheels, load_api
from conduit.impact.botocore import diff_service_models
from conduit.impact.run import collect_impacts, service_pins
from conduit.impact.webhook_schema import diff_webhook_schemas
from conduit.main import app
from conduit.outside_in.analyze import analyze_repo
from conduit.outside_in.catalog import catalog_spec

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "examples" / "service-clients"
IMPACT = Path(__file__).resolve().parents[1] / "fixtures" / "impact"
FEEDS = Path(__file__).resolve().parents[1] / "fixtures" / "feeds"


def _map():
    return analyze_repo(FIXTURE)


def _impacts():
    service_map = _map()
    found = []
    diffed: set[str] = set()
    for dep in ("sqlalchemy", "boto3", "stripe", "acme-sdk"):
        impacts, covered = collect_impacts(
            FIXTURE,
            service_map,
            fixtures=IMPACT,
            dep=dep,
            allow_network=False,
        )
        found.extend(impacts)
        diffed |= covered
    return found, diffed


def test_pins_ignore_ordinary_libraries():
    pins = service_pins(FIXTURE)
    assert pins["sqlalchemy"] == "1.4.52"
    assert pins["boto3"] == "1.34.0"
    assert pins["acme-sdk"] == "1.0.0"
    assert "pydantic" not in pins
    assert "fastapi" not in pins
    assert catalog_spec("pydantic") is None
    assert catalog_spec("requests") is None


def test_map_finds_each_outside_service_and_skips_ordinary_libraries():
    service_map = _map()
    assert set(service_map.services()) >= {"sqlalchemy", "boto3", "stripe", "acme"}
    assert "pydantic" not in service_map.services()
    assert not any(chain.call_rel == "unrelated.py" for chain in service_map.chains)

    query_get = [
        chain
        for chain in service_map.chains
        if chain.package == "sqlalchemy" and chain.sdk_method.endswith("Query.get")
    ]
    assert query_get
    assert query_get[0].call_rel == "db.py"
    assert query_get[0].kind == "db_call"

    put = [
        chain
        for chain in service_map.chains
        if chain.sdk_method == "s3.put_object"
    ]
    assert put
    assert put[0].kind == "cloud_call"
    assert "ACL" in put[0].params
    assert any("ETag" in field for field in put[0].response_fields)

    hooks = [chain for chain in service_map.chains if chain.kind == "webhook"]
    assert hooks
    assert hooks[0].service == "stripe"
    assert any(chain.response_fields == ("data.object.currency",) for chain in hooks)

    complete = [
        chain
        for chain in service_map.chains
        if chain.package == "acme-sdk" and chain.sdk_method.endswith("Client.complete")
    ]
    assert complete
    assert "max_tokens" in complete[0].params


def test_database_and_cloud_families_map_from_imports(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "\n".join(
            [
                "import psycopg",
                "from google.cloud import storage",
                "from azure.storage.blob import BlobClient",
                "",
                "def save(conn, bucket, blob):",
                "    psycopg.connect('postgresql://db')",
                "    client = storage.Client()",
                "    client.bucket(bucket)",
                "    BlobClient.from_connection_string('x').upload_blob(blob)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    service_map = analyze_repo(tmp_path)
    services = set(service_map.services())
    assert "psycopg" in services
    assert "google-cloud-storage" in services
    assert "azure-storage-blob" in services
    assert any(chain.kind in {"db_call", "db_constructor"} for chain in service_map.chains)
    assert any("upload_blob" in chain.sdk_method for chain in service_map.chains)


def test_wheel_diff_renames_a_parameter_and_drops_query_get():
    acme = diff_wheels(
        IMPACT / "wheels" / "acme_sdk-1.0.0-py3-none-any.whl",
        IMPACT / "wheels" / "acme_sdk-1.2.0-py3-none-any.whl",
    )
    renamed = next(item for item in acme if item.kind == "param_renamed")
    assert renamed.old == "max_tokens"
    assert renamed.new == "max_completion_tokens"
    assert renamed.confidence == "medium"
    api = load_api(IMPACT / "wheels" / "sqlalchemy-1.4.52-py3-none-any.whl")
    assert "sqlalchemy.orm.Query.get" in api
    sa = diff_wheels(
        IMPACT / "wheels" / "sqlalchemy-1.4.52-py3-none-any.whl",
        IMPACT / "wheels" / "sqlalchemy-2.0.36-py3-none-any.whl",
    )
    assert any(item.kind == "removed" and item.symbol.endswith("Query.get") for item in sa)
    assert any(item.kind == "deprecated" and item.symbol.endswith("Session.query") for item in sa)


def test_botocore_and_stripe_schema_diffs_are_trimmed_fixtures():
    changes = diff_service_models(
        IMPACT / "botocore" / "s3-2006-03-01-old.json",
        IMPACT / "botocore" / "s3-2006-03-01-new.json",
        service="s3",
    )
    assert any(item.kind == "param_removed" and item.old == "ACL" for item in changes)
    assert any(item.kind == "field_removed" and item.old == "VersionId" for item in changes)
    hooks = diff_webhook_schemas(
        IMPACT / "webhooks" / "stripe-2024-06-20.openapi.yaml",
        IMPACT / "webhooks" / "stripe-2024-11-20.openapi.yaml",
        provider="stripe",
    )
    assert any(item.kind == "field_removed" and item.symbol == "data.object.currency" for item in hooks)
    assert any(item.kind == "field_renamed" and item.old == "data.object.amount" and item.new.endswith("amount_minor") for item in hooks)


def test_impact_intersects_each_kind_and_skips_unread_fields():
    impacts, _diffed = _impacts()
    blob = json.dumps([item.to_dict() for item in impacts])
    assert "sqlalchemy" in blob
    assert "Query.get removed" in blob
    assert "db.py" in blob
    assert "boto3" in blob
    assert "ACL" in blob
    assert "storage.py" in blob
    assert "VersionId" not in blob
    assert "stripe" in blob
    assert "data.object.currency" in blob
    assert "webhooks.py" in blob
    assert "acme-sdk" in blob
    assert "max_tokens" in blob
    assert "acme_client.py" in blob
    assert "pydantic" not in blob
    assert "fastapi" not in blob

    acme = [item for item in impacts if item.dependency == "acme-sdk"]
    assert any(item.confidence == "medium" and item.source == "wheel" for item in acme)
    assert any(item.confidence == "low" and item.source == "changelog" for item in acme)
    query = next(item for item in impacts if item.dependency == "sqlalchemy" and "Query.get" in item.change)
    direct = next(site for site in query.sites if site.expr == "query.get")
    assert direct.confidence == "high"
    assert direct.path == "db.py"
    assert query.from_version == "1.4.52"
    assert query.to_version == "2.0.36"
    hook = next(item for item in impacts if item.dependency == "stripe")
    assert hook.from_version == "2024-06-20"
    assert hook.to_version == "2024-11-20"
    assert any(site.path == "webhooks.py" for site in hook.sites)


def test_impact_cli_is_offline(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("live network")

    monkeypatch.setattr("httpx.get", boom)
    monkeypatch.setattr("httpx.stream", boom)
    monkeypatch.setattr("httpx.Client", boom)
    result = CliRunner().invoke(
        app,
        [
            "impact",
            "--path",
            str(FIXTURE),
            "--dep",
            "boto3",
            "--fixtures",
            str(IMPACT),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["impacts"]
    assert payload["impacts"][0]["dependency"] == "boto3"
    assert any(site["path"] == "storage.py" for item in payload["impacts"] for site in item["sites"])

    refused = CliRunner().invoke(
        app,
        ["impact", "--path", str(FIXTURE), "--dep", "pydantic", "--fixtures", str(IMPACT)],
    )
    assert refused.exit_code == 2
    assert "not an outside service" in refused.stdout


def test_watch_prints_impact_instead_of_a_bare_version_bump():
    result = watch_outside(FIXTURE, client=FeedClient(root=FEEDS, allow_network=False))
    assert result.warnings == [], result.warnings
    packages = {str(packet.get("package") or "") for packet in result.packets}
    assert "sqlalchemy" not in packages
    assert "boto3" not in packages
    assert "acme-sdk" not in packages
    deps = {item["dependency"] for item in result.impacts}
    assert deps >= {"sqlalchemy", "boto3", "stripe", "acme-sdk"}

    cli = CliRunner().invoke(
        app,
        ["watch", "--path", str(FIXTURE), "--feeds", str(FEEDS), "--json"],
    )
    assert cli.exit_code == 0, cli.stdout
    body = json.loads(cli.stdout)
    assert any(item["dependency"] == "sqlalchemy" for item in body["impacts"])
    text = CliRunner().invoke(
        app,
        ["watch", "--path", str(FIXTURE), "--feeds", str(FEEDS)],
    )
    assert text.exit_code == 0, text.stdout
    assert "storage.py" in text.stdout
    assert "webhooks.py" in text.stdout
    assert "db.py" in text.stdout
    assert "acme_client.py" in text.stdout


def test_recorded_wheels_parse_as_python():
    api = load_api(IMPACT / "wheels" / "acme_sdk-1.0.0-py3-none-any.whl")
    assert any(name.endswith("Client.complete") for name in api)
    ast.parse((FIXTURE / "webhooks.py").read_text(encoding="utf-8"))
