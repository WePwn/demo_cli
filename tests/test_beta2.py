"""Tests for the 0.4.0b2 additions:

posture      six dispositions collapse into SAFE / REVIEW / BLOCKED.
file gating  Guard.evaluate_file_edit snapshots an existing file before an edit,
             allows a brand-new file, and escalates if it cannot snapshot.
recovery     points carry ids; find()/log/undo-by-id/prune work; dir size guard.
hook         Edit/Write tools route through the file path; new files allow.
locking      concurrent receipt appends keep the chain intact.
"""
import json
import os
import threading

import pytest

from demo_cli.config import Config
from demo_cli.decide import (decide, posture, SAFE, REVIEW, BLOCKED,
                             ALLOW, REVERSIBLE, DRY_RUN, SANDBOX,
                             CONTEXT_MISMATCH, ESCALATE)
from demo_cli.guard import Guard
from demo_cli import recovery, render
from demo_cli.receipts import append_receipt, Receipt, verify_chain
from demo_cli.hooks.claude_code import run_pretooluse, settings_snippet
import io


# --- posture ---------------------------------------------------------------

def test_posture_collapses_dispositions():
    assert posture(ALLOW) == SAFE
    assert posture(REVERSIBLE) == SAFE
    assert posture(DRY_RUN) == SAFE
    assert posture(SANDBOX) == SAFE
    assert posture(CONTEXT_MISMATCH) == REVIEW
    assert posture(ESCALATE) == BLOCKED


def test_posture_meta_has_colour():
    glyph, colour = render.posture_meta(ESCALATE)
    assert "BLOCKED" in glyph and colour == "red"


# --- file-edit gating ------------------------------------------------------

def _guard(tmp_path, mode="shadow"):
    return Guard(config=Config(mode=mode, project_root=str(tmp_path)))


def test_existing_file_edit_is_snapshotted(tmp_path):
    f = tmp_path / "config.py"
    f.write_text("original")
    g = _guard(tmp_path)
    r = g.evaluate_file_edit(str(f), tool_name="Edit")
    assert r.decision.decision == REVERSIBLE
    assert r.recovery_entry is not None
    assert os.path.exists(r.recovery_entry["recovery_point"])


def test_new_file_write_allows_without_snapshot(tmp_path):
    g = _guard(tmp_path)
    r = g.evaluate_file_edit(str(tmp_path / "brand_new.py"), tool_name="Write")
    assert r.decision.decision == ALLOW
    assert r.recovery_entry is None


def test_file_edit_records_receipt(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    g = _guard(tmp_path)
    g.evaluate_file_edit(str(f), tool_name="Edit")
    v = verify_chain(g.config.receipts_path)
    assert v.ok and v.entries == 1


# --- recovery ids / find / prune -------------------------------------------

def test_recovery_entry_has_id_and_action(tmp_path):
    f = tmp_path / "d.db"
    f.write_text("data")
    t = recovery.Target("file", str(f), str(f))
    entry = recovery.snapshot(t, str(tmp_path / "rec"), action="DELETE FROM t")
    assert entry["id"] and len(entry["id"]) == 8
    assert entry["action"] == "DELETE FROM t"


def test_find_by_id_prefix(tmp_path):
    rec = str(tmp_path / "rec")
    f = tmp_path / "d.db"
    f.write_text("data")
    t = recovery.Target("file", str(f), str(f))
    e = recovery.snapshot(t, rec, action="x")
    found = recovery.find(rec, e["id"][:4])
    assert found and found["id"] == e["id"]
    assert recovery.find(rec, "zzzzzzzz") is None


def test_prune_keep_deletes_old_points_not_receipts(tmp_path):
    rec = str(tmp_path / "rec")
    f = tmp_path / "d.db"
    for i in range(3):
        f.write_text(f"v{i}")
        recovery.snapshot(recovery.Target("file", str(f), str(f)), rec, action=f"e{i}")
    assert len(recovery.load_entries(rec)) == 3
    removed = recovery.prune(rec, keep=1)
    assert len(removed) == 2
    assert len(recovery.load_entries(rec)) == 1
    # the surviving point's artefact still exists
    surv = recovery.load_entries(rec)[0]
    assert os.path.exists(surv["recovery_point"])


def test_dir_snapshot_size_guard(tmp_path, monkeypatch):
    d = tmp_path / "big"
    d.mkdir()
    (d / "f.bin").write_bytes(b"0" * 5000)
    monkeypatch.setenv("DEMO_CLI_MAX_SNAPSHOT_MB", "0.001")  # ~1KB cap
    entry = recovery.snapshot(recovery.Target("dir", str(d), str(d)), str(tmp_path / "rec"))
    assert entry is None  # too big -> honest no-snapshot


# --- hook routing ----------------------------------------------------------

def _run_hook(payload):
    out = io.StringIO()
    run_pretooluse(io.StringIO(json.dumps(payload)), out)
    return out.getvalue()


def test_hook_gates_edit_tool(tmp_path):
    (tmp_path / ".demo_cli.toml").write_text('mode = "enforce"\n')
    f = tmp_path / "c.py"
    f.write_text("original")
    out = _run_hook({"tool_name": "Edit", "cwd": str(tmp_path),
                     "tool_input": {"file_path": str(f)}})
    decision = json.loads(out)["hookSpecificOutput"]["permissionDecision"]
    assert decision == "allow"  # snapshotted, reversible
    # a recovery point now exists for the project
    rec = os.path.join(str(tmp_path), ".demo_cli", "recovery")
    assert len(recovery.load_entries(rec)) == 1


def test_hook_ignores_unknown_tool(tmp_path):
    out = _run_hook({"tool_name": "WebFetch", "cwd": str(tmp_path),
                     "tool_input": {"url": "http://x"}})
    assert out == ""  # passes through, no decision emitted


def test_settings_snippet_has_both_matchers():
    matchers = [b["matcher"] for b in settings_snippet()["hooks"]["PreToolUse"]]
    assert "Bash" in matchers
    assert any("Edit" in m for m in matchers)


# --- concurrency -----------------------------------------------------------

def test_concurrent_appends_keep_chain_intact(tmp_path):
    path = str(tmp_path / "receipts.jsonl")

    def worker():
        for _ in range(10):
            append_receipt(path, Receipt(
                action_raw="x", action_type="shell", target_environment="dev",
                decision="ALLOW", reason="r", mode="shadow"))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    v = verify_chain(path)
    assert v.ok, v.detail
    assert v.entries == 40


# --- 0.4.0b3: plain rm must be gated, not only rm -rf -----------------------

from demo_cli.classify import classify_pipeline


def test_plain_rm_is_destructive():
    for cmd in ["rm app.db", "rm probe.db", "rm -f keepme.py", "rm -r build", "sudo rm x"]:
        c = classify_pipeline(cmd)
        assert c.is_destructive, f"{cmd} not classified destructive"
        assert c.needs_recovery, f"{cmd} would not snapshot"


def test_rm_rf_still_recognised():
    c = classify_pipeline("rm -rf /tmp/x")
    assert c.is_destructive and c.matched_rule == "rm_rf"


def test_rm_subcommands_are_not_false_positives():
    for cmd in ["git rm tracked.py", "docker rm container", "npm rm lodash"]:
        c = classify_pipeline(cmd)
        assert not c.is_destructive, f"{cmd} wrongly flagged as rm"


def test_plain_rm_snapshots_existing_file(tmp_path):
    f = tmp_path / "data.db"
    f.write_text("payload")
    g = Guard(config=Config(mode="shadow", project_root=str(tmp_path)))
    r = g.evaluate(f"rm {f}")
    assert r.recovery_entry is not None
    assert os.path.exists(r.recovery_entry["recovery_point"])
