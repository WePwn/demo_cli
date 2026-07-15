"""Windows PowerShell coverage (v0.4.0b7).

Confirmed real-world failure: Claude Code invoked
`Remove-Item -Recurse -Force ".\\victim"` under `tool_name="PowerShell"`. The
folder was deleted with no receipt and no recovery point, because (1) the
installed hook only matched "Bash", (2) `run_pretooluse` only evaluated
`tool_name == "Bash"`, (3) `recovery.py` only extracted targets for Unix
`rm`/`mv`, and (4) `ps_remove_item_rf` was unconditionally marked
nonrecoverable so it could never receive a snapshot even when its target was
known. This file proves each root cause is closed.
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import shutil

from demo_cli import recovery
from demo_cli.config import Config
from demo_cli.decide import ESCALATE, REVERSIBLE
from demo_cli.guard import Guard
from demo_cli.hooks.claude_code import (install_into_settings, run_pretooluse,
                                        settings_snippet)


def _cfg(tmp_path, mode="enforce"):
    return Config(mode=mode, project_root=str(tmp_path))


# --- 1. the installed matcher covers PowerShell -----------------------------

def test_settings_snippet_covers_both_shells():
    matchers = [b["matcher"] for b in settings_snippet()["hooks"]["PreToolUse"]]
    assert "Bash" in matchers
    assert "PowerShell" in matchers


def test_install_upgrades_bash_only_install_to_cover_powershell(tmp_path):
    # The exact shape of a pre-PowerShell-support install (this is what was
    # actually on disk in the failing session's .claude/settings.json).
    path = str(tmp_path / ".claude" / "settings.json")
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "demo_cli hook"}]},
                {"matcher": "Edit|Write|MultiEdit|NotebookEdit",
                 "hooks": [{"type": "command", "command": "demo_cli hook"}]},
            ]}
        }, f)

    install_into_settings(path)

    blocks = json.loads(open(path, encoding="utf-8").read())["hooks"]["PreToolUse"]
    matchers = [b["matcher"] for b in blocks]
    assert "Bash" in matchers and "PowerShell" in matchers
    # Upgraded in place, not duplicated: still exactly one Bash block.
    assert matchers.count("Bash") == 1


def test_install_into_settings_is_idempotent_for_powershell(tmp_path):
    path = str(tmp_path / ".claude" / "settings.json")
    install_into_settings(path)
    install_into_settings(path)
    matchers = [b["matcher"] for b in
                json.loads(open(path, encoding="utf-8").read())["hooks"]["PreToolUse"]]
    assert matchers.count("PowerShell") == 1


# --- 2. a PowerShell hook creates a receipt ---------------------------------

def test_powershell_hook_creates_a_receipt(tmp_path):
    (tmp_path / ".demo_cli.toml").write_text('mode = "shadow"\n')
    payload = {"tool_name": "PowerShell", "cwd": str(tmp_path), "session_id": "s1",
               "tool_input": {"command": "Get-ChildItem", "description": "list files"}}
    out = io.StringIO()
    rc = run_pretooluse(io.StringIO(json.dumps(payload)), out)
    assert rc == 0
    receipts_path = tmp_path / ".demo_cli" / "receipts.jsonl"
    assert receipts_path.exists()
    rec = json.loads(receipts_path.read_text().strip().splitlines()[-1])
    assert rec["declared_intent"]["reasoning"] == "list files"


def test_powershell_hook_allows_and_snapshots_recoverable_remove_item(tmp_path):
    (tmp_path / ".demo_cli.toml").write_text('mode = "enforce"\n')
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "a.txt").write_text("keep me")
    payload = {"tool_name": "PowerShell", "cwd": str(tmp_path),
               "tool_input": {"command": f"Remove-Item -Recurse -Force {victim}",
                              "description": "cleanup"}}
    out = io.StringIO()
    rc = run_pretooluse(io.StringIO(json.dumps(payload)), out)
    assert rc == 0
    decision = json.loads(out.getvalue())
    assert decision["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "recovery point" in decision["hookSpecificOutput"]["permissionDecisionReason"]
    entries = recovery.load_entries(os.path.join(str(tmp_path), ".demo_cli", "recovery"))
    assert len(entries) == 1


# --- 3. Remove-Item -Recurse -Force snapshots before execution -------------

def test_remove_item_recurse_force_snapshots_before_execution(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "a.txt").write_text("hello")
    (victim / "nested").mkdir()
    (victim / "nested" / "b.txt").write_text("world")

    g = Guard(config=_cfg(tmp_path))
    r = g.evaluate(f"Remove-Item -Recurse -Force {victim}")
    assert r.decision.decision == REVERSIBLE
    assert r.recovery_entry is not None

    # demo_cli only advises; it never performs the delete itself, and the
    # snapshot exists while the original is still untouched.
    snap = pathlib.Path(r.recovery_entry["recovery_point"])
    assert snap.is_dir()
    assert (snap / "a.txt").read_text() == "hello"
    assert (snap / "nested" / "b.txt").read_text() == "world"
    assert victim.exists()


# --- 4. undo restores the full folder tree and contents --------------------

def test_undo_restores_full_folder_tree_and_contents(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "a.txt").write_text("hello")
    (victim / "nested").mkdir()
    (victim / "nested" / "b.txt").write_text("world")

    g = Guard(config=_cfg(tmp_path))
    r = g.evaluate(f"Remove-Item -Recurse -Force {victim}")
    assert r.recovery_entry is not None

    # Simulate PowerShell actually performing the delete once demo_cli allowed it.
    shutil.rmtree(victim)
    assert not victim.exists()

    ok = recovery.restore_entry(r.recovery_entry)
    assert ok
    assert victim.exists()
    assert (victim / "a.txt").read_text() == "hello"
    assert (victim / "nested" / "b.txt").read_text() == "world"


# --- 5. an unresolved target escalates --------------------------------------

def test_missing_target_escalates(tmp_path):
    g = Guard(config=_cfg(tmp_path))
    missing = tmp_path / "ghost"
    r = g.evaluate(f"Remove-Item -Recurse -Force {missing}")
    assert r.decision.decision == ESCALATE
    assert r.permission == "deny"


def test_ambiguous_multiple_targets_escalate(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    g = Guard(config=_cfg(tmp_path))
    r = g.evaluate(f"Remove-Item -Recurse -Force {a} {b}")
    assert r.decision.decision == ESCALATE


def test_multiple_drive_or_path_flags_escalate(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    g = Guard(config=_cfg(tmp_path))
    r = g.evaluate(f'Remove-Item -LiteralPath "{a}" -Path "{b}" -Recurse -Force')
    assert r.decision.decision == ESCALATE


def test_target_outside_project_root_escalates(tmp_path):
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir()
    try:
        g = Guard(config=_cfg(tmp_path))
        r = g.evaluate(f"Remove-Item -Recurse -Force {outside}")
        assert r.decision.decision == ESCALATE
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_wildcard_target_escalates(tmp_path):
    (tmp_path / "victim1.txt").write_text("x")
    g = Guard(config=_cfg(tmp_path))
    r = g.evaluate(f"Remove-Item -Recurse -Force {tmp_path}\\victim*")
    assert r.decision.decision == ESCALATE


def test_unresolved_target_escalates_in_every_environment(tmp_path):
    # The truth-repair guarantee this whole rule exists for: no recovery ->
    # hard-stop everywhere, never waved through in a low-blast dev workspace.
    missing = tmp_path / "ghost"
    for env in ("production", "development", "staging", "unknown"):
        g = Guard(config=_cfg(tmp_path))
        r = g.evaluate(f"Remove-Item -Recurse -Force {missing}", actual_env=env)
        assert r.decision.decision == ESCALATE, env


# --- 6. no false recovery point is claimed ----------------------------------

def test_no_recovery_entry_on_escalate(tmp_path):
    g = Guard(config=_cfg(tmp_path))
    missing = tmp_path / "ghost"
    r = g.evaluate(f"Remove-Item -Recurse -Force {missing}")
    assert r.decision.decision == ESCALATE
    assert r.recovery_entry is None
    assert r.decision.recoverable is False


def test_hook_never_claims_a_recovery_point_it_did_not_capture(tmp_path):
    (tmp_path / ".demo_cli.toml").write_text('mode = "enforce"\n')
    missing = tmp_path / "ghost"
    payload = {"tool_name": "PowerShell", "cwd": str(tmp_path),
               "tool_input": {"command": f"Remove-Item -Recurse -Force {missing}"}}
    out = io.StringIO()
    run_pretooluse(io.StringIO(json.dumps(payload)), out)
    hook_out = json.loads(out.getvalue())["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"
    assert "recovery point" not in hook_out["permissionDecisionReason"]


# --- doctor self-test now drives a PowerShell payload too -------------------

def test_doctor_selftest_covers_bash_and_powershell():
    from demo_cli.cli import _hook_selftest, _SELFTEST_PAYLOADS
    tool_names = [t for t, _ in _SELFTEST_PAYLOADS]
    assert "Bash" in tool_names and "PowerShell" in tool_names
    for tool_name, command in _SELFTEST_PAYLOADS:
        assert _hook_selftest(tool_name, command) is True
