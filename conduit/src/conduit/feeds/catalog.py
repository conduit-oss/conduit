"""Which public feeds each catalogued outside service publishes.

OpenAPI previous/latest URLs are optional. The openai pair is the recorded
fixture used by offline watch (those raw paths are not a live branch). A
live fetch that 404s is skipped.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceFeeds:
    service_id: str
    package: str
    github_repo: str = ""
    pypi: str = ""
    npm: str = ""
    changelog_url: str = ""
    openapi_previous_url: str = ""
    openapi_latest_url: str = ""

    def github_atom_url(self) -> str:
        if not self.github_repo:
            return ""
        return f"https://github.com/{self.github_repo}/releases.atom"

    def pypi_url(self) -> str:
        if not self.pypi:
            return ""
        return f"https://pypi.org/pypi/{self.pypi}/json"

    def npm_url(self) -> str:
        if not self.npm:
            return ""
        return f"https://registry.npmjs.org/{self.npm}"


FEEDS: dict[str, ServiceFeeds] = {
    "openai": ServiceFeeds(
        service_id="openai",
        package="openai",
        github_repo="openai/openai-python",
        pypi="openai",
        npm="openai",
        changelog_url="https://platform.openai.com/docs/deprecations",
        openapi_previous_url=(
            "https://raw.githubusercontent.com/openai/openai-openapi/previous/openapi.yaml"
        ),
        openapi_latest_url=(
            "https://raw.githubusercontent.com/openai/openai-openapi/latest/openapi.yaml"
        ),
    ),
    "anthropic": ServiceFeeds(
        service_id="anthropic",
        package="anthropic",
        github_repo="anthropics/anthropic-sdk-python",
        pypi="anthropic",
        npm="@anthropic-ai/sdk",
        changelog_url="https://docs.anthropic.com/en/release-notes/api",
    ),
    "stripe": ServiceFeeds(
        service_id="stripe",
        package="stripe",
        github_repo="stripe/stripe-python",
        pypi="stripe",
        npm="stripe",
        changelog_url="https://docs.stripe.com/changelog",
    ),
    "twilio": ServiceFeeds(
        service_id="twilio",
        package="twilio",
        github_repo="twilio/twilio-python",
        pypi="twilio",
        npm="twilio",
    ),
    "boto3": ServiceFeeds(
        service_id="boto3",
        package="boto3",
        github_repo="boto/boto3",
        pypi="boto3",
        changelog_url="https://github.com/boto/boto3/blob/develop/CHANGELOG.rst",
    ),
}


def feeds_for(service_id: str) -> ServiceFeeds | None:
    return FEEDS.get((service_id or "").strip().lower())
