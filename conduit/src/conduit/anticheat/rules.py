"""Mechanical anti-cheat catalog. Does not trust pytest or the repair LLM.

Floor signals are packet-derived and AST-structural — not lab string needles.
Optional ``packet["anticheat"]`` may add ``banned_kwargs_on`` and ``deny_substrings``.
"""

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

_HTTP_SWALLOW_TYPES = {
    "RequestException",
    "HTTPError",
    "ConnectionError",
    "Timeout",
    "Exception",
    "BaseException",
}

_STUB_TOPLEVEL = frozenset(
    {
        "celery",
        "django",
        "xmltodict",
        "tldextract",
        "environ",
        "validators",
    }
)
_FORBIDDEN_DIR_PARTS = frozenset({".conduit", "vendor", "packets"})

_ORACLE_SHIM_NEEDLES = (
    "_patched_read_text",
    "_sanitize_text",
    "_original_read_text",
    "__legacy__",
)

_COMPAT_NAME_RE = re.compile(r"^_?Compat", re.I)
_INSTALL_COMPAT_RE = re.compile(r"_install_compat|install_compat", re.I)
_PATHISH_RE = re.compile(r"^/[A-Za-z0-9._~/-]+$")
_LEGACY_CALLEE_IN_TEXT_RE = re.compile(
    r"\b(?:openai\.)?(?:Completion|ChatCompletion|Edit|Engine|FineTune|Moderation|Image)"
    r"\.(?:create|list|retrieve)\b"
)


def legacy_callee_still_present(
    text: str, rel: str, packet: dict[str, Any]
) -> str | None:
    """Flag impl files that still *call* packet-known legacy SDK callees.

    Comments/docstrings mentioning old callees are allowed — only AST Call
    sites count, so migration notes do not trip reject_write.
    """
    if not is_impl_rel(rel):
        return None
    old_callees: set[str] = set()
    for rule in packet.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("type") or "") != "AST_CALL_REWRITE":
            continue
        old = str(rule.get("old_callee") or "").strip()
        # Bare tokens (Engine, Edit) are too ambiguous for substring checks.
        if old and "." in old:
            old_callees.add(old)
    body = text or ""
    if not old_callees:
        for match in _LEGACY_CALLEE_IN_TEXT_RE.findall(body):
            old_callees.add(match)
    if not old_callees:
        return None

    try:
        tree = ast.parse(body)
    except SyntaxError:
        # Unparseable writes: fall back to whole-text match.
        for old in sorted(old_callees):
            pat = re.compile(rf"(?<!\w){re.escape(old)}(?![\w.])")
            if pat.search(body):
                return (
                    f"{rel} still calls legacy SDK callee {old!r}; "
                    "migrate to the packet successor"
                )
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        chain = _attr_chain(node.func)
        if not chain:
            continue
        for old in old_callees:
            # Path-pair augment sometimes lists already-modern callees as
            # old_callee (e.g. openai.completions.create). Only flag true
            # legacy PascalCase SDK shapes.
            if not _LEGACY_CALLEE_IN_TEXT_RE.search(old) and not re.search(
                r"(?:ChatCompletion|Completion|Edit|Engine|FineTune|Image|"
                r"Moderation|Embedding|File)\.(?:create|list|retrieve)$",
                old,
            ):
                continue
            if chain == old or chain.endswith("." + old):
                return (
                    f"{rel} still calls legacy SDK callee {old!r}; "
                    "migrate to the packet successor"
                )
    return None


def echo_script_stub_finding(rel: str, text: str) -> str | None:
    """Reject shell scripts that echo legacy paths instead of migrating URLs."""
    posix = _posix(rel)
    if not posix.endswith(".sh"):
        return None
    body = text or ""
    if "echo" not in body.lower():
        return None
    if any(tok in body for tok in ("curl ", "wget ", "httpx.", "requests.", "http")):
        return None
    if _PATHISH_RE.search(body) or "/v1/" in body:
        return (
            f"{posix}: echo_script_stub — echo-stubs API paths without migrating "
            "URLs/models (scripts must use real endpoints, not echo placeholders)"
        )
    return None


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


def packet_anticheat(packet: dict[str, Any]) -> dict[str, Any]:
    raw = packet.get("anticheat")
    return raw if isinstance(raw, dict) else {}


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
    top = Path(posix).parts[0] if posix else ""
    if Path(top).stem in _STUB_TOPLEVEL:
        return "cannot invent third-party package stubs"
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


def references_package(text: str, package: str) -> bool:
    """Import or string/literal mention of the packet package."""
    pkg = (package or "").strip()
    if not pkg or not text:
        return False
    if imports_package(text, pkg):
        return True
    if f'"{pkg}"' in text or f"'{pkg}'" in text:
        return True
    if f'"{pkg}.' in text or f"'{pkg}." in text:
        return True
    return bool(re.search(rf"\b{re.escape(pkg)}\b", text))


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
                f"join([",
                "''.join",
                '""+',
            )
        ):
            return f"{rel} uses dynamic import to load {pkg} (anti-cheat evasion)"
    return None


def packet_http_paths(packet: dict[str, Any]) -> list[str]:
    """Path-like tokens from packet rules / api_patterns (no hardcoded SDK paths)."""
    seen: set[str] = set()
    out: list[str] = []

    def _add(raw: str) -> None:
        val = str(raw or "").strip()
        if not val or not val.startswith("/"):
            return
        # trim query/fragment; keep path prefix tokens
        path = val.split("?", 1)[0].split("#", 1)[0]
        if not _PATHISH_RE.match(path):
            return
        if path not in seen:
            seen.add(path)
            out.append(path)

    for rule in packet.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        for key in ("match", "replace", "old_path", "new_path", "path"):
            _add(str(rule.get(key) or ""))
    for item in packet.get("api_patterns") or []:
        _add(str(item))
    hints = packet_anticheat(packet)
    for item in hints.get("http_paths") or []:
        _add(str(item))
    return out


def packet_new_callees(packet: dict[str, Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for rule in packet.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("type") or "") != "AST_CALL_REWRITE":
            continue
        cal = str(rule.get("new_callee") or "").strip()
        if cal and cal not in seen:
            seen.add(cal)
            out.append(cal)
    return out


def packet_new_callee_roots(packet: dict[str, Any]) -> set[str]:
    roots: set[str] = set()
    for cal in packet_new_callees(packet):
        root = cal.split(".", 1)[0].strip()
        if root:
            roots.add(root)
    return roots


def banned_kwargs_map(packet: dict[str, Any]) -> dict[str, list[tuple[str, str | None]]]:
    """Map callee suffix -> list of (banned_old_kwarg, required_new_kwarg|None)."""
    by_callee: dict[str, list[tuple[str, str | None]]] = {}
    rewrite_callees = packet_new_callees(packet)

    def _add(callee: str, old: str, new: str | None) -> None:
        callee = (callee or "").strip()
        old = (old or "").strip()
        if not old:
            return
        key = callee or "*"
        by_callee.setdefault(key, [])
        pair = (old, (new or None))
        if pair not in by_callee[key]:
            by_callee[key].append(pair)

    for rule in packet.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("type") or "") != "AST_PARAM_RENAME":
            continue
        old = str(rule.get("old_param") or "").strip()
        new = str(rule.get("new_param") or "").strip() or None
        scoped = (
            str(rule.get("new_callee") or rule.get("callee") or "").strip()
        )
        if scoped:
            _add(scoped, old, new)
        elif rewrite_callees:
            for cal in rewrite_callees:
                _add(cal, old, new)
        else:
            _add("*", old, new)

    hints = packet_anticheat(packet).get("banned_kwargs_on") or {}
    if isinstance(hints, dict):
        for callee, kwargs in hints.items():
            if isinstance(kwargs, (list, tuple)):
                for kw in kwargs:
                    _add(str(callee), str(kw), None)
    return by_callee


def deny_substrings(packet: dict[str, Any]) -> list[str]:
    raw = packet_anticheat(packet).get("deny_substrings") or []
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(x).lower() for x in raw if str(x).strip()]


def fake_client_findings(
    text: str, rel: str, packet: dict[str, Any]
) -> list[str]:
    pkg = packet_package(packet)
    lowered = text.lower()
    hits: list[str] = []

    for needle in deny_substrings(packet):
        if needle and needle in lowered:
            hits.append(f"{rel} matches packet anticheat deny_substring ({needle})")
            break

    mocks = (
        "unittest.mock" in lowered
        or "from unittest import mock" in lowered
        or "magicmock" in lowered
        or "jest.mock" in lowered
        or "sinon." in lowered
    )
    if mocks and references_package(text, pkg):
        if (
            "patch(" in lowered
            or "magicmock" in lowered
            or "jest.mock" in lowered
            or "sinon." in lowered
        ):
            hits.append(
                f"{rel} mocks the official {pkg} SDK instead of migrating"
            )

    if "vcr" in lowered and ("use_cassette" in lowered or "@vcr" in lowered):
        if references_package(text, pkg) or packet_http_paths(packet):
            hits.append(
                f"{rel} records/replays HTTP instead of calling the new SDK"
            )
    return hits


def _handler_type_names(node: ast.ExceptHandler) -> set[str]:
    names: set[str] = set()
    if node.type is None:
        return {"Exception"}
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


_SDK_STUB_KEYS = frozenset({"choices", "usage", "results"})


def _is_literal_response(node: ast.AST | None) -> bool:
    """True for in-process SDK *success* stubs, not app error envelopes."""
    if node is None:
        return True
    if isinstance(node, ast.Constant):
        return node.value is None
    if isinstance(node, ast.Dict):
        keys = {
            str(k.value)
            for k in node.keys
            if isinstance(k, ast.Constant)
        }
        if not keys:
            return True
        if keys & _SDK_STUB_KEYS:
            return True
        if "id" in keys and ("data" in keys or "object" in keys):
            return True
        return False
    if isinstance(node, (ast.List, ast.Tuple)) and not node.elts:
        return True
    if isinstance(node, ast.Call):
        name = _attr_chain(node.func)
        if name.endswith("SimpleNamespace") or name == "SimpleNamespace":
            return True
        if isinstance(node.func, ast.Name) and node.func.id in {"dict", "list"}:
            return True
    return False


def _sdk_name_roots(tree: ast.AST, package: str) -> set[str]:
    """Names that mean a call into the migrated SDK (module, imports, client binds)."""
    pkg = (package or "").strip()
    if not pkg:
        return set()
    roots: set[str] = {pkg}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == pkg or alias.name.startswith(pkg + "."):
                    roots.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").strip()
            if mod != pkg and not mod.startswith(pkg + "."):
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                imported.add(alias.asname or alias.name)
    roots |= imported
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            value, targets = node.value, node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value, targets = node.value, [node.target]
        else:
            continue
        if not isinstance(value, ast.Call):
            continue
        func = value.func
        callee = ""
        if isinstance(func, ast.Name):
            callee = func.id
        elif isinstance(func, ast.Attribute):
            callee = _attr_chain(func)
        head = callee.split(".", 1)[0] if callee else ""
        if head not in imported and not (
            callee == pkg or callee.startswith(pkg + ".")
        ):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                roots.add(target.id)
    return roots


def _subtree_calls_roots(node: ast.AST, roots: set[str]) -> bool:
    if not roots:
        return False
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in roots:
            return True
        if isinstance(child, ast.Attribute):
            chain = _attr_chain(child)
            if any(chain == r or chain.startswith(r + ".") for r in roots):
                return True
    return False


def _try_body_calls_package(
    try_node: ast.Try, package: str, roots: set[str] | None = None
) -> bool:
    active = roots if roots is not None else ({package} if package else set())
    for stmt in try_node.body:
        if _subtree_calls_roots(stmt, active):
            return True
    return False


def _handler_calls_package(
    handler: ast.ExceptHandler, package: str, roots: set[str] | None = None
) -> bool:
    active = roots if roots is not None else ({package} if package else set())
    if _subtree_calls_roots(handler, active):
        return True
    pkg = (package or "").strip()
    if not pkg:
        return False
    for node in ast.walk(handler):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value == pkg or node.value.startswith(pkg + "."):
                return True
    return False


def synthetic_except_findings(
    text: str, rel: str, package: str
) -> list[str]:
    """Flag except handlers that swallow a real SDK call with a synthetic stub.

    Only when the try body invokes the migrated package (incl. OpenAI/client
    aliases). Unrelated fallbacks in the same file are ignored.
    """
    pkg = (package or "").strip()
    if not pkg:
        return []
    if not (imports_package(text, pkg) or references_package(text, pkg)):
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    roots = _sdk_name_roots(tree, pkg)
    findings: list[str] = []
    care = _HTTP_SWALLOW_TYPES | {"Exception", "BaseException"}

    class _V(ast.NodeVisitor):
        def visit_Try(self, node: ast.Try) -> None:
            for handler in node.handlers:
                types = _handler_type_names(handler)
                if not (types & care):
                    continue
                raises = any(isinstance(s, ast.Raise) for s in ast.walk(handler))
                if raises:
                    continue
                if not _try_body_calls_package(node, package, roots):
                    continue
                if _handler_calls_package(handler, package, roots):
                    continue
                for stmt in handler.body:
                    if isinstance(stmt, ast.Return) and _is_literal_response(
                        stmt.value
                    ):
                        findings.append(
                            f"{rel}:{handler.lineno} except path returns a "
                            "synthetic response without calling the official SDK"
                        )
                        break
            self.generic_visit(node)

    _V().visit(tree)
    return findings


def parallel_http_without_sdk(
    text: str, rel: str, package: str, packet: dict[str, Any]
) -> str | None:
    if not package or imports_package(text, package):
        return None
    if not is_impl_rel(rel):
        return None
    paths = packet_http_paths(packet)
    if not paths or not any(p in text for p in paths):
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
    """Reject dropping the SDK import only when the file still needs it.

    Centralizing ``OpenAI()`` / ``import openai`` behind a local client helper
    is a valid migration — other modules may stop importing the package.
    Flag only when this file still references ``package.`` without importing,
    or when parallel HTTP detection already covers the cheat.
    """
    if not package or not is_impl_rel(rel):
        return None
    if previous is None:
        return None
    if not (
        imports_package(previous, package) and not imports_package(content, package)
    ):
        return None
    # Still uses the package as a module attribute without importing it.
    if re.search(rf"(?<![\w.]){re.escape(package)}\.", content or ""):
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


def _attr_chain(node: ast.AST) -> str:
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    parts.reverse()
    return ".".join(parts)


def _is_path_read_text_target(node: ast.AST) -> bool:
    chain = _attr_chain(node)
    return chain in {
        "Path.read_text",
        "pathlib.Path.read_text",
    }


def oracle_io_shim_findings(text: str, rel: str) -> list[str]:
    """Flag Path.read_text / open patches that sanitize leftover-oracle reads."""
    posix = _posix(rel)
    name = Path(posix).name.lower()
    lowered = text.lower()
    hits: list[str] = []

    patches_read_text = False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None

    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if _is_path_read_text_target(target):
                        patches_read_text = True
            elif isinstance(node, ast.AugAssign) and _is_path_read_text_target(
                node.target
            ):
                patches_read_text = True
            elif isinstance(node, ast.Call):
                func = _attr_chain(node.func)
                if func.endswith("setattr") or func == "setattr":
                    args = node.args
                    if len(args) >= 2:
                        attr = args[1]
                        if (
                            isinstance(attr, ast.Constant)
                            and attr.value == "read_text"
                        ):
                            patches_read_text = True

    if "path.read_text =" in lowered or "pathlib.path.read_text =" in lowered:
        patches_read_text = True

    launders_oracle = "test_conduit_oracle" in lowered or (
        "forbidden" in lowered
        and ("__legacy__" in lowered or "_sanitize" in lowered)
    )
    shim_needles = any(n in lowered for n in _ORACLE_SHIM_NEEDLES)
    known_shim_file = name in {"sitecustomize.py", "_test_shim.py"} or (
        name.endswith("_test_shim.py")
    )

    if patches_read_text and (launders_oracle or shim_needles or known_shim_file):
        hits.append(
            f"{posix} patches Path.read_text to sanitize leftover-oracle file reads"
        )
    elif known_shim_file and shim_needles and (
        "path.read_text" in lowered or "forbidden" in lowered
    ):
        hits.append(
            f"{posix} installs a test-time I/O shim to launder leftover-oracle reads"
        )
    elif patches_read_text and "__legacy__" in lowered:
        hits.append(
            f"{posix} patches Path.read_text to hide leftover tokens"
        )
    return hits


def old_kwargs_on_new_callee_findings(
    text: str, rel: str, packet: dict[str, Any]
) -> list[str]:
    bans = banned_kwargs_map(packet)
    if not bans:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    hits: list[str] = []

    class _V(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            chain = _attr_chain(node.func)
            chain_l = chain.lower()
            keys = {
                kw.arg for kw in node.keywords if isinstance(kw.arg, str)
            }
            for callee, pairs in bans.items():
                if callee != "*":
                    if callee.lower() not in chain_l and not chain_l.endswith(
                        "." + callee.lower()
                    ):
                        # also allow exact suffix match
                        if not chain_l.endswith(callee.lower()):
                            continue
                for old, new in pairs:
                    if old in keys and (new is None or new not in keys):
                        if new:
                            hits.append(
                                f"{rel}:{node.lineno} {chain} still uses "
                                f"{old}= (migrate to {new}=)"
                            )
                        else:
                            hits.append(
                                f"{rel}:{node.lineno} {chain} still uses "
                                f"{old}= (migrate off legacy kwarg)"
                            )
            self.generic_visit(node)

    _V().visit(tree)
    # dedupe
    seen: set[str] = set()
    uniq: list[str] = []
    for item in hits:
        if item not in seen:
            seen.add(item)
            uniq.append(item)
    return uniq


def sdk_monkeypatch_findings(
    text: str, rel: str, package: str, packet: dict[str, Any]
) -> list[str]:
    """Flag replacing package.<new_callee_root> with Compat wrappers."""
    pkg = (package or "").strip()
    posix = _posix(rel)
    if not pkg or not is_impl_rel(posix):
        return []
    hits: list[str] = []
    roots = packet_new_callee_roots(packet)

    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None

    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and _COMPAT_NAME_RE.match(node.name):
                hits.append(
                    f"{posix}:{node.lineno} Compat wrapper {node.name} "
                    f"preserves legacy SDK shapes instead of migrating call sites"
                )
            if isinstance(node, ast.FunctionDef) and _INSTALL_COMPAT_RE.search(
                node.name
            ):
                hits.append(
                    f"{posix}:{node.lineno} {node.name} monkeypatches "
                    f"the official {pkg} SDK"
                )
            if roots and isinstance(node, ast.Assign):
                for target in node.targets:
                    chain = _attr_chain(target)
                    for root in roots:
                        want = f"{pkg}.{root}"
                        if chain == want or chain.endswith(f".{want}"):
                            hits.append(
                                f"{posix} assigns {want} = … "
                                "(monkeypatch instead of migrating call sites)"
                            )
            if roots and isinstance(node, ast.Call):
                func = _attr_chain(node.func)
                if func.endswith("setattr") or func == "setattr":
                    if len(node.args) >= 2:
                        obj = _attr_chain(node.args[0])
                        attr = node.args[1]
                        if (
                            obj == pkg
                            and isinstance(attr, ast.Constant)
                            and isinstance(attr.value, str)
                            and attr.value in roots
                        ):
                            hits.append(
                                f"{posix} setattr({pkg}, '{attr.value}', …) "
                                "monkeypatches the official SDK"
                            )

    if roots:
        compact = text.replace(" ", "")
        for root in roots:
            needle = f"{pkg}.{root}="
            if needle in compact:
                msg = (
                    f"{posix} assigns {pkg}.{root} = … "
                    "(monkeypatch instead of migrating call sites)"
                )
                if msg not in hits:
                    hits.append(msg)

    seen: set[str] = set()
    uniq: list[str] = []
    for item in hits:
        if item not in seen:
            seen.add(item)
            uniq.append(item)
    return uniq


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

    findings.extend(fake_client_findings(text, posix, packet))
    findings.extend(synthetic_except_findings(text, posix, pkg))
    findings.extend(oracle_io_shim_findings(text, posix))
    findings.extend(sdk_monkeypatch_findings(text, posix, pkg, packet))
    findings.extend(old_kwargs_on_new_callee_findings(text, posix, packet))

    legacy = legacy_callee_still_present(text, posix, packet)
    if legacy:
        findings.append(legacy)
    echo_stub = echo_script_stub_finding(posix, text)
    if echo_stub:
        findings.append(echo_stub)

    # Parallel HTTP without the SDK:
    # or brand-new files (previous == ""). Do not flag pre-existing generated clients.
    if previous is not None:
        if previous == "" or imports_package(previous, pkg):
            parallel = parallel_http_without_sdk(text, posix, pkg, packet)
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
        hidden = obfuscated_forbidden_tokens(text, leftover, path=posix)
        if hidden:
            findings.append(
                f"{posix} obfuscates leftover tokens via concat/join/byte-decode: "
                + ", ".join(hidden)
            )
        findings.extend(dummy_except_findings(text, posix, pkg))
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
    packet: dict[str, Any] | None = None,
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
    pkt = packet or {"package": pkg}
    http_parallel = any(
        parallel_http_without_sdk(text, rel, pkg, pkt) for rel, text in relevant
    )
    if http_parallel:
        return (
            f"official {pkg} is pinned ({pin}) but no {eco or 'impl'} file imports it"
        )
    return None
