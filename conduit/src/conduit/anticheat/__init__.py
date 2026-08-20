"""Consumer-repo anti-cheat: mechanical rules plus an additive LLM auditor."""

from conduit.anticheat.scan import (
    AnticheatReport,
    anticheat_failure_result,
    run_anticheat,
    run_anticheat_mechanical,
)
from conduit.anticheat.rules import reject_write
from conduit.anticheat.audit_log import MigrationAuditLog

__all__ = [
    "AnticheatReport",
    "MigrationAuditLog",
    "anticheat_failure_result",
    "reject_write",
    "run_anticheat",
    "run_anticheat_mechanical",
]
