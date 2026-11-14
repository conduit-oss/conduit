"""Post-edit recheck must not fail a site for sibling same-expr uses in the file.

Made-up package only (acmekit). Thing.create is removed in 2.x; each function
that still calls it is its own site. A correct rewrite of one function must
clear that site even when another function in the same module still uses the
old call. A bad rewrite of the edited span must still be rejected. The file is
not fully fixed while a sibling site remains.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from conduit.apply.client import FakeModelClient
from conduit.apply.engine import ApplyConfig, apply_sites
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.models import ImpactSite, SiteStatus
from conduit.apply.verify import remaining_breaks

from test_apply import _EXERCISE, _settings


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")


def _acmekit_fixtures(tmp_path: Path) -> Path:
    """acmekit 1.0: Thing.create / Thing.embed. 2.0: Client.things.create only."""
    fixtures = tmp_path / "fixtures"
    _write(
        fixtures / "old" / "acmekit" / "__init__.py",
        """\
        class Thing:
            @classmethod
            def create(cls, *, model, messages, temperature=None):
                class _Msg:
                    content = "ok"

                class _Choice:
                    message = _Msg()

                class _Reply:
                    choices = [_Choice()]

                return _Reply()

            @classmethod
            def embed(cls, *, input, model):
                class _Row:
                    vector = [0.1]

                class _Out:
                    data = [_Row()]

                return _Out()
        """,
    )
    _write(
        fixtures / "new" / "acmekit" / "__init__.py",
        """\
        from .client import Client
        """,
    )
    _write(
        fixtures / "new" / "acmekit" / "client.py",
        """\
        class _Things:
            def create(self, *, model, messages, temperature=None):
                class _Msg:
                    content = "ok"

                class _Choice:
                    message = _Msg()

                class _Reply:
                    choices = [_Choice()]

                return _Reply()

        class Client:
            def __init__(self, api_key=None):
                self.things = _Things()
        """,
    )
    (fixtures / "index.json").write_text(
        json.dumps({"packages": {"acmekit": {"wheels": {"1.0": "old", "2.0": "new"}}}}),
        encoding="utf-8",
    )
    return fixtures


_TWO_HELPERS = """\
import acmekit

def ask_short(prompt: str) -> str:
    reply = acmekit.Thing.create(
        model="acme-small",
        messages=[{"role": "user", "content": prompt}],
    )
    return reply.choices[0].message.content

def ask_long(prompt: str) -> str:
    reply = acmekit.Thing.create(
        model="acme-large",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    return reply.choices[0].message.content
"""


def _repo_with_two_helpers(tmp_path: Path) -> Path:
    """Consumer tree with vendored acmekit 1.x and two Thing.create helpers."""
    fixtures = _acmekit_fixtures(tmp_path)
    root = tmp_path / "repo"
    # Service map resolves acmekit from the consumer tree.
    for path in (fixtures / "old" / "acmekit").rglob("*.py"):
        rel = path.relative_to(fixtures / "old")
        _write(root / rel, path.read_text(encoding="utf-8"))
    _write(root / "helpers.py", _TWO_HELPERS)
    return root


def _short_site() -> ImpactSite:
    return ImpactSite(
        path="helpers.py",
        line=4,
        expr="acmekit.Thing.create",
        dependency="acmekit",
        from_version="1.0",
        to_version="2.0",
        change="Thing.create removed",
        confidence="high",
        source="wheel",
        kind="removed",
        symbol="acmekit.Thing.create",
    )


def _good_short_rewrite() -> str:
    return textwrap.dedent(
        """\
        import acmekit

        def ask_short(prompt: str) -> str:
            reply = acmekit.Client().things.create(
                model="acme-small",
                messages=[{"role": "user", "content": prompt}],
            )
            return reply.choices[0].message.content

        def ask_long(prompt: str) -> str:
            reply = acmekit.Thing.create(
                model="acme-large",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
            )
            return reply.choices[0].message.content
        """
    )


def _bad_short_rewrite() -> str:
    """Edited span still broken: invented Thing.make; sibling left alone."""
    return textwrap.dedent(
        """\
        import acmekit

        def ask_short(prompt: str) -> str:
            reply = acmekit.Thing.make(
                model="acme-small",
                messages=[{"role": "user", "content": prompt}],
            )
            return reply.choices[0].message.content

        def ask_long(prompt: str) -> str:
            reply = acmekit.Thing.create(
                model="acme-large",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
            )
            return reply.choices[0].message.content
        """
    )


def test_remaining_breaks_ignores_sibling_same_expr_outside_span(tmp_path: Path):
    """A correct rewrite of ask_short is clear even though ask_long still breaks."""
    root = _repo_with_two_helpers(tmp_path)
    fixtures = tmp_path / "fixtures"
    site = _short_site()
    patched = _good_short_rewrite()
    # ask_short occupies lines 3–8 in the patched file.
    found = remaining_breaks(
        root,
        site,
        patched,
        span=(3, 8),
        fixtures=fixtures,
        allow_network=False,
        original=_TWO_HELPERS,
    )
    assert found == [], found
    assert "acmekit.Thing.create" in (root / "helpers.py").read_text(encoding="utf-8")


def test_remaining_breaks_still_rejects_new_break_inside_span(tmp_path: Path):
    """Narrowing recheck is not a free pass: a break in the edited span fails."""
    root = _repo_with_two_helpers(tmp_path)
    fixtures = tmp_path / "fixtures"
    site = _short_site()
    found = remaining_breaks(
        root,
        site,
        _bad_short_rewrite(),
        span=(3, 8),
        fixtures=fixtures,
        allow_network=False,
        original=_TWO_HELPERS,
    )
    assert found, "expected a break inside the edited ask_short span"
    # Must point at the edited function, not only at the sibling.
    assert any(
        text.startswith("helpers.py:")
        and int(text.split(":", 1)[1].split(" ", 1)[0]) <= 8
        for text, _symbol in found
    ), found
    # Sibling line must not be the sole reason for rejection.
    sibling_only = all(":11 " in text or ":12 " in text for text, _ in found)
    assert not sibling_only, found


def _two_site_payload() -> dict:
    return {
        "dependency": "acmekit",
        "impacts": [
            {
                "dependency": "acmekit",
                "from_version": "1.0",
                "to_version": "2.0",
                "change": "Thing.create removed",
                "confidence": "high",
                "source": "wheel",
                "kind": "removed",
                "symbol": "acmekit.Thing.create",
                "old": "acmekit.Thing.create",
                "new": "",
                "sites": [
                    {
                        "path": "helpers.py",
                        "line": 4,
                        "expr": "acmekit.Thing.create",
                        "confidence": "high",
                        "via_wrapper": False,
                    },
                    {
                        "path": "helpers.py",
                        "line": 11,
                        "expr": "acmekit.Thing.create",
                        "confidence": "high",
                        "via_wrapper": False,
                    },
                ],
            }
        ],
        "deprecations": [],
        "diffed": ["acmekit"],
        "notes": [],
    }


def _fn(source: str) -> str:
    return json.dumps({"function_source": source, "new_imports": []})


def test_apply_accepts_one_helper_while_sibling_stays_unfixed(tmp_path: Path):
    """First site can land; second remains its own unfixed site — never verified broken."""
    root = _repo_with_two_helpers(tmp_path)
    fixtures = tmp_path / "fixtures"
    good_short = textwrap.dedent(
        """\
        def ask_short(prompt: str) -> str:
            reply = acmekit.Client().things.create(
                model="acme-small",
                messages=[{"role": "user", "content": prompt}],
            )
            return reply.choices[0].message.content
        """
    )
    # ask_long keeps the removed call so recheck rejects every attempt.
    still_long = textwrap.dedent(
        """\
        def ask_long(prompt: str) -> str:
            reply = acmekit.Thing.create(
                model="acme-large",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
            )
            return reply.choices[0].message.content
        """
    )
    client = FakeModelClient(
        responses=[
            _fn(good_short),
            "switched ask_short to Client.things.create",
            _fn(still_long),
            _fn(still_long),
            _fn(still_long),
        ]
    )
    report = apply_sites(
        sites_from_payload(_two_site_payload()),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=3),
            write=True,
            test_cmd=_EXERCISE,
            client=client,
            recheck_impact=True,
            fixtures=fixtures,
        ),
    )
    assert len(report.results) == 2, report.results
    first, second = report.results
    assert first.site.line == 4
    assert second.site.line == 11
    # First site accepted; sibling same-expr must not have blocked it.
    assert first.status in {
        SiteStatus.FIXED_VERIFIED,
        SiteStatus.FIXED_UNVERIFIED,
        SiteStatus.FIXED_NOT_COVERED,
        SiteStatus.FIXED_TESTS_COULDNT_RUN,
        SiteStatus.FIXED_NEEDS_REVIEW,
    }, (first.status, first.error, first.why)
    assert first.status != SiteStatus.NEEDS_HUMAN, (first.error, first.why)
    # File still has an unfixed Thing.create; that site is not verified.
    text = (root / "helpers.py").read_text(encoding="utf-8")
    assert "acmekit.Thing.create" in text
    assert "Client().things.create" in text or "things.create" in text
    assert second.status == SiteStatus.NEEDS_HUMAN, (second.status, second.error, second.why)
    assert second.status != SiteStatus.FIXED_VERIFIED
    assert "same function" not in (second.why or "")
