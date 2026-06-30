"""The decision function.

Pure and fail-closed. It is the single place that turns a description of a
command (classification + context + whether a recovery point was actually
captured) into a disposition. It never performs IO and never captures
anything itself, which is exactly why it cannot lie: `recovery_captured` is
supplied by the orchestrator only when a snapshot of the *real* target
succeeded. There is no default target to fall back to.

Invariant:
    a mutating action must be recoverable AND match its declared context,
    otherwise escalate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .classify import Classification

# Dispositions
ALLOW = "ALLOW"
DRY_RUN = "DRY_RUN"
REVERSIBLE = "REVERSIBLE"
CONTEXT_MISMATCH = "CONTEXT_MISMATCH"
SANDBOX = "SANDBOX"
ESCALATE = "ESCALATE"

# Dispositions that mean "do not let this proceed unattended".
BLOCKING = frozenset({ESCALATE})
# Dispositions that warrant surfacing to a human even though a snapshot exists.
ASK = frozenset({CONTEXT_MISMATCH})

# Three human-facing postures. The six dispositions are precise but jargony;
# a reader needs to grasp the stance in one glance. Posture collapses them into
# SAFE (will proceed) / REVIEW (needs a human) / BLOCKED (stopped), and the
# exact disposition stays available as a subtitle.
SAFE = "SAFE"
REVIEW = "REVIEW"
BLOCKED = "BLOCKED"

_POSTURE = {
    ALLOW: SAFE, DRY_RUN: SAFE, REVERSIBLE: SAFE, SANDBOX: SAFE,
    CONTEXT_MISMATCH: REVIEW,
    ESCALATE: BLOCKED,
}


def posture(disposition: str) -> str:
    """Collapse a disposition into one of SAFE / REVIEW / BLOCKED."""
    return _POSTURE.get(disposition, REVIEW)

_LOW_BLAST_ENVS = frozenset({"development", "test", "sandbox", "staging"})

INVARIANT = "mutating_actions_must_be_recoverable_and_match_the_declared_context"


@dataclass
class Decision:
    decision: str
    reason: str
    recoverable: bool = False
    surface: Optional[str] = None
    next_steps: List[str] = field(default_factory=list)

    @property
    def is_blocking(self) -> bool:
        return self.decision in BLOCKING

    @property
    def is_ask(self) -> bool:
        return self.decision in ASK


def decide(
    c: Classification,
    environment: str,
    recovery_captured: bool,
    mismatches: Optional[List[Tuple[str, str, str]]] = None,
    approval_ok: bool = False,
    preview_count: Optional[int] = None,
) -> Decision:
    mismatches = mismatches or []

    # 1. Opaque remote execution: the payload is unknown until it runs, so it
    #    can be neither previewed nor snapshotted.
    if c.remote_exec:
        return Decision(
            ESCALATE,
            "Opaque remote execution (fetch-and-run); payload unknown before it runs.",
            recoverable=False,
            next_steps=[
                "Download the script to a file first, review it, then run it.",
                "Or pin and verify a known checksum before executing.",
            ],
        )

    # 2. Nothing mutating - outside the invariant entirely.
    if not c.needs_recovery:
        return Decision(ALLOW, "Non-mutating action; outside the invariant.", recoverable=False)

    # 3. A surface a local snapshot cannot truthfully cover. Honest escalation:
    #    we do NOT claim recoverability we cannot provide.
    if c.nonrecoverable_surface:
        if approval_ok:
            return Decision(
                ALLOW,
                f"Non-recoverable surface ({c.nonrecoverable_surface}) carried a valid "
                "structural approval the agent could not forge.",
                recoverable=False,
                surface=c.nonrecoverable_surface,
            )
        return Decision(
            ESCALATE,
            f"Mutation of a non-recoverable surface ({c.nonrecoverable_surface}); a local "
            "snapshot cannot cover its blast radius.",
            recoverable=False,
            surface=c.nonrecoverable_surface,
            next_steps=[
                "Confirm the external effect is intended and authorised.",
                "Supply a structural approval token, or perform it outside the agent.",
            ],
        )

    # 4. Valid-but-wrong-context: a reasonable mutation pointed at the wrong place.
    if mismatches:
        if recovery_captured:
            return Decision(
                CONTEXT_MISMATCH,
                "Mutating action did not match the declared context; a recovery point "
                "was captured first and the mismatch is surfaced for review.",
                recoverable=True,
            )
        return Decision(
            ESCALATE,
            "Mutating action did not match the declared context and no recovery point "
            "could be captured.",
            recoverable=False,
            next_steps=[
                "Verify the target environment / branch / account is the intended one.",
                "Provide a snapshot target (--target / --db / --db-url) or back up first.",
            ],
        )

    # 5. Recoverable: a recovery point of the real target was captured.
    if recovery_captured:
        if preview_count is not None:
            return Decision(
                DRY_RUN,
                f"Previewed: {preview_count} row(s) would be affected. Recovery point "
                "captured first; reversible.",
                recoverable=True,
            )
        return Decision(
            REVERSIBLE,
            "Recovery point captured before the action; reversible.",
            recoverable=True,
        )

    # 6. Not recoverable, but low blast radius (non-production).
    if environment in _LOW_BLAST_ENVS:
        return Decision(
            SANDBOX,
            f"Non-production target ({environment}); low blast radius.",
            recoverable=False,
        )

    # 7. Fail-closed: destructive / mutating on production or an undetermined
    #    target, with no recovery path. Never silently allowed.
    return Decision(
        ESCALATE,
        f"No recovery path for a {c.matched_rule or 'mutating action'} on "
        f"'{environment}'; cannot auto-recover. Human input required.",
        recoverable=False,
        next_steps=[
            "Add --db / --db-url / --target so a recovery point can be captured.",
            "Or create an independent backup before proceeding.",
        ],
    )
