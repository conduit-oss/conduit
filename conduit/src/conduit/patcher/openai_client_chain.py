"""Gold OpenAI 1.x chain: OpenAI(api_key=) + client.* + object response + no <1.0 guard."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Iterable

import libcst as cst

from conduit.patcher.engine import ChangeRecord, PatchReport

_MODERN_CALLS = (
    "openai.audio.transcriptions.create",
    "openai.audio.translations.create",
    "openai.chat.completions.create",
    "openai.embeddings.create",
)
_CLIENT_CALLS = tuple("client." + name[len("openai.") :] for name in _MODERN_CALLS)
_TEXT_GET_RE = re.compile(r"""(\w+)\.get\(\s*["']text["']\s*\)""")
_TEXT_SUB_RE = re.compile(r"""(\w+)\s*\[\s*["']text["']\s*\]""")
_STALE_COMMENT_RES = (
    (re.compile(r"legacy OpenAI 0\.28\s+", re.I), "OpenAI "),
    (re.compile(r"legacy 0\.28\s+", re.I), ""),
    (re.compile(r"the pre-1\.0 interface\.?\s*", re.I), ""),
    (re.compile(r"Whisper needs openai\s*<\s*1(?:\.0)?[;.]?\s*", re.I), ""),
    (re.compile(r"needs openai\s*<\s*1(?:\.0)?[;.]?\s*", re.I), ""),
    (re.compile(r"using the legacy OpenAI 0\.28 chat API", re.I), "using the OpenAI chat API"),
)


def apply_openai_client_chain(
    root: Path,
    files: Iterable[Path],
    *,
    to_version: str = "",
) -> PatchReport:
    """Upgrade rewritten module-level calls to client form + response + guard strip."""
    report = PatchReport()
    root = root.resolve()
    major = _major(to_version)
    for path in files:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.is_file() or resolved.suffix.lower() != ".py":
            continue
        try:
            rel = str(resolved.relative_to(root)).replace("\\", "/")
            text = resolved.read_text(encoding="utf-8-sig")
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        updated, details = transform_openai_client_chain(text, to_major=major)
        if updated == text:
            continue
        resolved.write_text(updated, encoding="utf-8")
        for detail in details:
            report.add(
                ChangeRecord(
                    event_id="client_chain",
                    path=rel,
                    rule_type="CLIENT_CHAIN",
                    detail=detail,
                )
            )
    return report


def transform_openai_client_chain(
    source: str,
    *,
    to_major: int | None = None,
) -> tuple[str, list[str]]:
    details: list[str] = []
    text = source
    rewritten, n_client = _upgrade_to_client(text)
    if n_client:
        text = rewritten
        details.append(f"rewrote {n_client} call(s) onto OpenAI client")
    # Polish only after a live 1.x call exists. Do not half-edit leftover 0.28 sites.
    if not _has_live_modern_call(text):
        return text, details
    if to_major is None or to_major >= 1:
        stripped, n_guard = _strip_version_guards(text)
        if n_guard:
            text = stripped
            details.append(f"removed {n_guard} version-guard skip(s)")
    text2, n_resp = _rewrite_response_access(text)
    if n_resp:
        text = text2
        details.append(f"rewrote {n_resp} response access(es)")
    text3, n_stale = _rewrite_stale_comments(text)
    if n_stale:
        text = text3
        details.append(f"updated {n_stale} stale comment(s)")
    return text, details


def _has_live_modern_call(source: str) -> bool:
    return any(name in source for name in _MODERN_CALLS + _CLIENT_CALLS)


def _upgrade_to_client(source: str) -> tuple[str, int]:
    hits = [name for name in _MODERN_CALLS if name in source]
    if not hits:
        return source, 0
    key_expr = _api_key_expr(source)
    text = source
    if "from openai import OpenAI" not in text and "import OpenAI" not in text:
        text = _insert_openai_import(text)
    if "OpenAI(" not in text or not re.search(r"\bclient\s*=\s*OpenAI\(", text):
        text = _insert_client(text, key_expr)
    n = 0
    for old in hits:
        new = "client." + old[len("openai.") :]
        count = text.count(old)
        if count:
            text = text.replace(old, new)
            n += count
    # Drop leftover module-level api_key assigns that the client now owns.
    text = re.sub(
        r"^openai\.api_key\s*=\s*[^\n]+\n",
        "",
        text,
        flags=re.M,
    )
    text = re.sub(r"^[ \t]*openai\.api_key\s*=\s*[^\n]+\n", "", text, flags=re.M)
    return text, n


def _insert_openai_import(source: str) -> str:
    if re.search(r"^from openai import OpenAI\b", source, re.M):
        return source
    if re.search(r"^import openai\b", source, re.M):
        return re.sub(
            r"^import openai\b",
            "import openai\nfrom openai import OpenAI",
            source,
            count=1,
            flags=re.M,
        )
    return "from openai import OpenAI\n" + source


def _insert_client(source: str, key_expr: str) -> str:
    assign = f"client = OpenAI(api_key={key_expr})"
    if assign in source or re.search(r"\bclient\s*=\s*OpenAI\(", source):
        return source
    match = re.search(r"^openai\.api_key\s*=\s*.+$", source, re.M)
    if match:
        return source[: match.start()] + assign + source[match.end() :]
    # Function-local key assign
    fn_match = re.search(r"^([ \t]+)openai\.api_key\s*=\s*.+$", source, re.M)
    if fn_match:
        indent = fn_match.group(1)
        return source[: fn_match.start()] + f"{indent}{assign}" + source[fn_match.end() :]
    # After imports
    lines = source.splitlines(keepends=True)
    insert_at = 0
    seen_import = False
    for i, line in enumerate(lines):
        if line.startswith(("import ", "from ")):
            seen_import = True
            insert_at = i + 1
            continue
        if seen_import and line.strip() == "":
            insert_at = i + 1
            continue
        if seen_import:
            break
    lines.insert(insert_at, assign + "\n")
    return "".join(lines)


def _api_key_expr(source: str) -> str:
    match = re.search(r"openai\.api_key\s*=\s*(.+)$", source, re.M)
    if match:
        return match.group(1).strip()
    if "get_openai_api_key" in source:
        return "get_openai_api_key()"
    if "os.getenv" in source:
        return 'os.getenv("OPENAI_API_KEY")'
    return 'os.getenv("OPENAI_API_KEY")'


def _rewrite_response_access(source: str) -> tuple[str, int]:
    n = 0
    text = source
    for pattern, repl in (
        (_TEXT_GET_RE, r"\1.text"),
        (_TEXT_SUB_RE, r"\1.text"),
    ):
        text, count = pattern.subn(repl, text)
        n += count
    for match, replace in (
        ("['choices'][0]['message']['content']", ".choices[0].message.content"),
        ('["choices"][0]["message"]["content"]', ".choices[0].message.content"),
    ):
        if match in text:
            n += text.count(match)
            text = text.replace(match, replace)
    return text, n


def _rewrite_stale_comments(source: str) -> tuple[str, int]:
    n = 0
    text = source
    for pattern, repl in _STALE_COMMENT_RES:
        text, count = pattern.subn(repl, text)
        n += count
    return text, n


def _strip_version_guards(source: str) -> tuple[str, int]:
    helpers = _version_helper_names(source)
    if not helpers and "__version__" not in source:
        return source, 0
    try:
        module = cst.parse_module(source)
    except Exception:
        return source, 0
    transformer = _GuardStripTransformer(helpers)
    updated = module.visit(transformer)
    if not transformer.changes:
        return source, 0
    return updated.code, transformer.changes


def _version_helper_names(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        dumped = ast.dump(node)
        if "__version__" in dumped:
            names.add(node.name)
    return names


class _GuardStripTransformer(cst.CSTTransformer):
    def __init__(self, helpers: set[str]) -> None:
        self.helpers = helpers
        self.changes = 0

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.BaseStatement | cst.RemovalSentinel:
        if original_node.name.value in self.helpers:
            self.changes += 1
            return cst.RemoveFromParent()
        return updated_node

    def leave_If(
        self, original_node: cst.If, updated_node: cst.If
    ) -> cst.BaseStatement | cst.FlattenSentinel[cst.BaseStatement] | cst.RemovalSentinel:
        test_code = ""
        try:
            test_code = cst.Module([]).code_for_node(original_node.test)
        except Exception:
            test_code = ""
        hits_helper = any(name in test_code for name in self.helpers)
        hits_version = "__version__" in test_code
        if not (hits_helper or hits_version):
            return updated_node
        if not _body_returns(original_node.body):
            return updated_node
        self.changes += 1
        return cst.RemoveFromParent()


def _body_returns(body: cst.BaseSuite) -> bool:
    if not isinstance(body, cst.IndentedBlock):
        return False
    for stmt in body.body:
        if isinstance(stmt, cst.SimpleStatementLine) and any(
            isinstance(small, cst.Return) for small in stmt.body
        ):
            return True
    return False


def _major(version: str) -> int | None:
    head = (version or "").strip().lstrip("vV").split(".", 1)[0]
    if head.isdigit():
        return int(head)
    return None
