"""DeprecationScraperWorker: extract deprecation metadata from vendor pages."""

from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from conduit.detect.modules.openai.models_legacy import ChangeType, RawSignal, Severity
from conduit.detect.modules.openai.workers.base import Worker, fixtures_dir

DEFAULT_URL = "https://platform.openai.com/docs/deprecations"

# Model / system IDs we care about for string migrations (not prose / prices / APIs alone)
_MODEL_ID_RE = re.compile(
    r"^(?:ft-)?"
    r"(?:"
    r"gpt-[a-z0-9._-]+"
    r"|o[0-9][a-z0-9._-]*"
    r"|dall-e-[0-9]"
    r"|chatgpt-[a-z0-9._-]+"
    r"|text-[a-z0-9._-]+"
    r"|code-[a-z0-9._-]+"
    r"|babbage-[a-z0-9._-]+"
    r"|ada(?![a-z])"
    r"|curie(?![a-z])"
    r"|davinci(?![a-z])"
    r"|computer-use-[a-z0-9._-]+"
    r"|omni-moderation(?:-[a-z0-9._-]+)?"
    r"|text-moderation-[a-z0-9._-]+"
    r"|text-embedding-[a-z0-9._-]+"
    r"|text-similarity-[a-z0-9._-]+"
    r"|whisper-[a-z0-9._-]+"
    r"|tts-[a-z0-9._-]+"
    r"|gpt-image-[a-z0-9._-]+"
    r"|codex-[a-z0-9._-]+"
    r")$",
    re.IGNORECASE,
)

_ENDPOINT_RE = re.compile(r"^/v1/[a-z0-9/_-]+$", re.IGNORECASE)


def _normalize_deadline(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    # Normalize unicode dashes
    raw = raw.replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%dT00:00:00Z")
        except ValueError:
            continue
    # "at earliest 2024-06-13"
    m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return f"{m.group(1)}T00:00:00Z"
    return raw if "T" in raw else None


def _parse_severity(raw: str | None) -> Severity:
    value = (raw or "WARNING").strip().upper()
    if value in Severity.__members__:
        return Severity[value]
    if value in {"HIGH", "ERROR", "FATAL"}:
        return Severity.CRITICAL
    return Severity.WARNING


def _split_ids(cell: str) -> list[str]:
    """Split a table cell that may list several model ids."""
    cell = cell.replace(",", "|")
    parts = [p.strip() for p in cell.split("|")]
    out: list[str] = []
    for part in parts:
        # Drop trailing prose after id
        token = part.split()[0] if part.split() else ""
        token = token.strip("*`\"'")
        if token:
            out.append(token)
    return out


def _primary_replacement(cell: str) -> str | None:
    """Pick the first concrete model/endpoint replacement from a cell."""
    if not cell or cell.strip() in {"---", "-", "n/a", "N/A"}:
        return None
    # Prefer first model-like token; else first endpoint; else first token
    for token in _split_ids(cell):
        if _MODEL_ID_RE.match(token) or _ENDPOINT_RE.match(token):
            return token
    # "gpt-5 or gpt-4.1*" style
    m = re.search(
        r"(gpt-[a-z0-9._-]+|o[0-9][a-z0-9._-]*|omni-moderation[a-z0-9._-]*|/v1/[a-z0-9/_-]+)",
        cell,
        re.I,
    )
    if m:
        return m.group(1).rstrip("*")
    return None


def _col_index(headers: list[str], *needles: str) -> int | None:
    for i, h in enumerate(headers):
        hl = h.lower()
        if any(n in hl for n in needles):
            return i
    return None


def _fixture_row_signal(
    *,
    legacy: str,
    replacement: str | None,
    shutdown: str | None,
    severity: Severity,
    source_url: str,
) -> RawSignal:
    if _ENDPOINT_RE.match(legacy):
        return RawSignal(
            vendor="openai",
            change_type=ChangeType.API_BREAKING,
            severity=severity,
            affected_pattern=legacy,
            replacement_pattern=replacement,
            deadline=_normalize_deadline(shutdown),
            source_url=source_url,
            description=(
                f"Endpoint {legacy} deprecated; "
                f"replace with {replacement or 'see docs'}"
            ),
        )
    return RawSignal(
        vendor="openai",
        change_type=ChangeType.MODEL_DEPRECATION,
        severity=severity,
        affected_pattern=legacy,
        replacement_pattern=replacement,
        deadline=_normalize_deadline(shutdown),
        source_url=source_url,
        description=f"Model {legacy} deprecated; replace with {replacement}",
    )


def _signals_from_fixture_rows(soup: BeautifulSoup, source_url: str) -> list[RawSignal]:
    signals: list[RawSignal] = []
    for row in soup.select("tr[data-legacy]"):
        legacy = row.get("data-legacy") or ""
        replacement = row.get("data-replacement")
        shutdown = row.get("data-shutdown")
        severity = _parse_severity(row.get("data-severity"))
        if not legacy:
            continue
        signals.append(
            _fixture_row_signal(
                legacy=str(legacy),
                replacement=str(replacement) if replacement else None,
                shutdown=str(shutdown) if shutdown else None,
                severity=severity,
                source_url=source_url,
            )
        )
    if signals:
        return signals

    for row in soup.select("table#deprecations tbody tr"):
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) < 3:
            continue
        legacy, replacement, shutdown = cells[0], cells[1], cells[2]
        severity = _parse_severity(cells[3] if len(cells) > 3 else "WARNING")
        signals.append(
            _fixture_row_signal(
                legacy=legacy,
                replacement=replacement or None,
                shutdown=shutdown,
                severity=severity,
                source_url=source_url,
            )
        )
    return signals


def _signals_from_live_tables(soup: BeautifulSoup, source_url: str) -> list[RawSignal]:
    signals: list[RawSignal] = []
    seen: set[tuple[str, str | None]] = set()

    for table in soup.select("table"):
        rows = table.select("tr")
        if len(rows) < 2:
            continue
        headers = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
        if not headers:
            continue
        shut_i = _col_index(headers, "shutdown")
        model_i = _col_index(
            headers,
            "model / system",
            "model family",
            "model snapshot",
            "deprecated model",
            "legacy model",
            "system",
        )
        repl_i = _col_index(headers, "replacement", "substitute")
        if shut_i is None or model_i is None or repl_i is None:
            continue

        for row in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if max(shut_i, model_i, repl_i) >= len(cells):
                continue
            shutdown = cells[shut_i]
            legacy_cell = cells[model_i]
            repl_cell = cells[repl_i]
            replacement = _primary_replacement(repl_cell)
            deadline = _normalize_deadline(shutdown)

            for legacy in _split_ids(legacy_cell):
                if _ENDPOINT_RE.match(legacy):
                    key = (legacy, replacement)
                    if key in seen:
                        continue
                    seen.add(key)
                    signals.append(
                        RawSignal(
                            vendor="openai",
                            change_type=ChangeType.API_BREAKING,
                            severity=Severity.CRITICAL,
                            affected_pattern=legacy,
                            replacement_pattern=replacement,
                            deadline=deadline,
                            source_url=source_url,
                            description=(
                                f"Endpoint {legacy} deprecated; "
                                f"replace with {replacement or 'see docs'}"
                            ),
                        )
                    )
                    continue
                if not _MODEL_ID_RE.match(legacy):
                    continue
                key = (legacy, replacement)
                if key in seen:
                    continue
                seen.add(key)
                signals.append(
                    RawSignal(
                        vendor="openai",
                        change_type=ChangeType.MODEL_DEPRECATION,
                        severity=Severity.CRITICAL,
                        affected_pattern=legacy,
                        replacement_pattern=replacement,
                        deadline=deadline,
                        source_url=source_url,
                        description=(
                            f"Model {legacy} deprecated; replace with {replacement or 'see docs'}"
                        ),
                    )
                )
    return signals


def parse_deprecation_html(html: str, source_url: str) -> list[RawSignal]:
    soup = BeautifulSoup(html, "html.parser")
    fixture = _signals_from_fixture_rows(soup, source_url)
    if fixture:
        return fixture
    return _signals_from_live_tables(soup, source_url)


class DeprecationScraperWorker(Worker):
    name = "DeprecationScraperWorker"

    def run(self, *, demo: bool = False, client_state=None, majors_only: bool = True) -> list[RawSignal]:
        if demo:
            fixture = fixtures_dir() / "deprecations" / "openai_deprecations.html"
            html = fixture.read_text(encoding="utf-8")
            return parse_deprecation_html(html, DEFAULT_URL)

        try:
            resp = httpx.get(DEFAULT_URL, timeout=45.0, follow_redirects=True)
            resp.raise_for_status()
            return parse_deprecation_html(resp.text, str(resp.url))
        except httpx.HTTPError as exc:
            raise RuntimeError(f"deprecation scrape failed: {exc}") from exc
