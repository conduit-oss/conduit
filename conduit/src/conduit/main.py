"""Conduit CLI — map outside services, watch what they publish, and show breaking lines."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from conduit.service.cli import install as install_service

app = typer.Typer(
    name="conduit",
    help="Map outside services and show the lines that break when they change",
    add_completion=False,
)
install_service(app)
console = Console()


def _resolve_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    if not root.exists():
        console.print(f"[red]Path not found:[/red] {root}")
        raise typer.Exit(2)
    if not root.is_dir():
        console.print(f"[red]Not a directory:[/red] {root}")
        raise typer.Exit(2)
    return root


def _load_hop(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        console.print(f"[red]Could not read hop document:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(data, dict):
        console.print("[red]Hop document must be a JSON object.[/red]")
        raise typer.Exit(2)
    from conduit.packet_schema import validate_packet

    errors = validate_packet(data)
    if errors:
        for err in errors:
            console.print(f"[red]schema:[/red] {err}")
        raise typer.Exit(1)
    return data


def _watch_feeds(
    root: Path,
    *,
    json_out: bool,
    feeds: Path | None,
    out: Path | None,
    service: str | None,
    show_callers: bool = False,
) -> None:
    """Poll public feeds for outside services on this repo's map."""
    from conduit.feeds.client import FeedClient
    from conduit.feeds.watch import watch_outside

    if feeds is None:
        client = FeedClient(allow_network=True)
    else:
        fixture_root = feeds.expanduser().resolve()
        if not fixture_root.is_dir():
            console.print(f"[red]Feeds directory not found:[/red] {fixture_root}")
            raise typer.Exit(2)
        client = FeedClient(root=fixture_root, allow_network=False)
    result = watch_outside(root, client=client, service=service)
    if out is not None:
        dest = out.expanduser().resolve()
        dest.mkdir(parents=True, exist_ok=True)
        for packet in result.packets:
            name = str(packet.get("packet_id") or "packet") + ".json"
            (dest / name).write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
    if json_out:
        console.print_json(data=result.to_dict(), highlight=False)
    else:
        if not result.packets and not result.impacts:
            console.print("No released changes hit call sites on this map.")
        for packet in result.packets:
            console.print(
                f"{packet.get('package')} {packet.get('from_version')} -> "
                f"{packet.get('to_version')}  ({packet.get('packet_id')})"
            )
            for rule in packet.get("rules") or []:
                if not isinstance(rule, dict):
                    continue
                if rule.get("type") == "DEPENDENCY_BUMP":
                    detail = f"{rule.get('from_version')} -> {rule.get('to_version')}"
                elif rule.get("type") == "AST_CALL_REWRITE":
                    detail = f"{rule.get('old_callee')} -> {rule.get('new_callee')}"
                elif rule.get("type") == "AST_PARAM_RENAME":
                    detail = f"{rule.get('old_param')} -> {rule.get('new_param')}"
                else:
                    detail = rule.get("type")
                console.print(f"  {rule.get('type')}: {detail}")
        if result.impacts:
            from conduit.impact.model import Impact, SiteHit
            from conduit.impact.report import format_impacts

            rendered = [
                Impact(
                    dependency=str(item.get("dependency") or ""),
                    from_version=str(item.get("from_version") or ""),
                    to_version=str(item.get("to_version") or ""),
                    change=str(item.get("change") or ""),
                    confidence=str(item.get("confidence") or ""),
                    source=str(item.get("source") or ""),
                    kind=str(item.get("kind") or ""),
                    sites=tuple(
                        SiteHit(
                            path=str(site.get("path") or ""),
                            line=int(site.get("line") or 0),
                            expr=str(site.get("expr") or ""),
                            confidence=str(site.get("confidence") or ""),
                            via_wrapper=bool(site.get("via_wrapper")),
                        )
                        for site in (item.get("sites") or [])
                        if isinstance(site, dict)
                    ),
                )
                for item in result.impacts
                if isinstance(item, dict)
            ]
            console.print(
                format_impacts(rendered, show_callers=show_callers),
                markup=False,
                highlight=False,
                soft_wrap=True,
                end="",
            )
        for item in result.skipped:
            console.print(f"[yellow]skipped:[/yellow] {item}")
        for warning in result.warnings:
            console.print(f"[yellow]Warning:[/yellow] {warning}")
    raise typer.Exit(0)


@app.command("watch")
def watch_cmd(
    path: Path = typer.Option(Path("."), "--path"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable packets"),
    show_callers: bool = typer.Option(
        False,
        "--show-callers",
        help="List wrapper callers under each breaking SDK line. Default is a count.",
    ),
    feeds: Optional[Path] = typer.Option(
        None,
        "--feeds",
        help="Recorded copies of public feeds (offline). Omit to fetch those URLs.",
    ),
    out: Optional[Path] = typer.Option(
        None,
        "--out",
        help="Write each feed packet as JSON into this directory.",
    ),
    service: Optional[str] = typer.Option(
        None,
        "--service",
        help="Limit feed watch to one outside service.",
    ),
) -> None:
    """Watch outside services on the map.

    Diff the pinned client against the newer surface: old and new wheels
    (removed, renamed, and changed-signature functions, and return types),
    service-model documents, and webhook schemas. Release notes are extra
    hints. A version bump with no call-site hit is not a report. Internal
    modules are skipped.
    """
    root = _resolve_root(path)
    _watch_feeds(
        root,
        json_out=json_out,
        feeds=feeds,
        out=out,
        service=service,
        show_callers=show_callers,
    )


@app.command("map")
def map_cmd(
    path: Path = typer.Option(Path("."), "--path", help="Repo root to trace"),
    service: Optional[str] = typer.Option(
        None,
        "--service",
        help="Limit to one outside service or module",
    ),
    packet: Optional[Path] = typer.Option(
        None,
        "--packet",
        help="Hop document; adds blast radius for that change",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable service map"),
    markdown: bool = typer.Option(
        False, "--markdown", help="Markdown report instead of the terminal report"
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Skip the on-disk analysis cache (wheels, diffs, types, classifier).",
    ),
) -> None:
    """Trace outside services inward through wrappers, aliases, and re-exports.

    API clients and REST hosts, database drivers, cloud calls (service and
    operation), and incoming webhook handlers. Read-only. Python only. With
    --packet, each chain lists what that hop would break and a confidence.
    """
    root = _resolve_root(path)
    data: dict | None = None
    if packet is not None:
        packet_file = packet.expanduser().resolve()
        if not packet_file.is_file():
            console.print(f"[red]Hop document not found:[/red] {packet_file}")
            raise typer.Exit(2)
        data = _load_hop(packet_file)

    from contextlib import nullcontext

    from conduit.analysis_cache import without_cache
    from conduit.outside_in.bridge import build_service_map
    from conduit.outside_in.report import format_report, service_map_to_dict

    with without_cache() if no_cache else nullcontext():
        service_map, blast = build_service_map(root, service=service, packet=data)
        if json_out:
            console.print_json(data=service_map_to_dict(service_map, blast), highlight=False)
        else:
            console.print(
                format_report(service_map, blast, markdown=markdown),
                markup=False,
                highlight=False,
                soft_wrap=True,
                end="",
            )


@app.command("apply")
def apply_cmd(
    path: Path = typer.Option(Path("."), "--path", help="Repo root to fix"),
    impact: Path = typer.Option(
        ...,
        "--impact",
        help="JSON from `conduit impact --json` (call sites + evidence)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print context packs only; do not call the model",
    ),
    write: bool = typer.Option(
        False,
        "--write",
        help="Write verified fixes into the working tree",
    ),
    test_cmd: Optional[str] = typer.Option(
        None,
        "--test-cmd",
        help="Shell command to verify a fix in a scratch copy of the tree",
    ),
    fixtures: Optional[Path] = typer.Option(
        None,
        "--fixtures",
        help="Recorded wheels/specs for the post-patch impact re-check (offline).",
    ),
    base_url: Optional[str] = typer.Option(
        None,
        "--base-url",
        help="Token Factory chat-completions base URL (or CONDUIT_APPLY_BASE_URL)",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        help="Large model id for proposing diffs (or CONDUIT_APPLY_MODEL)",
    ),
    model_small: Optional[str] = typer.Option(
        None,
        "--model-small",
        help="Small model id for summaries (or CONDUIT_APPLY_MODEL_SMALL)",
    ),
    enable_thinking: Optional[bool] = typer.Option(
        None,
        "--enable-thinking/--no-enable-thinking",
        help="Propose model: chat_template_kwargs.enable_thinking "
        "(default off; or CONDUIT_APPLY_ENABLE_THINKING)",
    ),
    enable_thinking_small: Optional[bool] = typer.Option(
        None,
        "--enable-thinking-small/--no-enable-thinking-small",
        help="Summarize model: chat_template_kwargs.enable_thinking "
        "(default off; or CONDUIT_APPLY_ENABLE_THINKING_SMALL)",
    ),
    max_tokens_per_site: Optional[int] = typer.Option(
        None,
        "--max-tokens-per-site",
        help="Cost guard: max tokens spent per call site",
    ),
    max_attempts: Optional[int] = typer.Option(
        None,
        "--max-attempts",
        help="Retries per site after verification failure (default 3)",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable report"),
) -> None:
    """Fix impact call sites with a Token Factory chat-completions model.

    Reads existing impact JSON (no re-scan). Builds a context pack per site,
    asks the model for a function-scoped unified diff, verifies the patch
    (parse, impact re-check on a scratch copy, optional tests), and optionally
    writes verified fixes. Never prints API keys. Does not open a PR (use a
    later run for ``--pr``).
    """
    root = _resolve_root(path)
    impact_path = impact.expanduser().resolve()
    if not impact_path.is_file():
        console.print(f"[red]Impact JSON not found:[/red] {impact_path}")
        raise typer.Exit(2)
    if dry_run and write:
        console.print("[red]Use either --dry-run or --write, not both.[/red]")
        raise typer.Exit(2)

    from conduit.apply.config import TokenFactorySettings
    from conduit.apply.engine import ApplyConfig, apply_sites
    from conduit.apply.impact_input import load_impact_sites
    from conduit.apply.report import format_report

    try:
        sites = load_impact_sites(impact_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        console.print(f"[red]Could not read impact JSON:[/red] {exc}")
        raise typer.Exit(2) from exc

    fixture_root: Path | None = None
    if fixtures is not None:
        fixture_root = fixtures.expanduser().resolve()
        if not fixture_root.is_dir():
            console.print(f"[red]Fixtures directory not found:[/red] {fixture_root}")
            raise typer.Exit(2)

    settings = TokenFactorySettings.from_env(
        base_url=base_url,
        model=model,
        model_small=model_small,
        max_tokens_per_site=max_tokens_per_site,
        max_attempts=max_attempts,
        enable_thinking=enable_thinking,
        enable_thinking_small=enable_thinking_small,
    )
    report = apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=settings,
            dry_run=dry_run,
            write=write,
            test_cmd=test_cmd,
            recheck_impact=True,
            fixtures=fixture_root,
            allow_network=fixture_root is None,
        ),
    )
    if json_out:
        payload = report.to_dict()
        if dry_run:
            payload["context_packs"] = [
                item.context.to_dict()
                for item in report.results
                if item.context is not None
            ]
        console.print_json(data=payload, highlight=False)
    else:
        console.print(
            format_report(report, include_context=dry_run),
            markup=False,
            highlight=False,
            soft_wrap=True,
            end="",
        )
    needs_human = any(
        item.status.value == "needs-human" for item in report.results
    )
    raise typer.Exit(1 if needs_human and not dry_run else 0)


@app.command("impact")
def impact_cmd(
    path: Path = typer.Option(Path("."), "--path", help="Repo to check"),
    dep: str = typer.Option(
        ...,
        "--dep",
        help="Outside service or its client package",
    ),
    to: Optional[str] = typer.Option(
        None,
        "--to",
        help="Newer released version. Default is the recorded surface for this client.",
    ),
    from_version: Optional[str] = typer.Option(
        None,
        "--from",
        help="Version to compare from when the repo has no pin, or the pin does not match a recorded surface.",
    ),
    fixtures: Optional[Path] = typer.Option(
        None,
        "--fixtures",
        help="Recorded wheels, service models, and webhook schemas (offline).",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable impact"),
    show_callers: bool = typer.Option(
        False,
        "--show-callers",
        help="List wrapper callers under each breaking SDK line. Default is a count.",
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Skip the on-disk analysis cache (wheels, diffs, types, classifier).",
    ),
) -> None:
    """Show which lines break when an outside service or its client changes.

    Diffs the pin (or ``--from``) against ``--to`` or the newer recorded
    surface: public API of the two wheels, including return types, service
    models, and a webhook schema. Prints each file:line. The breaking SDK
    line is the finding; wrapper callers collapse to a count unless
    ``--show-callers`` is set. A version comes from ``--from``, an exact pin,
    a lockfile, the installed distribution, or the highest release matching a
    requirement range. If none of those exist, the note says ``not compared``
    and why.
    """
    root = _resolve_root(path)
    from contextlib import nullcontext

    from conduit.analysis_cache import without_cache

    with without_cache() if no_cache else nullcontext():
        _impact_body(
            root=root,
            dep=dep,
            to=to,
            from_version=from_version,
            fixtures=fixtures,
            json_out=json_out,
            show_callers=show_callers,
        )


def _impact_body(
    *,
    root: Path,
    dep: str,
    to: Optional[str],
    from_version: Optional[str],
    fixtures: Optional[Path],
    json_out: bool,
    show_callers: bool,
) -> None:
    from conduit.impact.report import format_impacts, impacts_to_dicts
    from conduit.impact.run import collect_impacts
    from conduit.outside_in.bridge import build_service_map
    from conduit.outside_in.specs import evidence_spec

    from conduit.deps.manifests import read_installed

    pins = read_installed(root)
    pinned = (from_version or "").strip() or pins.get(dep.lower()) or ""
    # The distribution under analysis is the one this repo pins, not whichever
    # release is newest. A newer release that grew its own I/O does not make
    # the pinned release a service, and the reverse is true too.
    spec = evidence_spec(dep, pinned or None)
    if spec is None or spec.family:
        console.print(
            f"[red]{dep} is not an outside service.[/red] "
            "Impact covers API clients, database drivers, cloud SDKs, and incoming webhooks."
        )
        raise typer.Exit(2)
    fixture_root: Path | None = None
    if fixtures is not None:
        fixture_root = fixtures.expanduser().resolve()
        if not fixture_root.is_dir():
            console.print(f"[red]Fixtures directory not found:[/red] {fixture_root}")
            raise typer.Exit(2)
    service_map, _blast = build_service_map(root)
    impacts, diffed, notes = collect_impacts(
        root,
        service_map,
        fixtures=fixture_root,
        dep=dep,
        to_version=to,
        from_version=from_version,
        allow_network=fixture_root is None,
    )
    if json_out:
        breaks = [item for item in impacts if item.kind != "deprecated"]
        deprecations = [item for item in impacts if item.kind == "deprecated"]
        console.print_json(
            data={
                "dependency": dep,
                "impacts": impacts_to_dicts(breaks),
                "deprecations": impacts_to_dicts(deprecations),
                "diffed": sorted(diffed),
                "notes": notes,
            },
            highlight=False,
        )
    else:
        if notes and not impacts:
            text = "\n".join(notes) + "\n"
        else:
            text = format_impacts(impacts, show_callers=show_callers)
            if notes:
                text = "\n".join(notes) + "\n" + text
        console.print(
            text,
            markup=False,
            highlight=False,
            soft_wrap=True,
            end="",
        )
    raise typer.Exit(0)


if __name__ == "__main__":
    app()
