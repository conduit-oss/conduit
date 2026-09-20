"""Public declaration rewrite API."""

from conduit.patcher.declarations.apply import (
    RewriteResult,
    StructuralResidual,
    rewrite_declarations,
    scan_declaration_residuals,
)

__all__ = [
    "RewriteResult",
    "StructuralResidual",
    "rewrite_declarations",
    "scan_declaration_residuals",
]
