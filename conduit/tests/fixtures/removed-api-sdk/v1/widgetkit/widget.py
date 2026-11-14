"""Compatibility shim: public import path still works in v1."""

from widgetkit._internal.widget import Widget

__all__ = ["Widget"]
