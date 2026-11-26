"""Detect files that need a whole-file structural rewrite.

Some SDK upgrades change shared state (module-level entry points become a
client instance, exceptions move modules). Per-call-site edits leave imports,
client setup, and error types inconsistent. Qualifying files are rewritten in
one model call; others keep the per-site path.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from conduit.apply.models import ImpactSite

#: Whole-file mode is only for files at or under this many lines.
WHOLE_FILE_MAX_LINES = 800

_ERROR_SUFFIX = re.compile(r"(Error|Exception|Warning)$")
_CLIENT_TOKEN = re.compile(r"(^|\.)Client(\.|$)")


def file_line_count(root: Path, path: str) -> int:
    try:
        return len((root / path).read_text(encoding="utf-8").splitlines())
    except OSError:
        return 0


def qualifies_for_whole_file(root: Path, sites: list[ImpactSite]) -> bool:
    """True when ``sites`` (one file) should use the whole-file rewrite path."""
    if not sites:
        return False
    paths = {site.path for site in sites}
    if len(paths) != 1:
        return False
    path = sites[0].path
    if file_line_count(root, path) > WHOLE_FILE_MAX_LINES:
        return False
    active = [site for site in sites if not site.via_wrapper]
    if not active:
        return False
    if any(_is_module_to_client(site) for site in active):
        return True
    if any(_is_moved_exception_or_import(site) for site in active):
        return True
    if _three_plus_share_sdk_import(root, path, active):
        return True
    return False


def group_sites_by_file(sites: list[ImpactSite]) -> dict[str, list[ImpactSite]]:
    """Preserve first-seen order of paths; sites within a file keep input order."""
    grouped: dict[str, list[ImpactSite]] = {}
    for site in sites:
        grouped.setdefault(site.path, []).append(site)
    return grouped


def _is_module_to_client(site: ImpactSite) -> bool:
    """Removed module-level entry point with a client/instance replacement."""
    if site.kind != "removed":
        return False
    if not _looks_module_level(site):
        return False
    return _has_client_replacement(site)


def _looks_module_level(site: ImpactSite) -> bool:
    """Call goes through a package/module name, not a local client variable."""
    expr = (site.expr or "").strip()
    symbol = (site.symbol or "").strip()
    dep = (site.dependency or "").replace("-", "_")
    parts = [part for part in expr.split(".") if part]
    if len(parts) >= 2:
        first = parts[0]
        if first == dep or first.replace("-", "_") == dep:
            return True
        # package.Class.method / package.module.func
        if first.islower() and any(part[:1].isupper() for part in parts[1:]):
            return True
    if symbol and dep:
        root = dep if symbol.startswith(dep + ".") else dep.replace("_", "-")
        if symbol.startswith(root + "."):
            rest = symbol[len(root) + 1 :]
            # package.Client(...) is construction; package.Thing.create is module-level.
            if rest == "Client" or rest.startswith("Client."):
                return False
            return True
    change = (site.change or "").lower()
    if "module-level" in change or "module level" in change:
        return True
    return False


def _has_client_replacement(site: ImpactSite) -> bool:
    texts: list[str] = [site.new_signature or "", site.new_docstring or "", site.change or ""]
    for cand in site.candidates:
        texts.append(str(cand.get("symbol") or ""))
        texts.append(str(cand.get("signature") or ""))
    blob = "\n".join(texts)
    if _CLIENT_TOKEN.search(blob):
        return True
    lower = blob.lower()
    return "client." in lower and ("instance" in lower or "method" in lower or "()" in blob)


def _is_moved_exception_or_import(site: ImpactSite) -> bool:
    """Exception/error class or import module moved or renamed."""
    if site.kind not in {"removed", "renamed"}:
        return False
    leaf = (site.symbol or site.expr or "").rsplit(".", 1)[-1]
    if _ERROR_SUFFIX.search(leaf):
        return True
    change = (site.change or "").lower()
    if any(word in change for word in ("exception", "error class", "error type")) and any(
        word in change for word in ("moved", "renamed", "moved to", "import")
    ):
        return True
    if "import" in change and any(word in change for word in ("moved", "renamed", "module")):
        return True
    # Renamed module path (no call): symbol is a dotted module, new is another.
    if site.kind == "renamed" and "." in (site.symbol or "") and "." in (site.new_signature or site.old_signature or ""):
        if not any(ch == "(" for ch in (site.symbol or "") + (site.new_signature or "")):
            return True
    return False


def _three_plus_share_sdk_import(root: Path, path: str, sites: list[ImpactSite]) -> bool:
    if len(sites) < 3:
        return False
    deps = {(site.dependency or "").replace("-", "_") for site in sites if site.dependency}
    if len(deps) != 1:
        return False
    dep = next(iter(deps))
    if not dep:
        return False
    try:
        source = (root / path).read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return False
    imported = _sdk_import_roots(tree)
    if not imported:
        # No parseable import of the dep: still qualify when every site names it.
        return all((site.dependency or "").replace("-", "_") == dep for site in sites)
    return bool(imported & {dep, dep.replace("_", "-")}) or any(
        name == dep or name.startswith(dep + ".") for name in imported
    )


def _sdk_import_roots(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add((alias.asname or alias.name).split(".", 1)[0])
                found.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".", 1)[0])
    return found


def whole_file_signatures(sites: list[ImpactSite]) -> list[dict[str, str]]:
    """Deduped new-release signatures / candidates for the whole-file prompt."""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for site in sites:
        rows: list[tuple[str, str]] = []
        if site.new_signature:
            rows.append((site.symbol or site.expr, site.new_signature))
        for cand in site.candidates:
            symbol = str(cand.get("symbol") or "")
            signature = str(cand.get("signature") or "")
            if symbol or signature:
                rows.append((symbol, signature or symbol))
        for symbol, signature in rows:
            key = f"{symbol}|{signature}"
            if key in seen:
                continue
            seen.add(key)
            out.append({"symbol": symbol, "signature": signature})
    return out


def site_still_present(source: str, site: ImpactSite) -> bool:
    """True when the flagged expression (or its leaf) still appears in ``source``."""
    expr = (site.expr or "").strip()
    if not expr:
        return False
    if expr in source:
        return True
    leaf = expr.rsplit(".", 1)[-1]
    # Bare leaf is too weak alone; require the dotted form or old symbol.
    symbol = (site.symbol or "").strip()
    if symbol and symbol in source:
        return True
    old = (site.old_signature or "").strip()
    if old and old in source and "(" not in old:
        return True
    # Module-level Class.method still referenced as Class.method
    if "." in expr:
        tail = ".".join(expr.split(".")[-2:])
        if tail in source:
            return True
    return False
