import io
import json

from demo_cli.config import Config
from demo_cli.context import Intent
from demo_cli.guard import Guard
from demo_cli.decide import ESCALATE, REVERSIBLE, ALLOW, CONTEXT_MISMATCH
from demo_cli.hooks.claude_code import run_pretooluse
import sqlite3


def _cfg(tmp_path, mode="enforce"):
    return Config(mode=mode, project_root=str(tmp_path))


def test_guard_rm_rf_no_target_escalates(tmp_path):
    # The headline regression: rm -rf with no resolvable target must not be
    # reported as recoverable.
    g = Guard(config=_cfg(tmp_path))
    r = g.evaluate("rm -rf ./build")
    assert r.decision.decision == ESCALATE
    assert r.recovery_entry is None
    assert r.allowed is False


def test_guard_snapshots_named_db_and_is_reversible(tmp_path):
    db = tmp_path / "app.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE t (id INTEGER)")
    con.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)])
    con.commit()
    con.close()
    g = Guard(config=_cfg(tmp_path))
    r = g.evaluate(f"sqlite3 {db} 'DELETE FROM t'", explicit_db=str(db))
    assert r.recovery_entry is not None
    assert r.decision.decision in (REVERSIBLE, "DRY_RUN")
    assert r.allowed is True


def test_shadow_mode_never_blocks(tmp_path):
    g = Guard(config=_cfg(tmp_path, mode="shadow"))
    r = g.evaluate("rm -rf /srv/data")  # would escalate in enforce
    assert r.decision.decision == ESCALATE
    assert r.allowed is True  # observe-only


def test_context_mismatch_with_snapshot(tmp_path):
    db = tmp_path / "app.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE t (id INTEGER)")
    con.commit()
    con.close()
    g = Guard(config=_cfg(tmp_path))
    r = g.evaluate(f"sqlite3 {db} 'DELETE FROM t'", explicit_db=str(db),
                   intent=Intent(env="staging"), actual_env="production")
    assert r.decision.decision == CONTEXT_MISMATCH
    assert r.permission == "ask"


def test_hook_denies_in_enforce(tmp_path):
    payload = {"tool_name": "Bash", "cwd": str(tmp_path),
               "tool_input": {"command": "rm -rf /srv/data", "description": "cleanup"}}
    # write a config that enforces
    (tmp_path / ".demo_cli.toml").write_text('mode = "enforce"\n')
    out = io.StringIO()
    rc = run_pretooluse(io.StringIO(json.dumps(payload)), out)
    assert rc == 0
    decision = json.loads(out.getvalue())
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_passes_through_non_bash(tmp_path):
    payload = {"tool_name": "Read", "cwd": str(tmp_path), "tool_input": {"file_path": "x"}}
    out = io.StringIO()
    rc = run_pretooluse(io.StringIO(json.dumps(payload)), out)
    assert rc == 0 and out.getvalue() == ""


def test_hook_records_reasoning_in_receipt(tmp_path):
    (tmp_path / ".demo_cli.toml").write_text('mode = "shadow"\n')
    payload = {"tool_name": "Bash", "cwd": str(tmp_path), "session_id": "s1",
               "tool_input": {"command": "SELECT 1", "description": "check row count"}}
    out = io.StringIO()
    run_pretooluse(io.StringIO(json.dumps(payload)), out)
    receipts = (tmp_path / ".demo_cli" / "receipts.jsonl").read_text().strip().splitlines()
    rec = json.loads(receipts[-1])
    assert rec["declared_intent"]["reasoning"] == "check row count"
