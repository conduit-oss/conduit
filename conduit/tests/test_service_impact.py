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
from conduit.impact.intersect import _method_match
from conduit.impact.model import ApiChange
from conduit.impact.run import NO_PIN_NOTE, collect_impacts, service_pins
from conduit.impact.webhook_schema import diff_webhook_schemas
from conduit.main import app
from conduit.outside_in.analyze import analyze_repo
from conduit.outside_in.specs import evidence_spec

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "examples" / "service-clients"
IMPACT = Path(__file__).resolve().parents[1] / "fixtures" / "impact"
FEEDS = Path(__file__).resolve().parents[1] / "fixtures" / "feeds"
STUBS = Path(__file__).resolve().parents[1] / "tests" / "stubs"


def _map():
    return analyze_repo(FIXTURE)


def _impacts():
    service_map = _map()
    found = []
    diffed: set[str] = set()
    for dep in ("sqlalchemy", "boto3"):
        impacts, covered, _notes = collect_impacts(
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
    assert "pydantic" not in pins
    assert "fastapi" not in pins
    assert evidence_spec("pydantic") is None
    assert evidence_spec("requests") is None


def test_map_finds_evidence_services_and_called_third_party_dists():
    service_map = _map()
    assert set(service_map.services()) >= {"sqlalchemy", "boto3", "stripe"}
    from conduit.outside_in.report import format_report

    rendered = format_report(service_map, markdown=True)
    assert "evidence: `" in rendered
    assert "->" in rendered
    assert "pydantic" not in service_map.services()
    assert "fastapi" not in service_map.services()
    assert not any(chain.package == "requests" or "example.com" in chain.sdk_method for chain in service_map.chains)

    query_get = [
        chain
        for chain in service_map.chains
        if chain.package == "sqlalchemy" and chain.sdk_method.endswith("Query.from_self")
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


def test_wheel_diff_renames_a_parameter_and_drops_from_self():
    acme = diff_wheels(
        STUBS / "acme-sdk" / "wheels" / "acme_sdk-1.0.0-py3-none-any.whl",
        STUBS / "acme-sdk" / "wheels" / "acme_sdk-1.2.0-py3-none-any.whl",
    )
    renamed = next(item for item in acme if item.kind == "param_renamed")
    assert renamed.old == "max_tokens"
    assert renamed.new == "max_completion_tokens"
    assert renamed.confidence == "medium"
    index = json.loads((IMPACT / "index.json").read_text(encoding="utf-8"))
    wheels = index["packages"]["sqlalchemy"]["wheels"]
    old = IMPACT / wheels["1.4.52"]
    new = IMPACT / wheels["2.1.4"]
    api = load_api(old)
    assert any(name.endswith("Query.from_self") for name in api)
    sa = diff_wheels(old, new)
    assert any(item.kind == "removed" and item.symbol.endswith("Query.from_self") for item in sa)


def test_service_model_and_webhook_stubs_still_diff():
    """Hand-written documents stay in the unit stubs. They are not recordings."""
    changes = diff_service_models(
        STUBS / "service-model" / "s3-2006-03-01-old.json",
        STUBS / "service-model" / "s3-2006-03-01-new.json",
        service="s3",
    )
    assert any(item.kind == "param_removed" and item.old == "ACL" for item in changes)
    assert any(item.kind == "field_removed" and item.old == "VersionId" for item in changes)
    hooks = diff_webhook_schemas(
        STUBS / "webhooks" / "stripe-2024-06-20.openapi.yaml",
        STUBS / "webhooks" / "stripe-2024-11-20.openapi.yaml",
        provider="stripe",
    )
    assert any(item.kind == "field_removed" and item.symbol == "data.object.currency" for item in hooks)
    assert any(item.kind == "field_renamed" and item.old == "data.object.amount" and item.new.endswith("amount_minor") for item in hooks)


def test_impact_intersects_each_kind_and_skips_unread_fields():
    impacts, _diffed = _impacts()
    blob = json.dumps([item.to_dict() for item in impacts])
    assert "sqlalchemy" in blob
    assert "Query.from_self removed" in blob
    assert "db.py" in blob
    assert "pydantic" not in blob
    assert "fastapi" not in blob
    assert "acme-sdk" not in blob
    query = next(item for item in impacts if item.dependency == "sqlalchemy" and "from_self" in item.change)
    direct = next(site for site in query.sites if site.expr == "query.from_self")
    assert direct.confidence == "high"
    assert direct.path == "db.py"
    assert query.from_version == "1.4.52"
    assert query.to_version == "2.1.4"
    assert "boto3" in _diffed


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
            "sqlalchemy",
            "--fixtures",
            str(IMPACT),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["impacts"]
    assert payload["impacts"][0]["dependency"] == "sqlalchemy"
    assert any("from_self" in item["change"] for item in payload["impacts"])
    assert any(site["path"] == "db.py" for item in payload["impacts"] for site in item["sites"])

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
    deps = {item["dependency"] for item in result.impacts}
    assert "sqlalchemy" in deps

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
    assert "db.py" in text.stdout
    assert "from_self" in text.stdout


def test_recorded_wheels_parse_as_python():
    api = load_api(STUBS / "acme-sdk" / "wheels" / "acme_sdk-1.0.0-py3-none-any.whl")
    assert any(name.endswith("Client.complete") for name in api)
    ast.parse((FIXTURE / "webhooks.py").read_text(encoding="utf-8"))


def test_pair_match_uses_class_and_method_not_the_bare_name():
    symbol = "openai.api_resources.audio.Audio.transcribe"
    change = ApiChange(
        kind="removed",
        symbol=symbol,
        old=symbol,
        new="",
        detail="Audio.transcribe removed",
        confidence="high",
        source="wheel",
    )
    assert _method_match("openai.Audio.transcribe", symbol, change, [change]) == "pair"
    assert _method_match("helper.transcribe", symbol, change, [change]) is None
    other = ApiChange(
        kind="removed",
        symbol="openai.other.Audio.transcribe",
        old="openai.other.Audio.transcribe",
        new="",
        detail="Audio.transcribe removed",
        confidence="high",
        source="wheel",
    )
    assert _method_match("openai.Audio.transcribe", symbol, change, [change, other]) is None


def test_openai_public_names_unpinned_exceptions_and_dict_response(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("conduit.impact.versions.installed_version", lambda _package: "")
    """Trimmed 0.28-shaped wheels: internal paths, no pin, retry, dict access."""
    fixtures = tmp_path / "impact"
    wheels = fixtures / "wheels"
    wheels.mkdir(parents=True)
    old = wheels / "openai-0.28.1-py3-none-any.whl"
    new = wheels / "openai-1.0.0-py3-none-any.whl"
    _write_wheel(old, "0.28.1", _OLD_OPENAI)
    _write_wheel(new, "1.0.0", _NEW_OPENAI)
    (fixtures / "index.json").write_text(
        json.dumps(
            {
                "packages": {
                    "openai": {
                        "from": "0.28.1",
                        "to": "1.0.0",
                        "wheels": {
                            "0.28.1": "wheels/openai-0.28.1-py3-none-any.whl",
                            "1.0.0": "wheels/openai-1.0.0-py3-none-any.whl",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    changes = diff_wheels(old, new)
    removed = next(
        item
        for item in changes
        if item.kind == "removed" and item.symbol.endswith("chat_completion.ChatCompletion.create")
    )
    assert "openai.ChatCompletion.create" in removed.aliases
    legacy = next(
        item
        for item in changes
        if item.kind == "removed" and item.symbol.endswith("legacy.ChatCompletion.create")
    )
    assert "openai.ChatCompletion.create" not in legacy.aliases
    assert any(
        item.kind == "response_shape"
        and item.symbol.endswith("chat_completion.ChatCompletion.create")
        and item.confidence == "medium"
        for item in changes
    )
    assert any(item.kind == "removed" and item.symbol.endswith("error.RateLimitError") for item in changes)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(
        "\n".join(
            [
                "import openai",
                "from tenacity import retry_if_exception_type",
                "",
                "@retry_if_exception_type(openai.error.RateLimitError)",
                "def ask(prompt, exc):",
                "    resp = openai.ChatCompletion.create(model='gpt-3.5-turbo', messages=[])",
                "    text = resp['choices'][0]['message']['content']",
                "    openai.Audio.transcribe('a.wav')",
                "    if isinstance(exc, openai.error.APIError):",
                "        return openai.error.Timeout",
                "    try:",
                "        return text",
                "    except openai.error.ServiceUnavailableError:",
                "        raise",
                "",
            ]
        ),
        encoding="utf-8",
    )
    service_map = analyze_repo(repo)
    impacts, _diffed, notes = collect_impacts(
        repo,
        service_map,
        fixtures=fixtures,
        dep="openai",
        allow_network=False,
    )
    assert impacts == []
    assert notes == [NO_PIN_NOTE]
    bare = CliRunner().invoke(
        app,
        ["impact", "--path", str(repo), "--dep", "openai", "--fixtures", str(fixtures)],
    )
    assert bare.exit_code == 0, bare.stdout
    assert NO_PIN_NOTE in bare.stdout
    assert "No call sites" not in bare.stdout

    (repo / "requirements.txt").write_text("openai==0.27.0\n", encoding="utf-8")
    _impacts, _diffed, mismatched = collect_impacts(
        repo,
        service_map,
        fixtures=fixtures,
        dep="openai",
        allow_network=False,
    )
    assert _impacts == []
    assert any("0.27.0" in note and note.startswith("not compared:") for note in mismatched)
    refused = CliRunner().invoke(
        app,
        ["impact", "--path", str(repo), "--dep", "openai", "--fixtures", str(fixtures)],
    )
    assert "No call sites" not in refused.stdout
    assert "0.27.0" in refused.stdout

    impacts, _diffed, notes = collect_impacts(
        repo,
        service_map,
        fixtures=fixtures,
        dep="openai",
        from_version="0.28.1",
        allow_network=False,
    )
    assert notes == []
    create = next(item for item in impacts if item.change == "ChatCompletion.create removed")
    assert create.confidence == "high"
    assert create.from_version == "0.28.1"
    assert create.to_version == "1.0.0"
    assert any(site.expr == "openai.ChatCompletion.create" and site.confidence == "high" for site in create.sites)
    audio = next(item for item in impacts if "Audio.transcribe removed" in item.change)
    assert audio.confidence == "medium"
    assert any("Audio.transcribe" in site.expr for site in audio.sites)
    shape = next(item for item in impacts if item.change.startswith("ChatCompletion.create return changed"))
    assert shape.confidence == "medium"
    assert any("choices" in site.expr and site.confidence == "medium" for site in shape.sites)
    assert any("RateLimitError" in site.expr for item in impacts for site in item.sites)
    assert any("APIError" in site.expr for item in impacts for site in item.sites)
    assert any("ServiceUnavailableError" in site.expr for item in impacts for site in item.sites)
    assert any(site.expr == "openai.error.Timeout" for item in impacts for site in item.sites)

    cli = CliRunner().invoke(
        app,
        [
            "impact",
            "--path",
            str(repo),
            "--dep",
            "openai",
            "--from",
            "0.28.1",
            "--fixtures",
            str(fixtures),
        ],
    )
    assert cli.exit_code == 0, cli.stdout
    assert "ChatCompletion.create removed" in cli.stdout
    assert "RateLimitError" in cli.stdout
    assert "choices" in cli.stdout
    assert "not compared" not in cli.stdout


def _write_wheel(path: Path, version: str, files: dict[str, str]) -> None:
    import zipfile

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"openai-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: openai\nVersion: {version}\n",
        )
        archive.writestr(
            f"openai-{version}.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: conduit-fixtures\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        for name, text in files.items():
            archive.writestr(name, text)


_OLD_OPENAI = {
    "openai/__init__.py": (
        "from openai.api_resources import ChatCompletion\n"
        "from openai.error import APIError, RateLimitError\n"
    ),
    "openai/api_resources/__init__.py": (
        "from openai.api_resources.chat_completion import ChatCompletion\n"
    ),
    "openai/api_resources/chat_completion.py": (
        "from openai.api_resources.abstract.engine_api_resource import EngineAPIResource\n"
        "\n"
        "class ChatCompletion(EngineAPIResource):\n"
        "    @classmethod\n"
        "    def create(cls, *args, **kwargs):\n"
        "        return super().create(*args, **kwargs)\n"
    ),
    "openai/api_resources/legacy.py": (
        "class ChatCompletion:\n"
        "    @classmethod\n"
        "    def create(cls, *args, **kwargs):\n"
        "        return {}\n"
    ),
    "openai/api_resources/audio.py": (
        "from openai import util\n"
        "\n"
        "class Audio:\n"
        "    @classmethod\n"
        "    def transcribe(cls, *args, **kwargs):\n"
        "        return util.convert_to_openai_object({})\n"
    ),
    "openai/api_resources/abstract/__init__.py": "",
    "openai/api_resources/abstract/engine_api_resource.py": (
        "from openai import util\n"
        "\n"
        "class EngineAPIResource:\n"
        "    @classmethod\n"
        "    def create(cls, *args, **kwargs):\n"
        "        obj = util.convert_to_openai_object({})\n"
        "        return obj\n"
    ),
    "openai/openai_object.py": (
        "class OpenAIObject(dict):\n"
        "    @classmethod\n"
        "    def construct_from(cls, values):\n"
        "        return cls(values)\n"
    ),
    "openai/util.py": (
        "from openai.openai_object import OpenAIObject\n"
        "\n"
        "def convert_to_openai_object(resp, *args, **kwargs):\n"
        "    klass = OpenAIObject\n"
        "    return klass.construct_from(resp)\n"
    ),
    "openai/error.py": (
        "class OpenAIError(Exception):\n"
        "    pass\n"
        "\n"
        "class APIError(OpenAIError):\n"
        "    pass\n"
        "\n"
        "class RateLimitError(OpenAIError):\n"
        "    pass\n"
        "\n"
        "class Timeout(OpenAIError):\n"
        "    pass\n"
        "\n"
        "class ServiceUnavailableError(OpenAIError):\n"
        "    pass\n"
    ),
}

_NEW_OPENAI = {
    "openai/__init__.py": "class OpenAI:\n    pass\n",
    "openai/_models.py": (
        "import pydantic\n"
        "\n"
        "class BaseModel(pydantic.BaseModel):\n"
        "    pass\n"
    ),
    "openai/types/__init__.py": "",
    "openai/types/chat_completion.py": (
        "from openai._models import BaseModel\n"
        "\n"
        "class ChatCompletion(BaseModel):\n"
        "    choices: list\n"
    ),
}
