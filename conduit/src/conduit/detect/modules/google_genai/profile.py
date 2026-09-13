"""VendorProfile for google_genai — fill remaining URLs / custom parsers as needed."""

from __future__ import annotations

from conduit.detect.vendor_profile import VendorProfile

PROFILE = VendorProfile(
    name='google_genai',
    packages=['google-generativeai'],
    ecosystems=['pypi'],
    model_id_pattern=None,
    api_pattern=None,
    deprecations_url='https://ai.google.dev/gemini-api/docs/migrate',
    changelog_url='https://github.com/googleapis/python-genai/releases',
    openapi_repo=None,
    openapi_source_url=None,
    sdk_release_repos={'googleapis/python-genai': {'package': 'google-genai', 'ecosystems': ['pip', 'pyproject']}},
    live_catalog_url=None,
    live_catalog_auth_env=None,
    models_catalog_url=None,
    model_doc_url_template=None,
    evidence_seeds=['https://ai.google.dev/gemini-api/docs/migrate', 'https://github.com/googleapis/python-genai/releases'],
    evidence_hosts=['ai.google.dev', 'googleapis.github.io'],
    evidence_query_templates=[
        "{package} python SDK migration {from_version} to {to_version}",
        "{package} API deprecations endpoint replacement",
    ],
    fixtures_name='google_genai',
    parser_kind="generic",
    demo_packet_fallback=False,
)
