"""Whole-token scan plus literal concat/join reconstruction (anti-obfuscation)."""

from __future__ import annotations

import ast
import re

_TOKEN_CHAR = r"A-Za-z0-9_." + r"-"

_JS_JOIN = re.compile(
    r"\[\s*(?P<parts>(?:(['\"])(?:\\.|(?!\2).)*\2\s*,\s*)*(['\"])(?:\\.|(?!\3).)*\3)"
    r"\s*\]\s*\.\s*join\s*\(\s*(?P<sep>(['\"])(?:\\.|(?!\5).)*\5)\s*\)",
    re.DOTALL,
)
_PY_JOIN = re.compile(
    r"(?P<sep>(['\"])(?:\\.|(?!\2).)*\2)\s*\.\s*join\s*\(\s*\[\s*"
    r"(?P<parts>(?:(['\"])(?:\\.|(?!\4).)*\4\s*,\s*)*(['\"])(?:\\.|(?!\5).)*\5)"
    r"\s*\]\s*\)",
    re.DOTALL,
)
_STR_LIT = re.compile(r"""(['"])(?:\\.|(?!\1).)*\1""")


def token_in_text(text: str, token: str) -> bool:
    """True if ``token`` appears as a whole token (same bounds as exact_replace)."""
    if not token or token not in text:
        return False
    pattern = re.compile(rf"(?<![{_TOKEN_CHAR}]){re.escape(token)}(?![{_TOKEN_CHAR}])")
    return pattern.search(text) is not None


def _unquote(lit: str) -> str:
    try:
        return ast.literal_eval(lit)
    except (ValueError, SyntaxError):
        if len(lit) >= 2 and lit[0] == lit[-1] and lit[0] in {"'", '"'}:
            return lit[1:-1]
        return lit


def _join_from_match(parts_blob: str, sep_lit: str) -> str | None:
    parts = [_unquote(m.group(0)) for m in _STR_LIT.finditer(parts_blob)]
    if not parts:
        return None
    return _unquote(sep_lit).join(parts)


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


def reconstructed_literals(text: str) -> list[str]:
    """String values built from literal concat/join in Python or JS/TS source."""
    found: list[str] = []
    seen: set[str] = set()

    def _add(value: str | None) -> None:
        if not value or value in seen:
            return
        seen.add(value)
        found.append(value)

    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    if tree is not None:

        class _Visitor(ast.NodeVisitor):
            def visit_Call(self, node: ast.Call) -> None:
                _add(_join_call(node))
                self.generic_visit(node)

            def visit_BinOp(self, node: ast.BinOp) -> None:
                _add(_folded_add(node))
                self.generic_visit(node)

            def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
                parts: list[str] = []
                ok = True
                for val in node.values:
                    if isinstance(val, ast.Constant) and isinstance(val.value, str):
                        parts.append(val.value)
                    else:
                        ok = False
                        break
                if ok:
                    _add("".join(parts))
                self.generic_visit(node)

        _Visitor().visit(tree)

    for match in _PY_JOIN.finditer(text):
        _add(_join_from_match(match.group("parts"), match.group("sep")))
    for match in _JS_JOIN.finditer(text):
        _add(_join_from_match(match.group("parts"), match.group("sep")))
    return found


def obfuscated_forbidden_tokens(text: str, forbidden: list[str]) -> list[str]:
    """Forbidden tokens reconstructed from concat/join but not present as whole tokens."""
    hits: list[str] = []
    rebuilt = reconstructed_literals(text)
    for token in forbidden:
        if not token or token_in_text(text, token):
            continue
        if any(token == item or token_in_text(item, token) for item in rebuilt):
            hits.append(token)
    return hits
