"""Claude Code PreToolUse integration - the auto-fire path.

Claude Code runs a PreToolUse hook *before* a tool executes and passes the tool
call as JSON on stdin:

    {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": "...",
     "tool_input": {"command": "...", "description": "..."}, ...}

A hook steers Claude Code by printing JSON on stdout:

    {"hookSpecificOutput": {"hookEventName": "PreToolUse",
        "permissionDecision": "allow" | "deny" | "ask",
        "permissionDecisionReason": "..."}}

Mapping:
    shadow mode  -> never interferes: evaluate, snapshot, record a receipt,
                    then exit 0 so the normal permission flow is unchanged.
    enforce mode -> ESCALATE => deny, CONTEXT_MISMATCH => ask, otherwise allow
                    (recoverable mutations are snapshotted first, then allowed).

Posture: a *decision* is fail-closed (no recovery path on prod => escalate),
but our *own* errors are fail-open - a bug in this tool must never brick the
user's agent. If we cannot parse or evaluate, we step aside (exit 0).
"""
from __future__ import annotations

import json
import os
import sys
from typing import Dict

from ..config import load_config
from ..context import Intent
from ..guard import Guard

_FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _emit(stdout, permission: str, reason: str) -> None:
    stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": permission,
            "permissionDecisionReason": reason,
        }
    }))
    stdout.flush()


def run_pretooluse(stdin, stdout) -> int:
    # Fail-open on our own parsing errors: never brick the agent.
    try:
        raw = stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0

    tool_name = data.get("tool_name")
    tool_input = data.get("tool_input") or {}
    cwd = data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    description = tool_input.get("description")

    is_bash = tool_name == "Bash"
    is_file = tool_name in _FILE_TOOLS
    if not (is_bash or is_file):
        # Beta scope: gate Bash + file-write tools. Everything else passes.
        return 0

    try:
        cfg = load_config(start=cwd)
        guard = Guard(config=cfg)
        if is_bash:
            command = (tool_input.get("command") or "").strip()
            if not command:
                return 0
            result = guard.evaluate(
                command,
                intent=Intent(reasoning=description),
                agent_id=data.get("agent_id", "claude-code"),
                session_id=data.get("session_id", "unknown"),
            )
        else:
            # Edit / Write / MultiEdit -> file_path ; NotebookEdit -> notebook_path
            file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
            if not file_path:
                return 0
            result = guard.evaluate_file_edit(
                file_path, tool_name=tool_name,
                intent=Intent(reasoning=description),
                agent_id=data.get("agent_id", "claude-code"),
                session_id=data.get("session_id", "unknown"),
            )
    except Exception as exc:  # our bug must not block the user
        sys.stderr.write(f"demo_cli: internal error, stepping aside ({exc})\n")
        return 0

    # shadow mode: observe only - but make a genuinely interesting decision or a
    # fresh snapshot *visible* on stderr, so value isn't silently buried.
    if guard.mode != "enforce":
        if result.decision.is_blocking or result.decision.is_ask:
            sys.stderr.write(f"demo_cli [shadow] {result.decision.decision}: {result.decision.reason}\n")
        elif result.recovery_entry:
            rp = os.path.basename(result.recovery_entry["recovery_point"])
            rid = result.recovery_entry.get("id", "")
            sys.stderr.write(f"demo_cli [shadow] snapshot {rid} captured ({rp}); undo with `demo_cli undo {rid}`\n")
        return 0

    reason = result.decision.reason
    if result.recovery_entry:
        rid = result.recovery_entry.get("id", "")
        reason += f"  (recovery point {rid}; undo with `demo_cli undo {rid}`)"
    _emit(stdout, result.permission, reason)
    return 0


# --------------------------------------------------------------------------
# Installation helpers
# --------------------------------------------------------------------------

_FILE_MATCHER = "Edit|Write|MultiEdit|NotebookEdit"


def settings_snippet() -> Dict:
    return {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "demo_cli hook"}]},
                {"matcher": _FILE_MATCHER, "hooks": [{"type": "command", "command": "demo_cli hook"}]},
            ]
        }
    }


def install_into_settings(path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    settings: Dict = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                settings = json.load(f)
        except Exception:
            settings = {}
    hooks = settings.setdefault("hooks", {})
    pre = hooks.setdefault("PreToolUse", [])

    def _present(matcher: str) -> bool:
        return any(
            isinstance(b, dict) and b.get("matcher") == matcher
            and any(h.get("command") == "demo_cli hook" for h in b.get("hooks", []))
            for b in pre
        )

    for matcher in ("Bash", _FILE_MATCHER):
        if not _present(matcher):
            pre.append({"matcher": matcher,
                        "hooks": [{"type": "command", "command": "demo_cli hook"}]})

    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
