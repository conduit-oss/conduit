"""Made-up SDK v1: public re-exports from an internal module path."""

from widgetkit._internal.widget import Widget
from widgetkit.error import GoneError

__all__ = ["GoneError", "Widget"]
