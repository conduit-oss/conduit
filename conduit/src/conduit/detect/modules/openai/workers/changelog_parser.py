"""ChangelogParserWorker: extract signature changes from RSS/HTML changelogs."""

from __future__ import annotations

import re
from typing import Any

import feedparser
import httpx

from conduit.detect.modules.openai.models_legacy import ChangeType, RawSignal, Severity
from conduit.detect.modules.openai.workers.base import Worker, fixtures_dir, resolve_profile

CHANGELOG_URL = "https://platform.openai.com/docs/changelog"

RENAME_RE = re.compile(
    r"(?P<old>\b[a-z_][a-z0-9_]*)\s*(?:has been\s+)?renamed to\s+"
    r"(?P<new>\b[a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
# "Deprecated the Chat Completions <param> parameter in favor of <new>"
DEPRECATED_PARAM_RE = re.compile(
    r"[Dd]eprecat(?:ed|ing)\s+the\s+(?:Chat Completions\s+)?"
    r"(?P<old>`?[a-z_][a-z0-9_]*`?)\s+parameter\s+"
    r"(?:in favor of|replaced by|renamed to)\s+"
    r"(?P<new>`?[a-z_][a-z0-9_]*`?)",
    re.IGNORECASE,
)
MIGRATE_RE = re.compile(
    r"[Mm]igrate(?:\s+from)?\s+(?P<old>`?[a-zA-Z0-9._-]+`?)\s+to\s+"
    r"(?P<new>`?[a-zA-Z0-9._-]+`?)"
)
SHUTDOWN_RE = re.compile(
    r"[Mm]odel\s+(?P<model>`?[a-zA-Z0-9._-]+`?)\s+will shut down on\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2})"
)


def _strip_ticks(value: str) -> str:
    return value.strip().strip("`")


def parse_changelog_text(text: str, source_url: str | None = None) -> list[RawSignal]:
    signals: list[RawSignal] = []

    for match in RENAME_RE.finditer(text):
        old_p, new_p = match.group("old"), match.group("new")
        signals.append(
            RawSignal(
                vendor="openai",
                change_type=ChangeType.PARAM_RENAME,
                severity=Severity.CRITICAL,
                affected_pattern=old_p,
                replacement_pattern=new_p,
                source_url=source_url,
                description=f"Parameter renamed: {old_p} -> {new_p}",
                extra={
                    "old_param": old_p,
                    "new_param": new_p,
                },
            )
        )

    for match in DEPRECATED_PARAM_RE.finditer(text):
        old_p, new_p = _strip_ticks(match.group("old")), _strip_ticks(match.group("new"))
        if old_p == new_p:
            continue
        signals.append(
            RawSignal(
                vendor="openai",
                change_type=ChangeType.PARAM_RENAME,
                severity=Severity.CRITICAL,
                affected_pattern=old_p,
                replacement_pattern=new_p,
                source_url=source_url,
                description=f"Parameter deprecated: {old_p} -> {new_p}",
                extra={
                    "old_param": old_p,
                    "new_param": new_p,
                },
            )
        )

    for match in SHUTDOWN_RE.finditer(text):
        model = _strip_ticks(match.group("model"))
        date = match.group("date")
        migrate = MIGRATE_RE.search(text)
        replacement = _strip_ticks(migrate.group("new")) if migrate else None
        signals.append(
            RawSignal(
                vendor="openai",
                change_type=ChangeType.MODEL_DEPRECATION,
                severity=Severity.CRITICAL,
                affected_pattern=model,
                replacement_pattern=replacement,
                deadline=f"{date}T00:00:00Z",
                source_url=source_url or "https://platform.openai.com/docs/deprecations",
                description=f"Model {model} shutting down {date}",
            )
        )

    return signals


def _llm_extract(text: str) -> list[RawSignal]:
    from conduit.llm import extract_json_payload, get_llm_client

    client = get_llm_client()
    if client is None:
        return []

    prompt = (
        "Extract API signature changes from this changelog. "
        'Return JSON object {"changes": [...]} where each item has keys: '
        "change_type (PARAM_RENAME|MODEL_DEPRECATION), affected_pattern, "
        "replacement_pattern, deadline (ISO8601 or null), description.\n\n"
        f"{text[:6000]}"
    )
    try:
        payload: Any = client.complete_json(
            system="Extract changelog API changes. JSON only.",
            user=prompt,
        )
        if not payload:
            payload = extract_json_payload(prompt) or {}
        items = payload if isinstance(payload, list) else payload.get("changes", [])
    except Exception:
        return []

    signals: list[RawSignal] = []
    for item in items:
        try:
            ctype = ChangeType(item.get("change_type", "PARAM_RENAME"))
        except ValueError:
            ctype = ChangeType.PARAM_RENAME
        signals.append(
            RawSignal(
                vendor="openai",
                change_type=ctype,
                severity=Severity.WARNING,
                affected_pattern=str(item.get("affected_pattern", "")),
                replacement_pattern=item.get("replacement_pattern"),
                deadline=item.get("deadline"),
                description=item.get("description"),
                source_url="https://platform.openai.com/docs/changelog",
                extra={
                    "old_param": item.get("affected_pattern"),
                    "new_param": item.get("replacement_pattern"),
                },
            )
        )
    return [s for s in signals if s.affected_pattern]


class ChangelogParserWorker(Worker):
    name = "ChangelogParserWorker"

    def run(
        self,
        *,
        demo: bool = False,
        client_state=None,
        majors_only: bool = True,
        profile=None,
    ) -> list[RawSignal]:
        signals: list[RawSignal] = []
        blobs: list[str] = []
        prof = resolve_profile(profile)
        changelog_url = prof.changelog_url or CHANGELOG_URL
        fx = fixtures_dir(prof.fixtures_name)

        if demo:
            feed_path = fx / "changelogs" / "openai_changelog.rss"
            parsed = feedparser.parse(feed_path.read_text(encoding="utf-8"))
            for entry in parsed.entries:
                title = getattr(entry, "title", "") or ""
                summary = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
                link = getattr(entry, "link", None)
                blob = f"{title}\n{summary}"
                blobs.append(blob)
                signals.extend(parse_changelog_text(blob, link))
        else:
            try:
                resp = httpx.get(changelog_url, timeout=45.0, follow_redirects=True)
                resp.raise_for_status()
                from bs4 import BeautifulSoup

                text = BeautifulSoup(resp.text, "html.parser").get_text("\n", strip=True)
                blobs.append(text)
                signals.extend(parse_changelog_text(text, str(resp.url)))
            except httpx.HTTPError as exc:
                raise RuntimeError(f"changelog fetch failed: {exc}") from exc

        from conduit.llm import get_llm_client

        if get_llm_client() is not None and blobs:
            llm_signals = _llm_extract("\n\n".join(blobs))
            seen = {(s.change_type, s.affected_pattern, s.replacement_pattern) for s in signals}
            for s in llm_signals:
                key = (s.change_type, s.affected_pattern, s.replacement_pattern)
                if key not in seen:
                    signals.append(s)
                    seen.add(key)

        return signals
