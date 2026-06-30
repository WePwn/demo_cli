"""demo_cli - a pre-execution safety layer for AI coding agents.

Preview before execution, capture a recovery point first, make it undoable,
and write a tamper-evident receipt of every decision. The library is built
around one invariant:

    a mutating action must be recoverable AND match its declared context,
    otherwise it is escalated - never silently allowed, never falsely
    reported as "recovered".

Public surface:
    Guard          orchestrates classify -> context -> snapshot -> decide -> receipt
    Decision       the disposition + reason returned for a command
    classify_pipeline / classify     pure command classification
    decide         the pure, fail-closed decision function
    load_config    read .demo_cli.toml (declared targets and environments)
"""
from .version import __version__
from .classify import Classification, classify, classify_pipeline
from .decide import Decision, decide, ALLOW, DRY_RUN, REVERSIBLE, CONTEXT_MISMATCH, SANDBOX, ESCALATE
from .config import Config, load_config
from .guard import Guard, GuardResult

__all__ = [
    "__version__",
    "Guard",
    "GuardResult",
    "Classification",
    "classify",
    "classify_pipeline",
    "Decision",
    "decide",
    "Config",
    "load_config",
    "ALLOW",
    "DRY_RUN",
    "REVERSIBLE",
    "CONTEXT_MISMATCH",
    "SANDBOX",
    "ESCALATE",
]
