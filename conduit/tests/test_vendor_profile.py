"""VendorProfile + profile-backed scaffold."""

from __future__ import annotations

from pathlib import Path

from conduit.detect.modules.openai import OpenAIModule
from conduit.detect.modules.openai.profile import OPENAI_PROFILE
from conduit.detect.profile_runner import stock_workers_for
from conduit.detect.vendor_profile import profile_for_package
from conduit.scaffold.module_new import scaffold_module


def test_openai_profile_is_attached_and_discoverable():
    mod = OpenAIModule()
    assert mod.profile is OPENAI_PROFILE
    assert mod.profile.matches_package("openai")
    assert profile_for_package("openai") is not None
    assert profile_for_package("openai").name == "openai"
    seeds = mod.evidence_seeds()
    assert any("deprecations" in u for u in seeds)
    hosts = mod.evidence_hosts()
    assert "developers.openai.com" in hosts
    queries = mod.evidence_queries(from_version="0.28.1", to_version="1.0.0")
    assert queries and "0.28.1" in queries[0]


def test_stock_workers_follow_openai_profile_urls():
    names = [cls.__name__ for cls in stock_workers_for(OPENAI_PROFILE)]
    assert names == [
        "OpenAPIDiffWorker",
        "DeprecationScraperWorker",
        "ModelPollingWorker",
        "ChangelogParserWorker",
        "SDKReleaseWorker",
    ]


def test_scaffold_emits_profile_backed_module(tmp_path: Path):
    mod_dir = scaffold_module(
        "acme",
        package="acme-sdk",
        ecosystem="pypi",
        target_root=tmp_path,
        out_of_tree=True,
        deprecations_url="https://example.com/deprecations",
        openapi_repo="https://github.com/acme/openapi",
        sdk_repo="acme/acme-python",
        catalog_url="https://example.com/models.md",
        live_catalog_url="https://api.example.com/v1/models",
        live_catalog_auth_env="ACME_API_KEY",
        evidence_hosts="example.com,github.com",
        interactive=False,
    )
    assert (mod_dir / "profile.py").is_file()
    assert (mod_dir / "__init__.py").is_file()
    profile_src = (mod_dir / "profile.py").read_text(encoding="utf-8")
    assert "https://example.com/deprecations" in profile_src
    assert "acme/acme-python" in profile_src
    assert "parser_kind=\"generic\"" in profile_src or "parser_kind='generic'" in profile_src
    init_src = (mod_dir / "__init__.py").read_text(encoding="utf-8")
    assert "run_profile_module" in init_src
    assert "stock_workers_for" in init_src
