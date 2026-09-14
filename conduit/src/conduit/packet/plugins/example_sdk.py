"""Minimal example packet plugin (seeds + one rename).

Register via entry points::

    [project.entry-points."conduit.packet_plugins"]
    example-sdk = "conduit.packet.plugins.example_sdk:ExampleSdkPlugin"

Or copy this file into your own package and point the entry point there.
Tiny plugins guide enrich and may add a small rename table — dense AST still
needs OpenAPI/maps like a full vendor plugin.
"""

from __future__ import annotations

from typing import Any

from conduit.packet.plugin import (
    BasePacketPlugin,
    EnrichGuide,
    ProposeResult,
)


class ExampleSdkPlugin(BasePacketPlugin):
    """Demo plugin for the fictional ``example-sdk`` package."""

    name = "example-sdk"
    packages = ["example-sdk"]

    def propose(
        self,
        *,
        package: str,
        ecosystem: str,
        from_version: str,
        to_version: str,
        base_packet: dict[str, Any],
        source_urls: list[str],
    ) -> ProposeResult:
        del package, ecosystem, from_version, base_packet, source_urls
        return ProposeResult(
            rules=[
                {
                    "type": "AST_ATTR_RENAME",
                    "target_files": ["*.py"],
                    "old_attr": "example_sdk.Client.do_thing",
                    "new_attr": "example_sdk.Client.run",
                    "reason": (
                        "example-sdk v2 renames Client.do_thing → Client.run "
                        "(plugin rename table)."
                    ),
                }
            ],
            sources=[
                {
                    "url": "https://example.com/example-sdk/migrate",
                    "kind": "docs",
                }
            ],
            notes_append=(
                f"example-sdk plugin proposed a single attr rename for target {to_version}."
            ),
        )

    def guide_enrich(
        self,
        *,
        package: str,
        ecosystem: str,
        from_version: str,
        to_version: str,
        packet: dict[str, Any],
        source_urls: list[str],
    ) -> EnrichGuide:
        del ecosystem, from_version, packet
        seeds = list(source_urls) + [
            "https://example.com/example-sdk/migrate",
            "https://example.com/example-sdk/CHANGELOG.md",
        ]
        return EnrichGuide(
            seed_urls=list(dict.fromkeys(seeds)),
            suggested_queries=[
                f"{package} migration guide {to_version}",
                f"{package} changelog breaking changes {to_version}",
            ],
            allow_hosts=["example.com"],
            prompt_extra=(
                "Prefer documented Client.do_thing → Client.run renames; "
                "do not invent other callees."
            ),
            context_chunks=[
                "example-sdk: Client.do_thing was renamed to Client.run in v2."
            ],
        )

    def after_enrich(
        self,
        *,
        packet: dict[str, Any],
        enrich_warnings: list[str],
    ) -> dict[str, Any]:
        del enrich_warnings
        from conduit.packet.plugin import drop_uncited_enrich_rewrites

        cleaned, _dropped = drop_uncited_enrich_rewrites(packet)
        return cleaned
