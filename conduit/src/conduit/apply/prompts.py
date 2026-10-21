"""Prompt text for propose / summarize steps.

No hard-coded fix recipes — the model sees only this site's evidence.
"""

from __future__ import annotations

import json

from conduit.apply.models import ContextPack


def propose_messages(pack: ContextPack, *, feedback: str = "") -> list[tuple[str, str]]:
    system = (
        "You fix one broken call site in a Python function.\n"
        "Rules:\n"
        "- Rewrite ONLY the flagged function body (same function name).\n"
        "- You may list new import lines if required; do not edit other functions.\n"
        "- Use only the release-diff evidence, new signature/docstring, and candidates "
        "provided (candidates are newer-wheel public symbols that share name tokens).\n"
        "- Respond with a JSON object only:\n"
        '  {"function_source": "<full def ...>", "new_imports": ["import ...", ...]}\n'
        "- function_source must be the complete function definition (def/async def),\n"
        "  not a unified diff and not the whole file.\n"
        "- If function_name is empty, the flagged line is a module-level statement:\n"
        "  function_source is that statement; return only its rewritten statement(s).\n"
        "- If feedback says a value has the wrong type or lacks an attribute in the new\n"
        "  release, build or read a value of the expected type inside this function from\n"
        "  its own inputs (helpers outside this function keep their current types).\n"
    )
    user = {
        "file": pack.file_path,
        "flagged_line": pack.site.line,
        "flagged_expr": pack.site.expr,
        "function_name": pack.function_name,
        "function_start_line": pack.function_start_line,
        "function_end_line": pack.function_end_line,
        "imports": pack.imports,
        "function_source": pack.function_source,
        "release_diff": pack.release_diff,
        "new_signature": pack.new_signature,
        "new_docstring": pack.new_docstring,
        "candidates": pack.candidates,
        "call_kwargs": pack.release_diff.get("call_kwargs") or [],
    }
    helpers = same_file_helpers(pack)
    if helpers:
        # Read-only: shows how this function's inputs are shaped.
        user["helpers_called_read_only"] = helpers
    body = (
        "Context pack (JSON):\n"
        f"{json.dumps(user, indent=2)}\n\n"
        "Return JSON with function_source and new_imports that fixes the flagged call site."
    )
    if feedback:
        body += f"\n\nPrevious attempt failed:\n{feedback}\nRetry with a corrected rewrite."
    return [("system", system), ("user", body)]


def same_file_helpers(pack: ContextPack, *, limit: int = 3, max_lines: int = 40) -> list[str]:
    """Sources of functions in the same file that the flagged function calls."""
    import ast

    try:
        tree = ast.parse(pack.full_source)
    except (SyntaxError, ValueError):
        return []
    called: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and pack.function_start_line <= node.lineno <= pack.function_end_line
        ):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name and name != pack.function_name and name not in called:
                called.append(name)
    lines = pack.full_source.splitlines()
    out: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in called
            and not (pack.function_start_line <= node.lineno <= pack.function_end_line)
            and (node.end_lineno or node.lineno) - node.lineno < max_lines
        ):
            start = min([node.lineno, *[deco.lineno for deco in node.decorator_list]])
            out.append("\n".join(lines[start - 1 : node.end_lineno]))
            if len(out) >= limit:
                break
    return out


def summarize_messages(pack: ContextPack, diff: str) -> list[tuple[str, str]]:
    system = "Reply with one short sentence explaining why this change fixes the call site."
    user = (
        f"Change: {pack.site.change}\n"
        f"Expr: {pack.site.expr}\n"
        f"Diff:\n{diff}\n"
    )
    return [("system", system), ("user", user)]
