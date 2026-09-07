"""Map 0.x SDK resource methods to REST paths via OBJECT_NAME + URL action."""

from __future__ import annotations

import ast
from pathlib import Path

from conduit.export_delta.resolve import package_scan_root

_SKIP_DIRS = {"tests", "test", "types", "vendor", "_vendor"}
_GET_URL_NAMES = {"_get_url", "class_url"}


def extract_resource_paths(tree: Path, *, package: str) -> dict[str, str]:
    """Return ``Class.method`` / ``openai.Class.method`` → ``/v1/...`` from a from-pin tree."""
    root = package_scan_root(tree)
    pkg_dir = _package_dir(root, package)
    if pkg_dir is None:
        return {}
    out: dict[str, str] = {}
    for py in _scan_py_files(pkg_dir):
        try:
            text = py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for callee, path in _resource_paths_from_source(text).items():
            out.setdefault(callee, path)
            if not callee.startswith("openai."):
                out.setdefault(f"openai.{callee}", path)
    return out


def resource_path_for(callee: str, table: dict[str, str]) -> str | None:
    token = (callee or "").strip()
    if not token:
        return None
    if token in table:
        return table[token]
    if token.startswith("openai.") and token[7:] in table:
        return table[token[7:]]
    if f"openai.{token}" in table:
        return table[f"openai.{token}"]
    return None


def _package_dir(root: Path, package: str) -> Path | None:
    name = package.replace("-", "_")
    for candidate in (root / name, root / "src" / name, root):
        if candidate.is_dir() and (candidate / "__init__.py").is_file():
            return candidate
    return None


def _scan_py_files(pkg_dir: Path) -> list[Path]:
    files = [
        py
        for py in pkg_dir.glob("*.py")
        if not (py.name.startswith("_") and py.name != "__init__.py")
    ]
    for sub in pkg_dir.iterdir():
        if not sub.is_dir() or sub.name.startswith(("_", ".")):
            continue
        if sub.name.lower() in _SKIP_DIRS:
            continue
        files.extend(
            py
            for py in sub.glob("*.py")
            if not (py.name.startswith("_") and py.name != "__init__.py")
        )
    return files


def _resource_paths_from_source(source: str) -> dict[str, str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    out: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name.startswith("_"):
            continue
        object_name = _class_object_name(node)
        if not object_name:
            continue
        base = "/v1/" + object_name.replace(".", "/")
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name.startswith("_"):
                continue
            action = _method_url_action(item)
            path = f"{base}/{action}" if action else base
            out[f"{node.name}.{item.name}"] = path
    return out


def _class_object_name(node: ast.ClassDef) -> str | None:
    for item in node.body:
        if not isinstance(item, ast.Assign):
            continue
        names = [t.id for t in item.targets if isinstance(t, ast.Name)]
        if "OBJECT_NAME" not in names:
            continue
        if isinstance(item.value, ast.Constant) and isinstance(item.value.value, str):
            return item.value.value.strip() or None
    return None


def _method_url_action(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        name = _call_name(child.func)
        if name not in _GET_URL_NAMES and not name.endswith("._get_url"):
            continue
        if child.args and isinstance(child.args[0], ast.Constant):
            value = child.args[0].value
            if isinstance(value, str) and value.isidentifier():
                return value
    return None


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts: list[str] = [func.attr]
        cur: ast.AST = func.value
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        return ".".join(reversed(parts))
    return ""
