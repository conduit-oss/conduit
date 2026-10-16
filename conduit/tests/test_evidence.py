"""Evidence classification: real wheels, and no vendor literals under conduit/src."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from conduit.outside_in.evidence import classify_distribution, spec_for_distribution

SRC = Path(__file__).resolve().parents[1] / "src"
_DENY = (
    "airflow",
    "amazonaws",
    "anthropic",
    "asyncpg",
    "boto",
    "clerk",
    "openai",
    "pinecone",
    "psycopg",
    "pymongo",
    "redis",
    "slack",
    "sqlalchemy",
    "stripe",
    "supabase",
    "twilio",
)


def _literals(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append(node.value)
    return found


def test_classifier_sources_have_no_vendor_literals():
    """Vendor names as string literals fail the build.

    Stdlib I/O modules and DB-API method names are the fixed vocabulary.
    Every module under conduit/src is scanned. There is no allowlist.
    """
    leaks: list[str] = []
    scanned = 0
    for path in SRC.rglob("*.py"):
        scanned += 1
        rel = path.relative_to(SRC).as_posix()
        for literal in _literals(path):
            lowered = literal.lower()
            for word in _DENY:
                if word in lowered:
                    leaks.append(f"{rel}: {word}")
                    break
    assert scanned > 0
    assert leaks == []


@pytest.mark.parametrize("name", ["pydantic", "pandas", "numpy", "attrs"])
def test_pure_compute_distributions_are_not_services(name: str):
    found = classify_distribution(name)
    assert found.loaded, name
    assert found.service is False
    assert found.transport is False


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("supabase", "api"),
        ("slack_sdk", "api"),
        ("psycopg2", "database"),
        ("stripe", "api"),
        ("boto3", "cloud"),
        ("sqlalchemy", "database"),
        ("openai", "api"),
    ],
)
def test_real_clients_classify_from_their_own_code(name: str, kind: str):
    found = classify_distribution(name)
    assert found.loaded, name
    assert found.service is True
    assert found.transport is False
    assert found.kind == kind
    hops = found.evidence.split(" -> ")
    assert hops[0] == name.replace("_", "-")
    assert hops[-1] in {"ssl", "socket", "http.client", "urllib.request", "db-api", "service-model"}


@pytest.mark.parametrize("version", ["3.13.0", "4.10.1"])
def test_wire_pool_cursor_driver_is_a_database(version: str):
    found = classify_distribution("pymongo", version)
    assert found.loaded, version
    assert found.service is True
    assert found.transport is False
    assert found.kind == "database"
    assert found.evidence.split(" -> ")[-1] in {"socket", "ssl", "db-api"}


def test_http_core_without_a_product_client_is_a_transport():
    core = classify_distribution("azure-core")
    assert core.loaded
    assert core.service is True
    assert core.transport is True
    assert spec_for_distribution("azure-core") is None
    product = classify_distribution("azure-storage-blob")
    assert product.loaded
    assert product.service is True
    assert product.transport is False
    assert product.kind == "api"
    assert spec_for_distribution("azure-storage-blob") is not None


def test_caller_methods_choose_the_client_they_use():
    found = classify_distribution("supabase")
    assert found.loaded
    assert found.service is True
    assert found.transport is False
    hops = found.evidence.split(" -> ")
    assert "postgrest" in hops
    assert hops[-1] in {"ssl", "socket", "http.client", "urllib.request", "db-api", "service-model"}
