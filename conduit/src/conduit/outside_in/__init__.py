"""Outside-in mode: external service → wrappers → call sites.

Python is implemented. A future JS/TS pass should return the same
``ServiceMap`` from this package; Watch and apply consume that model
and do not care which language produced it.
"""

from conduit.outside_in.analyze import analyze_repo
from conduit.outside_in.model import ServiceMap

__all__ = ["ServiceMap", "analyze_repo"]
