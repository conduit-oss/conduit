"""Declared third-party distributions are outside services when a call reaches them."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from typer.testing import CliRunner

from conduit.main import app
from conduit.outside_in.analyze import analyze_repo


def _write(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")


def _user_tree(root: Path, *, slack_requirement: str = "slack_sdk") -> None:
    _write(
        root,
        "requirements.txt",
        f"""
        psycopg2
        supabase
        {slack_requirement}
        """,
    )
    _write(
        root,
        "database/sql_queries.py",
        """
        from supabase import create_client, Client
        import psycopg2

        url = "https://example.supabase.co"
        key = "k"
        supabase: Client = create_client(url, key)

        def load(x):
            psycopg2.connect("postgresql://db")
            return supabase.table("pipelines").select("*").eq("id", x).execute()
        """,
    )
    _write(
        root,
        "sendslack.py",
        """
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError

        class SendSlack:
            def __init__(self, token):
                self.client = WebClient(token=token)

            def send_message_if_possible(self, channel, text):
                try:
                    self.client.conversations_join(channel=channel)
                    return self.client.chat_postMessage(channel=channel, text=text)
                except SlackApiError:
                    return None
        """,
    )
    _write(
        root,
        "main.py",
        """
        from sendslack import SendSlack

        def run(token):
            return SendSlack(token).send_message_if_possible("C1", "hi")
        """,
    )
    _write(
        root,
        "sending.py",
        """
        import requests
        import httpx
        from urllib.parse import urlparse

        def post(url, payload):
            host = urlparse(url).hostname or ""
            if host.endswith("hooks.slack.com"):
                requests.post(url, json=payload)
                httpx.post(url, json=payload)
            else:
                requests.post(url, json=payload)
        """,
    )
    _write(
        root,
        "stdlib_only.py",
        """
        import json
        import os

        def dump(data):
            return json.dumps(data) + os.getcwd()
        """,
    )
    _write(
        root,
        "helper.py",
        """
        def ping():
            return 1
        """,
    )
    _write(
        root,
        "localmod.py",
        """
        import helper

        def use():
            return helper.ping()
        """,
    )


def test_map_finds_supabase_slack_webhook_and_keeps_psycopg2(tmp_path: Path):
    _user_tree(tmp_path)
    service_map = analyze_repo(tmp_path)
    services = set(service_map.services())
    assert "psycopg2" in services
    assert "supabase" in services
    assert "slack-sdk" in services
    assert "hooks.slack.com" in services
    assert "json" not in services
    assert "os" not in services
    assert "helper" not in services
    assert "requests" not in services
    assert "httpx" not in services

    supabase = [
        chain
        for chain in service_map.chains
        if chain.service == "supabase" and chain.call_rel == "database/sql_queries.py"
    ]
    assert any(chain.sdk_method.endswith("execute") and not chain.via_wrapper for chain in supabase)
    assert any("table" in chain.sdk_method and "select" in chain.sdk_method for chain in supabase)

    direct = [
        chain
        for chain in service_map.chains
        if chain.service == "slack-sdk"
        and not chain.via_wrapper
        and chain.call_rel == "sendslack.py"
        and chain.sdk_method.endswith("chat_postMessage")
    ]
    assert direct
    wrapped = [
        chain
        for chain in service_map.chains
        if chain.service == "slack-sdk"
        and chain.via_wrapper
        and chain.call_rel == "main.py"
        and chain.call_expr.startswith("SendSlack(")
        and chain.sdk_method.endswith("chat_postMessage")
    ]
    assert wrapped

    hooks = [
        chain
        for chain in service_map.chains
        if chain.service == "hooks.slack.com" and chain.kind == "http_call"
    ]
    assert {chain.call_expr.split("(")[0] for chain in hooks} >= {"requests.post", "httpx.post"}
    assert all(chain.call_rel == "sending.py" for chain in hooks)
    # The else branch posts the same way but is not the slack host.
    assert len(hooks) == 2

    cli = CliRunner().invoke(app, ["map", "--path", str(tmp_path), "--json"])
    assert cli.exit_code == 0, cli.stdout
    payload = json.loads(cli.stdout)
    assert "psycopg2" in payload["services"]
    assert "supabase" in payload["services"]
    assert "slack-sdk" in payload["services"]
    assert "hooks.slack.com" in payload["services"]


def test_hyphenated_distribution_matches_the_import_root(tmp_path: Path):
    _user_tree(tmp_path, slack_requirement="slack-sdk")
    services = set(analyze_repo(tmp_path).services())
    assert "slack-sdk" in services
    assert "supabase" in services


def test_annotated_instance_attribute_reaches_other_methods(tmp_path: Path):
    _write(tmp_path, "requirements.txt", "widget-sdk\n")
    _write(
        tmp_path,
        "box.py",
        """
        from widget_sdk import WebClient

        class Box:
            def __init__(self, token):
                self.client: WebClient = WebClient(token=token)

            def send(self, channel, text):
                return self.client.chat_postMessage(channel=channel, text=text)

        def run(token):
            return Box(token).send("C1", "hi")
        """,
    )
    chains = analyze_repo(tmp_path).chains
    direct = [
        chain
        for chain in chains
        if chain.service == "widget-sdk"
        and not chain.via_wrapper
        and chain.sdk_method.endswith("chat_postMessage")
    ]
    assert direct
    assert direct[0].call_symbol == "Box.send"
    wrapped = [
        chain
        for chain in chains
        if chain.via_wrapper and chain.call_rel == "box.py" and chain.call_symbol == "run"
    ]
    assert wrapped
    assert wrapped[0].sdk_method.endswith("chat_postMessage")


def test_lockfile_names_a_distribution_the_import_root_differs_from(tmp_path: Path):
    _write(
        tmp_path,
        "poetry.lock",
        """
        [[package]]
        name = "cool-thing"
        version = "1.2.3"
        """,
    )
    _write(
        tmp_path,
        "app.py",
        """
        import cool_thing

        def run():
            return cool_thing.Client().go()
        """,
    )
    chains = [chain for chain in analyze_repo(tmp_path).chains if chain.service == "cool-thing"]
    assert chains
    assert any(chain.sdk_method.endswith("go") for chain in chains)


def test_installed_top_level_maps_an_unrelated_import_name(tmp_path: Path):
    info = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages" / "weirdpkg-1.0.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Name: weirdpkg\nVersion: 1.0.0\n", encoding="utf-8")
    (info / "top_level.txt").write_text("not_the_dist_name\n", encoding="utf-8")
    _write(
        tmp_path,
        "app.py",
        """
        import not_the_dist_name

        def run():
            return not_the_dist_name.Client().ping()
        """,
    )
    services = set(analyze_repo(tmp_path).services())
    assert "weirdpkg" in services
    assert "not_the_dist_name" not in services


def test_import_without_a_call_is_not_a_service(tmp_path: Path):
    _write(tmp_path, "requirements.txt", "supabase\n")
    _write(tmp_path, "app.py", "import supabase\n")
    assert analyze_repo(tmp_path).services() == []


def test_stdlib_and_local_modules_are_not_services_even_if_required(tmp_path: Path):
    _write(
        tmp_path,
        "requirements.txt",
        """
        json
        helper
        os
        """,
    )
    _write(
        tmp_path,
        "helper.py",
        """
        def ping():
            return 1
        """,
    )
    _write(
        tmp_path,
        "app.py",
        """
        import json
        import os
        import helper

        def run(data):
            helper.ping()
            return json.dumps(data) + os.getcwd()
        """,
    )
    assert analyze_repo(tmp_path).services() == []
