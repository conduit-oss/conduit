"""OpenAI VendorProfile — info-points for the shared detect pipeline."""

from __future__ import annotations

from conduit.detect.vendor_profile import VendorProfile

_OPENAI_MODEL_ID_PATTERN = (
    r"(?:ft-)?"
    r"(?:"
    r"gpt-[a-z0-9._-]+"
    r"|o[0-9][a-z0-9._-]*"
    r"|dall-e-[0-9]"
    r"|chatgpt-[a-z0-9._-]+"
    r"|text-embedding-[a-z0-9._-]+"
    r"|text-similarity-[a-z0-9._-]+"
    r"|text-moderation-[a-z0-9._-]+"
    r"|whisper-[a-z0-9._-]+"
    r"|tts-[a-z0-9._-]+"
    r"|gpt-image-[a-z0-9._-]+"
    r"|codex-[a-z0-9._-]+"
    r"|omni-moderation(?:-[a-z0-9._-]+)?"
    r"|computer-use-[a-z0-9._-]+"
    r")"
)

_OPENAI_API_PATTERN = (
    r"(?:ChatCompletion|/v1/[a-z0-9/_-]+|chat\.completions|"
    r"Completion\.create|embeddings\.create)"
)

_PATH_TO_CALLEES: dict[str, list[str]] = {
    "/v1/chat/completions": [
        "chat.completions.create",
        "openai.chat.completions.create",
    ],
    "/v1/completions": [
        "completions.create",
        "openai.completions.create",
        "Completion.create",
    ],
    "/v1/edits": [
        "Edit.create",
        "openai.Edit.create",
    ],
    "/v1/engines": [
        "Engine.list",
        "openai.Engine.list",
    ],
    "/v1/fine-tunes": [
        "FineTune.list",
        "openai.FineTune.list",
        "FineTune.create",
        "openai.FineTune.create",
    ],
    "/v1/fine_tuning/jobs": [
        "fine_tuning.jobs.create",
        "openai.fine_tuning.jobs.create",
        "fine_tuning.jobs.list",
        "openai.fine_tuning.jobs.list",
    ],
    "/v1/models": [
        "models.list",
        "openai.models.list",
        "Engine.list",
        "openai.Engine.list",
    ],
    "/v1/embeddings": [
        "embeddings.create",
        "openai.embeddings.create",
    ],
    "/v1/images/generations": [
        "images.generate",
        "openai.images.generate",
        "Image.create",
        "openai.Image.create",
    ],
    "/v1/images/edits": [
        "images.edit",
        "openai.images.edit",
    ],
    "/v1/audio/transcriptions": [
        "audio.transcriptions.create",
        "openai.audio.transcriptions.create",
    ],
    "/v1/audio/translations": [
        "audio.translations.create",
        "openai.audio.translations.create",
    ],
    "/v1/audio/speech": [
        "audio.speech.create",
        "openai.audio.speech.create",
    ],
    "/v1/moderations": [
        "moderations.create",
        "openai.moderations.create",
        "Moderation.create",
        "openai.Moderation.create",
    ],
    "/v1/responses": [
        "responses.create",
        "openai.responses.create",
    ],
}

_API_PATTERN_TO_PATH: list[tuple[str, str]] = [
    (r"^chat\.completions(?:\.create)?$", "/v1/chat/completions"),
    (r"^ChatCompletion(?:\.create)?$", "/v1/chat/completions"),
    (r"^(?:openai\.)?embeddings\.create$", "/v1/embeddings"),
    (r"^(?:openai\.)?Completion\.create$", "/v1/completions"),
    (r"^(?:openai\.)?Edit\.create$", "/v1/edits"),
    (r"^(?:openai\.)?Engine(?:\.list|\.retrieve)?$", "/v1/engines"),
    (r"^(?:openai\.)?FineTune(?:\.list|\.create)?$", "/v1/fine-tunes"),
    (r"^(?:openai\.)?Image\.(?:create|create_edit)$", "/v1/images/generations"),
    (r"^(?:openai\.)?Moderation\.create$", "/v1/moderations"),
]


def openai_known_ids(*, demo: bool = False, include_api_models: bool = True) -> set[str]:
    from conduit.detect.modules.openai.known_models import collect_known_model_ids

    return collect_known_model_ids(demo=demo, include_api_models=include_api_models)


OPENAI_PROFILE = VendorProfile(
    name="openai",
    packages=["openai"],
    ecosystems=["pypi", "npm"],
    model_id_pattern=_OPENAI_MODEL_ID_PATTERN,
    api_pattern=_OPENAI_API_PATTERN,
    known_ids=openai_known_ids,
    deprecations_url="https://platform.openai.com/docs/deprecations",
    changelog_url="https://platform.openai.com/docs/changelog",
    openapi_repo="https://github.com/openai/openai-openapi",
    openapi_source_url="https://github.com/openai/openai-openapi",
    sdk_release_repos={
        "openai/openai-python": {
            "package": "openai",
            "ecosystems": ["pip", "pyproject"],
        },
        "openai/openai-node": {
            "package": "openai",
            "ecosystems": ["npm"],
        },
    },
    live_catalog_url="https://api.openai.com/v1/models",
    live_catalog_auth_env="OPENAI_API_KEY",
    models_catalog_url="https://developers.openai.com/api/docs/models.md",
    model_doc_url_template="https://developers.openai.com/api/docs/models/{model_id}.md",
    path_to_callees=dict(_PATH_TO_CALLEES),
    api_pattern_to_path=list(_API_PATTERN_TO_PATH),
    evidence_seeds=[
        "https://developers.openai.com/api/llms.txt",
        "https://developers.openai.com/api/docs/llms.txt",
        "https://developers.openai.com/api/reference/llms.txt",
        "https://platform.openai.com/docs/deprecations",
        "https://developers.openai.com/api/docs/deprecations",
        "https://developers.openai.com/api/docs/models",
        "https://developers.openai.com/api/docs/models.md",
        "https://platform.openai.com/docs/changelog",
        "https://github.com/openai/openai-python/discussions/742",
        "https://github.com/openai/openai-python/discussions/",
        "https://github.com/openai/openai-python/blob/main/README.md",
        "https://github.com/openai/",
    ],
    evidence_hosts=[
        "platform.openai.com",
        "developers.openai.com",
        "github.com",
    ],
    evidence_query_templates=[
        "{package} python SDK migration {from_version} to {to_version}",
        "{package} API deprecations endpoint replacement",
        "{package} chat completions max_tokens max_completion_tokens",
        "{package} model supported endpoints chat completions",
        "site:developers.openai.com/api/docs/models supported endpoints",
    ],
    fixtures_name="openai",
    parser_kind="openai_html",
    demo_packet_fallback=True,
)
