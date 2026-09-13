"""AST parameter drop (omit kwargs) via libcst (Python) and heuristics (JS)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import libcst as cst


def _attr_chain(node: cst.BaseExpression) -> str | None:
    parts: list[str] = []
    cur: cst.BaseExpression | None = node
    while isinstance(cur, cst.Attribute):
        parts.append(cur.attr.value)
        cur = cur.value
    if isinstance(cur, cst.Name):
        parts.append(cur.value)
        return ".".join(reversed(parts))
    return None


def _literal_matches(value: cst.BaseExpression, allowed: list[Any]) -> bool:
    """True if ``value`` is a simple literal equal to one of ``allowed``."""
    if not allowed:
        return True
    normalized: list[Any] = []
    for item in allowed:
        if isinstance(item, bool):
            normalized.append(item)
        elif isinstance(item, (int, float)):
            normalized.append(item)
            if isinstance(item, int):
                normalized.append(float(item))
        elif isinstance(item, str):
            normalized.append(item)
        else:
            normalized.append(item)

    if isinstance(value, cst.Integer):
        try:
            n = int(value.value.replace("_", ""), 0)
        except ValueError:
            return False
        return n in normalized or float(n) in normalized
    if isinstance(value, cst.Float):
        try:
            f = float(value.value.replace("_", ""))
        except ValueError:
            return False
        return f in normalized
    if isinstance(value, cst.SimpleString):
        raw = value.value
        if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
            raw = raw[1:-1]
        return raw in normalized
    if isinstance(value, cst.Name):
        if value.value == "True":
            return True in normalized
        if value.value == "False":
            return False in normalized
        if value.value == "None":
            return None in normalized
    return False


class _ParamDropTransformer(cst.CSTTransformer):
    def __init__(
        self,
        function_target: str,
        param: str,
        values: list[Any] | None,
    ) -> None:
        self.function_target = function_target
        self.param = param
        self.values = list(values) if values else []
        self.changes = 0
        self._suffix = function_target.split(".")[-1]

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.Call:
        if not self._call_matches(original_node):
            return updated_node
        new_args: list[cst.BaseArgument] = []
        changed = False
        for arg in updated_node.args:
            if (
                isinstance(arg, cst.Arg)
                and arg.keyword is not None
                and arg.keyword.value == self.param
                and _literal_matches(arg.value, self.values)
            ):
                changed = True
                self.changes += 1
                continue
            new_args.append(arg)
        if changed:
            return updated_node.with_changes(args=new_args)
        return updated_node

    def _call_matches(self, node: cst.Call) -> bool:
        name = _attr_chain(node.func)
        if not name:
            return False
        if name == self.function_target or name.endswith(self.function_target):
            return True
        return name.endswith(self._suffix)


def drop_python_params(
    content: str,
    *,
    function_target: str,
    param: str,
    values: list[Any] | None = None,
) -> tuple[str, int]:
    try:
        module = cst.parse_module(content)
    except Exception:
        return drop_js_params(
            content,
            function_target=function_target,
            param=param,
            values=values,
        )
    transformer = _ParamDropTransformer(function_target, param, values)
    updated = module.visit(transformer)
    return updated.code, transformer.changes


def drop_js_params(
    content: str,
    *,
    function_target: str,
    param: str,
    values: list[Any] | None = None,
) -> tuple[str, int]:
    """Heuristic drop of ``param:`` / ``param=`` near function_target calls."""
    suffix = function_target.split(".")[-1]
    if suffix not in content and function_target not in content:
        return content, 0

    if values:
        # Only drop known literal forms (e.g. temperature: 0 / temperature=0).
        parts: list[str] = []
        for v in values:
            if isinstance(v, bool):
                parts.append("true" if v else "false")
                parts.append("True" if v else "False")
            elif isinstance(v, (int, float)) and float(v) == 0.0:
                parts.extend(["0", "0.0"])
            else:
                parts.append(re.escape(str(v)))
        val_alt = "|".join(dict.fromkeys(parts))
        pattern = re.compile(
            rf"(,?)\s*{re.escape(param)}\s*[:=]\s*(?:{val_alt})\s*(,?)",
        )
    else:
        pattern = re.compile(
            rf"(,?)\s*{re.escape(param)}\s*[:=]\s*[^,\}}\)]+",
        )

    def _sub(m: re.Match[str]) -> str:
        leading, trailing = m.group(1) or "", m.group(2) or ""
        if leading and trailing:
            return ","
        return ""

    new_content, count = pattern.subn(_sub, content)
    # Clean up double commas / trailing commas before )
    if count:
        new_content = re.sub(r",\s*,", ",", new_content)
        new_content = re.sub(r",\s*([\)\}])", r"\1", new_content)
    return new_content, count


def apply_param_drop(
    path: Path,
    content: str,
    *,
    function_target: str,
    param: str,
    values: list[Any] | None = None,
) -> tuple[str, int]:
    if path.suffix.lower() == ".py":
        return drop_python_params(
            content,
            function_target=function_target,
            param=param,
            values=values,
        )
    if path.suffix.lower() in {".js", ".jsx", ".ts", ".tsx"}:
        return drop_js_params(
            content,
            function_target=function_target,
            param=param,
            values=values,
        )
    return content, 0
