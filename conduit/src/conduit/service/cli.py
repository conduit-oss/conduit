"""``conduit service`` — stored maps and a scheduled watch pass."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from conduit.feeds.client import FeedClient
from conduit.service.worker import add_customer, tick

service_app = typer.Typer(
    help="Keep a customer's map, watch its outside services, and open a local fix PR",
    add_completion=False,
)
console = Console()


def install(parent: typer.Typer) -> None:
    parent.add_typer(service_app, name="service")


def _client(feeds: Optional[Path]) -> FeedClient:
    if feeds is None:
        return FeedClient(allow_network=True)
    root = feeds.expanduser().resolve()
    if not root.is_dir():
        console.print(f"[red]Feeds directory not found:[/red] {root}")
        raise typer.Exit(2)
    return FeedClient(root=root, allow_network=False)


@service_app.command("add")
def add_cmd(
    customer: str = typer.Option(..., "--customer", help="Customer id (lowercase)"),
    path: Path = typer.Option(..., "--path", help="Customer repo to map"),
    store: Path = typer.Option(Path(".conduit-service"), "--store", help="Service data directory"),
) -> None:
    """Store the outside-service map for one customer."""
    try:
        dest = add_customer(store, customer, path)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    console.print(f"Stored map for {customer} at {dest}")


@service_app.command("tick")
def tick_cmd(
    store: Path = typer.Option(Path(".conduit-service"), "--store"),
    feeds: Optional[Path] = typer.Option(
        None,
        "--feeds",
        help="Recorded feed fixtures. Omit to fetch public feeds.",
    ),
    customer: Optional[str] = typer.Option(None, "--customer"),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Write the fix into the customer repo. Default is a local dry-run.",
    ),
) -> None:
    """Run one watch pass over stored maps."""
    _report(store, feeds, customer, dry_run=not apply)


@service_app.command("run")
def run_cmd(
    store: Path = typer.Option(Path(".conduit-service"), "--store"),
    feeds: Optional[Path] = typer.Option(None, "--feeds"),
    customer: Optional[str] = typer.Option(None, "--customer"),
    interval: int = typer.Option(3600, "--interval", help="Seconds between passes"),
    once: bool = typer.Option(False, "--once", help="Run a single pass and exit"),
    apply: bool = typer.Option(False, "--apply"),
) -> None:
    """Scheduled watch. Cron can call ``tick`` instead of leaving this running."""
    if interval < 1 and not once:
        console.print("[red]--interval must be at least 1 unless --once[/red]")
        raise typer.Exit(2)
    while True:
        _report(store, feeds, customer, dry_run=not apply)
        if once:
            return
        time.sleep(interval)


def _report(
    store: Path,
    feeds: Optional[Path],
    customer: Optional[str],
    *,
    dry_run: bool,
) -> None:
    client = _client(feeds)
    try:
        results = tick(store, client=client, customer_id=customer, dry_run=dry_run)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    if not results:
        console.print("No customers stored.")
        return
    for result in results:
        if not result.events:
            console.print(f"{result.customer_id}: no new packets")
        for event in result.events:
            console.print(
                f"{result.customer_id}: {event.packet_id} "
                f"flagged {len(event.flagged)} call site(s) "
                f"apply_exit={event.apply_exit} -> {event.pr_path}"
            )
        for warning in result.warnings:
            console.print(f"[yellow]Warning:[/yellow] {warning}")
