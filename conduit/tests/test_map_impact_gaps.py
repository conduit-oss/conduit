"""Call-graph and impact gaps: aliases, local imports, versions, responses, REST."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from typer.testing import CliRunner

from conduit.impact.api import diff_wheels
from conduit.impact.intersect import intersect
from conduit.impact.model import ApiChange
from conduit.impact.notes import endpoint_changes_from_notes
from conduit.impact.openapi_rest import diff_openapi
from conduit.impact.report import format_impacts
from conduit.impact.run import NO_PIN_NOTE, collect_impacts
from conduit.impact.versions import resolve_client_version
from conduit.main import app
from conduit.outside_in.analyze import analyze_repo


def _write(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")


def _chains(root: Path, *, symbol: str | None = None, expr: str | None = None):
    found = analyze_repo(root).chains
    if symbol is not None:
        found = [chain for chain in found if chain.call_symbol == symbol]
    if expr is not None:
        found = [chain for chain in found if expr in chain.call_expr or expr in chain.sdk_method]
    return found


def test_partial_getattr_literal_and_lambda_reach_the_sdk(tmp_path: Path):
    _write(
        tmp_path,
        "aliases.py",
        """
        import functools
        import openai

        complete = functools.partial(openai.ChatCompletion.create, model="gpt-4")
        transcribe = getattr(openai.Audio, "transcribe")
        hidden = lambda prompt: openai.Completion.create(model="x", prompt=prompt)

        def ask(prompt):
            return complete(messages=[{"role": "user", "content": prompt}])

        def audio():
            return transcribe("a.wav")

        def legacy(prompt):
            return hidden(prompt)
        """,
    )
    partial_calls = _chains(tmp_path, symbol="ask", expr="complete")
    assert partial_calls
    assert any(item.sdk_method.endswith("ChatCompletion.create") for item in partial_calls)

    getattr_calls = _chains(tmp_path, symbol="audio", expr="transcribe")
    assert getattr_calls
    assert any(item.sdk_method.endswith("Audio.transcribe") for item in getattr_calls)

    direct = [
        chain
        for chain in analyze_repo(tmp_path).chains
        if chain.call_rel == "aliases.py"
        and not chain.via_wrapper
        and chain.sdk_method.endswith("Completion.create")
    ]
    assert direct, "lambda body should keep the SDK call as the primary line"
    callers = _chains(tmp_path, symbol="legacy", expr="hidden")
    assert callers
    assert callers[0].via_wrapper
    assert callers[0].sdk_method.endswith("Completion.create")


def test_nested_module_does_not_shadow_the_dependency(tmp_path: Path):
    _write(
        tmp_path,
        "app/providers/stripe.py",
        """
        import stripe

        class StripeProvider:
            def __init__(self, key):
                self.client = stripe.StripeClient(key)
        """,
    )
    chains = [
        chain
        for chain in analyze_repo(tmp_path).chains
        if chain.sdk_method.endswith("StripeClient") and not chain.via_wrapper
    ]
    assert chains


def test_variable_getattr_stays_unresolved(tmp_path: Path):
    _write(
        tmp_path,
        "dynamic.py",
        """
        import openai

        def hidden(method):
            return getattr(openai, method).create()
        """,
    )
    assert not any("ChatCompletion" in chain.sdk_method for chain in analyze_repo(tmp_path).chains)


def test_local_and_enclosing_imports_are_calls(tmp_path: Path):
    _write(
        tmp_path,
        "local_import.py",
        """
        def ask(prompt):
            import openai
            return openai.ChatCompletion.create(model="gpt-4", messages=[])

        def outer():
            from openai import OpenAI

            def inner(prompt):
                client = OpenAI()
                return client.chat.completions.create(model="gpt-4", messages=[])

            return inner(prompt)
        """,
    )
    legacy = _chains(tmp_path, symbol="ask")
    assert any(item.sdk_method.endswith("ChatCompletion.create") for item in legacy)
    modern = _chains(tmp_path, symbol="outer.inner")
    assert any("chat.completions.create" in item.sdk_method for item in modern)


def test_version_branch_is_an_impact_when_the_dependency_changes(tmp_path: Path):
    _write(
        tmp_path,
        "gate.py",
        """
        import importlib.metadata
        import openai
        from packaging.version import Version

        def ask():
            if Version(openai.__version__) < Version("1.0.0"):
                return openai.ChatCompletion.create(model="gpt-4", messages=[])
            if importlib.metadata.version("openai") >= "1.0.0":
                client = openai.OpenAI()
                return client.chat.completions.create(model="gpt-4", messages=[])

        def legacy_audio():
            if int(str(getattr(openai, "__version__", "0")).split(".")[0]) < 1:
                return openai.Audio.transcribe("a.wav")
        """,
    )
    gates = [chain for chain in analyze_repo(tmp_path).chains if chain.kind == "version_gate"]
    assert len(gates) >= 3
    assert any("getattr" in chain.call_expr for chain in gates)
    assert all(chain.package == "openai" for chain in gates)

    fixtures = tmp_path / "impact"
    fixtures.mkdir()
    (fixtures / "index.json").write_text(
        json.dumps({"packages": {"openai": {"from": "0.28.1", "to": "1.0.0"}}}),
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("openai==0.28.1\n", encoding="utf-8")
    impacts, _diffed, notes = collect_impacts(
        tmp_path, analyze_repo(tmp_path), fixtures=fixtures, dep="openai", allow_network=False
    )
    branch = next(item for item in impacts if item.change == "code branches on openai version")
    assert branch.from_version == "0.28.1"
    assert branch.to_version == "1.0.0"
    assert len(branch.sites) >= 2
    assert notes == [] or all("REST" in note or "not compared" in note for note in notes)


def test_response_aliases_are_their_own_lines(tmp_path: Path):
    _write(
        tmp_path,
        "read.py",
        """
        import openai

        def ask():
            resp = openai.ChatCompletion.create(model="gpt-4", messages=[])
            choice = resp.choices[0]
            message = choice.message
            return message.content

        def raw():
            return openai.ChatCompletion.create(model="gpt-4", messages=[])

        def caller():
            data = raw()
            return data["choices"][0]["message"]["content"]

        def stream():
            res = openai.ChatCompletion.create(model="gpt-4", messages=[], stream=True)
            for chunk in res:
                if len(chunk["choices"]) > 0:
                    text = chunk["choices"][0]["delta"].get("content", "")
            return text
        """,
    )
    chains = analyze_repo(tmp_path).chains
    response = [chain for chain in chains if chain.kind.endswith("_response")]
    exprs = " ".join(chain.call_expr for chain in response)
    assert "resp.choices[0]" in exprs or ".choices" in exprs
    assert "choice.message" in exprs
    assert "message.content" in exprs
    assert any("choices" in chain.call_expr and chain.call_symbol == "caller" for chain in response)
    assert any("chunk[" in chain.call_expr and "choices" in chain.call_expr for chain in response)


def test_awaited_response_is_tracked(tmp_path: Path):
    _write(
        tmp_path,
        "async_read.py",
        """
        import openai

        async def ask():
            response = await openai.ChatCompletion.acreate(model="gpt-4", messages=[])
            return response.choices[0].message["content"]
        """,
    )
    response = [chain for chain in analyze_repo(tmp_path).chains if chain.kind.endswith("_response")]
    assert any("choices" in chain.call_expr for chain in response)


def test_wrapper_callers_collapse_unless_requested(tmp_path: Path):
    _write(
        tmp_path,
        "llm.py",
        """
        import openai

        def complete(prompt):
            return openai.ChatCompletion.create(model="gpt-4", messages=[], max_tokens=16)
        """,
    )
    _write(
        tmp_path,
        "app.py",
        """
        import llm

        def ask(prompt):
            return llm.complete(prompt)

        def main():
            return ask("hi")
        """,
    )
    change = ApiChange(
        kind="removed",
        symbol="openai.ChatCompletion.create",
        old="openai.ChatCompletion.create",
        new="",
        detail="ChatCompletion.create removed",
        confidence="high",
        source="wheel",
        aliases=("openai.ChatCompletion.create",),
    )
    impacts = intersect(
        [change],
        analyze_repo(tmp_path).chains,
        dependency="openai",
        from_version="0.28.1",
        to_version="1.0.0",
    )
    text = format_impacts(impacts)
    assert "llm.py" in text
    assert "ChatCompletion.create" in text
    assert "wrapper caller" in text
    assert "app.py" not in text
    expanded = format_impacts(impacts, show_callers=True)
    assert "app.py" in expanded
    assert "wrapper callers" in expanded
    runner = CliRunner().invoke(
        app,
        ["impact", "--path", str(tmp_path), "--dep", "openai", "--help"],
        env={"COLUMNS": "120", "TERM": "dumb", "NO_COLOR": "1"},
    )
    assert runner.exit_code == 0
    help_text = runner.stdout or runner.output or ""
    assert "--show-callers" in help_text


def test_unpinned_versions_resolve_from_locks_installs_and_pypi(tmp_path: Path, monkeypatch):
    # CI installs ``./conduit[llm]``, which puts openai on the runner. The empty
    # tree has no pin; hide the site-packages hit so this case stays about the
    # repo, not the test environment.
    monkeypatch.setattr("conduit.impact.versions.installed_version", lambda _package: "")
    empty = tmp_path / "empty"
    empty.mkdir()
    version, reason = resolve_client_version(empty, "openai", allow_network=False)
    assert version == ""
    assert reason == NO_PIN_NOTE
    assert "lockfile" in reason
    assert "installed distribution" in reason

    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "pyproject.toml").write_text(
        '[project]\nname="demo"\ndependencies=["openai>=0.27,<1"]\n',
        encoding="utf-8",
    )
    (locked / "poetry.lock").write_text(
        '[[package]]\nname = "openai"\nversion = "0.28.1"\n',
        encoding="utf-8",
    )
    assert resolve_client_version(locked, "openai", allow_network=False) == ("0.28.1", "")

    uv_root = tmp_path / "uv"
    uv_root.mkdir()
    (uv_root / "uv.lock").write_text(
        '[[package]]\nname = "stripe"\nversion = "5.0.0"\n',
        encoding="utf-8",
    )
    assert resolve_client_version(uv_root, "stripe", allow_network=False) == ("5.0.0", "")

    ranged = tmp_path / "range"
    ranged.mkdir()
    (ranged / "requirements.txt").write_text("openai>=0.27,<1\n", encoding="utf-8")
    offline, why = resolve_client_version(ranged, "openai", allow_network=False)
    assert offline == ""
    assert "network is disabled" in why
    assert ">=0.27,<1" in why

    monkeypatch.setattr(
        "conduit.impact.versions._pypi_json",
        lambda _package: {
            "info": {"version": "1.30.0"},
            "releases": {"0.27.0": [{}], "0.28.1": [{}], "1.0.0": [{}]},
        },
    )
    assert resolve_client_version(ranged, "openai", allow_network=True) == ("0.28.1", "")

    monkeypatch.setattr("conduit.impact.versions.installed_version", lambda _package: "0.28.0")
    bare = tmp_path / "bare"
    bare.mkdir()
    assert resolve_client_version(bare, "openai", allow_network=False) == ("0.28.0", "")


def test_rest_urls_and_openapi_fields(tmp_path: Path):
    _write(
        tmp_path,
        "rest_clear.py",
        """
        import aiohttp
        import httpx
        import requests

        HOST = "https://api.openai.com"
        client = httpx.Client(base_url="https://api.stripe.com")

        def charge():
            return client.post("/v1/charges", json={"amount": 1, "currency": "usd"})

        def models(name):
            return requests.get(f"{HOST}/v1/models/{name}")

        async def session_call():
            async with aiohttp.ClientSession(base_url="https://api.openai.com/v1") as session:
                async with session.post("/chat/completions", json={"model": "gpt-4"}) as resp:
                    return resp
        """,
    )
    chains = analyze_repo(tmp_path).chains
    http = [chain for chain in chains if chain.kind == "http_call" and not chain.via_wrapper]
    methods = " ".join(chain.sdk_method for chain in http)
    assert "api.stripe.com/v1/charges" in methods
    assert "api.openai.com/v1/models/" in methods
    assert "api.openai.com/v1/chat/completions" in methods
    charge = next(chain for chain in http if "charges" in chain.sdk_method)
    assert "amount" in charge.params

    old = tmp_path / "old.yaml"
    new = tmp_path / "new.yaml"
    old.write_text(
        textwrap.dedent(
            """
            openapi: 3.0.0
            paths:
              /v1/charges:
                post:
                  operationId: createCharge
                  requestBody:
                    content:
                      application/json:
                        schema:
                          type: object
                          properties:
                            amount: {type: integer}
                            currency: {type: string}
                  responses:
                    "200":
                      content:
                        application/json:
                          schema:
                            type: object
                            properties:
                              id: {type: string}
                              paid: {type: boolean}
              /v1/legacy:
                get:
                  operationId: legacy
                  responses:
                    "200":
                      description: ok
            """
        ).lstrip(),
        encoding="utf-8",
    )
    new.write_text(
        textwrap.dedent(
            """
            openapi: 3.0.0
            paths:
              /v1/payment_intents:
                post:
                  operationId: createCharge
                  responses:
                    "200":
                      description: ok
              /v1/charges:
                post:
                  operationId: createChargeV2
                  requestBody:
                    content:
                      application/json:
                        schema:
                          type: object
                          properties:
                            amount: {type: integer}
                  responses:
                    "200":
                      content:
                        application/json:
                          schema:
                            type: object
                            properties:
                              id: {type: string}
            """
        ).lstrip(),
        encoding="utf-8",
    )
    changes = diff_openapi(old, new, service="stripe")
    assert any(item.kind == "endpoint_renamed" and item.old == "/v1/charges" for item in changes)
    assert any(item.kind == "endpoint_removed" and item.old == "/v1/legacy" for item in changes)
    assert any(item.kind == "field_removed" and item.old == "currency" and "request field" in item.detail for item in changes)
    assert any(item.kind == "field_removed" and item.old == "paid" and "response field" in item.detail for item in changes)

    hints = endpoint_changes_from_notes("POST /v1/legacy has been removed\n")
    assert hints and hints[0].kind == "endpoint_removed"
    assert hints[0].source == "changelog"

    fixtures = tmp_path / "impact"
    (fixtures / "openapi").mkdir(parents=True)
    (fixtures / "openapi" / "old.yaml").write_text(old.read_text(encoding="utf-8"), encoding="utf-8")
    (fixtures / "openapi" / "new.yaml").write_text(new.read_text(encoding="utf-8"), encoding="utf-8")
    (fixtures / "index.json").write_text(
        json.dumps(
            {
                "packages": {
                    "stripe": {
                        "from": "1.0.0",
                        "to": "2.0.0",
                        "openapi": {"from": "openapi/old.yaml", "to": "openapi/new.yaml"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("stripe==1.0.0\n", encoding="utf-8")
    impacts, diffed, notes = collect_impacts(
        tmp_path,
        analyze_repo(tmp_path),
        fixtures=fixtures,
        dep="stripe",
        allow_network=False,
    )
    assert "stripe" in diffed
    assert any("currency" in item.change for item in impacts)
    assert any("renamed" in item.change and "/v1/charges" in item.change for item in impacts)
    assert not any("had no OpenAPI spec" in note for note in notes)

    bare = tmp_path / "nospec"
    _write(
        bare,
        "only.py",
        """
        import requests

        def ping():
            return requests.get("https://api.stripe.com/v1/charges")
        """,
    )
    (bare / "requirements.txt").write_text("stripe==1.0.0\n", encoding="utf-8")
    plain = tmp_path / "plain-fixtures"
    plain.mkdir()
    (plain / "index.json").write_text(
        json.dumps({"packages": {"stripe": {"from": "1.0.0", "to": "2.0.0"}}}),
        encoding="utf-8",
    )
    _impacts, _diffed, missing = collect_impacts(
        bare,
        analyze_repo(bare),
        fixtures=plain,
        dep="stripe",
        allow_network=False,
    )
    assert any("had no OpenAPI spec" in note and "api.stripe.com/v1/charges" in note for note in missing)


def _primary_lines(impacts) -> set[tuple[str, int]]:
    return {
        (site.path, site.line)
        for item in impacts
        for site in item.sites
        if not site.via_wrapper
    }


def test_module_attribute_diff_flags_only_names_the_new_wheel_dropped(tmp_path: Path):
    _write(tmp_path, "old/openai/__init__.py", "api_base = None\napi_key = None\napi_type = None\n")
    _write(tmp_path, "new/openai/__init__.py", "api_key = None\napi_type = None\n")
    changes = diff_wheels(tmp_path / "old", tmp_path / "new")
    removed = {item.symbol for item in changes if item.kind == "removed"}
    assert "openai.api_base" in removed
    assert "openai.api_key" not in removed
    assert "openai.api_type" not in removed

    repo = tmp_path / "repo"
    _write(
        repo,
        "app.py",
        """
        import openai

        def configure(base, key):
            openai.api_base = base
            openai.api_key = key
            openai.api_type = "azure"
        """,
    )
    impacts = intersect(
        changes,
        analyze_repo(repo).chains,
        dependency="openai",
        from_version="0",
        to_version="1",
    )
    text = " ".join(site.expr for item in impacts for site in item.sites)
    assert "api_base" in text
    assert "api_key" not in text
    assert "api_type" not in text


def test_referenced_callable_is_flagged_when_it_is_not_called(tmp_path: Path):
    repo = tmp_path / "repo"
    _write(
        repo,
        "loaders.py",
        """
        import openai

        def chat():
            return openai.ChatCompletion.create

        def complete():
            return openai.Completion.acreate
        """,
    )
    changes = [
        ApiChange(
            kind="removed",
            symbol="openai.api_resources.chat_completion.ChatCompletion.create",
            old="openai.api_resources.chat_completion.ChatCompletion.create",
            new="",
            detail="ChatCompletion.create removed",
            confidence="high",
            source="wheel",
            aliases=("openai.ChatCompletion.create",),
        ),
        ApiChange(
            kind="removed",
            symbol="openai.api_resources.completion.Completion.acreate",
            old="openai.api_resources.completion.Completion.acreate",
            new="",
            detail="Completion.acreate removed",
            confidence="high",
            source="wheel",
            aliases=("openai.Completion.acreate",),
        ),
    ]
    impacts = intersect(
        changes,
        analyze_repo(repo).chains,
        dependency="openai",
        from_version="0.28.1",
        to_version="1.0.0",
    )
    lines = _primary_lines(impacts)
    assert ("loaders.py", 5) in lines or any(path.endswith("loaders.py") for path, _line in lines)
    exprs = " ".join(site.expr for item in impacts for site in item.sites if not site.via_wrapper)
    assert "ChatCompletion.create" in exprs
    assert "Completion.acreate" in exprs
    assert all(not site.via_wrapper for item in impacts for site in item.sites)


def test_return_shape_follows_instance_method_and_super_assignment(tmp_path: Path):
    _write(
        tmp_path,
        "old/openai/__init__.py",
        """
        from openai.resources import Mod, Child
        """,
    )
    _write(
        tmp_path,
        "old/openai/obj.py",
        """
        class Obj(dict):
            def request(self, *args, **kwargs):
                return Obj()
        """,
    )
    _write(
        tmp_path,
        "old/openai/resources.py",
        """
        from openai.obj import Obj

        class Parent(Obj):
            @classmethod
            def create(cls, *args, **kwargs):
                return Obj()

        class Child(Parent):
            @classmethod
            def create(cls, *args, **kwargs):
                response = super().create(*args, **kwargs)
                return response

        class Mod(Obj):
            @classmethod
            def _prepare(cls):
                instance = cls()
                return instance, {}

            @classmethod
            def create(cls, *args, **kwargs):
                instance, params = cls._prepare()
                return instance.request("post", params)
        """,
    )
    _write(
        tmp_path,
        "new/openai/__init__.py",
        """
        class Mod:
            pass
        """,
    )
    _write(
        tmp_path,
        "new/openai/_models.py",
        """
        import pydantic

        class BaseModel(pydantic.BaseModel):
            pass

        class Reply(BaseModel):
            results: list
        """,
    )
    changes = diff_wheels(tmp_path / "old", tmp_path / "new")
    shaped = {item.symbol for item in changes if item.kind == "response_shape"}
    assert any(symbol.endswith("Mod.create") for symbol in shaped)
    assert any(symbol.endswith("Child.create") for symbol in shaped)

    repo = tmp_path / "repo"
    _write(
        repo,
        "use.py",
        """
        import openai

        def moderate(text):
            res = openai.Mod.create(text)
            return res["results"][0]["flagged"]
        """,
    )
    impacts = intersect(
        changes,
        analyze_repo(repo).chains,
        dependency="openai",
        from_version="0",
        to_version="1",
    )
    reads = [
        site.expr
        for item in impacts
        if "return changed" in item.change
        for site in item.sites
        if not site.via_wrapper
    ]
    assert any("results" in expr for expr in reads)


def test_public_name_that_still_exists_is_not_a_removal(tmp_path: Path):
    _write(
        tmp_path,
        "old/openai/__init__.py",
        "from openai.error import APIError\n",
    )
    _write(
        tmp_path,
        "old/openai/error.py",
        "class APIError(Exception):\n    pass\n",
    )
    _write(
        tmp_path,
        "new/openai/__init__.py",
        "class APIError(Exception):\n    pass\n",
    )
    changes = diff_wheels(tmp_path / "old", tmp_path / "new")
    removed = [item for item in changes if item.kind == "removed" and item.symbol.endswith("APIError")]
    assert removed
    assert all("openai.APIError" not in item.aliases for item in removed)
    repo = tmp_path / "repo"
    _write(
        repo,
        "errs.py",
        """
        import openai

        def modern():
            return openai.APIError

        def legacy():
            return openai.error.APIError
        """,
    )
    impacts = intersect(
        changes,
        analyze_repo(repo).chains,
        dependency="openai",
        from_version="0",
        to_version="1",
    )
    exprs = " ".join(site.expr for item in impacts for site in item.sites if not site.via_wrapper)
    assert "openai.error.APIError" in exprs
    assert "openai.APIError" not in exprs.replace("openai.error.APIError", "")


def _impacts(changes, repo: Path):
    return intersect(
        changes,
        analyze_repo(repo).chains,
        dependency="openai",
        from_version="0",
        to_version="1",
        return_classes=getattr(changes, "return_classes", None),
        surviving=getattr(changes, "surviving", None),
        methods=getattr(changes, "methods", None),
    )


def _primary(impacts) -> list[str]:
    return [
        f"{site.path}:{site.line}:{site.expr}"
        for item in impacts
        for site in item.sites
        if not site.via_wrapper
    ]


def test_star_import_keeps_a_method_that_moved(tmp_path: Path):
    _write(
        tmp_path,
        "old/openai/collection.py",
        """
        class Collection:
            def insert(self, doc):
                return None

            def insert_one(self, doc):
                return None
        """,
    )
    _write(tmp_path, "old/openai/__init__.py", "from openai.collection import Collection\n")
    _write(
        tmp_path,
        "new/openai/collection.py",
        "from openai.synchronous.collection import *\n",
    )
    _write(
        tmp_path,
        "new/openai/synchronous/collection.py",
        """
        class Collection:
            def insert_one(self, doc):
                return None
        """,
    )
    _write(tmp_path, "new/openai/__init__.py", "")
    changes = diff_wheels(tmp_path / "old", tmp_path / "new")
    removed = {item.symbol for item in changes if item.kind == "removed"}
    assert "openai.collection.Collection.insert" in removed
    assert "openai.collection.Collection.insert_one" not in removed


def test_subscript_and_instance_field_follow_the_wheel_return_class(tmp_path: Path):
    _write(
        tmp_path,
        "old/openai/__init__.py",
        """
        class Collection:
            def insert(self, doc):
                return None

        class Database:
            def __getitem__(self, name):
                return Collection(self, name)

        class Client:
            def __getitem__(self, name):
                return Database(self, name)
        """,
    )
    _write(
        tmp_path,
        "new/openai/__init__.py",
        """
        class Collection:
            def insert_one(self, doc):
                return None

        class Database:
            def __getitem__(self, name):
                return Collection(self, name)

        class Client:
            def __getitem__(self, name):
                return Database(self, name)
        """,
    )
    changes = diff_wheels(tmp_path / "old", tmp_path / "new")
    repo = tmp_path / "repo"
    _write(
        repo,
        "jobs.py",
        """
        import openai

        class Repo:
            def __init__(self):
                self.client = openai.Client()

            def save(self, doc):
                db = self.client["app"]
                collection = db["jobs"]
                collection.insert(doc)
        """,
    )
    hits = _primary(_impacts(changes, repo))
    assert any("collection.insert" in hit for hit in hits)


def test_splatted_dict_keys_are_call_parameters(tmp_path: Path):
    _write(
        tmp_path,
        "old/openai/__init__.py",
        """
        class Messages:
            def create(self, model, temperature=None):
                return None

        class Client:
            @property
            def messages(self):
                return Messages(self)
        """,
    )
    _write(
        tmp_path,
        "new/openai/__init__.py",
        """
        class Messages:
            def create(self, model):
                return None

        class Client:
            @property
            def messages(self):
                return Messages(self)
        """,
    )
    changes = diff_wheels(tmp_path / "old", tmp_path / "new")
    repo = tmp_path / "repo"
    _write(
        repo,
        "ask.py",
        """
        import openai

        def ask():
            client = openai.Client()
            kwargs = {"model": "m"}
            kwargs["temperature"] = 0
            return client.messages.create(**kwargs)
        """,
    )
    impacts = _impacts(changes, repo)
    assert any("temperature" in item.change for item in impacts)
    primary = _primary(impacts)
    assert any("kwargs[" in hit and "temperature" in hit for hit in primary)
    assert any("messages.create" in hit for hit in primary)


def test_kwargs_origin_traces_dict_update_setdefault_and_partial(tmp_path: Path):
    _write(
        tmp_path,
        "old/openai/__init__.py",
        """
        class Messages:
            def create(self, model, temperature=None, top_p=None, max_tokens=None):
                return None

        class Client:
            @property
            def messages(self):
                return Messages(self)
        """,
    )
    _write(
        tmp_path,
        "new/openai/__init__.py",
        """
        class Messages:
            def create(self, model):
                return None

        class Client:
            @property
            def messages(self):
                return Messages(self)
        """,
    )
    changes = diff_wheels(tmp_path / "old", tmp_path / "new")
    repo = tmp_path / "repo"
    _write(
        repo,
        "ask.py",
        """
        import functools
        import openai

        def build():
            return {"max_tokens": 16}

        def ask():
            client = openai.Client()
            kwargs = dict(model="m")
            kwargs.update(temperature=0.2)
            kwargs.setdefault("top_p", 0.9)
            kwargs.update(build())
            return client.messages.create(**kwargs)

        def via_partial():
            client = openai.Client()
            create = functools.partial(client.messages.create, temperature=0.1)
            return create(model="m")
        """,
    )
    impacts = _impacts(changes, repo)
    primary = _primary(impacts)
    text = "\n".join(primary)
    assert "temperature" in text
    assert "top_p" in text
    assert "max_tokens" in text
    assert "update" in text or "setdefault" in text or "dict(" in text
    assert "partial" in text
    assert any("messages.create" in hit for hit in primary)


def test_kwargs_origin_traces_typed_assign_and_helper_return(tmp_path: Path):
    _write(
        tmp_path,
        "old/openai/__init__.py",
        """
        class Messages:
            def create(self, model, temperature=None):
                return None

        class Client:
            @property
            def messages(self):
                return Messages(self)
        """,
    )
    _write(
        tmp_path,
        "new/openai/__init__.py",
        """
        class Messages:
            def create(self, model):
                return None

        class Client:
            @property
            def messages(self):
                return Messages(self)
        """,
    )
    changes = diff_wheels(tmp_path / "old", tmp_path / "new")
    repo = tmp_path / "repo"
    _write(
        repo,
        "ask.py",
        """
        from typing import Any, Dict
        import openai

        def options():
            body = {}
            body["temperature"] = 0.3
            return body

        def ask():
            client = openai.Client()
            request_kwargs: Dict[str, Any] = {"model": "m"}
            request_kwargs["temperature"] = 0.3
            return client.messages.create(**request_kwargs)

        def ask_helper():
            client = openai.Client()
            return client.messages.create(model="m", **options())
        """,
    )
    impacts = _impacts(changes, repo)
    primary = _primary(impacts)
    assert any("request_kwargs" in hit and "temperature" in hit for hit in primary)
    assert any("body[" in hit or "options" in hit or "temperature" in hit for hit in primary)
    # Origin lines are primary; the call stays as context in the expression.
    assert any("→" in hit and "messages.create" in hit for hit in primary)

def test_constructor_return_class_matches_a_later_method(tmp_path: Path):
    _write(
        tmp_path,
        "old/openai/__init__.py",
        """
        class Engine:
            def execute(self, sql):
                return None

        def create_engine(url):
            return Engine()
        """,
    )
    _write(
        tmp_path,
        "new/openai/__init__.py",
        """
        class Engine:
            pass

        def create_engine(url):
            return Engine()
        """,
    )
    changes = diff_wheels(tmp_path / "old", tmp_path / "new")
    repo = tmp_path / "repo"
    _write(
        repo,
        "db.py",
        """
        import openai

        class Store:
            def open(self):
                self.engine = openai.create_engine("sqlite")

            def read(self):
                return self.engine.execute("select 1")
        """,
    )
    hits = _primary(_impacts(changes, repo))
    assert any("engine.execute" in hit for hit in hits)


def test_a_name_the_new_wheel_still_exports_is_not_a_pair_match(tmp_path: Path):
    _write(
        tmp_path,
        "old/openai/__init__.py",
        "from openai.engine import Engine\nfrom openai.future.engine import Engine as FutureEngine\n",
    )
    _write(tmp_path, "old/openai/engine.py", "class Engine:\n    pass\n")
    _write(tmp_path, "old/openai/future/__init__.py", "")
    _write(tmp_path, "old/openai/future/engine.py", "class Engine:\n    pass\n")
    _write(tmp_path, "new/openai/__init__.py", "from openai.engine import Engine\n")
    _write(tmp_path, "new/openai/engine.py", "class Engine:\n    pass\n")
    changes = diff_wheels(tmp_path / "old", tmp_path / "new")
    repo = tmp_path / "repo"
    _write(
        repo,
        "types.py",
        """
        import openai

        def anno(engine: openai.engine.Engine):
            return engine
        """,
    )
    hits = _primary(_impacts(changes, repo))
    assert not any("engine.Engine" in hit for hit in hits)


def test_nested_src_import_resolves_a_base_class_field(tmp_path: Path):
    _write(
        tmp_path,
        "app/src/common/dao.py",
        """
        import openai

        class DAO:
            def __init__(self):
                self.client = openai.Client()
        """,
    )
    _write(
        tmp_path,
        "app/src/jobs.py",
        """
        from src.common.dao import DAO

        class Jobs(DAO):
            def save(self, doc):
                self.client["jobs"].insert(doc)
        """,
    )
    chains = _chains(tmp_path, symbol="Jobs.save", expr="insert")
    assert chains
    assert any(chain.sdk_method.endswith("Client.__getitem__.insert") for chain in chains)


def test_subclass_uses_a_field_assigned_on_the_base(tmp_path: Path):
    _write(
        tmp_path,
        "old/openai/__init__.py",
        """
        class Collection:
            def insert(self, doc):
                return None

        class Client:
            def __getitem__(self, name):
                return Collection()
        """,
    )
    _write(
        tmp_path,
        "new/openai/__init__.py",
        """
        class Collection:
            pass

        class Client:
            def __getitem__(self, name):
                return Collection()
        """,
    )
    changes = diff_wheels(tmp_path / "old", tmp_path / "new")
    repo = tmp_path / "repo"
    _write(
        repo,
        "jobs.py",
        """
        import openai

        class Manager:
            def __init__(self, **kwargs):
                self.client = kwargs.get("client", openai.Client())

        class Repo(Manager):
            def save(self, doc):
                self.client["jobs"].insert(doc)
        """,
    )
    hits = _primary(_impacts(changes, repo))
    assert any("insert" in hit for hit in hits)


def test_optional_import_assigned_none_still_reaches_the_sdk(tmp_path: Path):
    _write(
        tmp_path,
        "old/openai/__init__.py",
        """
        class ChatCompletion:
            @staticmethod
            def create(model):
                return None
        """,
    )
    _write(tmp_path, "new/openai/__init__.py", "class ChatCompletion:\n    pass\n")
    changes = diff_wheels(tmp_path / "old", tmp_path / "new")
    repo = tmp_path / "repo"
    _write(
        repo,
        "ask.py",
        """
        try:
            import openai
        except ImportError:
            openai = None

        def ask(prompt):
            return openai.ChatCompletion.create(model=prompt)
        """,
    )
    hits = _primary(_impacts(changes, repo))
    assert any("ChatCompletion.create" in hit for hit in hits)


def test_reexported_factory_return_matches_the_public_name(tmp_path: Path):
    _write(
        tmp_path,
        "old/openai/engine.py",
        """
        class Engine:
            def execute(self, sql):
                return None

        def create_engine(url):
            return Engine()
        """,
    )
    _write(tmp_path, "old/openai/__init__.py", "from openai.engine import create_engine\n")
    _write(
        tmp_path,
        "new/openai/engine.py",
        """
        class Engine:
            pass

        def create_engine(url):
            return Engine()
        """,
    )
    _write(tmp_path, "new/openai/__init__.py", "from openai.engine import create_engine\n")
    changes = diff_wheels(tmp_path / "old", tmp_path / "new")
    repo = tmp_path / "repo"
    _write(
        repo,
        "db.py",
        """
        import openai

        def read():
            return openai.create_engine("sqlite").execute("select 1")
        """,
    )
    hits = _primary(_impacts(changes, repo))
    assert any("execute" in hit for hit in hits)


def test_a_variable_holding_a_class_is_what_the_call_returns(tmp_path: Path):
    _write(
        tmp_path,
        "old/openai/__init__.py",
        """
        class Engine:
            def execute(self, sql):
                return None

        class Future:
            def execute(self, sql):
                return None

        def create_engine(url, **kwargs):
            if kwargs.pop("future", False):
                default = Future
            else:
                default = Engine
            factory = kwargs.pop("cls", default)
            return factory()
        """,
    )
    _write(
        tmp_path,
        "new/openai/__init__.py",
        """
        class Engine:
            pass

        class Future:
            pass

        def create_engine(url, **kwargs):
            if kwargs.pop("future", False):
                default = Future
            else:
                default = Engine
            factory = kwargs.pop("cls", default)
            return factory()
        """,
    )
    changes = diff_wheels(tmp_path / "old", tmp_path / "new")
    repo = tmp_path / "repo"
    _write(
        repo,
        "db.py",
        """
        import openai

        def read():
            return openai.create_engine("sqlite").execute("select 1")
        """,
    )
    hits = _primary(_impacts(changes, repo))
    assert any("execute" in hit for hit in hits)


def test_inherited_method_on_a_subscripted_base_matches(tmp_path: Path):
    _write(
        tmp_path,
        "old/openai/__init__.py",
        """
        class Base:
            def export(self):
                return {}

        class Child(Base["Child"]):
            pass

        class Factory:
            def make(self):
                return Child()
        """,
    )
    _write(
        tmp_path,
        "new/openai/__init__.py",
        """
        class Base:
            pass

        class Child(Base["Child"]):
            pass

        class Factory:
            def make(self):
                return Child()
        """,
    )
    changes = diff_wheels(tmp_path / "old", tmp_path / "new")
    repo = tmp_path / "repo"
    _write(
        repo,
        "hook.py",
        """
        import openai

        def run():
            event = openai.Factory().make()
            return event.export()
        """,
    )
    hits = _primary(_impacts(changes, repo))
    assert any("export" in hit for hit in hits)


def test_runner_keeps_the_wheel_return_class(tmp_path: Path):
    """collect_impacts used to copy the diff into a plain list and drop returns."""
    _write(
        tmp_path,
        "wheels/old/openai/engine.py",
        """
        class Engine:
            def execute(self, sql):
                return None

        def create_engine(url):
            return Engine()
        """,
    )
    _write(tmp_path, "wheels/old/openai/__init__.py", "from openai.engine import create_engine\n")
    _write(
        tmp_path,
        "wheels/new/openai/engine.py",
        """
        class Engine:
            pass

        def create_engine(url):
            return Engine()
        """,
    )
    _write(tmp_path, "wheels/new/openai/__init__.py", "from openai.engine import create_engine\n")
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "index.json").write_text(
        json.dumps({"packages": {"openai": {"wheels": {"0": "../wheels/old", "1": "../wheels/new"}}}}),
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    _write(
        repo,
        "db.py",
        """
        import openai

        def read():
            return openai.create_engine("sqlite").execute("select 1")
        """,
    )
    impacts, diffed, _notes = collect_impacts(
        repo,
        analyze_repo(repo),
        fixtures=fixtures,
        dep="openai",
        from_version="0",
        to_version="1",
    )
    assert "openai" in diffed
    assert any("execute" in site.expr for item in impacts for site in item.sites)


def test_removed_type_flags_its_import_and_annotations(tmp_path: Path):
    from conduit.impact.run import _import_chains

    _write(
        tmp_path,
        "app.py",
        """
        from openai.openai_object import OpenAIObject
        import openai

        def wrap(resp: OpenAIObject) -> OpenAIObject:
            result: OpenAIObject = resp
            return result

        def ask(prompt):
            return openai.Completion.create(model="x", prompt=prompt)

        def later(resp: "OpenAIObject"):
            return resp
        """,
    )
    removed = ApiChange(
        kind="removed",
        symbol="openai.openai_object.OpenAIObject",
        old="openai.openai_object.OpenAIObject",
        new="",
        detail="openai_object.OpenAIObject removed",
        confidence="high",
        source="wheel",
    )
    # The same class under a public re-export is one break per line, not two.
    reexport = ApiChange(
        kind="removed",
        symbol="openai.OpenAIObject",
        old="openai.OpenAIObject",
        new="",
        detail="openai.OpenAIObject removed",
        confidence="high",
        source="wheel",
        aliases=("openai.openai_object.OpenAIObject",),
    )
    renamed_param = ApiChange(
        kind="param_removed",
        symbol="openai.openai_object.OpenAIObject",
        old="api_key",
        new="",
        detail="OpenAIObject api_key removed",
        confidence="high",
        source="wheel",
    )
    service_map = analyze_repo(tmp_path)
    impacts = intersect(
        [removed, reexport, renamed_param],
        (*service_map.chains, *_import_chains(service_map)),
        dependency="openai",
        from_version="0.28.1",
        to_version="1.0.0",
    )
    hits = [(site.line, site.expr) for item in impacts for site in item.sites]
    assert sorted(line for line, _expr in hits) == [1, 4, 5, 11]
    assert (1, "import OpenAIObject") in hits
    assert all(item.kind == "removed" for item in impacts)


def test_two_removed_keywords_on_one_call_are_two_breaks(tmp_path: Path):
    _write(
        tmp_path,
        "app.py",
        """
        import openai

        def ask(prompt):
            return openai.Completion.create(model="x", prompt=prompt)
        """,
    )
    changes = [
        ApiChange(
            kind="param_removed",
            symbol="openai.Completion.create",
            old=name,
            new="",
            detail=f"parameter {name} removed from Completion.create",
            confidence="high",
            source="wheel",
        )
        for name in ("model", "prompt")
    ]
    impacts = intersect(
        changes,
        analyze_repo(tmp_path).chains,
        dependency="openai",
        from_version="0.28.1",
        to_version="1.0.0",
    )
    assert sorted(item.change for item in impacts) == [
        "parameter model removed from Completion.create",
        "parameter prompt removed from Completion.create",
    ]
