"""Per-customer map, one watch pass, and a local fix PR.

``conduit apply`` is deferred, so the pass flags call sites and does not rewrite the tree.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from conduit.feeds.client import FeedClient
from conduit.feeds.watch import watch_outside
from conduit.human_checklist import format_human_checklist
from conduit.outside_in.bridge import build_service_map
from conduit.outside_in.report import service_map_to_dict


@dataclass
class ServiceEvent:
    customer_id: str
    packet_id: str
    flagged: list[dict[str, Any]]
    pr_path: str
    apply_exit: int
    apply_output: str


@dataclass
class TickResult:
    customer_id: str
    events: list[ServiceEvent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def add_customer(store: Path, customer_id: str, repo: Path) -> Path:
    """Scan ``repo`` and store that customer's outside-service map."""
    _check_id(customer_id)
    root = repo.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"repo is not a directory: {root}")
    dest = _customer_dir(store, customer_id)
    dest.mkdir(parents=True, exist_ok=True)
    service_map, _blast = build_service_map(root)
    (dest / "customer.json").write_text(
        json.dumps({"id": customer_id, "repo": str(root)}, indent=2) + "\n",
        encoding="utf-8",
    )
    (dest / "map.json").write_text(
        json.dumps(service_map_to_dict(service_map), indent=2) + "\n",
        encoding="utf-8",
    )
    seen = dest / "seen.json"
    if not seen.is_file():
        seen.write_text("[]\n", encoding="utf-8")
    return dest


def tick(
    store: Path,
    *,
    client: FeedClient,
    customer_id: str | None = None,
    dry_run: bool = True,
) -> list[TickResult]:
    """One scheduled pass: watch each stored map, flag call sites, dry-run apply."""
    results: list[TickResult] = []
    for dest in _customer_dirs(store, customer_id):
        record = json.loads((dest / "customer.json").read_text(encoding="utf-8"))
        cid = str(record.get("id") or dest.name)
        repo = Path(str(record.get("repo") or ""))
        result = TickResult(customer_id=cid)
        if not repo.is_dir():
            result.warnings.append(f"repo missing: {repo}")
            results.append(result)
            continue
        service_map, _blast = build_service_map(repo)
        (dest / "map.json").write_text(
            json.dumps(service_map_to_dict(service_map), indent=2) + "\n",
            encoding="utf-8",
        )
        watched = watch_outside(repo, client=client)
        result.warnings.extend(watched.warnings)
        seen = set(_read_seen(dest))
        for packet in watched.packets:
            packet_id = str(packet.get("packet_id") or "")
            if not packet_id or packet_id in seen:
                continue
            event = _open_fix(dest, repo, packet, dry_run=dry_run)
            result.events.append(event)
            if event.apply_exit == 0:
                seen.add(packet_id)
        _write_seen(dest, seen)
        results.append(result)
    return results


def _open_fix(dest: Path, repo: Path, packet: dict, *, dry_run: bool) -> ServiceEvent:
    packet_id = str(packet.get("packet_id") or "packet")
    packet_dir = dest / "packets"
    packet_dir.mkdir(parents=True, exist_ok=True)
    packet_path = packet_dir / f"{packet_id}.json"
    packet_path.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
    flagged = flag_call_sites(repo, packet)
    apply_exit, apply_output = run_apply(repo, packet_path, dry_run=dry_run)
    pr_dir = dest / "prs"
    pr_dir.mkdir(parents=True, exist_ok=True)
    pr_path = pr_dir / f"{packet_id}.md"
    pr_path.write_text(
        render_pr(
            packet,
            flagged=flagged,
            apply_exit=apply_exit,
            apply_output=apply_output,
            dry_run=dry_run,
            repo=repo,
            packet_path=packet_path,
        ),
        encoding="utf-8",
    )
    return ServiceEvent(
        customer_id=dest.name,
        packet_id=packet_id,
        flagged=flagged,
        pr_path=str(pr_path),
        apply_exit=apply_exit,
        apply_output=apply_output,
    )


def flag_call_sites(repo: Path, packet: dict) -> list[dict[str, Any]]:
    """Call sites on the map that this packet would break.

    When the packet names no callee, every chain for that service is listed
    so a version-only hop is still visible.
    """
    _service_map, blast = build_service_map(repo, packet=packet)
    if blast is None:
        return []
    specific = [item for item in blast.affected if item.breaks]
    rows_src = specific or list(blast.affected)
    vague = not specific
    rows: list[dict[str, Any]] = []
    for item in rows_src:
        chain = item.chain
        detail = (
            "no callee rename in the packet; call site is on this service"
            if vague
            else "; ".join(brk.detail for brk in item.breaks)
        )
        rows.append(
            {
                "path": chain.call_rel,
                "line": chain.call_lineno,
                "symbol": chain.call_symbol,
                "via_wrapper": chain.via_wrapper,
                "detail": detail,
            }
        )
    return rows


def run_apply(repo: Path, packet_path: Path, *, dry_run: bool) -> tuple[int, str]:
    """``conduit apply`` is deferred. Flagged lines stay in the PR; the tree is not rewritten."""
    del repo, packet_path
    mode = "dry-run" if dry_run else "apply"
    return 0, (
        f"conduit apply is deferred ({mode}). "
        "The tree was not rewritten. Call sites are listed above."
    )


def render_pr(
    packet: dict,
    *,
    flagged: list[dict[str, Any]],
    apply_exit: int,
    apply_output: str,
    dry_run: bool,
    repo: Path,
    packet_path: Path,
) -> str:
    package = packet.get("package") or ""
    hop = f"{packet.get('from_version') or '?'} -> {packet.get('to_version') or '?'}"
    mode = "dry-run" if dry_run else "apply"
    lines = [
        f"# {package} {hop}",
        "",
        f"Local fix PR ({mode}). `conduit apply` is deferred, so this does not rewrite the tree. No GitHub App.",
        "",
        "```",
        f"conduit impact --path {repo}",
        "```",
        "",
        "## Affected call sites",
        "",
    ]
    if not flagged:
        lines.append("No call sites on the stored map matched this packet.")
    for row in flagged:
        wrapper = " wrapper" if row.get("via_wrapper") else ""
        lines.append(
            f"- `{row['path']}:{row['line']}` {row['symbol']}{wrapper} — {row['detail']}"
        )
    lines.extend(["", "## Still needs a human", ""])
    checklist = format_human_checklist(packet, leftovers=())
    if checklist.strip():
        lines.append(checklist.rstrip())
    else:
        lines.append("No packet side effects. Read the flagged call sites before editing.")
    lines.append("")
    lines.append("Rewriting the tree is deferred with `conduit apply`.")
    lines.extend(["", f"## apply deferred ({mode}) exit {apply_exit}", "", "```", apply_output.rstrip(), "```", ""])
    return "\n".join(lines)


def _customer_dir(store: Path, customer_id: str) -> Path:
    return store.expanduser().resolve() / "customers" / customer_id


def _customer_dirs(store: Path, customer_id: str | None) -> list[Path]:
    root = store.expanduser().resolve() / "customers"
    if customer_id:
        dest = root / customer_id
        if not (dest / "customer.json").is_file():
            raise FileNotFoundError(f"no stored customer {customer_id}")
        return [dest]
    if not root.is_dir():
        return []
    found = []
    for child in sorted(root.iterdir()):
        if (child / "customer.json").is_file():
            found.append(child)
    return found


def _read_seen(dest: Path) -> list[str]:
    path = dest / "seen.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [str(item) for item in payload]


def _write_seen(dest: Path, seen: set[str]) -> None:
    (dest / "seen.json").write_text(
        json.dumps(sorted(seen), indent=2) + "\n",
        encoding="utf-8",
    )


def _check_id(customer_id: str) -> None:
    if not customer_id or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in customer_id):
        raise ValueError(
            "customer id must be lowercase letters, numbers, dash, or underscore"
        )
