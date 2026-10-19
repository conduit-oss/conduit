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
        "- Edit ONLY the flagged function (and module imports if required).\n"
        "- Do not change any other function or class.\n"
        "- Use only the release-diff evidence and the new signature/docstring provided.\n"
        "- Return a single unified diff for that file. No commentary outside the diff.\n"
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
    }
    body = (
        "Context pack (JSON):\n"
        f"{json.dumps(user, indent=2)}\n\n"
        "Write a minimal unified diff that fixes the flagged call site."
    )
    if feedback:
        body += f"\n\nPrevious attempt failed:\n{feedback}\nRetry with a corrected diff."
    return [("system", system), ("user", body)]


def summarize_messages(pack: ContextPack, diff: str) -> list[tuple[str, str]]:
    system = "Reply with one short sentence explaining why this diff fixes the call site."
    user = (
        f"Change: {pack.site.change}\n"
        f"Expr: {pack.site.expr}\n"
        f"Diff:\n{diff}\n"
    )
    return [("system", system), ("user", user)]
