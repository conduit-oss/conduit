"""Whole-token scan plus literal concat/join reconstruction (anti-obfuscation)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

_TOKEN_CHAR = r"A-Za-z0-9_." + r"-"

_JS_SUFFIXES = {".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"}
_PY_SUFFIXES = {".py", ".pyi"}

# Cheap anchors only — string bodies are parsed from known offsets (no nested
# backref quantifiers over whole-file finditer, which hangs on minified JS).
_PY_JOIN_TAIL = re.compile(r"""\.\s*join\s*\(\s*\[""")
_JS_JOIN_OPEN = re.compile(r"""\[\s*['"]""")
_STR_LIT = re.compile(r"""(['"])(?:\\.|(?!\1).)*\1""")


def token_in_text(text: str, token: str) -> bool:
    """True if ``token`` appears as a whole token (same bounds as exact_replace)."""
    if not token or token not in text:
        return False
    pattern = re.compile(rf"(?<![{_TOKEN_CHAR}]){re.escape(token)}(?![{_TOKEN_CHAR}])")
    return pattern.search(text) is not None


def _suffix_for(path: str | None) -> str:
    if not path:
        return ""
    return Path(str(path).replace("\\", "/")).suffix.lower()


def _is_js_path(path: str | None) -> bool:
    return _suffix_for(path) in _JS_SUFFIXES


def _is_py_path(path: str | None) -> bool:
    suf = _suffix_for(path)
    return not suf or suf in _PY_SUFFIXES


def _unquote(lit: str) -> str:
    try:
        return ast.literal_eval(lit)
    except (ValueError, SyntaxError):
        if len(lit) >= 2 and lit[0] == lit[-1] and lit[0] in {"'", '"'}:
            return lit[1:-1]
        return lit


def _parse_str_list_body(body: str) -> list[str] | None:
    """Parse comma-separated string literals until ``]``; None if non-literal."""
    parts: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        while i < n and body[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break
        if body[i] == "]":
            return parts
        if body[i] not in {"'", '"'}:
            return None
        m = _STR_LIT.match(body, i)
        if not m:
            return None
        parts.append(_unquote(m.group(0)))
        i = m.end()
    return None


def _string_lit_ending_at(text: str, end: int) -> str | None:
    """If ``text[:end]`` ends with a Python/JS string literal, return that lit."""
    if end <= 0:
        return None
    q = text[end - 1]
    if q not in {"'", '"'}:
        return None
    j = end - 2
    while j >= 0:
        if text[j] == q:
            # Count preceding backslashes — even => real closer at j is opener.
            bs = 0
            k = j - 1
            while k >= 0 and text[k] == "\\":
                bs += 1
                k -= 1
            if bs % 2 == 1:
                j -= 1
                continue
            cand = text[j:end]
            if _STR_LIT.fullmatch(cand):
                return cand
            return None
        j -= 1
    return None


def _scan_py_joins(text: str) -> list[str]:
    """Find ``\"sep\".join([\"a\", \"b\"])`` without catastrophic backtracking."""
    found: list[str] = []
    for m in _PY_JOIN_TAIL.finditer(text):
        # Walk left over whitespace between sep literal and ``.join``.
        left = m.start()
        while left > 0 and text[left - 1] in " \t\r\n":
            left -= 1
        sep_lit = _string_lit_ending_at(text, left)
        if sep_lit is None:
            continue
        parts = _parse_str_list_body(text[m.end() :])
        if parts is not None:
            found.append(_unquote(sep_lit).join(parts))
    return found


def _scan_js_joins(text: str) -> list[str]:
    """Find ``[\"a\", \"b\"].join(\"sep\")`` without catastrophic backtracking."""
    found: list[str] = []
    for m in _JS_JOIN_OPEN.finditer(text):
        start = m.start()
        parts = _parse_str_list_body(text[start + 1 :])
        if parts is None:
            continue
        i = start + 1
        n = len(text)
        ok = True
        while i < n:
            while i < n and text[i] in " \t\r\n,":
                i += 1
            if i < n and text[i] == "]":
                i += 1
                break
            if i >= n or text[i] not in {"'", '"'}:
                ok = False
                break
            lit = _STR_LIT.match(text, i)
            if not lit:
                ok = False
                break
            i = lit.end()
        else:
            ok = False
        if not ok:
            continue
        while i < n and text[i] in " \t\r\n":
            i += 1
        if not text.startswith(".join", i):
            continue
        i += len(".join")
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n or text[i] != "(":
            continue
        i += 1
        while i < n and text[i] in " \t\r\n":
            i += 1
        sep_m = _STR_LIT.match(text, i)
        if not sep_m:
            continue
        found.append(_unquote(sep_m.group(0)).join(parts))
    return found


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _folded_add(node: ast.AST) -> str | None:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _folded_add(node.left)
        right = _folded_add(node.right)
        if left is not None and right is not None:
            return left + right
        return None
    return _const_str(node)


def _join_call(node: ast.Call) -> str | None:
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "join"):
        return None
    sep = _const_str(func.value)
    if sep is None or not node.args:
        return None
    arg = node.args[0]
    elts: list[ast.AST] | None = None
    if isinstance(arg, (ast.List, ast.Tuple)):
        elts = list(arg.elts)
    if elts is None:
        return None
    parts = [_const_str(elt) for elt in elts]
    if any(p is None for p in parts):
        return None
    return sep.join(parts)  # type: ignore[arg-type]


def _int_const(node: ast.AST) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    return None


def _byte_ints(node: ast.AST) -> list[int] | None:
    if isinstance(node, (ast.List, ast.Tuple)):
        vals = [_int_const(elt) for elt in node.elts]
        if vals and all(v is not None for v in vals):
            return [int(v) for v in vals]  # type: ignore[arg-type]
    return None


def _decode_byte_call(node: ast.Call) -> str | None:
    """Fold ``_decode(ord, …)`` / ``bytes([…]).decode()`` into plaintext."""
    func = node.func
    # bytes([...]).decode() / bytes((...)).decode("utf-8")
    if isinstance(func, ast.Attribute) and func.attr == "decode":
        inner = func.value
        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
            if inner.func.id == "bytes" and inner.args:
                codes = _byte_ints(inner.args[0])
                if codes:
                    try:
                        return bytes(codes).decode("utf-8")
                    except (ValueError, UnicodeDecodeError):
                        return None
        return None
    # _decode(103, 112, …) or decode(103, …)
    if isinstance(func, ast.Name) and func.id in {"_decode", "decode"}:
        codes = [_int_const(arg) for arg in node.args]
        if codes and all(c is not None for c in codes):
            try:
                return bytes(int(c) for c in codes).decode("utf-8")  # type: ignore[arg-type]
            except (ValueError, UnicodeDecodeError):
                return None
    return None


def _joined_const_str(node: ast.JoinedStr) -> str | None:
    """Fold f-strings whose interpolations are string constants (``f\"/{'engines'}\"``)."""
    parts: list[str] = []
    for val in node.values:
        chunk = _const_str(val)
        if chunk is None and isinstance(val, ast.FormattedValue):
            inner = val.value
            chunk = _const_str(inner)
            if chunk is None and isinstance(inner, ast.JoinedStr):
                chunk = _joined_const_str(inner)
        if chunk is None:
            return None
        parts.append(chunk)
    return "".join(parts)


def reconstructed_literals(text: str, *, path: str | None = None) -> list[str]:
    """String values built from literal concat/join in Python or JS/TS source."""
    found: list[str] = []
    seen: set[str] = set()

    def _add(value: str | None) -> None:
        if not value or value in seen:
            return
        seen.add(value)
        found.append(value)

    js = _is_js_path(path)
    py = _is_py_path(path)

    tree = None
    if py and not js:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:

            class _Visitor(ast.NodeVisitor):
                def visit_Call(self, node: ast.Call) -> None:
                    _add(_join_call(node))
                    _add(_decode_byte_call(node))
                    self.generic_visit(node)

                def visit_BinOp(self, node: ast.BinOp) -> None:
                    _add(_folded_add(node))
                    self.generic_visit(node)

                def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
                    _add(_joined_const_str(node))
                    self.generic_visit(node)

            _Visitor().visit(tree)

    # Linear join scanners — never run Python join scan on JS/TS sources.
    if py and not js:
        for value in _scan_py_joins(text):
            _add(value)
    if js or (not path and tree is None):
        # JS path, or pathless non-Python snippet (legacy callers).
        for value in _scan_js_joins(text):
            _add(value)
    elif not path and tree is not None:
        # Pathless Python: AST already covered joins; still scan JS form for
        # mixed snippets is unnecessary — skip to keep pathless Python fast.
        pass

    return found


def obfuscated_forbidden_tokens(
    text: str,
    forbidden: list[str],
    *,
    path: str | None = None,
) -> list[str]:
    """Forbidden tokens reconstructed from concat/join but not present as whole tokens."""
    hits: list[str] = []
    rebuilt = reconstructed_literals(text, path=path)
    for token in forbidden:
        if not token or token_in_text(text, token):
            continue
        if any(token == item or token_in_text(item, token) for item in rebuilt):
            hits.append(token)
    return hits
