"""Scaffold a new detect module."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

MODULE_INIT = '''"""Detect module: {name}."""

from __future__ import annotations

from conduit.detect.models import ChangeSignal
from conduit.detect.modules.base import DetectContext, DetectModule
from conduit.detect.modules.{name}.profile import PROFILE
from conduit.detect.profile_runner import run_profile_module, stock_workers_for


class {class_name}Module(DetectModule):
    name = "{name}"
    packages = {packages!r}
    profile = PROFILE

    def run(self, ctx: DetectContext) -> list[ChangeSignal]:
        return run_profile_module(
            ctx,
            profile=self.profile,
            workers=stock_workers_for(self.profile),
        )
'''

OUT_OF_TREE_INIT = '''"""Detect module: {name}."""

from __future__ import annotations

from conduit.detect.models import ChangeSignal
from conduit.detect.modules.base import DetectContext, DetectModule
from conduit.detect.profile_runner import run_profile_module, stock_workers_for

from .profile import PROFILE


class {class_name}Module(DetectModule):
    name = "{name}"
    packages = {packages!r}
    profile = PROFILE

    def run(self, ctx: DetectContext) -> list[ChangeSignal]:
        return run_profile_module(
            ctx,
            profile=self.profile,
            workers=stock_workers_for(self.profile),
        )
'''

PROFILE_PY = '''"""VendorProfile for {name} — fill remaining URLs / custom parsers as needed."""

from __future__ import annotations

from conduit.detect.vendor_profile import VendorProfile

PROFILE = VendorProfile(
    name={name!r},
    packages={packages!r},
    ecosystems={ecosystems!r},
    model_id_pattern={model_id_pattern!r},
    api_pattern={api_pattern!r},
    deprecations_url={deprecations_url!r},
    changelog_url={changelog_url!r},
    openapi_repo={openapi_repo!r},
    openapi_source_url={openapi_source_url!r},
    sdk_release_repos={sdk_release_repos!r},
    live_catalog_url={live_catalog_url!r},
    live_catalog_auth_env={live_catalog_auth_env!r},
    models_catalog_url={catalog_url!r},
    model_doc_url_template={model_doc_template!r},
    evidence_seeds={evidence_seeds!r},
    evidence_hosts={evidence_hosts!r},
    evidence_query_templates=[
        "{{package}} python SDK migration {{from_version}} to {{to_version}}",
        "{{package}} API deprecations endpoint replacement",
    ],
    fixtures_name={name!r},
    parser_kind="generic",
    demo_packet_fallback=False,
)
'''

MODULE_MD = """# {name} detect module

This module is **profile-driven**. OpenAI is the reference (`conduit.detect.modules.openai.profile.OPENAI_PROFILE`).

## What was generated

- `profile.py` — URLs, scan patterns, evidence hosts
- `run()` calls `run_profile_module` with `stock_workers_for(PROFILE)`
  - OpenAPI diff if `openapi_repo` is set
  - Live catalog polling if `live_catalog_url` is set
  - SDK release stepping if `sdk_release_repos` is set
  - OpenAI HTML deprecation/changelog scrapers are **not** enabled (`parser_kind=generic`)

## Checklist

1. Confirm links in `profile.py`. Empty strings should be `None` if unused.
2. **Custom parsers:** deprecation pages and changelogs almost always need a vendor-specific worker. Add it under `workers/` and include it in `run()`'s worker list. Do not invent path/call successors.
3. Fill `path_to_callees` / `api_pattern_to_path` if you want param-rename `function_target`s.
4. Add offline fixtures under `fixtures/{name}/` (openapi/, deprecations/, models/, sdk_releases/).
5. Register built-in in `conduit.detect.modules.discovery._builtin_modules`, **or** for an external package:

```toml
[project.entry-points."conduit.detect_modules"]
{name} = "your_package.module:{class_name}Module"
```

6. Verify: `conduit module list` then `conduit detect --module {name} --demo`
"""

TEST_STUB = '''"""Smoke test for {name} module."""

from conduit.detect.modules.{name} import {class_name}Module


def test_module_name():
    mod = {class_name}Module()
    assert mod.name == "{name}"
    assert mod.profile is not None
    assert mod.profile.name == "{name}"
'''


def _class_name(name: str) -> str:
    parts = [p for p in name.replace("-", "_").split("_") if p]
    return "".join(p[:1].upper() + p[1:] for p in parts)


def _ask(label: str, default: str = "") -> str:
    if not sys.stdin.isatty():
        return default
    try:
        import typer

        return str(typer.prompt(label, default=default or ""))
    except Exception:
        return default


def _none_if_empty(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _parse_hosts(raw: str | None) -> list[str]:
    if not raw:
        return ["github.com"]
    return [h.strip() for h in raw.replace(";", ",").split(",") if h.strip()]


def _sdk_repos(package: str, sdk_repo: str | None) -> dict[str, dict[str, Any]]:
    repo = _none_if_empty(sdk_repo)
    if not repo:
        return {}
    # Accept "org/name" or full github URL
    slug = repo
    if "github.com/" in repo:
        slug = repo.split("github.com/", 1)[1].rstrip("/").removesuffix(".git")
    return {slug: {"package": package, "ecosystems": ["pip", "pyproject"]}}


def scaffold_module(
    name: str,
    *,
    package: str | None = None,
    ecosystem: str = "pypi",
    target_root: Path,
    out_of_tree: bool = False,
    deprecations_url: str | None = None,
    changelog_url: str | None = None,
    openapi_repo: str | None = None,
    sdk_repo: str | None = None,
    catalog_url: str | None = None,
    model_doc_template: str | None = None,
    live_catalog_url: str | None = None,
    live_catalog_auth_env: str | None = None,
    evidence_hosts: str | None = None,
    interactive: bool | None = None,
) -> Path:
    """
    Create a profile-backed detect module.
    Default: under target_root/src/conduit/detect/modules/<name>/
    """
    class_name = _class_name(name)
    pkg = package or name
    packages = [pkg]
    ask = sys.stdin.isatty() if interactive is None else interactive
    if ask:
        if not package:
            pkg = _ask("PyPI/npm package name", pkg) or pkg
            packages = [pkg]
        deprecations_url = deprecations_url or _ask("Deprecations URL (blank to skip)", "")
        changelog_url = changelog_url or _ask("Changelog URL (blank to skip)", "")
        openapi_repo = openapi_repo or _ask("OpenAPI git repo URL (blank to skip)", "")
        sdk_repo = sdk_repo or _ask("SDK GitHub repo org/name (blank to skip)", "")
        catalog_url = catalog_url or _ask("Models/catalog URL (blank to skip)", "")
        model_doc_template = model_doc_template or _ask(
            "Per-id doc URL template with {model_id} (blank to skip)", ""
        )
        live_catalog_url = live_catalog_url or _ask("Live list API URL (blank to skip)", "")
        live_catalog_auth_env = live_catalog_auth_env or _ask(
            "Live list API key env var (blank to skip)", ""
        )
        evidence_hosts = evidence_hosts or _ask(
            "Evidence hosts (comma-separated)", "github.com"
        )

    dep = _none_if_empty(deprecations_url)
    change = _none_if_empty(changelog_url)
    oas = _none_if_empty(openapi_repo)
    catalog = _none_if_empty(catalog_url)
    doc_tmpl = _none_if_empty(model_doc_template)
    live = _none_if_empty(live_catalog_url)
    auth_env = _none_if_empty(live_catalog_auth_env)
    hosts = _parse_hosts(evidence_hosts)
    seeds = [u for u in (dep, change, catalog, oas) if u]
    repos = _sdk_repos(pkg, sdk_repo)

    ctx = dict(
        name=name,
        class_name=class_name,
        packages=packages,
        ecosystems=[ecosystem],
        model_id_pattern=None,
        api_pattern=None,
        deprecations_url=dep,
        changelog_url=change,
        openapi_repo=oas,
        openapi_source_url=oas,
        sdk_release_repos=repos,
        live_catalog_url=live,
        live_catalog_auth_env=auth_env,
        catalog_url=catalog,
        model_doc_template=doc_tmpl,
        evidence_seeds=seeds,
        evidence_hosts=hosts,
    )

    if out_of_tree:
        mod_dir = target_root / name
    else:
        mod_dir = target_root / "src" / "conduit" / "detect" / "modules" / name
    mod_dir.mkdir(parents=True, exist_ok=True)
    (mod_dir / "workers").mkdir(exist_ok=True)
    (mod_dir / "workers" / ".gitkeep").write_text("", encoding="utf-8")
    init_src = OUT_OF_TREE_INIT if out_of_tree else MODULE_INIT
    (mod_dir / "__init__.py").write_text(
        init_src.format(name=name, class_name=class_name, packages=packages),
        encoding="utf-8",
    )
    (mod_dir / "profile.py").write_text(PROFILE_PY.format(**ctx), encoding="utf-8")
    (mod_dir / "MODULE.md").write_text(
        MODULE_MD.format(name=name, class_name=class_name),
        encoding="utf-8",
    )
    fixtures = target_root / "fixtures" / name
    if not out_of_tree:
        fixtures.mkdir(parents=True, exist_ok=True)
        (fixtures / ".gitkeep").write_text("", encoding="utf-8")
        tests_dir = target_root / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / f"test_module_{name}.py").write_text(
            TEST_STUB.format(name=name, class_name=class_name),
            encoding="utf-8",
        )
    return mod_dir
