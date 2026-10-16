"""Evidence classification: real wheels, and no vendor literals in the classifier."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from conduit.outside_in.evidence import classify_distribution

SRC = Path(__file__).resolve().parents[1] / "src"
# The migration product, impact fixtures, and the CLI still name the services
# they migrate. The classifier (outside_in, feeds) is not on this list.
_LEGACY = (
    "conduit/anticheat/",
    "conduit/context_filter.py",
    "conduit/credentials.py",
    "conduit/detect/",
    "conduit/export_delta/",
    "conduit/impact/",
    "conduit/integrity.py",
    "conduit/llm/",
    "conduit/main.py",
    "conduit/packet/",
    "conduit/patcher/",
    "conduit/run_preflight.py",
    "conduit/scaffold/",
    "conduit/self_correct.py",
    "conduit/service/",
    "conduit/test_gen.py",
    "conduit/test_runner.py",
)
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
    Legacy migration modules are listed above and must not include the
    classifier or the feed catalog.
    """
    assert not any(item.startswith("conduit/outside_in/") or item.startswith("conduit/feeds/") for item in _LEGACY)
    leaks: list[str] = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if any(rel == item or rel.startswith(item) for item in _LEGACY):
            continue
        for literal in _literals(path):
            lowered = literal.lower()
            for word in _DENY:
                if word in lowered:
                    leaks.append(f"{rel}: {word}")
                    break
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
