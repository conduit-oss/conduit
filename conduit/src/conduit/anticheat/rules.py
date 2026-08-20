"""Mechanical anti-cheat catalog. Does not trust pytest or the repair LLM."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from conduit.integrity import (
    dummy_except_findings,
    packet_new_tokens,
    unused_marker_literals,
)
from conduit.test_gen import (
    CONDUIT_GENERATED_NAMES,
    is_conduit_generated_rel,
    oracle_forbidden_tokens,
)
from conduit.text_tokens import obfuscated_forbidden_tokens

_SKIP_NEEDLES = (
    "pytest.mark.skip",
    "pytest.mark.xfail",
    "unittest.skip",
    "unittest.expectedFailure",
)

_FAKE_CLIENT_NEEDLES = (
    "_fakeresponse",
    "_fallback_response",
    "chatcmpl-fallback",
    "ftjob-fallback",
    "file-fallback",
    "asst-fallback",
    "exactly the word: pong",
    "reply with exactly: ok",
)

_HTTP_SWALLOW_TYPES = {
    "RequestException",
    "HTTPError",
    "ConnectionError",
    "Timeout",
}

_SDK_HTTP_PATHS = (
    "/chat/completions",
    "/v1/chat/completions",
    "/embeddings",
    "/fine_tuning/jobs",
)

_FORBIDDEN_DIR_PARTS = frozenset({".conduit", "vendor", "packets"})


def _posix(rel: str) -> str:
    return rel.replace("\\", "/")


def is_test_rel(rel: str) -> bool:
    posix = _posix(rel).lower()
    name = Path(posix).name
    return (
        posix.startswith("tests/")
        or "/tests/" in posix
        or name.startswith("test_")
        or name.endswith(".test.js")
        or name.endswith(".spec.js")
        or name == "conftest.py"
    )


def is_impl_rel(rel: str) -> bool:
    if is_test_rel(rel):
        return False
    name = Path(_posix(rel)).name
    if name in {
        "requirements.txt",
        "pyproject.toml",
        "package.json",
        "constraints.txt",
        "go.mod",
    }:
        return False
    return True


def packet_package(packet: dict[str, Any]) -> str:
    return str(packet.get("package") or "").strip()


def forbidden_write_reason(rel: str) -> str | None:
    """Paths self-correct must never edit."""
    posix = _posix(rel)
    parts = Path(posix).parts
    name = Path(posix).name
    if is_conduit_generated_rel(posix) or name in CONDUIT_GENERATED_NAMES:
        return "cannot edit leftover-token oracle, smoke, or functional tests"
    if any(part in _FORBIDDEN_DIR_PARTS for part in parts):
        return f"cannot edit {posix}"
    if name == "conduit-packet.json":
        return "cannot edit published packet files"
    lowered = posix.lower()
    if lowered.endswith(".jsonl") and (
        "/data/" in lowered or "knowledge" in lowered
    ):
        return "cannot edit knowledge/prose seed files"
    return None


def _import_regexes(package: str) -> list[re.Pattern[str]]:
    pkg = re.escape(package)
    return [
        re.compile(rf"\bimport\s+{pkg}\b"),
        re.compile(rf"\bfrom\s+{pkg}\b"),
        re.compile(rf"""from\s+['"]{pkg}(?:/|\.|\s*['"])"""),
        re.compile(rf"""import\s+['"]{pkg}['"]"""),
        re.compile(rf"""require\s*\(\s*['"]{pkg}['"]"""),
        re.compile(rf"""from\s+['"]{pkg}['"]"""),
    ]


def imports_package(text: str, package: str) -> bool:
    pkg = (package or "").strip()
    if not pkg or not text:
        return False
    return any(rx.search(text) for rx in _import_regexes(pkg))


def dynamic_package_import(text: str, package: str, rel: str) -> str | None:
    """importlib / concat import of the packet package (import-check evasion)."""
    pkg = (package or "").strip()
    if not pkg or pkg not in text:
        return None
    if "importlib" in text and (
        "import_module" in text or "__import__" in text
    ):
        if any(
            needle in text
            for needle in (
                f'"{pkg}"',
                f"'{pkg}'",
                f'join([',
                "''.join",
                '""+',
            )
        ):
            return f"{rel} uses dynamic import to load {pkg} (anti-cheat evasion)"
    return None


def fake_client_findings(text: str, rel: str) -> list[str]:
    lowered = text.lower()
    hits: list[str] = []
    for needle in _FAKE_CLIENT_NEEDLES:
        if needle in lowered:
            hits.append(f"{rel} fake/stub API client ({needle})")
            break
    if "simplenamespace" in lowered and "completions" in lowered:
        if "chatcmpl" in lowered or "request_text" in lowered or "requests." in lowered:
            hits.append(f"{rel} SimpleNamespace stub impersonates chat.completions")
    if "unittest.mock" in lowered or "from unittest import mock" in lowered:
        if "openai" in lowered and ("patch(" in lowered or "MagicMock" in lowered):
            hits.append(f"{rel} mocks the official SDK instead of migrating")
    if "vcr" in lowered and ("use_cassette" in lowered or "@vcr" in lowered):
        hits.append(f"{rel} records/replays HTTP instead of calling the new SDK")
    return hits


def _handler_type_names(node: ast.ExceptHandler) -> set[str]:
    names: set[str] = set()
    if node.type is None:
        return names
    if isinstance(node.type, ast.Name):
        names.add(node.type.id)
    elif isinstance(node.type, ast.Attribute):
        names.add(node.type.attr)
    elif isinstance(node.type, ast.Tuple):
        for elt in node.type.elts:
            if isinstance(elt, ast.Name):
                names.add(elt.id)
            elif isinstance(elt, ast.Attribute):
                names.add(elt.attr)
    return names


def http_swallow_findings(text: str, rel: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    findings: list[str] = []

    class _V(ast.NodeVisitor):
        def visit_Try(self, node: ast.Try) -> None:
            for handler in node.handlers:
                types = _handler_type_names(handler)
                if not (types & _HTTP_SWALLOW_TYPES):
                    continue
                raises = any(isinstance(s, ast.Raise) for s in ast.walk(handler))
                if raises:
                    continue
                for stmt in ast.walk(handler):
                    if isinstance(stmt, ast.Name) and stmt.id in {
                        "_fallback_response",
                        "_FakeResponse",
                    }:
                        findings.append(
                            f"{rel}:{handler.lineno} HTTP error path returns a fake client"
                        )
                        break
                    if isinstance(stmt, ast.Call):
                        func = stmt.func
                        name = ""
                        if isinstance(func, ast.Name):
                            name = func.id
                        elif isinstance(func, ast.Attribute):
                            name = func.attr
                        if name in {"_fallback_response", "_FakeResponse"}:
                            findings.append(
                                f"{rel}:{handler.lineno} HTTP error path returns a fake client"
                            )
                            break
            self.generic_visit(node)

    _V().visit(tree)
    return findings


def parallel_http_without_sdk(text: str, rel: str, package: str) -> str | None:
    if not package or imports_package(text, package):
        return None
    if not is_impl_rel(rel):
        return None
    if not any(p in text for p in _SDK_HTTP_PATHS):
        return None
    httpish = (
        "requests." in text
        or "import requests" in text
        or "fetch(" in text
        or "httpx." in text
        or "urllib" in text
    )
    if httpish:
        return (
            f"{rel} calls {package} HTTP paths without importing the official SDK"
        )
    return None


def dropped_sdk_import(
    *,
    rel: str,
    previous: str | None,
    content: str,
    package: str,
) -> str | None:
    if not package or not is_impl_rel(rel):
        return None
    if previous is None:
        return None
    if imports_package(previous, package) and not imports_package(content, package):
        return (
            f"{rel} dropped official {package} import; keep the SDK and migrate call sites"
        )
    return None


def generated_skip_findings(rel: str, text: str) -> list[str]:
    lowered = text.lower()
    if any(n in lowered for n in _SKIP_NEEDLES) or ".skip(" in lowered or ".xfail(" in lowered:
        return [f"{rel} weakens generated tests with skip/xfail"]
    return []


def weak_assert_findings(rel: str, text: str) -> list[str]:
    if not is_test_rel(rel):
        return []
    hits: list[str] = []
    if "assert isinstance(" in text and "len(" in text and "strip()" in text:
        if "assert " in text and text.count("assert ") <= 3:
            hits.append(f"{rel} assertions are too weak to catch a dummy reply")
    return hits


def file_findings(
    rel: str,
    text: str,
    packet: dict[str, Any],
    *,
    previous: str | None = None,
    mode: str = "scan",
) -> list[str]:
    """Mechanical findings for one file (write reject or repo scan)."""
    posix = _posix(rel)
    pkg = packet_package(packet)
    leftover = oracle_forbidden_tokens(packet)
    interesting = packet_new_tokens(packet)
    findings: list[str] = []

    if mode == "reject":
        forbidden = forbidden_write_reason(posix)
        if forbidden:
            return [f"{posix}: {forbidden}"]

    if is_conduit_generated_rel(posix):
        findings.extend(generated_skip_findings(posix, text))
        return findings

    dyn = dynamic_package_import(text, pkg, posix)
    if dyn:
        findings.append(dyn)

    dropped = dropped_sdk_import(
        rel=posix, previous=previous, content=text, package=pkg
    )
    if dropped:
        findings.append(dropped)

    findings.extend(fake_client_findings(text, posix))
    findings.extend(http_swallow_findings(text, posix))

    # Parallel HTTP without the SDK: only on rewrites that dropped the import,
    # or brand-new files (previous == ""). Do not flag pre-existing generated clients.
    if previous is not None:
        if previous == "" or imports_package(previous, pkg):
            parallel = parallel_http_without_sdk(text, posix, pkg)
            if parallel:
                findings.append(parallel)

    is_test = (
        posix.startswith("tests/")
        or "/tests/" in posix
        or Path(posix).name.startswith("test_")
        or Path(posix).name.endswith(".test.js")
        or Path(posix).name == "conftest.py"
    )
    if mode == "reject" and is_test:
        skip_needles = (
            "pytest.mark.skip",
            "pytest.mark.xfail",
            "unittest.skip",
            ".skip(",
            ".xfail(",
            "pytest.skip",
        )
        if any(n in text.lower() for n in skip_needles):
            findings.append(f"{posix} cannot weaken tests with skip/xfail")

    if is_impl_rel(posix):
        hidden = obfuscated_forbidden_tokens(text, leftover)
        if hidden:
            findings.append(
                f"{posix} obfuscates leftover tokens via concat/join: "
                + ", ".join(hidden)
            )
        findings.extend(dummy_except_findings(text, posix))
        markers = unused_marker_literals(text, interesting)
        if markers:
            findings.append(
                f"{posix} unused migration marker literals: " + ", ".join(markers)
            )
    return findings


def reject_write(
    rel: str,
    content: str,
    *,
    packet: dict[str, Any],
    previous: str | None = None,
) -> str | None:
    hits = file_findings(
        rel, content, packet, previous=previous, mode="reject"
    )
    return hits[0] if hits else None


def repo_sdk_import_finding(
    *,
    package: str,
    ecosystem: str | None,
    pin: str | None,
    impl_texts: list[tuple[str, str]],
) -> str | None:
    """Pinned package must still be imported in impl for that ecosystem."""
    pkg = (package or "").strip()
    if not pkg or not pin:
        return None
    eco = str(ecosystem or "").strip().lower()
    suffixes = {".py"}
    if eco in {"npm"}:
        suffixes = {".ts", ".js", ".tsx", ".jsx"}
    elif eco not in {"pypi", "pip", "pyproject"}:
        suffixes = {".py", ".ts", ".js", ".tsx", ".jsx"}
    relevant = [
        (rel, text)
        for rel, text in impl_texts
        if Path(rel).suffix.lower() in suffixes and is_impl_rel(rel)
    ]
    if not relevant:
        return None
    if any(imports_package(text, pkg) for _rel, text in relevant):
        return None
    http_parallel = any(
        parallel_http_without_sdk(text, rel, pkg) for rel, text in relevant
    )
    if http_parallel:
        return (
            f"official {pkg} is pinned ({pin}) but no {eco or 'impl'} file imports it"
        )
    return None
