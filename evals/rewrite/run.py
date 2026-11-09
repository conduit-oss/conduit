#!/usr/bin/env python3
"""Minimum viable rewrite eval runner for ``conduit apply``.

Loads labeled cases from ``catalog.json``, runs each through ``apply_sites``
with recorded (fake) model responses by default, and grades with code checks
against the expected answers written before any model run.

Usage:
  python evals/rewrite/run.py --smoke
  python evals/rewrite/run.py --full
  python evals/rewrite/run.py --case typical-rename-verified
  python evals/rewrite/run.py --smoke --json-out /tmp/rewrite-eval.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = Path(__file__).resolve().parent / "catalog.json"
if str(ROOT / "conduit" / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "conduit" / "src"))

from conduit.apply.client import FakeModelClient  # noqa: E402
from conduit.apply.config import TokenFactorySettings  # noqa: E402
from conduit.apply.engine import ApplyConfig, apply_sites  # noqa: E402
from conduit.apply.impact_input import sites_from_payload  # noqa: E402
from conduit.apply.models import SiteStatus  # noqa: E402
from conduit.apply.testrun import record_baseline  # noqa: E402

_EXERCISE_SCRIPT = textwrap.dedent(
    """\
    import importlib, inspect, pathlib, sys
    sys.path.insert(0, ".")
    class Stub:
        def __getattr__(self, name):
            return Stub()
        def __call__(self, *args, **kwargs):
            return Stub()
        def __getitem__(self, key):
            return Stub()
        def __iter__(self):
            return iter([Stub()])
    ran = 0
    for file in sorted(pathlib.Path(".").glob("*.py")):
        module = None
        for _ in range(5):
            try:
                module = importlib.import_module(file.stem)
                break
            except ModuleNotFoundError as exc:
                sys.modules[exc.name] = Stub()
            except Exception:
                break
        if module is None:
            continue
        for name, func in vars(module).items():
            if inspect.isfunction(func) and func.__module__ == module.__name__:
                try:
                    func(*[Stub() for _ in inspect.signature(func).parameters])
                except Exception:
                    pass
                ran += 1
    print(f"{ran} passed")
    """
)
_EXERCISE = "python3 -c " + __import__("shlex").quote(_EXERCISE_SCRIPT)


@dataclass
class CaseResult:
    case_id: str
    category: str
    passed: bool
    expected_statuses: list[str]
    actual_statuses: list[str]
    false_keep: bool
    tokens_used: int
    elapsed_seconds: float
    errors: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _settings(**kwargs) -> TokenFactorySettings:
    return TokenFactorySettings.from_env(api_key="eval-key-not-for-logging", **kwargs)


def _site_cleared(recipe: str | None) -> Callable[..., bool] | None:
    if not recipe or recipe == "null":
        return None
    if recipe == "always":
        return lambda *_a, **_k: True
    if recipe == "never":
        return lambda *_a, **_k: False
    if recipe == "new_without_old":
        return lambda _root, _site, patched: "old_call" not in patched and "new_call" in patched
    if recipe == "statement_without_object_":
        # Ignore unrelated `object_=` names in helper/stub signatures; only the
        # call site keyword must move to `statement=`.
        return lambda _root, _site, patched: (
            "scalar(object_=" not in patched and "scalar(statement=" in patched
        )
    if recipe == "path_left_only":
        return lambda root, site, patched: (
            site.path.endswith("left.py")
            and "old_call" not in patched
            and "new_call" in patched
        )
    raise ValueError(f"unknown site_cleared recipe: {recipe}")


def _resolve_test_cmd(value: str | None) -> str | None:
    if value is None:
        return None
    if value == "exercise":
        return _EXERCISE
    return value


def _materialize(case: dict[str, Any], dest: Path) -> None:
    for rel, body in (case.get("files") or {}).items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def _is_fixed_keep(status: str) -> bool:
    return status.startswith("fixed")


def grade_case(case: dict[str, Any], report, client: FakeModelClient, root: Path) -> CaseResult:
    expected = case.get("expected") or {}
    actual = [item.status.value for item in report.results]
    errors: list[str] = []

    if "statuses" in expected:
        want = list(expected["statuses"])
        if actual != want and sorted(actual) != sorted(want):
            errors.append(f"statuses: expected {want}, got {actual}")

    if "status_by_line" in expected:
        by_line = {str(item.site.line): item.status.value for item in report.results}
        for line, status in expected["status_by_line"].items():
            if by_line.get(str(line)) != status:
                errors.append(
                    f"line {line}: expected {status}, got {by_line.get(str(line))}"
                )

    if "status_by_path" in expected:
        by_path = {item.site.path: item.status.value for item in report.results}
        for path, status in expected["status_by_path"].items():
            if by_path.get(path) != status:
                errors.append(
                    f"path {path}: expected {status}, got {by_path.get(path)}"
                )

    if expected.get("model_calls") is not None and len(client.calls) != expected["model_calls"]:
        errors.append(f"model_calls: expected {expected['model_calls']}, got {len(client.calls)}")

    if expected.get("min_attempts") is not None:
        attempts = max((item.attempts for item in report.results), default=0)
        if attempts < int(expected["min_attempts"]):
            errors.append(f"min_attempts: expected >= {expected['min_attempts']}, got {attempts}")

    if expected.get("max_attempts_seen") is not None:
        attempts = max((item.attempts for item in report.results), default=0)
        if attempts > int(expected["max_attempts_seen"]):
            errors.append(
                f"max_attempts_seen: expected <= {expected['max_attempts_seen']}, got {attempts}"
            )

    for path, needles in (expected.get("file_must_contain") or {}).items():
        text = (root / path).read_text(encoding="utf-8")
        for needle in needles:
            if needle not in text:
                errors.append(f"{path} missing {needle!r}")

    for path, needles in (expected.get("file_must_not_contain") or {}).items():
        text = (root / path).read_text(encoding="utf-8")
        for needle in needles:
            if needle in text:
                errors.append(f"{path} still contains {needle!r}")

    if expected.get("why_must_contain"):
        why = " ".join(item.why or "" for item in report.results)
        for needle in expected["why_must_contain"]:
            if needle not in why:
                errors.append(f"why missing {needle!r}")

    if expected.get("context_must_contain"):
        pack = report.results[0].context if report.results else None
        blob = "" if pack is None else (pack.function_source or "")
        for needle in expected["context_must_contain"]:
            if needle not in blob:
                errors.append(f"context missing {needle!r}")

    if expected.get("release_diff_old") or expected.get("release_diff_new"):
        pack = report.results[0].context if report.results else None
        release = {} if pack is None else (pack.release_diff or {})
        if expected.get("release_diff_old") and release.get("old") != expected["release_diff_old"]:
            errors.append(
                f"release_diff.old: expected {expected['release_diff_old']!r}, got {release.get('old')!r}"
            )
        if expected.get("release_diff_new") and release.get("new") != expected["release_diff_new"]:
            errors.append(
                f"release_diff.new: expected {expected['release_diff_new']!r}, got {release.get('new')!r}"
            )

    # False keep: any fixed* status when the labeled expectation forbids keeps.
    expected_fixed = any(_is_fixed_keep(s) for s in expected.get("statuses") or [])
    if "status_by_path" in expected:
        expected_fixed = expected_fixed or any(
            _is_fixed_keep(s) for s in expected["status_by_path"].values()
        )
    if "status_by_line" in expected:
        expected_fixed = expected_fixed or any(
            _is_fixed_keep(s) for s in expected["status_by_line"].values()
        )
    actual_fixed = any(_is_fixed_keep(s) for s in actual)
    false_keep = bool(actual_fixed and not expected_fixed)
    if expected.get("false_keep") is False and false_keep:
        errors.append("false keep: kept a fix when the label expected needs-human/skip/dry-run")
    if false_keep and expected.get("false_keep") is not True:
        # Always surface false keeps as failures unless the case explicitly allows them.
        if "false keep:" not in " ".join(errors):
            errors.append("false keep: unexpected fixed status")

    return CaseResult(
        case_id=case["id"],
        category=case.get("category") or "unknown",
        passed=not errors,
        expected_statuses=list(expected.get("statuses") or []),
        actual_statuses=actual,
        false_keep=false_keep,
        tokens_used=int(report.tokens_used or 0),
        elapsed_seconds=0.0,
        errors=errors,
        details={
            "summary": report.to_dict().get("summary"),
            "attempts": [item.attempts for item in report.results],
            "model_calls": len(client.calls),
        },
    )


def run_case(case: dict[str, Any]) -> CaseResult:
    mode = case.get("mode") or "recorded"
    if mode != "recorded":
        return CaseResult(
            case_id=case["id"],
            category=case.get("category") or "unknown",
            passed=False,
            expected_statuses=list((case.get("expected") or {}).get("statuses") or []),
            actual_statuses=[],
            false_keep=False,
            tokens_used=0,
            elapsed_seconds=0.0,
            errors=[f"unsupported mode {mode!r}; MVP runner is recorded-only"],
        )

    started = time.perf_counter()
    tmp = Path(tempfile.mkdtemp(prefix=f"conduit-rewrite-eval-{case['id']}-"))
    try:
        root = tmp / "app"
        root.mkdir()
        _materialize(case, root)
        sites = sites_from_payload(case["impact"])
        responses = list(case.get("responses") or [])
        client = FakeModelClient(responses=responses)
        test_cmd = _resolve_test_cmd(case.get("test_cmd"))
        baseline = None
        if case.get("record_baseline") and test_cmd:
            baseline = record_baseline(test_cmd, root)
        settings_kwargs: dict[str, Any] = {}
        if case.get("max_attempts") is not None:
            settings_kwargs["max_attempts"] = int(case["max_attempts"])
        config = ApplyConfig(
            root=root,
            settings=_settings(**settings_kwargs),
            dry_run=bool(case.get("dry_run")),
            write=bool(case.get("write")),
            test_cmd=test_cmd,
            client=client,
            site_cleared=_site_cleared(case.get("site_cleared")),
            baseline=baseline,
        )
        report = apply_sites(sites, config)
        result = grade_case(case, report, client, root)
        result.elapsed_seconds = round(time.perf_counter() - started, 3)
        return result
    except Exception as exc:  # noqa: BLE001 - surface as a failed case
        return CaseResult(
            case_id=case["id"],
            category=case.get("category") or "unknown",
            passed=False,
            expected_statuses=list((case.get("expected") or {}).get("statuses") or []),
            actual_statuses=[],
            false_keep=False,
            tokens_used=0,
            elapsed_seconds=round(time.perf_counter() - started, 3),
            errors=[f"runner exception: {type(exc).__name__}: {exc}"],
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def select_cases(catalog: dict[str, Any], *, smoke: bool, case_id: str | None, category: str | None) -> list[dict]:
    cases = list(catalog["cases"])
    if case_id:
        found = [c for c in cases if c["id"] == case_id]
        if not found:
            raise SystemExit(f"unknown case id: {case_id}")
        return found
    if category:
        cases = [c for c in cases if c.get("category") == category]
    if smoke:
        smoke_ids = set(catalog.get("smoke_case_ids") or [c["id"] for c in cases if c.get("smoke")])
        cases = [c for c in cases if c["id"] in smoke_ids]
    return cases


def summarize(results: list[CaseResult], catalog: dict[str, Any], *, smoke: bool) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    false_keeps = sum(1 for r in results if r.false_keep)
    accuracy = (passed / total) if total else 0.0
    threshold = float(catalog["smoke_threshold"] if smoke else catalog["accuracy_floor"])
    tokens = sum(r.tokens_used for r in results)
    completed = sum(1 for r in results if r.passed)
    # Cost proxy: recorded runs use FakeModelClient token estimates.
    cost_per_completed = (tokens / completed) if completed else None
    by_category: dict[str, dict[str, int]] = {}
    for result in results:
        bucket = by_category.setdefault(result.category, {"passed": 0, "total": 0})
        bucket["total"] += 1
        if result.passed:
            bucket["passed"] += 1
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "accuracy": round(accuracy, 4),
        "threshold": threshold,
        "clears_threshold": accuracy >= threshold and false_keeps <= int(catalog.get("max_false_keeps", 0)),
        "false_keeps": false_keeps,
        "max_false_keeps": int(catalog.get("max_false_keeps", 0)),
        "tokens_used": tokens,
        "cost_per_completed_task_tokens": (
            round(cost_per_completed, 2) if cost_per_completed is not None else None
        ),
        "by_category": by_category,
        "elapsed_seconds": round(sum(r.elapsed_seconds for r in results), 3),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--smoke", action="store_true", help="Run the PR smoke subset")
    group.add_argument("--full", action="store_true", help="Run every labeled case")
    parser.add_argument("--case", help="Run one case id")
    parser.add_argument("--category", help="Filter by category")
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    parser.add_argument("--json-out", type=Path, help="Write machine-readable results")
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="Run the selected set this many times (prompt-iteration stability)",
    )
    args = parser.parse_args(argv)

    catalog = load_catalog(args.catalog)
    smoke = bool(args.smoke) or not (args.full or args.case or args.category)
    if args.full:
        smoke = False
    cases = select_cases(catalog, smoke=smoke and not args.case, case_id=args.case, category=args.category)
    if not cases:
        raise SystemExit("no cases selected")

    all_results: list[CaseResult] = []
    for round_i in range(max(1, args.repeats)):
        if args.repeats > 1:
            print(f"== repeat {round_i + 1}/{args.repeats} ==")
        for case in cases:
            result = run_case(case)
            all_results.append(result)
            mark = "PASS" if result.passed else "FAIL"
            print(
                f"[{mark}] {result.case_id} ({result.category}) "
                f"statuses={result.actual_statuses} tokens={result.tokens_used} "
                f"{result.elapsed_seconds:.2f}s"
            )
            for err in result.errors:
                print(f"       - {err}")

    # For repeats, grade on the latest pass of each case id.
    if args.repeats > 1:
        latest: dict[str, CaseResult] = {}
        for result in all_results:
            latest[result.case_id] = result
        scored = list(latest.values())
    else:
        scored = all_results

    summary = summarize(scored, catalog, smoke=smoke and not args.case)
    print()
    print(
        f"accuracy {summary['accuracy']:.0%} "
        f"({summary['passed']}/{summary['total']}) "
        f"threshold {summary['threshold']:.0%} "
        f"false_keeps={summary['false_keeps']}/{summary['max_false_keeps']} "
        f"tokens={summary['tokens_used']} "
        f"cost_per_completed_task_tokens={summary['cost_per_completed_task_tokens']} "
        f"elapsed={summary['elapsed_seconds']}s"
    )
    for category, counts in sorted(summary["by_category"].items()):
        rate = counts["passed"] / counts["total"] if counts["total"] else 0
        print(f"  {category}: {counts['passed']}/{counts['total']} ({rate:.0%})")

    payload = {
        "summary": summary,
        "results": [
            {
                "id": r.case_id,
                "category": r.category,
                "passed": r.passed,
                "false_keep": r.false_keep,
                "expected_statuses": r.expected_statuses,
                "actual_statuses": r.actual_statuses,
                "tokens_used": r.tokens_used,
                "elapsed_seconds": r.elapsed_seconds,
                "errors": r.errors,
                "details": r.details,
            }
            for r in scored
        ],
        "policy": {
            "accuracy_floor": catalog.get("accuracy_floor"),
            "smoke_threshold": catalog.get("smoke_threshold"),
            "max_false_keeps": catalog.get("max_false_keeps"),
            "objective": catalog.get("objective"),
        },
        "mode": "smoke" if smoke and not args.case else "full",
    }
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.json_out}")

    return 0 if summary["clears_threshold"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
