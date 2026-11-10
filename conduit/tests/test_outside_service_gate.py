"""Outside-service gate: host bindings, path templates, Client vs transport.

Synthetic package names only. No vendor SDK names, no migration tables.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

from conduit.outside_in.evidence import (
    _Code,
    _assigned_hosts,
    _path_templates,
    _scan,
    _finish,
    classify_distribution,
    spec_for_distribution,
)
from conduit.outside_in.specs import evidence_spec


def _install_package(root: Path, dist: str, import_name: str, version: str, source: str) -> None:
    site = root / ".venv" / "lib" / "python3.12" / "site-packages"
    info = site / f"{dist}-{version}.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(f"Name: {dist}\nVersion: {version}\n", encoding="utf-8")
    (info / "top_level.txt").write_text(f"{import_name}\n", encoding="utf-8")
    package = site / import_name
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")


def test_urlish_api_url_names_and_https_literals_are_hosts():
    """Host detection is name-shape + URL-literal based, not a vendor list."""
    tree = ast.parse(
        textwrap.dedent(
            """
            ACME_API_URL = "https://api.acmeai.com"
            api_url = "https://east.acmeai.com/v1"
            server_url = "https://edge.acmeai.com"
            # Plain name: value must look like an API endpoint.
            DEFAULT = "https://fallback.acmeai.com/v2"
            # Package homepage / prose must not count.
            __url__ = "https://acmeai.readthedocs.io"
            note = "see https://api.acmeai.com for docs"
            homepage = "https://www.acmeai.com"
            # Reserved / local hosts stay out.
            local = "http://localhost:8080"
            docs = "https://docs.acmeai.com/guide"
            """
        )
    )
    assert _assigned_hosts(tree) == {
        "api.acmeai.com",
        "east.acmeai.com",
        "edge.acmeai.com",
        "fallback.acmeai.com",
    }


def test_path_templates_from_fstring_format_and_percent():
    """Fixed paths after a templated host, including format/% construction."""
    tree = ast.parse(
        textwrap.dedent(
            """
            def a(self):
                return f"{self.protocol}://{self.domain}/api/v2/users"

            def b(self):
                return f"{self.api_url}/v1/generate"

            def c(base):
                return "{}/v1/embed".format(base)

            def d(host):
                return "https://%s/v1/chat" % host

            def e(scheme, host, path):
                return f"{scheme}://{host}{path}"
            """
        )
    )
    assert _path_templates(tree) == {"/api/v2/users", "/v1/generate", "/v1/embed", "/v1/chat"}


def test_sdk_client_over_http_helper_is_outside_service(tmp_path: Path):
    """A product Client beside an HTTP verb class is a service, not a transport.

    Made-up package ``acmeai``: declares ACME_API_URL, exposes Client with
    product methods, and also ships a Wire helper with get/post/request.
    """
    version = "1.0.0"
    _install_package(
        tmp_path,
        "acmeai",
        "acmeai",
        version,
        """
        import http.client

        ACME_API_URL = "https://api.acmeai.com"

        class Wire:
            def request(self, method, path):
                conn = http.client.HTTPSConnection("api.acmeai.com")
                conn.request(method, path)
                return conn.getresponse()

            def get(self, path):
                return self.request("GET", path)

            def post(self, path):
                return self.request("POST", path)

        class Client:
            def __init__(self, api_url=None):
                self.api_url = api_url or ACME_API_URL
                self._wire = Wire()

            def generate(self, prompt):
                return self._wire.post("/v1/generate")

            def embed(self, texts):
                return self._wire.post("/v1/embed")
        """,
    )
    found = classify_distribution("acmeai", version, root=tmp_path)
    assert found.loaded is True
    assert found.service is True
    assert found.transport is False
    assert found.kind == "api"
    assert "api.acmeai.com" in found.hosts
    assert "Client" in found.constructors
    assert evidence_spec("acmeai", version, root=tmp_path) is not None
    assert spec_for_distribution("acmeai", version, root=tmp_path) is not None


def test_sdk_client_without_host_name_still_counts_via_client_class(tmp_path: Path):
    """Product Client classes count even when the host binding uses a plain name.

    The URL literal on the right-hand side is still read; the Client class is
    independent evidence so an HTTP helper in the same package cannot demote it.
    """
    version = "2.0.0"
    _install_package(
        tmp_path,
        "acmeai",
        "acmeai",
        version,
        """
        import http.client

        # Plain name; value is the https URL (value-based host detection).
        ROOT = "https://api.acmeai.com"

        class Wire:
            def request(self, method, path):
                conn = http.client.HTTPSConnection("api.acmeai.com")
                conn.request(method, path)
                return conn.getresponse()

            def get(self, path):
                return self.request("GET", path)

            def post(self, path):
                return self.request("POST", path)

        class Client:
            def __init__(self):
                self.root = ROOT
                self._wire = Wire()

            def generate(self, prompt):
                return self._wire.post("/v1/generate")

            def chat(self, message):
                return self._wire.post("/v1/chat")
        """,
    )
    found = classify_distribution("acmeai", version, root=tmp_path)
    assert found.service is True
    assert found.transport is False
    assert "Client" in found.constructors


def test_pure_transport_session_is_still_refused(tmp_path: Path):
    """A generic HTTP Session with no fixed remote host stays a transport."""
    version = "0.1.0"
    _install_package(
        tmp_path,
        "plainhttp",
        "plainhttp",
        version,
        """
        import http.client

        class Session:
            def __init__(self):
                self._conn = None

            def request(self, method, url):
                host = url.split("/")[2]
                self._conn = http.client.HTTPSConnection(host)
                self._conn.request(method, url)
                return self._conn.getresponse()

            def get(self, url):
                return self.request("GET", url)

            def post(self, url):
                return self.request("POST", url)

            def put(self, url):
                return self.request("PUT", url)

            def delete(self, url):
                return self.request("DELETE", url)

            def head(self, url):
                return self.request("HEAD", url)

            def patch(self, url):
                return self.request("PATCH", url)

            def send(self, prepared):
                return self.request(prepared[0], prepared[1])

            def stream(self, method, url):
                return self.request(method, url)
        """,
    )
    found = classify_distribution("plainhttp", version, root=tmp_path)
    assert found.loaded is True
    assert found.service is True
    assert found.transport is True
    assert evidence_spec("plainhttp", version, root=tmp_path) is None
    assert spec_for_distribution("plainhttp", version, root=tmp_path) is None


def test_path_templates_alone_keep_a_templated_client_as_product():
    """Enough fixed paths after a host slot mean the package owns the API surface."""
    code = _Code(
        package="pathy",
        version="0",
        files=[
            (
                "pathy.py",
                textwrap.dedent(
                    """
                    import http.client

                    class Session:
                        def request(self, method, url):
                            host = url.split("/")[2]
                            conn = http.client.HTTPSConnection(host)
                            conn.request(method, url)
                            return conn.getresponse()

                        def get(self, url):
                            return self.request("GET", url)

                        def post(self, url):
                            return self.request("POST", url)

                        def put(self, url):
                            return self.request("PUT", url)

                        def delete(self, url):
                            return self.request("DELETE", url)

                        def head(self, url):
                            return self.request("HEAD", url)

                        def patch(self, url):
                            return self.request("PATCH", url)

                        def send(self, prepared):
                            return self.request(prepared[0], prepared[1])

                        def users(self, base):
                            return self.get(f"{base}/api/v2/users")

                        def orgs(self, base):
                            return self.get(f"{base}/api/v2/orgs")

                        def keys(self, base):
                            return self.get("{}/api/v2/keys".format(base))
                    """
                ).encode(),
            )
        ],
        requires=[],
        modules=("pathy",),
    )
    direct = _scan(code)
    assert len(direct.path_templates) >= 3
    result = _finish("pathy", direct, root=None, depth=0, stack=frozenset())
    assert result.service is True
    assert result.transport is False
