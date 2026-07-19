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
# Claude Code fires the same PreToolUse shape for both a POSIX shell (Bash) and
# a Windows one (PowerShell); tool_input.command carries the raw command line
# either way, so both route through the same command evaluation path.
_SHELL_TOOLS = {"Bash", "PowerShell"}

_BANNER = "\u2500" * 4

def _stderr(msg: str) -> None:
    sys.stderr.write(msg + "\n")

def _loud_save(result) -> None:
    """Unmissable stderr for a captured recovery point (enforce path)."""
    rid = result.recovery_entry.get("id", "")
    n = len(getattr(result, "affected_paths", None) or []) or None
    what = f"{n} files" if n else "target"
    _stderr("")
    _stderr(f"demo_cli \u2705 recovery point {rid} captured before this ran ({what}).")
    _stderr(f"         mistake? undo it with:  demo_cli undo {rid}")
    try:
        from ..render import feedback_url_for
        url = feedback_url_for(result)
        if url:
            _stderr(f"         wrong call?  report it (prefilled): {url}")
    except Exception:
        pass
    _stderr("")

def _loud_block(result) -> None:
    """Unmissable stderr for an escalate/block (enforce path)."""
    _stderr("")
    _stderr(f"demo_cli \u26d4 blocked: {result.decision.reason}")
    _stderr("         nothing was captured, and nothing is claimed to be.")
    try:
        from ..render import feedback_url_for
        url = feedback_url_for(result)
        if url:
            _stderr(f"         wrong call?  report it (prefilled): {url}")
    except Exception:
        pass
    _stderr("")


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
    # Fail-open on our own parsing errors: never brick the agent. But fail-open
    # LOUD - surface it on stderr so the user can see the tool stepped aside
    # rather than silently allowing.
    try:
        raw = stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        sys.stderr.write(f"demo_cli: could not parse hook input, stepping aside ({exc})\n")
        return 0

    tool_name = data.get("tool_name")
    tool_input = data.get("tool_input") or {}
    cwd = data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    description = tool_input.get("description")

    is_shell = tool_name in _SHELL_TOOLS
    is_file = tool_name in _FILE_TOOLS
    if not (is_shell or is_file):
        # Beta scope: gate shell + file-write tools. Everything else passes.
        return 0

    try:
        cfg = load_config(start=cwd)
        guard = Guard(config=cfg)
        if is_shell:
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
        _loud_save(result)          # <-- make the save FELT, on stderr
    elif result.decision.is_blocking:
        _loud_block(result)         # <-- make the block legible, with report link
    _emit(stdout, result.permission, reason)
    return 0


# --------------------------------------------------------------------------
# Installation helpers
# --------------------------------------------------------------------------

_FILE_MATCHER = "Edit|Write|MultiEdit|NotebookEdit"
# Separate matcher entries (rather than one "Bash|PowerShell" block) so an
# older Bash-only install upgrades by gaining a new PowerShell block, and so
# `"Bash" in matchers` stays a valid way to check coverage.
_SHELL_MATCHERS = ("Bash", "PowerShell")


def settings_snippet() -> Dict:
    return {
        "hooks": {
            "PreToolUse": [
                {"matcher": m, "hooks": [{"type": "command", "command": "demo_cli hook"}]}
                for m in _SHELL_MATCHERS
            ] + [
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

    # Existing Bash-only installs (pre-PowerShell-support) are upgraded here:
    # the Bash block is left untouched (no duplicate), and the missing
    # PowerShell block is appended so Windows shell commands start routing
    # through the hook too.
    for matcher in (*_SHELL_MATCHERS, _FILE_MATCHER):
        if not _present(matcher):
            pre.append({"matcher": matcher,
                        "hooks": [{"type": "command", "command": "demo_cli hook"}]})

    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
