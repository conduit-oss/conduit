"""Declared third-party distributions are outside services when a call reaches them."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

from typer.testing import CliRunner

from conduit.main import app
from conduit.outside_in.analyze import analyze_repo
from conduit.outside_in.evidence import classify_distribution
from conduit.outside_in.library_types import LibraryTypes


def _install_client(root: Path, dist: str, import_name: str, version: str = "1.0.0") -> None:
    """Put a real HTTP client on the repo's site-packages for one distribution."""
    site = root / ".venv" / "lib" / "python3.12" / "site-packages"
    info = site / f"{dist.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(f"Name: {dist}\nVersion: {version}\n", encoding="utf-8")
    (info / "top_level.txt").write_text(f"{import_name}\n", encoding="utf-8")
    package = site / import_name
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(
        "import http.client\n"
        "\n"
        "class Client:\n"
        "    def __init__(self, token=None):\n"
        "        self._conn = http.client.HTTPSConnection('api.invalid.invalid')\n"
        "\n"
        "    def chat_postMessage(self, channel, text):\n"
        "        self._conn.request('POST', '/chat')\n"
        "        return channel\n"
        "\n"
        "    def go(self):\n"
        "        self._conn.request('POST', '/go')\n"
        "        return 1\n"
        "\n"
        "    def ping(self):\n"
        "        self._conn.request('POST', '/ping')\n"
        "        return 1\n"
        "\n"
        "class WebClient(Client):\n"
        "    pass\n",
        encoding="utf-8",
    )


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
    _install_client(tmp_path, "widget-sdk", "widget_sdk")
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
    _install_client(tmp_path, "cool-thing", "cool_thing", version="1.2.3")
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
    _install_client(tmp_path, "weirdpkg", "not_the_dist_name")
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


def _install_package(root: Path, dist: str, import_name: str, version: str, source: str) -> Path:
    """Install one synthetic distribution into the repo's ``.venv`` and return site-packages."""
    site = root / ".venv" / "lib" / "python3.12" / "site-packages"
    info = site / f"{dist}-{version}.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(f"Name: {dist}\nVersion: {version}\n", encoding="utf-8")
    (info / "top_level.txt").write_text(f"{import_name}\n", encoding="utf-8")
    package = site / import_name
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    return site


def test_a_declared_host_is_the_service_when_no_transport_is_traced(tmp_path: Path):
    """A host read from the package is enough when no transport call was attributed.

    The name is synthetic. Nothing here names a real client.
    """
    version = "0.0.3"
    _install_package(
        tmp_path,
        "zzhost",
        "zzhost",
        version,
        """
        base_url = "https://billing.zzhost.com/v1"

        def ping():
            return base_url
        """,
    )
    found = classify_distribution("zzhost", version, root=tmp_path)
    assert found.loaded is True
    assert found.service is True
    assert found.kind == "api"
    assert "billing.zzhost.com" in found.evidence


def test_a_wrapper_is_not_the_service_its_driver_is(tmp_path: Path):
    """A package that only reaches a database through another dependency is not the service.

    The names are synthetic. The driver does the socket I/O. The wrapper only
    calls the driver. A caller that reaches the wrapper maps the driver.
    """
    version = "0.0.9"
    _install_package(
        tmp_path,
        "zzbolt",
        "zzbolt",
        version,
        """
        import socket

        def connect(dsn):
            return socket.create_connection(("db.invalid", 9))

        def cursor():
            return connect(dsn="")

        def execute(query):
            return cursor()

        def commit():
            return None
        """,
    )
    site = _install_package(
        tmp_path,
        "zzshelf",
        "zzshelf",
        version,
        """
        import ssl
        import zzbolt

        def connect(dsn):
            return zzbolt.connect(dsn)

        def cursor():
            return zzbolt.cursor()

        def execute(query):
            return zzbolt.execute(query)

        def commit():
            return zzbolt.commit()

        def label():
            # Unrelated to the database methods. Must not make this package the service.
            return ssl.create_default_context()
        """,
    )
    _write(tmp_path, "requirements.txt", f"zzbolt=={version}\nzzshelf=={version}\n")
    driver = classify_distribution("zzbolt", version, root=tmp_path)
    wrapper = classify_distribution("zzshelf", version, root=tmp_path)
    assert driver.service is True
    assert driver.kind == "database"
    assert wrapper.service is False
    assert "zzbolt" in wrapper.delegates

    _write(
        tmp_path,
        "direct.py",
        """
        import zzbolt

        def open():
            return zzbolt.connect("db")
        """,
    )
    sys.path.insert(0, str(site))
    try:
        direct = analyze_repo(tmp_path)
        assert "zzbolt" in direct.services()
        assert "zzshelf" not in direct.services()

        (tmp_path / "direct.py").unlink()
        _write(
            tmp_path,
            "via.py",
            """
            import zzshelf

            def open():
                return zzshelf.connect("db")
            """,
        )
        wrapped = analyze_repo(tmp_path)
        assert wrapped.services() == ["zzbolt"]
        assert any(
            chain.service == "zzbolt" and chain.sdk_method.endswith("connect") and chain.call_rel == "via.py"
            for chain in wrapped.chains
        )

        (tmp_path / "via.py").unlink()
        _write(tmp_path, "idle.py", "import zzshelf\n")
        assert analyze_repo(tmp_path).services() == []
    finally:
        sys.path.remove(str(site))


def test_subscript_getattr_and_untyped_fields_follow_the_library(tmp_path: Path):
    """``obj[key]``, ``obj.name``, and ``self.field`` use the library's own code."""
    version = "0.0.8"
    site = _install_package(
        tmp_path,
        "zzlane",
        "zzlane",
        version,
        """
        import socket

        class Record:
            def fetch(self):
                return socket.create_connection(("db.invalid", 9))

        class Bucket:
            def __getitem__(self, key):
                return Record()

            def __getattr__(self, name):
                return self.__getitem__(name)

        class Holder:
            def __init__(self, record):
                self.kept = record

            def open(self):
                self.kept = Record()

        class Sleeve:
            def __init__(self, record):
                self.record = record

        def connect(dsn):
            socket.create_connection(("db.invalid", 9))
            return Holder(Record())

        def cursor():
            return connect("")

        def execute(query):
            return cursor()

        def commit():
            return None
        """,
    )
    _write(tmp_path, "requirements.txt", f"zzlane=={version}\n")
    types = LibraryTypes(tmp_path)
    sys.path.insert(0, str(site))
    try:
        assert types.call_return("zzlane.Bucket.__getitem__") == ("zzlane.Record",)
        assert types.instance_attribute("zzlane.Bucket", "jobs") == "zzlane.Record"
        assert types.instance_attribute("zzlane.Holder", "kept") == "zzlane.Record"
        _write(
            tmp_path,
            "use.py",
            """
            import zzlane

            def by_key():
                bucket = zzlane.Bucket()
                return bucket["jobs"].fetch()

            def by_name():
                bucket = zzlane.Bucket()
                return bucket.jobs.fetch()

            def held():
                return zzlane.Sleeve(zzlane.Record()).record.fetch()

            def opened():
                holder = zzlane.Holder(None)
                holder.open()
                return holder.kept.fetch()

            class Job:
                def __init__(self, record):
                    self.record = record

                def boot(self):
                    self.record = make()

                def read(self):
                    return self.record.fetch()

            def make():
                return zzlane.Record()

            def entry():
                return Job(zzlane.Record()).read()

            def booted():
                job = Job(None)
                job.boot()
                return job.read()
            """,
        )
        service_map = analyze_repo(tmp_path)

        def fetched(symbol: str) -> bool:
            return any(
                chain.call_symbol == symbol and chain.sdk_method.endswith("Record.fetch")
                for chain in service_map.chains
            )

        for symbol in ("by_key", "by_name", "held", "opened", "Job.read"):
            assert fetched(symbol), symbol
        assert any(
            chain.via_wrapper and chain.call_symbol == "entry" and chain.sdk_method.endswith("Record.fetch")
            for chain in service_map.chains
        )
        assert any(
            chain.via_wrapper and chain.call_symbol == "booted" and chain.sdk_method.endswith("Record.fetch")
            for chain in service_map.chains
        )
    finally:
        sys.path.remove(str(site))


def test_instance_fields_follow_the_assignment_that_reaches_the_read(tmp_path: Path):
    """A later ``self.field = self.field[key]`` is the type later reads see.

    Inside one method the write before the read wins. Across methods every
    method's final type is possible, and the latest write in ``__init__`` is
    preferred.
    """
    version = "0.0.10"
    site = _install_package(
        tmp_path,
        "zzflow",
        "zzflow",
        version,
        """
        import socket

        class Record:
            def fetch(self):
                return socket.create_connection(("db.invalid", 9))

            def ping(self):
                return socket.create_connection(("db.invalid", 9))

        class Shelf:
            def __getitem__(self, key):
                return Record()

        class Lane:
            def __getitem__(self, key):
                return Shelf()

            def ping(self):
                return socket.create_connection(("db.invalid", 9))

        def connect(dsn):
            socket.create_connection(("db.invalid", 9))
            return Lane()

        def cursor():
            return connect("")

        def execute(query):
            return cursor()

        def commit():
            return None
        """,
    )
    _write(tmp_path, "requirements.txt", f"zzflow=={version}\n")
    sys.path.insert(0, str(site))
    try:
        _write(
            tmp_path,
            "use.py",
            """
            import zzflow

            class Store:
                def __init__(self):
                    self.lane = zzflow.Lane()
                    self.lane = self.lane["main"]
                    self.record = self.lane["job"]

                def read(self):
                    return self.record.fetch()

                def staged(self):
                    self.box = zzflow.Lane()
                    self.box.ping()
                    self.box = self.box["main"]
                    return self.box["job"].fetch()

            class Split:
                def __init__(self):
                    self.handle = zzflow.Lane()

                def swap(self):
                    self.handle = zzflow.Record()

                def read(self):
                    return self.handle.fetch()

                def ping(self):
                    return self.handle.ping()
            """,
        )
        chains = [
            chain
            for chain in analyze_repo(tmp_path).chains
            if chain.call_rel == "use.py" and not chain.via_wrapper
        ]

        def methods(symbol: str) -> set[str]:
            return {chain.sdk_method for chain in chains if chain.call_symbol == symbol}

        read = methods("Store.read")
        assert any(item.endswith("Record.fetch") for item in read), read
        assert not any(item.endswith("Lane.fetch") or item.endswith("Shelf.fetch") for item in read)

        staged = methods("Store.staged")
        assert any(item.endswith("Lane.ping") for item in staged), staged
        assert any(item.endswith("Record.fetch") for item in staged), staged

        split_read = methods("Split.read")
        assert any(item.endswith("Record.fetch") for item in split_read), split_read

        split_ping = methods("Split.ping")
        assert any(item.endswith("Lane.ping") for item in split_ping), split_ping
        assert any(item.endswith("Record.ping") for item in split_ping), split_ping
    finally:
        sys.path.remove(str(site))
