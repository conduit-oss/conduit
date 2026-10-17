"""Re-record offline watch fixtures from public releases.

The files under ``conduit/fixtures/feeds`` and ``conduit/fixtures/impact`` for
the pinned outside services on a repo become a snapshot of a live read:
project pages, release feeds, changelogs, the pinned and latest wheels, and
the versioned spec members those wheels (and their closed-pin dependencies)
actually ship. A package the public index does not know is skipped.

From the repo root:

    python scripts/record_fixtures.py --path examples/service-clients
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "conduit" / "src"))

from conduit.feeds.http import cached_get  # noqa: E402
from conduit.feeds.lookup import discover_live_feeds  # noqa: E402
from conduit.impact.run import _download_wheel  # noqa: E402
from conduit.impact.shipped_specs import (  # noqa: E402
    called_service_names,
    closed_pins,
    read_wheel_metadata,
    write_spec_snapshot,
)
from conduit.impact.versions import pypi_latest, wheel_backed_version  # noqa: E402
from conduit.outside_in.analyze import analyze_repo  # noqa: E402
from conduit.outside_in.specs import evidence_spec, normalize_dist  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=ROOT / "examples" / "service-clients")
    parser.add_argument("--feeds", type=Path, default=ROOT / "conduit" / "fixtures" / "feeds")
    parser.add_argument("--impact", type=Path, default=ROOT / "conduit" / "fixtures" / "impact")
    args = parser.parse_args()
    repo = args.path.resolve()
    feeds_dir = args.feeds.resolve()
    impact_dir = args.impact.resolve()
    service_map = analyze_repo(repo)
    pins = _exact_pins(repo)
    feed_index = _load_json(feeds_dir / "index.json")
    files = feed_index.setdefault("files", {})
    if not isinstance(files, dict):
        raise SystemExit("feed index has no files object")
    impact_packages: dict[str, dict] = {}
    recorded: list[str] = []
    for service_id in service_map.services():
        spec = evidence_spec(service_id, pins.get(service_id.lower()), root=repo)
        package = normalize_dist((spec.package if spec is not None else "") or service_id)
        pin = pins.get(package) or pins.get(service_id.lower()) or ""
        if not pin:
            _drop_package_urls(files, package)
            print(f"skip {package}: no exact pin")
            continue
        latest = ""
        try:
            advertised = pypi_latest(package)
            latest = wheel_backed_version(package, advertised) if advertised else ""
        except Exception as exc:
            print(f"skip {package}: project metadata failed ({exc})")
            _drop_package_urls(files, package)
            continue
        if not latest:
            print(f"skip {package}: not on the public index")
            _drop_package_urls(files, package)
            continue
        print(f"record {package} {pin} -> {latest}")
        discovered = discover_live_feeds(package, service_id, from_version=pin)
        _drop_package_urls(files, package)
        for url in _feed_urls(discovered):
            body, reason = cached_get(url)
            if not body:
                print(f"  miss {url} ({reason})")
                continue
            rel = _feed_rel(url, package)
            dest = feeds_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(body, encoding="utf-8")
            files[url] = rel
            print(f"  feed {rel}")
        old = _download_wheel(package, pin)
        new = _download_wheel(package, latest)
        if old is None or new is None:
            print(f"  wheels missing for {pin} or {latest}")
            continue
        wheel_rels = {
            pin: _copy_wheel(old, impact_dir / "wheels" / old.name),
            latest: _copy_wheel(new, impact_dir / "wheels" / new.name),
        }
        services = called_service_names(service_map.chains, package)
        for client in (old, new):
            for dep_name, dep_version in closed_pins(read_wheel_metadata(client)):
                dep = _download_wheel(dep_name, dep_version, max_bytes=32_000_000)
                if dep is None:
                    print(f"  dependency wheel missing {dep_name} {dep_version}")
                    continue
                dest = impact_dir / "wheels" / "deps" / dep.name
                count = write_spec_snapshot(dep, dest, services)
                if count:
                    print(f"  specs {dep_name} {dep_version}: {count} member(s)")
        impact_packages[package] = {
            "from": pin,
            "to": latest,
            "wheels": wheel_rels,
            "recorded": True,
        }
        recorded.append(package)
    _drop_unknown_projects(files)
    (feeds_dir / "index.json").write_text(json.dumps({"files": files}, indent=2) + "\n", encoding="utf-8")
    (impact_dir / "index.json").write_text(
        json.dumps({"packages": impact_packages}, indent=2) + "\n",
        encoding="utf-8",
    )
    _delete_unreferenced_feeds(feeds_dir, files)
    print("recorded:", ", ".join(recorded) or "(none)")
    return 0


def _exact_pins(repo: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in list(repo.rglob("requirements*.txt")) + list(repo.rglob("pyproject.toml")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            body = line.split("#", 1)[0].strip().strip("\"'")
            if "==" not in body:
                continue
            name, _, version = body.partition("==")
            name = name.strip().strip("\"'[").split("[", 1)[0]
            version = version.strip().strip("\"',")
            if name and version and " " not in name:
                found[normalize_dist(name)] = version
    return found


def _feed_urls(feeds) -> list[str]:
    urls = [
        feeds.pypi_url(),
        feeds.github_atom_url(),
        feeds.npm_url(),
        feeds.changelog_url,
        feeds.openapi_previous_url,
        feeds.openapi_latest_url,
    ]
    return [url for url in urls if url]


def _feed_rel(url: str, package: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    suffix = Path(parsed.path).suffix or ".txt"
    safe = package.replace("/", "-")
    if host == "pypi.org":
        return f"pypi/{safe}.json"
    if "npmjs.org" in host:
        return f"npm/{safe}.json"
    if host == "github.com" and parsed.path.endswith("/releases.atom"):
        return f"github/{safe}-releases.atom"
    if "openapi" in url.lower() or suffix in {".yaml", ".yml"}:
        kind = "previous" if "previous" in url.lower() else "latest"
        return f"openapi/{safe}-{kind}{suffix}"
    return f"changelog/{safe}{suffix if suffix != '.txt' else '.txt'}"


def _copy_wheel(source: Path, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(source.read_bytes())
    return dest.relative_to(dest.parents[1]).as_posix()


def _drop_unknown_projects(files: dict) -> None:
    """Drop fixture URLs whose PyPI project page does not exist."""
    from urllib.parse import urlparse

    for url in list(files):
        parsed = urlparse(url)
        if (parsed.hostname or "").lower() != "pypi.org":
            continue
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2 or parts[0] != "pypi":
            continue
        body, reason = cached_get(url)
        if body is None and "404" in (reason or ""):
            print(f"drop unknown project {parts[1]}")
            _drop_package_urls(files, parts[1])


def _drop_package_urls(files: dict, package: str) -> None:
    token = package.lower()
    for url in list(files):
        if token and token in url.lower():
            del files[url]


def _delete_unreferenced_feeds(feeds_dir: Path, files: dict) -> None:
    keep = {str(path) for path in files.values()}
    keep.add("index.json")
    for path in feeds_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(feeds_dir).as_posix()
        if rel not in keep:
            path.unlink()
            print(f"drop {rel}")


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


if __name__ == "__main__":
    raise SystemExit(main())
