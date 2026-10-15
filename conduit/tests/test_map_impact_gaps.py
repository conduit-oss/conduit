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
    )
    assert runner.exit_code == 0
    assert "--show-callers" in runner.stdout


def test_unpinned_versions_resolve_from_locks_installs_and_pypi(tmp_path: Path, monkeypatch):
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
