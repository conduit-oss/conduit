"""Fix flagged call sites with a model, verifying each change locally.

``conduit apply`` reads impact JSON, packs per-site context, asks a
chat-completions model for a function-scoped unified diff, verifies the
patch in a scratch tree, and optionally writes verified fixes.
"""

from __future__ import annotations

from conduit.apply.engine import ApplyConfig, ApplyReport, apply_sites
from conduit.apply.models import SiteStatus

__all__ = [
    "ApplyConfig",
    "ApplyReport",
    "SiteStatus",
    "apply_sites",
]
