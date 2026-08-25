"""Apply post-rules to consumer repo files."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any, Iterable

from conduit.patcher.engine import ChangeRecord, PatchReport, _glob_ok, _iter_candidate_files
from conduit.patcher.post_rules.store import (
    load_learned_post_rules,
    merge_post_rules,
    packet_post_rules,
)
from conduit.patcher.post_rules.validator import validate_post_rules
from conduit.patcher.post_rules.vendor import load_vendor_post_rules
from conduit.patcher.string_replace import write_if_changed
from conduit.test_gen import is_conduit_generated_rel, oracle_forbidden_tokens, token_in_text


def _rel(root: Path, path: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def _replace_function(text: str, name: str, new_body: str) -> tuple[str, bool]:
    pattern = re.compile(
        rf"^def {re.escape(name)}\([^)]*\):.*?^(?=def |\Z)",
        re.M | re.S,
    )
    if not pattern.search(text):
        return text, False
    return pattern.sub(new_body.rstrip() + "\n\n", text, count=1), True


def _list_wrapper_body(fn_name: str, list_call: str = "openai.files.list()") -> str:
    return f'''def {fn_name}():
    """Return a plain list from the SDK list API."""
    if not get_api_key():
        configure()

    page = {list_call}
    data = getattr(page, "data", None)
    if data is not None:
        return list(data)
    if isinstance(page, dict):
        return list(page.get("data") or [])
    return []'''


def _moderation_body(fn_name: str, create_call: str = "openai.moderations.create(**kwargs)") -> str:
    return f'''def {fn_name}(text, model=None):
    """Score text via Moderations API; return a dict with ``flagged``."""
    if not get_api_key():
        configure()

    kwargs = {{"input": text}}
    if model is not None:
        kwargs["model"] = model
    response = {create_call}
    results = getattr(response, "results", None)
    if results is None and isinstance(response, dict):
        results = response.get("results") or []
    if not results:
        return {{"flagged": False}}
    first = results[0]
    if hasattr(first, "model_dump"):
        return first.model_dump()
    if isinstance(first, dict):
        return first
    return {{"flagged": bool(getattr(first, "flagged", False))}}'''


def _delegate_body(
    *,
    name: str,
    delegate_module: str,
    delegate_symbol: str,
    model_param: str = "engine",
) -> str:
    return f'''def {name}(prompt, {model_param}=None, temperature=0.7, max_tokens=16, stop=None):
    """Delegate to shared completion helper."""
    return {delegate_symbol}(
        prompt,
        model={model_param},
        temperature=temperature,
        max_tokens=max_tokens,
        stop=stop,
    )'''


def _forbidden_token_set(packet: dict[str, Any]) -> set[str]:
    return {t for t in oracle_forbidden_tokens(packet) if t}


def _safe_model_id(packet: dict[str, Any], preferred: str | None = None) -> str:
    """Pick a model id that is not an oracle-forbidden leftover token."""
    forbidden = _forbidden_token_set(packet)
    if preferred and preferred not in forbidden:
        return preferred
    live = packet.get("live_test") if isinstance(packet.get("live_test"), dict) else {}
    default = str(live.get("default_completion_model") or "").strip()
    if default and default not in forbidden:
        return default
    aliases = packet.get("runtime_model_aliases") or {}
    if isinstance(aliases, dict):
        for key in aliases:
            key = str(key).strip()
            if key and key not in forbidden:
                return key
    for rule in packet.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("type") or "") != "EXACT_STRING_REPLACE":
            continue
        replace = str(rule.get("replace") or "").strip()
        if replace and replace not in forbidden:
            return replace
    return "gpt-5.6-terra"


def _alias_prefixes(packet: dict[str, Any]) -> tuple[str, ...]:
    live = packet.get("live_test") if isinstance(packet.get("live_test"), dict) else {}
    prefixes = live.get("alias_prefixes") or []
    if prefixes:
        return tuple(str(p) for p in prefixes if p)
    aliases = packet.get("runtime_model_aliases") or {}
    if isinstance(aliases, dict):
        return tuple(
            sorted({str(k).rsplit("-", 1)[0] + "-" for k in aliases if "-" in str(k)})
        )
    return ()


def _completions_module_from_packet(packet: dict[str, Any]) -> str:
    default = _safe_model_id(packet)
    prefixes = _alias_prefixes(packet)
    prefix_lines = ""
    if prefixes:
        joined = ", ".join(repr(p) for p in prefixes)
        prefix_lines = f"_ALIAS_PREFIXES = ({joined},)\n\n"
        resolve_extra = (
            "    for prefix in _ALIAS_PREFIXES:\n"
            "        if raw.startswith(prefix):\n"
            "            return DEFAULT_COMPLETION_MODEL\n"
        )
    else:
        resolve_extra = ""

    module = f'''"""Prompt completions using the OpenAI SDK."""

from __future__ import annotations

from openai import OpenAI

from openai_text.client import configure, get_api_key

DEFAULT_COMPLETION_MODEL = "{default}"
{prefix_lines}
def _client():
    return OpenAI(api_key=get_api_key())


def _resolve_model(model: str | None) -> str:
    raw = str(model or "").strip()
    if not raw:
        return DEFAULT_COMPLETION_MODEL
{resolve_extra}    if raw.endswith("-instruct"):
        return raw
    return raw


def _choice_text(response) -> str:
    choice = response.choices[0]
    if hasattr(choice, "text") and choice.text is not None:
        return str(choice.text)
    message = choice.message if hasattr(choice, "message") else choice["message"]
    content = message.content if hasattr(message, "content") else message["content"]
    return str(content or "")


def complete_prompt(
    prompt,
    model=None,
    temperature=0.7,
    max_tokens=64,
    stop=None,
):
    """Complete a freeform prompt via Completions or Chat Completions."""
    if not get_api_key():
        configure()

    resolved = _resolve_model(model)
    client = _client()

    if resolved.endswith("-instruct"):
        kwargs = {{
            "model": resolved,
            "prompt": prompt,
            "max_tokens": max_tokens,
        }}
        if stop is not None:
            kwargs["stop"] = stop
        if temperature not in (None, 0):
            kwargs["temperature"] = temperature
        response = client.completions.create(**kwargs)
        return _choice_text(response)

    kwargs = {{
        "model": resolved,
        "messages": [{{"role": "user", "content": prompt}}],
        "max_completion_tokens": max_tokens,
    }}
    response = client.chat.completions.create(**kwargs)
    return _choice_text(response)
'''
    for token in _forbidden_token_set(packet):
        if token_in_text(module, token):
            raise ValueError(
                f"generated completions module contains forbidden token {token!r}"
            )
    return module


def _engines_module_from_rule(rule: dict[str, Any], packet: dict[str, Any]) -> str:
    delegate_module = str(rule.get("delegate_module") or "openai_text.completions")
    delegate_symbol = str(rule.get("delegate_symbol") or "complete_prompt")
    preferred = str(rule.get("default_model") or "").strip() or None
    default_engine = _safe_model_id(packet, preferred=preferred)

    return f'''"""Model inventory and legacy engine-style completions."""

from __future__ import annotations

from openai import OpenAI

from openai_text.client import configure, get_api_key
from {delegate_module} import {delegate_symbol}

DEFAULT_ENGINE_MODEL = "{default_engine}"


def _client():
    return OpenAI(api_key=get_api_key())


def _dump(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return obj


def list_engines():
    """Return the available model inventory from the OpenAI SDK."""
    if not get_api_key():
        configure()

    page = _client().models.list()
    return [_dump(item) for item in page.data]


def complete_with_engine(
    prompt,
    engine=DEFAULT_ENGINE_MODEL,
    temperature=0.7,
    max_tokens=16,
    stop=None,
):
    """Complete text using the provided engine/model identifier."""
    return {delegate_symbol}(
        prompt,
        model=engine,
        temperature=temperature,
        max_tokens=max_tokens,
        stop=stop,
    )
'''


def _apply_one_rule(
    root: Path,
    path: Path,
    rule: dict[str, Any],
    original: str,
    *,
    packet: dict[str, Any],
) -> tuple[str, str | None]:
    rtype = str(rule.get("type") or "")
    rel = _rel(root, path)
    name = str(rule.get("function_name") or "")

    if rtype == "FUNCTION_BODY_REPLACE":
        body = rule.get("body")
        if not isinstance(body, str):
            return original, None
        if rule.get("replace_module"):
            return body, f"replaced module {rel}"
        if name:
            updated, ok = _replace_function(original, name, body)
            if ok:
                return updated, f"replaced function {name} in {rel}"
        return original, None

    if rtype == "WRAPPER_ENSURE_LIST":
        hint = str(rule.get("list_callee_hint") or "openai.files.list()")
        fn = name or "list_files"
        body = _list_wrapper_body(fn, hint)
        updated, ok = _replace_function(original, fn, body)
        if ok:
            return updated, f"WRAPPER_ENSURE_LIST {fn} in {rel}"
        return original, None

    if rtype == "WRAPPER_ENSURE_DICT_KEY":
        hint = str(rule.get("create_callee_hint") or "openai.moderations.create(**kwargs)")
        fn = name or "moderate_text"
        body = _moderation_body(fn, hint)
        updated, ok = _replace_function(original, fn, body)
        if ok:
            return updated, f"WRAPPER_ENSURE_DICT_KEY {fn} in {rel}"
        return original, None

    if rtype == "WRAPPER_DELEGATE":
        fn = name or "complete_with_engine"
        if path.name == "engines.py" and fn == "complete_with_engine":
            return _engines_module_from_rule(rule, packet), f"WRAPPER_DELEGATE engines module {rel}"
        body = _delegate_body(
            name=fn,
            delegate_module=str(rule.get("delegate_module") or "openai_text.completions"),
            delegate_symbol=str(rule.get("delegate_symbol") or "complete_prompt"),
            model_param=str(rule.get("model_param") or "engine"),
        )
        updated, ok = _replace_function(original, fn, body)
        if ok:
            return updated, f"WRAPPER_DELEGATE {fn} in {rel}"
        return original, None

    if rtype == "RUNTIME_MODEL_ALIAS":
        if path.name == "completions.py" or rule.get("replace_module"):
            return _completions_module_from_packet(packet), f"RUNTIME_MODEL_ALIAS module {rel}"
        return original, None

    return original, None


def collect_post_rules(
    packet: dict[str, Any],
    root: Path,
    *,
    learned: bool = True,
    vendor: bool = True,
    extra_rules: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    lists: list[list[dict[str, Any]]] = [packet_post_rules(packet)]
    if extra_rules:
        lists.append(list(extra_rules))
    if vendor:
        lists.append(load_vendor_post_rules(packet))
    if learned:
        lists.append(load_learned_post_rules(root))
    return merge_post_rules(*lists)


def apply_post_rules(
    root: Path,
    packet: dict[str, Any],
    *,
    learned: bool = True,
    vendor: bool = True,
    file_allowlist: Iterable[Path] | None = None,
    dry_run: bool = False,
    extra_rules: list[dict[str, Any]] | None = None,
) -> PatchReport:
    """Apply merged post-rules; returns paths changed."""
    root = root.resolve()
    rules = collect_post_rules(
        packet, root, learned=learned, vendor=vendor, extra_rules=extra_rules
    )
    errors = validate_post_rules(rules, packet=packet)
    report = PatchReport()
    for err in errors:
        if err not in report.skips:
            report.skips.append(err)

    if not rules:
        return report

    files = _iter_candidate_files(root, file_allowlist)
    packet_id = str(packet.get("packet_id") or "packet")

    for rule in rules:
        target_files = rule.get("target_files") or ["*"]
        for path in files:
            rel = _rel(root, path)
            if is_conduit_generated_rel(rel):
                continue
            if not _glob_ok(path, target_files, root):
                continue
            try:
                original = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            updated, detail = _apply_one_rule(
                root, path, rule, original, packet=packet
            )
            if detail is None or updated == original:
                continue
            if write_if_changed(path, original, updated, dry_run=dry_run):
                report.add(
                    ChangeRecord(
                        event_id=packet_id,
                        path=rel,
                        rule_type=str(rule.get("type") or "POST"),
                        detail=detail,
                    )
                )

    report.stage_counts["post"] = len(rules)
    return report


def apply_post_rules_to_paths(
    root: Path,
    packet: dict[str, Any],
    paths: list[str],
) -> list[str]:
    """Re-apply post rules on specific paths; return rel paths updated."""
    allow = [root / p.replace("/", "\\") for p in paths]
    report = apply_post_rules(
        root,
        packet,
        file_allowlist=allow,
    )
    return list(report.files_modified)
