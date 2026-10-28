"""A fixed line's reason, read from the two sources and the checks it passed.

When the model gives no sentence (or a type check shaped the fix), the reason is
built from what Conduit already has: the original and rewritten function, the
impact record's change, and any declared-type or dict-key finding an earlier
attempt hit.
It says what changed (annotations, attributes read), what the type check caught
(the release's class against the repo's class, both by module) and which fields
the fix mapped into the expected class. Nothing here knows any library.
"""

from __future__ import annotations

import ast
import re

#: A rendered declared-type finding (see ``declared.render``).
_FINDING = re.compile(
    r"^\S+?:\d+ .*?\(\.\.\.\): (?P<kw>\w+)= passes (?P<have>.+?) \(from (?P<value>.+?)\) "
    r"but (?P<owner>[\w.]+) in (?P<where>.+?) expects (?P<want>.+?), a different class"
)
#: A rendered dict-key finding (see ``dictkeys.render``).
_KEY_FINDING = re.compile(
    r"^\S+?:\d+ .*?\(\.\.\.\): (?P<kw>\w+)= passes a dict with keys (?P<have>.+?) \(from (?P<value>.+?)\) "
    r"but (?P<owner>[\w.]+) in (?P<where>.+?) reads keys: required (?P<required>.+?)(?:; optional (?P<optional>.+?))?"
    r"; (?:missing required (?P<missing>.+?)|none of the passed keys is read); pass a dict"
)
_MAX_FIELDS = 4
_MAX_EXPR = 48


def _short(node: ast.AST) -> str:
    text = ast.unparse(node)
    return text if len(text) <= _MAX_EXPR else text[: _MAX_EXPR - 3] + "..."


def _function(source: str, inside: tuple[int, int], name: str = "") -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Innermost function overlapping ``inside`` (by name first, when given)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.lineno <= inside[1]
        and (node.end_lineno or node.lineno) >= inside[0]
    ]
    named = [node for node in found if node.name == name] if name else []
    pool = named or found
    return max(pool, key=lambda node: node.lineno) if pool else None


def _params(node: ast.AST) -> dict[str, ast.arg]:
    args = node.args
    items = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    items += [arg for arg in (args.vararg, args.kwarg) if arg is not None]
    return {arg.arg: arg for arg in items}


def _chain_root(node: ast.AST) -> str:
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _reads(node: ast.AST, roots: set[str]) -> list[str]:
    """``param.attr`` reads (one level), in order, without repeats."""
    out: list[str] = []
    for item in ast.walk(node):
        if (
            isinstance(item, ast.Attribute)
            and isinstance(item.value, ast.Name)
            and item.value.id in roots
            and not isinstance(item.ctx, (ast.Store, ast.Del))
        ):
            text = ast.unparse(item)
            if text not in out:
                out.append(text)
    return sorted(out, key=lambda text: text)


def _annotation_changes(old: ast.AST, new: ast.AST) -> list[str]:
    out: list[str] = []
    before, after = _params(old), _params(new)
    for name, arg in after.items():
        was = before.get(name)
        if was is None or arg.annotation is None or was.annotation is None:
            continue
        if ast.dump(arg.annotation) != ast.dump(was.annotation):
            out.append(f"`{name}` is now typed `{ast.unparse(arg.annotation)}` instead of `{ast.unparse(was.annotation)}`")
    if old.returns is not None and new.returns is not None and ast.dump(old.returns) != ast.dump(new.returns):
        out.append(f"it now returns `{ast.unparse(new.returns)}` instead of `{ast.unparse(old.returns)}`")
    return out


def _callee(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _mappings(old: ast.AST, new: ast.AST, roots: set[str], wanted: set[str]) -> list[str]:
    """New class-like calls whose keywords read the parameters: ``Name(field=value)``."""
    had = {ast.dump(item) for item in ast.walk(old) if isinstance(item, ast.Call)}
    out: list[str] = []
    for item in ast.walk(new):
        if not isinstance(item, ast.Call) or ast.dump(item) in had:
            continue
        name = _callee(item)
        if not name or not name[0].isupper() or (wanted and name not in wanted):
            continue
        pairs = [
            (kw.arg, kw.value)
            for kw in item.keywords
            if kw.arg and any(_chain_root(sub) in roots for sub in ast.walk(kw.value) if isinstance(sub, ast.Attribute))
        ]
        if not pairs:
            continue
        shown = ", ".join(f"{field} <- `{_short(value)}`" for field, value in pairs[:_MAX_FIELDS])
        if len(pairs) > _MAX_FIELDS:
            shown += f" and {len(pairs) - _MAX_FIELDS} more"
        text = f"builds `{ast.unparse(item.func)}` from the value's fields ({shown})"
        if text not in out:
            out.append(text)
    return out


def caught(findings: list[str]) -> list[dict[str, str]]:
    """Declared-type findings, parsed, in order, without repeats."""
    out: list[dict[str, str]] = []
    for text in findings:
        match = _FINDING.match((text or "").strip())
        if match and match.groupdict() not in out:
            out.append(match.groupdict())
    return out


def caught_keys(findings: list[str]) -> list[dict[str, str]]:
    """Dict-key findings, parsed, in order, without repeats."""
    out: list[dict[str, str]] = []
    for text in findings:
        match = _KEY_FINDING.match((text or "").strip())
        if match and match.groupdict() not in out:
            out.append(match.groupdict())
    return out


def change_why(
    original: str,
    patched: str,
    line: int,
    span: tuple[int, int],
    expr: str = "",
    change: str = "",
    findings: list[str] | None = None,
) -> str:
    """One sentence about a fixed function; ``""`` when nothing can be read."""
    old = _function(original, (line, line))
    new = _function(patched, span, old.name if old is not None else "")
    if old is None or new is None:
        return ""
    roots = set(_params(old)) | set(_params(new))
    roots.discard("self")
    roots.discard("cls")
    hits = caught(findings or [])
    parts = _annotation_changes(old, new)
    for hit in hits:
        parts.append(
            f"the type check caught `{hit['value']}` (`{hit['have']}`) passed as `{hit['kw']}=` "
            f"where `{hit['owner']}` in {hit['where']} expects `{hit['want']}`"
        )
    for hit in caught_keys(findings or []):
        lacks = f"lacked required {hit['missing']}" if hit["missing"] else "had none of the keys it reads"
        parts.append(
            f"the key check caught `{hit['value']}` passed as `{hit['kw']}=` to `{hit['owner']}`, "
            f"which reads {hit['required']}"
            + (f" (optional {hit['optional']})" if hit["optional"] else "")
            + f": the dict {lacks}"
        )
    wanted = {hit["want"].split(" or ")[0].rsplit(".", 1)[-1] for hit in hits}
    mapped = _mappings(old, new, roots, wanted) or (_mappings(old, new, roots, set()) if not hits else [])
    parts += mapped
    before, after = _reads(old, roots), _reads(new, roots)
    gone = [text for text in before if text not in after]
    added = [text for text in after if text not in before]
    if gone:
        parts.append("stops reading " + ", ".join(f"`{text}`" for text in gone[:_MAX_FIELDS]))
    if added:
        parts.append("now reads " + ", ".join(f"`{text}`" for text in added[:_MAX_FIELDS]))
    if not parts:
        return ""
    head = f"`{expr}`" if expr else f"`{new.name}`"
    because = f" ({change})" if change else ""
    return f"fixed {head}{because} in `{new.name}`: " + "; ".join(parts) + "."
