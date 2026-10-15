"""Versioned OpenAPI sources: catalog locations and pair resolution."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from conduit.impact.intersect import intersect
from conduit.impact.openapi_rest import diff_openapi
from conduit.impact.openapi_sources import SPEC_SOURCES, resolve_spec_pair, spec_source_for
from conduit.impact.run import collect_impacts
from conduit.outside_in.analyze import analyze_repo
from conduit.outside_in.bridge import build_service_map


def test_catalog_maps_hosts_to_verified_sources():
    stripe = spec_source_for("stripe")
    assert stripe is not None
    assert stripe.kind == "git_repo"
    assert stripe.repo == "stripe/openapi"
    assert "spec3.json" in stripe.path

    github = spec_source_for("github")
    assert github is not None
    assert github.ref_style == "commit_by_date"

    anthropic = spec_source_for("anthropic")
    assert anthropic is not None
    assert anthropic.kind == "stainless_sdk"

    openai = SPEC_SOURCES["openai"]
    assert "castiron" in openai.note or "openapi_spec_url" in openai.note


def test_diff_openapi_flags_newly_required_request_fields(tmp_path: Path):
    old = tmp_path / "old.yaml"
    new = tmp_path / "new.yaml"
    old.write_text(
        textwrap.dedent(
            """
            openapi: 3.0.0
            paths:
              /v1/charges:
                post:
                  operationId: createCharge
                  requestBody:
                    content:
                      application/json:
                        schema:
                          type: object
                          properties:
                            amount: {type: integer}
                            currency: {type: string}
                  responses:
                    "200":
                      description: ok
            """
        ).lstrip(),
        encoding="utf-8",
    )
    new.write_text(
        textwrap.dedent(
            """
            openapi: 3.0.0
            paths:
              /v1/charges:
                post:
                  operationId: createCharge
                  requestBody:
                    content:
                      application/json:
                        schema:
                          type: object
                          required: [currency]
                          properties:
                            amount: {type: integer}
                            currency: {type: string}
                  responses:
                    "200":
                      description: ok
            """
        ).lstrip(),
        encoding="utf-8",
    )
    changes = diff_openapi(old, new, service="stripe")
    assert any("became required" in item.detail and item.new == "currency" for item in changes)


def test_rest_impact_maps_removed_path_and_response_field(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pay.py").write_text(
        textwrap.dedent(
            """
            import requests

            def record(item_id, quantity):
                return requests.post(
                    f"https://api.stripe.com/v1/subscription_items/{item_id}/usage_records",
                    json={"quantity": quantity},
                )

            def session_shipping(session):
                data = requests.get(
                    f"https://api.stripe.com/v1/checkout/sessions/{session}"
                ).json()
                return data["shipping_details"]
            """
        ).lstrip(),
        encoding="utf-8",
    )
    old = tmp_path / "old.yaml"
    new = tmp_path / "new.yaml"
    old.write_text(
        textwrap.dedent(
            """
            openapi: 3.0.0
            paths:
              /v1/subscription_items/{subscription_item}/usage_records:
                post:
                  operationId: createUsageRecord
                  responses:
                    "200":
                      description: ok
              /v1/checkout/sessions/{session}:
                get:
                  operationId: getSession
                  responses:
                    "200":
                      content:
                        application/json:
                          schema:
                            type: object
                            properties:
                              id: {type: string}
                              shipping_details: {type: object}
            """
        ).lstrip(),
        encoding="utf-8",
    )
    new.write_text(
        textwrap.dedent(
            """
            openapi: 3.0.0
            paths:
              /v1/checkout/sessions/{session}:
                get:
                  operationId: getSession
                  responses:
                    "200":
                      content:
                        application/json:
                          schema:
                            type: object
                            properties:
                              id: {type: string}
            """
        ).lstrip(),
        encoding="utf-8",
    )
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "old.yaml").write_text(old.read_text(encoding="utf-8"), encoding="utf-8")
    (fixtures / "new.yaml").write_text(new.read_text(encoding="utf-8"), encoding="utf-8")
    (fixtures / "index.json").write_text(
        json.dumps(
            {
                "packages": {
                    "stripe": {
                        "from": "1.0.0",
                        "to": "2.0.0",
                        "openapi": {"from": "old.yaml", "to": "new.yaml"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    service_map, _ = build_service_map(repo)
    impacts, diffed, notes = collect_impacts(
        repo,
        service_map,
        fixtures=fixtures,
        dep="stripe",
        from_version="1.0.0",
        to_version="2.0.0",
        allow_network=False,
    )
    assert "stripe" in diffed
    text = "\n".join(
        f"{site.path}:{site.line}:{site.expr}:{item.change}"
        for item in impacts
        for site in item.sites
        if not site.via_wrapper
    )
    assert "usage_records" in text
    assert "shipping_details" in text


def test_diff_openapi_does_not_treat_static_path_as_template(tmp_path: Path):
    """``/v1/invoices/upcoming`` must not match ``/v1/invoices/{invoice}``."""
    old = tmp_path / "old.yaml"
    new = tmp_path / "new.yaml"
    old.write_text(
        textwrap.dedent(
            """
            openapi: 3.0.0
            paths:
              /v1/invoices/upcoming:
                get:
                  operationId: GetInvoicesUpcoming
                  responses: {"200": {description: ok}}
              /v1/invoices/{invoice}:
                get:
                  operationId: GetInvoicesInvoice
                  responses: {"200": {description: ok}}
            """
        ).lstrip(),
        encoding="utf-8",
    )
    new.write_text(
        textwrap.dedent(
            """
            openapi: 3.0.0
            paths:
              /v1/invoices/{invoice}:
                get:
                  operationId: GetInvoicesInvoice
                  responses: {"200": {description: ok}}
              /v1/invoices/create_preview:
                post:
                  operationId: PostInvoicesCreatePreview
                  responses: {"200": {description: ok}}
            """
        ).lstrip(),
        encoding="utf-8",
    )
    changes = diff_openapi(old, new, service="stripe")
    assert any(
        item.kind == "endpoint_removed" and item.old == "/v1/invoices/upcoming"
        for item in changes
    )


def test_sdk_static_request_path_maps_to_http_call(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "bill.py").write_text(
        textwrap.dedent(
            """
            import stripe

            def report(si_id):
                return stripe.SubscriptionItem._static_request(
                    "post",
                    "/v1/subscription_items/%s/usage_records" % si_id,
                    params={"quantity": 1},
                )
            """
        ).lstrip(),
        encoding="utf-8",
    )
    service_map, _ = build_service_map(repo)
    http = [c for c in service_map.chains if c.kind == "http_call" and not c.via_wrapper]
    assert http
    assert "usage_records" in http[0].sdk_method


def test_resolve_spec_pair_stripe_tags_live():
    old, new, note = resolve_spec_pair(
        "stripe", from_ref="v1000", to_ref="v2565", allow_network=True
    )
    assert note == ""
    assert old is not None and new is not None
    changes = diff_openapi(old, new, service="stripe")
    assert any(
        item.kind == "endpoint_removed" and "usage_records" in (item.old or "")
        for item in changes
    )
    assert any(
        item.kind == "endpoint_removed" and item.old == "/v1/invoices/upcoming"
        for item in changes
    )


def test_resolve_spec_pair_stainless_reports_missing_url():
    # Current anthropic tip has no openapi_spec_url (verified); expect an honest miss.
    old, new, note = resolve_spec_pair(
        "anthropic", from_ref="1.5.0", to_ref="1.14.0", allow_network=True
    )
    assert old is None and new is None
    assert "openapi_spec_url" in note


def test_resolve_spec_pair_stainless_older_tags_have_urls():
    old, new, note = resolve_spec_pair(
        "anthropic", from_ref="0.40.0", to_ref="1.0.0", allow_network=True
    )
    assert note == ""
    assert old is not None and new is not None
