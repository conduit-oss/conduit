"""Consumer-repo anti-cheat: mechanical rules plus an additive LLM auditor."""

from conduit.anticheat.scan import (
    AnticheatReport,
    anticheat_failure_result,
    run_anticheat,
    run_anticheat_mechanical,
)
from conduit.anticheat.rules import reject_write

__all__ = [
    "AnticheatReport",
    "anticheat_failure_result",
    "reject_write",
    "run_anticheat",
    "run_anticheat_mechanical",
]
