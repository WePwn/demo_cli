"""The Guard: the out-of-band reference monitor that ties the pieces together.

Flow for one command:

    classify -> resolve target (no default) -> build context (declared-first)
    -> compare declared intent to context -> snapshot the real target
    -> preview affected rows -> verify structural approval -> decide
    -> append a hash-chained receipt

Two modes:
    shadow   observe only - evaluate, snapshot, and record, but never block.
             The recommended way to adopt: prove value with zero workflow risk.
    enforce  gate - a blocking disposition prevents the action from proceeding.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import os

from . import approval, preview as preview_mod, recovery
from .classify import Classification, classify_pipeline, is_sql_preview_candidate
from .config import Config, load_config
from .context import Context, Intent, build_context, compare_intent
from .decide import (ALLOW, ASK, BLOCKING, CONTEXT_MISMATCH, DRY_RUN, ESCALATE,
                     REVERSIBLE, SANDBOX, Decision, decide)
from .receipts import Receipt, append_receipt


@dataclass
class GuardResult:
    command: str
    classification: Classification
    context: Context
    decision: Decision
    mode: str
    target: Optional[recovery.Target] = None
    recovery_entry: Optional[dict] = None
    preview_count: Optional[int] = None
    preview_rows: list = field(default_factory=list)
    preview_cols: list = field(default_factory=list)
    mismatches: List[Tuple[str, str, str]] = field(default_factory=list)
    receipt: Optional[Receipt] = None

    @property
    def allowed(self) -> bool:
        """Whether the action may proceed unattended."""
        if self.mode == "shadow":
            return True
        return self.decision.decision not in BLOCKING and not self.decision.is_ask

    @property
    def permission(self) -> str:
        """Map the disposition to a Claude Code permissionDecision."""
        d = self.decision.decision
        if d in BLOCKING:
            return "deny"
        if d in ASK:
            return "ask"
        return "allow"


class Guard:
    def __init__(self, config: Optional[Config] = None, mode: Optional[str] = None):
        self.config = config or load_config()
        self.mode = mode or self.config.mode

    def evaluate(
        self,
        command: str,
        target_path: Optional[str] = None,
        explicit_db: Optional[str] = None,
        db_url: Optional[str] = None,
        intent: Optional[Intent] = None,
        actual_env: Optional[str] = None,
        approval_token: Optional[str] = None,
        agent_id: str = "unknown",
        session_id: str = "unknown",
    ) -> GuardResult:
        intent = intent or Intent()
        command = command.strip()

        c = classify_pipeline(command)

        # Trou 2: when no target was supplied explicitly, resolve the real
        # filesystem operand of an rm / mv so the snapshot actually fires on the
        # auto-fire path (the flagship "rm -> undo" moment). Bounded to the
        # project root so we never try to copy a home/system tree, and honest
        # about multi-path rm (extract_path_operand returns None -> escalate).
        if not target_path and not explicit_db and not db_url:
            cand = recovery.extract_path_operand(command)
            if cand:
                ap = os.path.abspath(cand)
                root = os.path.abspath(self.config.project_root or os.getcwd())
                try:
                    within = os.path.commonpath([ap, root]) == root
                except ValueError:
                    within = False
                if within and os.path.exists(ap):
                    target_path = ap

        target = recovery.resolve_target(command, explicit_db, db_url, target_path)
        label = target.label if target else None

        # Declared-first environment: config target match feeds the context.
        config_env = self.config.declared_env(label)
        ctx = build_context(command, target_label=label,
                            declared_env=actual_env, config_env=config_env,
                            cwd=self.config.project_root)

        mismatches = compare_intent(intent, ctx)

        # Snapshot the *real* target, honouring the per-target recovery strategy.
        rule = self.config.match_target(label)
        strategy = rule.recovery if rule else "snapshot"

        # Managed / remote Postgres: a pg_dump over the wire is not a recovery
        # point we can stand behind for a system we don't control, so treat it
        # as a non-recoverable surface (honest escalation) instead of claiming
        # reversibility. Local Postgres (localhost) still snapshots normally.
        remote_pg = bool(target and target.kind == "postgres" and recovery.is_remote_pg(target.ref))
        if remote_pg and not c.nonrecoverable_surface:
            c.nonrecoverable_surface = "managed_database"

        entry = (recovery.snapshot(target, self.config.recovery_dir, strategy, action=command)
                 if (c.needs_recovery and not remote_pg) else None)
        recovery_captured = entry is not None

        # Preview affected rows for previewable SQL on a recoverable target.
        preview_count, preview_rows, preview_cols = None, [], []
        if recovery_captured and is_sql_preview_candidate(command):
            preview_count, preview_rows, preview_cols = preview_mod.preview(command, target)

        # Structural approval (only meaningful for the non-recoverable case).
        approval_ok = False
        key = self.config.approver_key
        if approval_token and key:
            approval_ok = approval.verify(command, approval_token, key)

        decision = decide(
            c, ctx.environment, recovery_captured,
            mismatches=mismatches, approval_ok=approval_ok, preview_count=preview_count,
        )

        receipt = Receipt(
            action_raw=command,
            action_type=c.action_type,
            target_environment=ctx.environment,
            decision=decision.decision,
            reason=decision.reason,
            mode=self.mode,
            matched_rule=c.matched_rule,
            classification="destructive" if c.is_destructive else ("mutating" if c.is_mutating else "safe"),
            recovery_point=entry["recovery_point"] if entry else None,
            nonrecoverable_surface=c.nonrecoverable_surface,
            dry_run_affected_rows=preview_count,
            context=ctx.as_dict(),
            declared_intent=intent.as_dict(),
            context_mismatches=[list(m) for m in mismatches],
            pipeline_segments=c.segments if c.is_pipeline else [],
            remote_exec=c.remote_exec,
            agent_id=agent_id,
            session_id=session_id,
        )
        append_receipt(self.config.receipts_path, receipt)

        return GuardResult(
            command=command, classification=c, context=ctx, decision=decision, mode=self.mode,
            target=target, recovery_entry=entry,
            preview_count=preview_count, preview_rows=preview_rows, preview_cols=preview_cols,
            mismatches=mismatches, receipt=receipt,
        )

    def evaluate_file_edit(
        self,
        file_path: str,
        tool_name: str = "Edit",
        intent: Optional[Intent] = None,
        agent_id: str = "unknown",
        session_id: str = "unknown",
    ) -> GuardResult:
        """Evaluate an agent file mutation (Edit / Write / MultiEdit / Notebook).

        This is the second door: agents mangle files through file-write tools,
        not only through Bash. We snapshot the *existing* file before it is
        overwritten so the change is reversible, and record a receipt. Creating
        a brand-new file has nothing to overwrite, so it proceeds with no
        snapshot. If an existing file cannot be snapshotted, we escalate rather
        than claim a recovery we did not take.
        """
        from .classify import Classification

        intent = intent or Intent()
        ap = os.path.abspath(file_path)
        exists = os.path.exists(ap)
        is_dir = exists and os.path.isdir(ap)

        c = Classification(
            is_mutating=True, action_type="filewrite",
            matched_rule=f"file_{tool_name.lower()}", segments=[ap],
        )

        target = recovery.Target("dir" if is_dir else "file", ap, ap) if exists else None
        label = target.label if target else None
        config_env = self.config.declared_env(label)
        ctx = build_context(f"{tool_name} {ap}", target_label=label,
                            config_env=config_env, cwd=self.config.project_root)

        rule = self.config.match_target(label)
        strategy = rule.recovery if rule else "snapshot"
        entry = (recovery.snapshot(target, self.config.recovery_dir, strategy,
                                   action=f"{tool_name} {os.path.basename(ap)}")
                 if exists else None)
        recovery_captured = entry is not None

        if not exists:
            decision = Decision(ALLOW, "New file; creation has nothing to overwrite.",
                                recoverable=False)
        elif recovery_captured:
            decision = Decision(REVERSIBLE, "File snapshotted before the edit; reversible.",
                                recoverable=True)
        else:
            decision = Decision(
                ESCALATE,
                "Could not snapshot the file before editing; no recovery path.",
                recoverable=False,
                next_steps=["Check the file is readable and within the project root."],
            )

        receipt = Receipt(
            action_raw=f"{tool_name} {ap}",
            action_type="filewrite",
            target_environment=ctx.environment,
            decision=decision.decision,
            reason=decision.reason,
            mode=self.mode,
            matched_rule=c.matched_rule,
            classification="mutating",
            recovery_point=entry["recovery_point"] if entry else None,
            context=ctx.as_dict(),
            declared_intent=intent.as_dict(),
            agent_id=agent_id,
            session_id=session_id,
        )
        append_receipt(self.config.receipts_path, receipt)

        return GuardResult(
            command=f"{tool_name} {ap}", classification=c, context=ctx,
            decision=decision, mode=self.mode, target=target, recovery_entry=entry,
            receipt=receipt,
        )
