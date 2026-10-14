"""Public feeds for outside services.

Watch reads feeds a service already publishes (GitHub releases, PyPI, npm,
OpenAPI, changelog pages) and turns a change into a migration packet.
Internal repo-to-repo contracts are not a source.
"""

from conduit.feeds.watch import watch_outside

__all__ = ["watch_outside"]
