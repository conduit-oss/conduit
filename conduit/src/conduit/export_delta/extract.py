"""Extract public symbol names from an unpacked package tree."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from conduit.export_delta.resolve import package_scan_root


def extract_exports_from_tree(
    tree: Path,
    *,
    package: str,
    ecosystem: str = "pypi",
) -> set[str]:
    root = package_scan_root(tree)
    if ecosystem == "npm":
        return _extract_npm(root, package)
    return _extract_python(root, package)


def _extract_python(root: Path, package: str) -> set[str]:
    symbols: set[str] = set()
    # Prefer package dir named after import name
    candidates = [
        root / package.replace("-", "_"),
        root / "src" / package.replace("-", "_"),
        root,
    ]
    pkg_dir = next((c for c in candidates if c.is_dir() and (c / "__init__.py").is_file()), None)
    if pkg_dir is None:
        # wheel layout: top-level modules
        for init in root.rglob("__init__.py"):
            if any(p.startswith(".") for p in init.parts):
                continue
            pkg_dir = init.parent
            break
    if pkg_dir is None:
        return symbols

    init = pkg_dir / "__init__.py"
    if init.is_file():
        try:
            text = init.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        if text:
            symbols |= _python_file_exports(text)
            symbols |= _python_class_method_exports(text)

    # Shallow: immediate submodules + one extra package level (api_resources/*.py).
    skip_subs = {"tests", "test", "types", "vendor", "_vendor"}
    py_files = [
        py
        for py in pkg_dir.glob("*.py")
        if not (py.name.startswith("_") and py.name != "__init__.py")
    ]
    for sub in pkg_dir.iterdir():
        if not sub.is_dir() or sub.name.startswith(("_", ".")):
            continue
        if sub.name.lower() in skip_subs:
            continue
        py_files.extend(
            py
            for py in sub.glob("*.py")
            if not (py.name.startswith("_") and py.name != "__init__.py")
        )

    for py in py_files:
        try:
            text = py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        symbols |= _python_file_exports(text)
        symbols |= _python_class_method_exports(text)
        symbols.add(py.stem)

    return {s for s in symbols if s and not s.startswith("_")}


def _python_file_exports(source: str) -> set[str]:
    symbols: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return symbols

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "__all__" and isinstance(
                    node.value, (ast.List, ast.Tuple)
                ):
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            symbols.add(elt.value)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                symbols.add(node.name)
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and not t.id.startswith("_"):
                    symbols.add(t.id)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if not node.target.id.startswith("_"):
                symbols.add(node.target.id)
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                if name != "*" and not name.startswith("_"):
                    symbols.add(name)
    return symbols


def _python_class_method_exports(source: str) -> set[str]:
    """Public ClassName.method symbols (Audio.transcribe, ChatCompletion.create)."""
    symbols: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return symbols
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name.startswith("_"):
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name.startswith("_"):
                continue
            symbols.add(f"{node.name}.{item.name}")
    return symbols


_EXPORT_RE = re.compile(
    r"export\s+(?:declare\s+)?(?:type\s+|interface\s+|class\s+|function\s+|const\s+|enum\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)"
)
_EXPORT_AS_RE = re.compile(
    r"export\s*\{([^}]+)\}"
)


def _extract_npm(root: Path, package: str) -> set[str]:
    symbols: set[str] = set()
    pkg_json = root / "package.json"
    types_path: Path | None = None
    if pkg_json.is_file():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        for key in ("types", "typings"):
            rel = data.get(key)
            if isinstance(rel, str):
                candidate = root / rel
                if candidate.is_file():
                    types_path = candidate
                    break
        exports = data.get("exports")
        if isinstance(exports, dict):
            for k in exports:
                if isinstance(k, str) and k not in {".", "./"}:
                    symbols.add(k.lstrip("./"))

    dts_files: list[Path] = []
    if types_path:
        dts_files.append(types_path)
    else:
        dts_files.extend(root.glob("*.d.ts"))
        dts_files.extend((root / "dist").glob("*.d.ts") if (root / "dist").is_dir() else [])
        dts_files.extend((root / "types").glob("*.d.ts") if (root / "types").is_dir() else [])

    for path in dts_files[:20]:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        symbols |= set(_EXPORT_RE.findall(text))
        for block in _EXPORT_AS_RE.findall(text):
            for part in block.split(","):
                part = part.strip()
                if not part:
                    continue
                # `foo as bar` or `default as Foo`
                bits = part.split()
                name = bits[-1] if bits else part
                if name and name != "type":
                    symbols.add(name)

    return {s for s in symbols if s and not s.startswith("_")}
