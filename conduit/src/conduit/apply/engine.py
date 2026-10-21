"""Propose → splice → verify loop for each impact site."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from conduit.apply.client import ChatMessage, ModelClient
from conduit.apply.config import TokenFactorySettings
from conduit.apply.context import ContextError, build_context_pack, enclosing_function
from conduit.apply.feedback import trim_test_feedback
from conduit.apply.models import ContextPack, ImpactSite, SiteResult, SiteStatus
from conduit.apply.prompts import propose_messages, summarize_messages
from conduit.apply.splice import PatchError, parse_model_edit, splice_edit_with_span, unified_diff_for
from conduit.apply.signatures import new_release_wheel, signatures_for
from conduit.apply.verify import remaining_breaks

# Optional hook: given root + site + patched file text, return True if the site
# is no longer flagged. Unit tests inject this; the CLI uses impact re-check.
SiteClearedCheck = Callable[[Path, ImpactSite, str], bool]


@dataclass
class ApplyConfig:
    root: Path
    settings: TokenFactorySettings
    dry_run: bool = False
    write: bool = False
    test_cmd: str | None = None
    client: ModelClient | None = None
    site_cleared: SiteClearedCheck | None = None
    #: When True and ``site_cleared`` is unset, re-run ``collect_impacts`` on a
    #: scratch copy of the tree with the patch applied.
    recheck_impact: bool = False
    fixtures: Path | None = None
    allow_network: bool = False
    max_sites: int | None = None


@dataclass
class ApplyReport:
    results: list[SiteResult] = field(default_factory=list)
    tokens_used: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "results": [item.to_dict() for item in self.results],
            "tokens_used": self.tokens_used,
            "summary": {
                status.value: sum(1 for r in self.results if r.status == status)
                for status in SiteStatus
            },
        }


def apply_sites(sites: list[ImpactSite], config: ApplyConfig) -> ApplyReport:
    report = ApplyReport()
    client = config.client
    owns_client = False
    if client is None and not config.dry_run:
        from conduit.apply.client import TokenFactoryClient

        client = TokenFactoryClient(
            base_url=config.settings.base_url,
            api_key=config.settings.api_key,
        )
        owns_client = True
    try:
        selected = sites if config.max_sites is None else sites[: config.max_sites]
        if config.test_cmd and not config.dry_run and client is not None:
            _apply_batch(selected, config, client, report)
            return report
        covered, texts = _one_per_function(config.root, selected)
        # Lines that import or construct a removed class go first, so later
        # rechecks resolve calls through the new client instead of the old one.
        for site in sorted(selected, key=lambda item: not _names_removed_class(item)):
            if id(site) in covered:
                report.results.append(SiteResult(site=site, status=SiteStatus.SKIPPED, why=covered[id(site)]))
                continue
            moved = _relocate(config.root, site, texts.get(id(site), "")) if config.write else site
            if moved is None:
                why = "line rewritten by an earlier fix in this file"
                report.results.append(SiteResult(site=site, status=SiteStatus.SKIPPED, why=why))
                continue
            report.results.append(_apply_one(moved, config, client, report))
    finally:
        if owns_client and client is not None and hasattr(client, "close"):
            client.close()  # type: ignore[union-attr]
    return report


def _apply_batch(selected: list[ImpactSite], config: ApplyConfig, client: ModelClient, report: ApplyReport) -> None:
    """With a test command: rewrite every site (each must pass the static recheck),
    run the tests once, retry only the sites the failures point at, and revert
    the ones that cannot be cleared. Tests see the whole migration at once, so
    an import every module needs no longer fails each site on its own."""
    with tempfile.TemporaryDirectory(prefix="conduit-apply-batch-") as tmp:
        scratch = Path(tmp) / "tree"
        shutil.copytree(config.root, scratch, ignore=shutil.ignore_patterns(*_SCRATCH_IGNORE))
        work = replace(config, root=scratch, write=True, test_cmd=None)
        originals: dict[str, str] = {}
        accepted: dict[int, dict[str, Any]] = {}  # id(site) -> sink, in application order
        slots: dict[int, int] = {}
        texts_of: dict[int, str] = {}
        covered, texts = _one_per_function(config.root, selected)
        for site in sorted(selected, key=lambda item: not _names_removed_class(item)):
            if id(site) in covered:
                report.results.append(SiteResult(site=site, status=SiteStatus.SKIPPED, why=covered[id(site)]))
                continue
            if site.via_wrapper:
                report.results.append(_apply_one(site, work, client, report))
                continue
            originals.setdefault(site.path, _read(config.root / site.path))
            texts_of[id(site)] = texts.get(id(site), "")
            moved = _relocate(scratch, site, texts_of[id(site)])
            sink: dict[str, Any] = {"site": site}
            result = _apply_one(moved, work, client, report, sink=sink) if moved else None
            slots[id(site)] = len(report.results)
            report.results.append(result or SiteResult(site=site, status=SiteStatus.SKIPPED, why="line rewritten by an earlier fix in this file"))
            if result is not None and result.status == SiteStatus.FIXED_UNVERIFIED:
                accepted[id(site)] = sink
        failure = ""
        for _round in range(config.settings.max_attempts + 1):
            ok, output = _run_cmd(config.test_cmd or "", scratch)
            if ok or not accepted:
                failure = "" if ok else output
                break
            failure = output
            blamed = _blame(output, scratch, accepted) or list(accepted)
            if _round == config.settings.max_attempts:
                break
            for key in blamed:
                sink = accepted.pop(key)
                site = sink["site"]
                _rebuild(scratch, originals, accepted, texts_of, site.path)
                moved = _relocate(scratch, site, texts_of[key])
                note = trim_test_feedback(output)
                retry_sink: dict[str, Any] = {"site": site}
                result = None
                used = report.results[slots[key]].attempts
                if moved is not None and used < config.settings.max_attempts and sink.get("tokens", 0) < config.settings.max_tokens_per_site:
                    feedback = (
                        "Your previous rewrite passed the static check but the tests failed:\n"
                        f"{sink.get('block', '')}\nTest output:\n{note}"
                    )
                    result = _apply_one(moved, work, client, report, feedback=feedback, spent=sink.get("tokens", 0), used=used, sink=retry_sink)
                if result is not None and result.status == SiteStatus.FIXED_UNVERIFIED:
                    accepted[key] = retry_sink
                    report.results[slots[key]] = result
                else:
                    _rebuild(scratch, originals, accepted, texts_of, site.path)
                    report.results[slots[key]] = _reverted(result or report.results[slots[key]], site, note)
        if failure and accepted:
            # Revert what the last failure points at, then confirm the rest pass.
            for key in _blame(failure, scratch, accepted) or list(accepted):
                sink = accepted.pop(key)
                _rebuild(scratch, originals, accepted, texts_of, sink["site"].path)
                report.results[slots[key]] = _reverted(report.results[slots[key]], sink["site"], trim_test_feedback(failure))
            ok, output = _run_cmd(config.test_cmd or "", scratch) if accepted else (True, "")
            if not ok:
                for key in list(accepted):
                    sink = accepted.pop(key)
                    _rebuild(scratch, originals, accepted, texts_of, sink["site"].path)
                    report.results[slots[key]] = _reverted(report.results[slots[key]], sink["site"], trim_test_feedback(output))
        for key in accepted:
            done = report.results[slots[key]]
            report.results[slots[key]] = replace(done, status=SiteStatus.FIXED_VERIFIED)
        for path, before in originals.items():
            after = _read(scratch / path)
            for key in accepted:
                if report.results[slots[key]].site.path == path:
                    report.results[slots[key]].diff = unified_diff_for(path, before, after)
            if config.write and after != before:
                _write_file(config.root, path, after)


_SCRATCH_IGNORE = (".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _run_cmd(command: str, cwd: Path) -> tuple[bool, str]:
    proc = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True, check=False)
    return proc.returncode == 0, ((proc.stdout or "") + (proc.stderr or "")).strip()


def _rebuild(scratch: Path, originals: dict[str, str], accepted: dict[int, dict[str, Any]], texts: dict[int, str], path: str) -> None:
    """The file as its original plus every accepted edit, re-spliced in order."""
    _write_file(scratch, path, originals[path])
    for key, sink in list(accepted.items()):
        site = sink["site"]
        if site.path != path:
            continue
        moved = _relocate(scratch, site, texts.get(key, ""))
        try:
            if moved is None:
                raise PatchError("site line not found")
            patched, _diff, _span = splice_edit_with_span(build_context_pack(scratch, moved), sink["edit"])
        except (PatchError, ContextError):
            accepted.pop(key)
            continue
        _write_file(scratch, path, patched)


def _blame(output: str, scratch: Path, accepted: dict[int, dict[str, Any]]) -> list[int]:
    """Sites a test failure points at: a traceback line inside the rewrite, or a
    name the failure reports (ImportError, NameError, AttributeError) that the
    rewrite mentions. Otherwise the sites whose file the output names."""
    located: dict[str, list[int]] = {}
    for match in re.finditer(r'([\w./\\-]+\.py)(?::|", line )(\d+)', output):
        located.setdefault(match.group(1).replace("\\", "/"), []).append(int(match.group(2)))
    names = set(re.findall(r"(?:cannot import name|name|attribute) '(\w+)'", output))
    by_line: list[int] = []
    by_file: list[int] = []
    for key, sink in accepted.items():
        path = sink["site"].path
        block = sink.get("block", "")
        text = _read(scratch / path)
        at = text.find(block) if block else -1
        first = text.count("\n", 0, at) + 1 if at >= 0 else 0
        last = first + block.count("\n")
        lines = [n for shown, found in located.items() if shown.endswith(path) for n in found]
        if (at >= 0 and any(first <= n <= last for n in lines)) or any(re.search(rf"\b{name}\b", block) for name in names):
            by_line.append(key)
        elif lines or path in output:
            by_file.append(key)
    return by_line or by_file


def _reverted(result: SiteResult, site: ImpactSite, note: str) -> SiteResult:
    return SiteResult(
        site=site,
        status=SiteStatus.NEEDS_HUMAN,
        diff=result.diff,
        attempts=result.attempts,
        error="reverted: the tests still fail with this rewrite\n" + note,
        context=result.context,
    )


def _names_removed_class(site: ImpactSite) -> bool:
    tail = (site.expr.split() or [""])[-1].rsplit(".", 1)[-1]
    return site.kind == "removed" and tail[:1].isupper() and tail == site.symbol.rsplit(".", 1)[-1]


def _one_per_function(root: Path, sites: list[ImpactSite]) -> tuple[dict[int, str], dict[int, str]]:
    """The rewrite replaces a whole function and the recheck covers its span,
    so one site per enclosing function is enough; the rest point at it."""
    first: dict[tuple[str, int], ImpactSite] = {}
    covered: dict[int, str] = {}
    texts: dict[int, str] = {}
    for site in sites:
        if site.via_wrapper:
            continue
        try:
            lines = (root / site.path).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        try:
            span = enclosing_function("\n".join(lines), site.line)
        except ContextError:
            span = None
        key = (site.path, span[2] if span else -site.line)
        lead = first.setdefault(key, site)
        if lead is not site:
            covered[id(site)] = f"same function as {lead.path}:{lead.line}; covered by that rewrite and its recheck"
        elif 0 < site.line <= len(lines):
            texts[id(site)] = lines[site.line - 1].strip()
    return covered, texts


def _relocate(root: Path, site: ImpactSite, text: str) -> ImpactSite | None:
    """Earlier written fixes shift lines: find the site's original line text again."""
    try:
        lines = (root / site.path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return site
    if not text or (0 < site.line <= len(lines) and lines[site.line - 1].strip() == text):
        return site
    hits = [number for number, line in enumerate(lines, 1) if line.strip() == text]
    if not hits:
        return None
    return replace(site, line=min(hits, key=lambda number: abs(number - site.line)))


def _apply_one(
    site: ImpactSite,
    config: ApplyConfig,
    client: ModelClient | None,
    report: ApplyReport,
    *,
    feedback: str = "",
    spent: int = 0,
    used: int = 0,
    sink: dict[str, Any] | None = None,
) -> SiteResult:
    if site.via_wrapper:
        return SiteResult(
            site=site,
            status=SiteStatus.SKIPPED,
            why="wrapper caller; fixed at the SDK line",
        )
    try:
        pack = build_context_pack(config.root, site)
    except ContextError as exc:
        return SiteResult(site=site, status=SiteStatus.NEEDS_HUMAN, error=str(exc))

    if config.dry_run:
        return SiteResult(
            site=site,
            status=SiteStatus.DRY_RUN,
            why="context pack only (--dry-run); model not called",
            context=pack,
        )

    if client is None:
        return SiteResult(
            site=site,
            status=SiteStatus.NEEDS_HUMAN,
            error="no model client available",
            context=pack,
        )

    target = _new_signatures(config, site, [site.symbol])
    if target:
        pack.release_diff["new_release_signatures"] = target

    last_error = ""
    last_diff = ""
    earlier: list[str] = []
    attempts = used
    site_tokens = spent
    sink = sink if sink is not None else {}
    for attempt in range(used + 1, config.settings.max_attempts + 1):
        attempts = attempt
        remaining = config.settings.max_tokens_per_site - site_tokens
        if remaining <= 0:
            last_error = "token budget exhausted for this site"
            break
        try:
            edit_text, used = _propose(
                client,
                pack,
                config.settings,
                feedback=feedback,
                max_tokens=min(2048, remaining),
            )
            site_tokens += used
            report.tokens_used += used
            edit = parse_model_edit(edit_text)
            patched, diff, span = splice_edit_with_span(pack, edit)
            last_diff = diff
            _verify_site_cleared(config, site, patched, span)
            tests_ok, test_err = _run_tests(config, site, patched)
            if not tests_ok:
                feedback = trim_test_feedback(test_err or "tests failed")
                last_error = feedback
                continue
            why = ""
            why_budget = config.settings.max_tokens_per_site - site_tokens
            if why_budget > 0:
                why, why_tokens = _summarize(
                    client, pack, diff, config.settings, max_tokens=min(128, why_budget)
                )
                site_tokens += why_tokens
                report.tokens_used += why_tokens
            status = (
                SiteStatus.FIXED_VERIFIED
                if config.test_cmd
                else SiteStatus.FIXED_UNVERIFIED
            )
            if config.write:
                _write_file(config.root, site.path, patched)
            sink.update(edit=edit, block="".join(patched.splitlines(keepends=True)[span[0] - 1 : span[1]]))
            sink["tokens"] = site_tokens
            return SiteResult(
                site=site,
                status=status,
                why=why,
                diff=diff,
                attempts=attempts,
                context=pack,
            )
        except RecheckError as exc:
            last_error = str(exc)
            feedback = _with_earlier(last_error, exc.findings, earlier)
            earlier.extend(text for text in exc.findings if text not in earlier)
        except PatchError as exc:
            last_error = str(exc)
            feedback = last_error
        except Exception as exc:  # noqa: BLE001 — surface to retry / needs-human
            last_error = str(exc)
            feedback = last_error

    sink["tokens"] = site_tokens
    return SiteResult(
        site=site,
        status=SiteStatus.NEEDS_HUMAN,
        why="",
        diff=last_diff,
        attempts=attempts,
        error=last_error.split("\nNew release signatures", 1)[0] or "max attempts exhausted",
        context=pack,
    )


def _propose(
    client: ModelClient,
    pack: ContextPack,
    settings: TokenFactorySettings,
    *,
    feedback: str,
    max_tokens: int,
) -> tuple[str, int]:
    messages = [
        ChatMessage(role=role, content=content)
        for role, content in propose_messages(pack, feedback=feedback)
    ]
    result = client.chat(
        model=settings.model,
        messages=messages,
        max_tokens=max_tokens,
        enable_thinking=settings.enable_thinking,
    )
    return result.content, result.total_tokens


def _summarize(
    client: ModelClient,
    pack: ContextPack,
    diff: str,
    settings: TokenFactorySettings,
    *,
    max_tokens: int,
) -> tuple[str, int]:
    messages = [
        ChatMessage(role=role, content=content)
        for role, content in summarize_messages(pack, diff)
    ]
    result = client.chat(
        model=settings.model_small,
        messages=messages,
        max_tokens=max_tokens,
        enable_thinking=settings.enable_thinking_small,
    )
    text = result.content.strip()
    why = text.splitlines()[0] if text else ""
    return why, result.total_tokens


def _verify_site_cleared(
    config: ApplyConfig, site: ImpactSite, patched: str, span: tuple[int, int]
) -> None:
    if config.site_cleared is not None:
        if not config.site_cleared(config.root, site, patched):
            raise PatchError("impact site still flagged after patch")
        return
    if not config.recheck_impact:
        return
    breaks = remaining_breaks(
        config.root,
        site,
        patched,
        span=span,
        fixtures=config.fixtures,
        allow_network=config.allow_network,
    )
    if breaks:
        # Fed back to the model as the retry reason; the last one is the Why.
        message = "rewrite still breaks against the new release:\n- " + "\n- ".join(
            text for text, _symbol in breaks
        )
        signatures = _new_signatures(config, site, [symbol for _text, symbol in breaks])
        if signatures:
            message += "\nNew release signatures (read from its own code):\n" + "\n".join(
                signatures.values()
            )
        raise RecheckError(message, [text for text, _symbol in breaks])


class RecheckError(PatchError):
    """Recheck findings for one attempt; ``findings`` carries them one by one."""

    def __init__(self, message: str, findings: list[str]) -> None:
        super().__init__(message)
        self.findings = findings


def _with_earlier(message: str, current: list[str], earlier: list[str]) -> str:
    """Remind the model of findings an earlier attempt hit that this one did not repeat."""
    missing = [text for text in earlier if text not in current]
    if not missing:
        return message
    head, sep, tail = message.partition("\nNew release signatures")
    head += "\nEarlier attempts also broke (keep these fixed):\n- " + "\n- ".join(missing)
    return head + sep + tail


def _new_signatures(config: ApplyConfig, site: ImpactSite, symbols: list[str]) -> dict[str, str]:
    """Signatures of ``symbols`` in the new release's own code, when it defines them."""
    if not config.recheck_impact or not any(symbols):
        return {}
    try:
        wheel = new_release_wheel(
            site.dependency,
            site.to_version,
            fixtures=config.fixtures,
            allow_network=config.allow_network,
        )
        return signatures_for(wheel, [symbol for symbol in symbols if symbol])
    except Exception:  # noqa: BLE001 — prompt context only; never blocks a fix
        return {}


def _run_tests(
    config: ApplyConfig, site: ImpactSite, patched: str
) -> tuple[bool, str]:
    if not config.test_cmd:
        return True, ""
    with tempfile.TemporaryDirectory(prefix="conduit-apply-") as tmp:
        scratch = Path(tmp) / "tree"
        shutil.copytree(
            config.root,
            scratch,
            ignore=shutil.ignore_patterns(
                ".git",
                ".venv",
                "venv",
                "__pycache__",
                ".pytest_cache",
                "node_modules",
            ),
        )
        target = scratch / site.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(patched, encoding="utf-8")
        proc = subprocess.run(
            config.test_cmd,
            shell=True,
            cwd=scratch,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return True, ""
        err = (proc.stdout or "") + (proc.stderr or "")
        return False, err.strip() or f"test command exited {proc.returncode}"


def _write_file(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
