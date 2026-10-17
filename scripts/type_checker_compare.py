#!/usr/bin/env python3
"""Compare the hand-written resolver with Pyright on the known-break repos.

The wheel diff and the impact matcher run once per pin. Each repo is analyzed
twice, and only ``CONDUIT_TYPES`` changes. Ground-truth files are not edited.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "conduit" / "src"))

import known_break_eval as scoring  # noqa: E402

from conduit.impact.run import collect_impacts, diff_wheels  # noqa: E402
from conduit.impact.versions import highest_on_pypi, lowest_on_pypi  # noqa: E402
from conduit.impact.versions import _is_unbounded_lower  # noqa: E402
from conduit.outside_in.analyze import analyze_repo, specs_for  # noqa: E402
from conduit.outside_in.specs import evidence_spec  # noqa: E402
from conduit.outside_in.typecheck import STATS, reset_stats  # noqa: E402

_LANGSERVER = Path("/tmp/pyright-spike/node_modules/pyright/dist/pyright-langserver.js")
_VENVS = Path("/tmp/venvs/pins")

GROUNDS = (
    ROOT / "docs" / "known-breaks.json",
    ROOT / "docs" / "known-breaks-fresh.json",
    ROOT / "docs" / "known-breaks-third.json",
    ROOT / "docs" / "known-breaks-fourth.json",
    ROOT / "docs" / "known-breaks-fifth.json",
)
OUT = ROOT / "docs" / "type-checker-compare.json"
_DIFFS: dict[tuple[str, str], list] = {}


class _DiffCopy(list):
    pass


def _cached_diff(old: Path, new: Path):
    key = (str(old), str(new))
    if key not in _DIFFS:
        print(f"   diff {old.name} -> {new.name}", flush=True)
        _DIFFS[key] = diff_wheels(old, new)
    found = _DIFFS[key]
    copy = _DiffCopy(found)
    for attr in ("return_classes", "surviving", "methods"):
        if hasattr(found, attr):
            setattr(copy, attr, getattr(found, attr))
    return copy


def _service_sites(service_map, dep: str) -> int:
    key = dep.lower()
    count = 0
    for chain in service_map.chains:
        if chain.via_wrapper:
            continue
        if key not in {chain.package.lower(), chain.service.lower()}:
            continue
        if chain.kind.endswith("_call") or chain.kind.endswith("_constructor"):
            count += 1
    return count


def _impacts(project: Path, dep: str, service_map):
    impacts, _diffed, notes = collect_impacts(
        project,
        service_map,
        dep=dep,
        allow_network=True,
    )
    breaks = [item for item in impacts if item.kind != "deprecated"]
    return {
        "impacts": [item.to_dict() for item in breaks],
        "notes": notes,
        "exit": 0,
    }


def _not_service(dep: str) -> dict:
    return {
        "impacts": [],
        "notes": [f"{dep} is not an outside service."],
        "exit": 2,
    }


def _pin_version(pin: str) -> str:
    """The release this repo is diffed from: exact pin, else the range rule."""
    req = Requirement(pin.split(";")[0].strip())
    spec = str(req.specifier)
    if not spec:
        return ""
    exact = [item.version for item in req.specifier if item.operator in {"==", "==="}]
    if exact:
        return exact[0]
    if _is_unbounded_lower(spec):
        return lowest_on_pypi(req.name, spec)
    return highest_on_pypi(req.name, spec)


def _venv_python(pin: str) -> str:
    version = _pin_version(pin)
    req = Requirement(pin.split(";")[0].strip())
    if not version:
        return ""
    dest = _VENVS / f"{req.name}-{version}".replace("/", "-")
    python = dest / "bin" / "python"
    if not python.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.check_call([sys.executable, "-m", "virtualenv", str(dest)])
        subprocess.check_call([str(dest / "bin" / "pip"), "install", "-q", f"{req.name}=={version}"])
    return str(python)


def _analyze(project: Path, specs, mode: str, pin: str):
    os.environ["CONDUIT_TYPES"] = mode
    if mode == "pyright":
        os.environ["CONDUIT_PYRIGHT"] = str(_LANGSERVER)
        python = _venv_python(pin)
        if python:
            os.environ["CONDUIT_PYTHON"] = python
    else:
        os.environ.pop("CONDUIT_PYTHON", None)
    reset_stats()
    mapped = analyze_repo(project, specs=specs)
    stats = dict(STATS)
    return mapped, stats


def main() -> None:
    import conduit.impact.run as run

    run.diff_wheels = _cached_diff
    only = [item for item in sys.argv[1:] if not item.startswith("-")]
    rows = []
    if OUT.is_file() and only:
        previous = json.loads(OUT.read_text(encoding="utf-8"))
        rows = [row for row in previous.get("rows", []) if not any(part in row["repo"] for part in only)]
    for ground in GROUNDS:
        document = json.loads(ground.read_text(encoding="utf-8"))
        for repo in document["repos"]:
            if only and not any(part in repo["repo"] for part in only):
                continue
            print(f"== {repo['repo']} {repo['service']}", flush=True)
            project = scoring._checkout(repo)
            spec = evidence_spec(repo["service"], root=project)
            if spec is None or spec.family:
                payload = _not_service(repo["service"])
                native_row = scoring._score(repo, payload)
                checked_row = scoring._score(repo, payload)
                native_sites = 0
                checked_sites = 0
                stats = {"queries": 0, "concrete": 0, "disabled": False}
            else:
                specs = specs_for(root=project)
                native_map, _native_stats = _analyze(project, specs, "native", repo["pin"])
                checked_map, stats = _analyze(project, specs, "pyright", repo["pin"])
                native_sites = _service_sites(native_map, repo["service"])
                checked_sites = _service_sites(checked_map, repo["service"])
                native_row = scoring._score(repo, _impacts(project, repo["service"], native_map))
                checked_row = scoring._score(repo, _impacts(project, repo["service"], checked_map))
            row = {
                "repo": repo["repo"],
                "service": repo["service"],
                "set": ground.name,
                "native_sites": native_sites,
                "pyright_sites": checked_sites,
                "pyright_queries": stats.get("queries", 0),
                "pyright_concrete": stats.get("concrete", 0),
                "pyright_disabled": bool(stats.get("disabled")),
                "native_recall": native_row["recall"],
                "native_precision": native_row["precision"],
                "pyright_recall": checked_row["recall"],
                "pyright_precision": checked_row["precision"],
                "native_caught": native_row["caught"],
                "native_missed": native_row["missed"],
                "native_false_positives": native_row["false_positives"],
                "pyright_caught": checked_row["caught"],
                "pyright_missed": checked_row["missed"],
                "pyright_false_positives": checked_row["false_positives"],
                "expected": native_row["expected"],
                "native_versions": native_row.get("versions") or "",
                "pyright_versions": checked_row.get("versions") or "",
                "status": native_row.get("status") or "",
            }
            rows.append(row)
            OUT.write_text(json.dumps({"rows": rows}, indent=2) + "\n", encoding="utf-8")
            print(
                f"   sites {native_sites} vs {checked_sites} "
                f"recall {row['native_recall']} vs {row['pyright_recall']} "
                f"precision {row['native_precision']} vs {row['pyright_precision']} "
                f"pyright {stats.get('concrete', 0)}/{stats.get('queries', 0)}",
                flush=True,
            )
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
