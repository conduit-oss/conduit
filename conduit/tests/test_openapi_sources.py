"""Versioned OpenAPI sources: catalog locations and pair resolution."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from conduit.impact.intersect import intersect
from conduit.impact.openapi_rest import diff_openapi
from conduit.impact.openapi_sources import (
    SPEC_SOURCES,
    discover_spec_at_ref,
    pypi_source_repo,
    resolve_sdk_package_pair,
    resolve_spec_pair,
    spec_source_for,
)
from conduit.impact.run import collect_impacts
from conduit.outside_in.analyze import analyze_repo
from conduit.outside_in.bridge import build_service_map
from conduit.impact.openapi_rest import load_openapi, operations


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


def test_pypi_source_repo_from_project_urls():
    assert pypi_source_repo("apache-airflow-client") == "apache/airflow-client-python"
    assert pypi_source_repo("clerk-backend-api") == "clerk/clerk-sdk-python"
    assert pypi_source_repo("gr4vy") == "gr4vy/gr4vy-python"


def test_discover_openapi_generator_spec_at_tag():
    loc, why = discover_spec_at_ref("apache/airflow-client-python", "3.0.0")
    assert loc is not None and loc.endswith("spec/v2.yaml"), why


def test_discover_speakeasy_workflow_pins_openapi():
    loc, why = discover_spec_at_ref("clerk/clerk-sdk-python", "v1.8.0")
    assert loc is not None, why
    assert "openapi-specs" in loc or loc.startswith("url:")


def test_discover_fern_sdk_without_committed_spec():
    loc, why = discover_spec_at_ref("cohere-ai/cohere-python", "v5.0.0")
    assert loc is None
    assert "Fern" in why


def test_resolve_sdk_package_airflow_removes_dag_reports():
    old, new, note = resolve_sdk_package_pair(
        "apache-airflow-client", from_ref="3.0.0", to_ref="3.1.3"
    )
    assert note == ""
    assert old is not None and new is not None
    changes = diff_openapi(old, new, service="airflow")
    assert any(
        item.kind == "endpoint_removed" and item.old == "/api/v2/dagReports"
        for item in changes
    )


def test_resolve_apis_guru_github_removes_authorizations():
    old, new, note = resolve_spec_pair(
        "apis-guru-github", from_ref="2021-06-10", to_ref="2023-02-16", allow_network=True
    )
    assert note == ""
    assert old is not None and new is not None
    changes = diff_openapi(old, new, service="github")
    assert any(
        item.kind == "endpoint_removed" and item.old == "/authorizations"
        for item in changes
    )


def test_resolve_azure_storage_version_dirs():
    old, new, note = resolve_spec_pair(
        "azure-storage", from_ref="2023-05-01", to_ref="2026-09-01", allow_network=True
    )
    assert note == ""
    assert old is not None and new is not None


def test_resolve_google_drive_discovery_pair():
    old, new, note = resolve_spec_pair(
        "google-drive", from_ref="2019-06-01", to_ref="2026-09-01", allow_network=True
    )
    assert note == ""
    assert old is not None and new is not None
    # flatPath normalization: path format changes must not look like mass removals.
    before = {op.path for op in operations(load_openapi(old))}
    after = {op.path for op in operations(load_openapi(new))}
    assert any(path.startswith("/drive/v3/") for path in before | after)


def test_github_falls_back_to_apis_guru_when_primary_misses():
    # Early dates are missing from github/rest-api-description; APIs.guru has history.
    old, new, note = resolve_spec_pair(
        "github", from_ref="2021-06-10", to_ref="2023-02-16", allow_network=True
    )
    assert note == ""
    assert old is not None and new is not None


def test_openapi_generator_param_serialize_maps_to_http(tmp_path: Path):
    repo = tmp_path / "repo"
    pkg = repo / "airflow_client" / "client" / "api"
    pkg.mkdir(parents=True)
    (pkg / "dag_report_api.py").write_text(
        textwrap.dedent(
            """
            class DagReportApi:
                def __init__(self, api_client):
                    self.api_client = api_client

                def get_dag_reports(self):
                    return self.api_client.param_serialize(
                        method="GET",
                        resource_path="/api/v2/dagReports",
                    )
            """
        ).lstrip(),
        encoding="utf-8",
    )
    service_map, _ = build_service_map(repo)
    http = [c for c in service_map.chains if c.kind == "http_call" and not c.via_wrapper]
    assert http
    assert "dagReports" in http[0].sdk_method


def test_python_shebang_scripts_are_scanned(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    script = repo / "git-github-login"
    script.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python
            import requests

            def login():
                return requests.post("https://api.github.com/authorizations", json={})
            """
        ).lstrip(),
        encoding="utf-8",
    )
    service_map, _ = build_service_map(repo)
    http = [c for c in service_map.chains if c.kind == "http_call" and not c.via_wrapper]
    assert http
    assert "authorizations" in http[0].sdk_method
