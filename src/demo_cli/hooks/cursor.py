"""Cursor integration - the `beforeShellExecution` gate.

Cursor runs a `beforeShellExecution` hook *before* a terminal command executes
and passes the command as JSON on stdin:

    {"command": "<full command>", "cwd": "<dir>",
     "hook_event_name": "beforeShellExecution",
     "workspace_roots": ["..."], "sandbox": false,
     "conversation_id": "...", "generation_id": "...", ...}

A hook steers Cursor by printing JSON on stdout:

    {"continue": true,
     "permission": "allow" | "deny" | "ask",
     "user_message": "...",   # shown to the human
     "agent_message": "..."}  # sent back to the agent

Two properties of Cursor shape this adapter; both are encoded by
`install-hook --cursor` in the `hooks.json` it writes:

  * Cursor defaults to **fail-open**: a hook that crashes, times out, or emits
    invalid JSON lets the command through. The hook definition therefore carries
    ``failClosed: true`` so a genuine failure blocks instead. Deny-on-the-
    unrecoverable is the whole point - a guard that cannot run must not wave a
    destructive command through.
  * Only **deny** is reliably honored today - Cursor's own allow-list can
    override an ``allow``/``ask`` from the hook. That is fine: our core move is
    precisely to *deny* the unrecoverable, the one decision Cursor respects.

Mode mapping mirrors the Claude Code adapter:

    shadow   observe only - evaluate, snapshot, record a receipt, always return
             ``allow`` (interesting decisions/snapshots surface on stderr).
    enforce  ESCALATE => deny, CONTEXT_MISMATCH => ask, otherwise allow
             (recoverable mutations are snapshotted first, then allowed).

Failure posture (the deliberate difference from Claude Code): when we hold a
real command but cannot evaluate it, we fail **closed** (deny), matching the
``failClosed`` stance Cursor asks for. The one exception is a *missing* command
- Cursor has a known defect that delivers empty stdin on some remote Linux
workspaces; denying there would brick every command over a harness bug, so we
step aside instead. A true crash is still caught by ``failClosed: true``.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Dict, Optional

from ..config import load_config
from ..guard import Guard

# The command string Cursor invokes; also written into hooks.json on install.
HOOK_COMMAND = "demo_cli hook-cursor"


def _emit(stdout, permission: str, *, agent_message: Optional[str] = None,
          user_message: Optional[str] = None, cont: bool = True) -> None:
    payload: Dict = {"continue": cont, "permission": permission}
    if agent_message:
        payload["agent_message"] = agent_message
    if user_message:
        payload["user_message"] = user_message
    stdout.write(json.dumps(payload))
    stdout.flush()


def run_before_shell(stdin, stdout) -> int:
    try:
        raw = stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        # We got something we could not parse. There is no command to judge, so
        # we cannot honestly deny a specific action; step aside. (We handled the
        # error, so we emit an explicit allow rather than relying on failClosed.)
        sys.stderr.write("demo_cli: could not parse Cursor hook input; stepping aside\n")
        _emit(stdout, "allow")
        return 0

    command = (data.get("command") or "").strip()
    if not command:
        # Known Cursor defect: empty stdin on some remote workspaces. No command
        # to gate - do not brick the agent over a harness bug.
        sys.stderr.write("demo_cli: empty Cursor hook input (no command); stepping aside\n")
        _emit(stdout, "allow")
        return 0

    cwd = data.get("cwd") or (data.get("workspace_roots") or [None])[0] or os.getcwd()

    try:
        cfg = load_config(start=cwd)
        guard = Guard(config=cfg)
        result = guard.evaluate(
            command,
            agent_id=data.get("conversation_id", "cursor"),
            session_id=data.get("generation_id", "unknown"),
        )
    except Exception as exc:
        # We held a real command and could not evaluate it. Fail CLOSED: deny.
        # This is the deliberate opposite of the Claude Code adapter's fail-open
        # on internal error, and matches the failClosed posture Cursor asks for.
        sys.stderr.write(f"demo_cli: internal error evaluating command; failing closed ({exc})\n")
        _emit(stdout, "deny",
              agent_message="demo_cli could not evaluate this command safely and blocked it "
                            "(fail-closed). Retry once the guard is healthy, or get human review.",
              user_message="demo_cli blocked a command it could not evaluate (fail-closed).")
        return 0

    # shadow mode: observe only - never block. Surface the interesting cases on
    # stderr so the value is visible without steering Cursor.
    if guard.mode != "enforce":
        if result.decision.is_blocking or result.decision.is_ask:
            sys.stderr.write(f"demo_cli [shadow] {result.decision.decision}: {result.decision.reason}\n")
        elif result.recovery_entry:
            rid = result.recovery_entry.get("id", "")
            sys.stderr.write(f"demo_cli [shadow] snapshot {rid} captured; undo with `demo_cli undo {rid}`\n")
        _emit(stdout, "allow")
        return 0

    permission = result.permission  # deny (ESCALATE) / ask (CONTEXT_MISMATCH) / allow
    reason = result.decision.reason
    if result.recovery_entry:
        rid = result.recovery_entry.get("id", "")
        reason += f"  (recovery point {rid}; undo with `demo_cli undo {rid}`)"
    _emit(
        stdout, permission,
        agent_message=reason if permission != "allow" else None,
        user_message=(f"demo_cli: {result.decision.decision}" if permission != "allow" else None),
    )
    return 0


# --------------------------------------------------------------------------
# Installation helpers
# --------------------------------------------------------------------------

def settings_snippet() -> Dict:
    """The `.cursor/hooks.json` fragment demo_cli installs. `failClosed: true`
    is mandatory: on Cursor a failed hook otherwise fails open."""
    return {
        "version": 1,
        "hooks": {
            "beforeShellExecution": [
                {"command": HOOK_COMMAND, "failClosed": True},
            ]
        },
    }


def install_into_hooks_json(path: str) -> None:
    """Merge the beforeShellExecution hook into an existing `.cursor/hooks.json`,
    preserving any other hooks the user already declared. Idempotent, and it
    keeps `failClosed: true` even if a prior entry omitted it."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    settings: Dict = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                settings = json.load(f)
        except Exception:
            settings = {}
    settings.setdefault("version", 1)
    hooks = settings.setdefault("hooks", {})
    bse = hooks.setdefault("beforeShellExecution", [])

    existing = [b for b in bse if isinstance(b, dict) and b.get("command") == HOOK_COMMAND]
    if existing:
        for b in existing:
            b["failClosed"] = True
    else:
        bse.append({"command": HOOK_COMMAND, "failClosed": True})

    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
