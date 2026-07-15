"""Cursor beforeShellExecution adapter: deny-on-the-unrecoverable, fail-closed
on our own evaluation error, and a failClosed:true hooks.json on install."""
import io
import json
import os

from demo_cli.hooks.cursor import (run_before_shell, settings_snippet,
                                    install_into_hooks_json, HOOK_COMMAND)


def _run(payload, tmp_path, mode="enforce"):
    (tmp_path / ".demo_cli.toml").write_text(f'mode = "{mode}"\n')
    out = io.StringIO()
    rc = run_before_shell(io.StringIO(json.dumps(payload)), out)
    return rc, json.loads(out.getvalue())


def test_cursor_allows_remove_item_recurse_force_with_recoverable_target(tmp_path):
    # v0.4.0b7: a resolvable, in-project target is genuinely snapshotted and
    # allowed through the Cursor surface too, not blanket-denied.
    build = tmp_path / "build"
    build.mkdir()
    payload = {"command": f"Remove-Item -Recurse -Force {build}", "cwd": str(tmp_path)}
    rc, resp = _run(payload, tmp_path)
    assert rc == 0
    assert resp["permission"] == "allow"
    assert resp["continue"] is True


def test_cursor_denies_remove_item_recurse_force_unresolved_target(tmp_path):
    # The truth-repair claim survives for the case it was written for.
    missing = tmp_path / "does-not-exist"
    payload = {"command": f"Remove-Item -Recurse -Force {missing}", "cwd": str(tmp_path)}
    rc, resp = _run(payload, tmp_path)
    assert rc == 0
    assert resp["permission"] == "deny"
    assert resp["continue"] is True


def test_cursor_denies_rm_rf_unresolvable_target(tmp_path):
    payload = {"command": "rm -rf /srv/data", "cwd": str(tmp_path)}
    _, resp = _run(payload, tmp_path)
    assert resp["permission"] == "deny"


def test_cursor_allows_safe_read(tmp_path):
    payload = {"command": "SELECT 1", "cwd": str(tmp_path)}
    _, resp = _run(payload, tmp_path)
    assert resp["permission"] == "allow"


def test_cursor_shadow_mode_never_denies(tmp_path):
    payload = {"command": "Remove-Item -Recurse -Force ./build", "cwd": str(tmp_path)}
    _, resp = _run(payload, tmp_path, mode="shadow")
    assert resp["permission"] == "allow"


def test_cursor_empty_stdin_steps_aside(tmp_path):
    # Known Cursor empty-stdin defect: do not brick the agent (allow, not deny).
    out = io.StringIO()
    rc = run_before_shell(io.StringIO(""), out)
    assert rc == 0
    assert json.loads(out.getvalue())["permission"] == "allow"


def test_cursor_empty_command_steps_aside(tmp_path):
    payload = {"command": "", "cwd": str(tmp_path)}
    _, resp = _run(payload, tmp_path)
    assert resp["permission"] == "allow"


def test_cursor_unparseable_stdin_steps_aside(tmp_path):
    out = io.StringIO()
    rc = run_before_shell(io.StringIO("{not json"), out)
    assert rc == 0
    assert json.loads(out.getvalue())["permission"] == "allow"


def test_cursor_snippet_declares_failclosed(tmp_path):
    entry = settings_snippet()["hooks"]["beforeShellExecution"][0]
    assert entry["failClosed"] is True
    assert entry["command"] == HOOK_COMMAND


def test_cursor_install_writes_failclosed_hook(tmp_path):
    path = str(tmp_path / ".cursor" / "hooks.json")
    install_into_hooks_json(path)
    data = json.loads(open(path, encoding="utf-8").read())
    assert data["version"] == 1
    bse = data["hooks"]["beforeShellExecution"]
    assert any(b["command"] == HOOK_COMMAND and b["failClosed"] is True for b in bse)


def test_cursor_install_is_idempotent(tmp_path):
    path = str(tmp_path / ".cursor" / "hooks.json")
    install_into_hooks_json(path)
    install_into_hooks_json(path)
    bse = json.loads(open(path, encoding="utf-8").read())["hooks"]["beforeShellExecution"]
    assert sum(1 for b in bse if b["command"] == HOOK_COMMAND) == 1


def test_cursor_install_preserves_existing_hooks(tmp_path):
    path = str(tmp_path / ".cursor" / "hooks.json")
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "hooks": {"afterFileEdit": [{"command": "./fmt.sh"}]}}, f)
    install_into_hooks_json(path)
    data = json.loads(open(path, encoding="utf-8").read())
    assert "afterFileEdit" in data["hooks"]
    assert "beforeShellExecution" in data["hooks"]


def test_cursor_install_upgrades_missing_failclosed(tmp_path):
    # A prior entry without failClosed must be repaired to failClosed:true.
    path = str(tmp_path / ".cursor" / "hooks.json")
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "hooks": {
            "beforeShellExecution": [{"command": HOOK_COMMAND}]}}, f)
    install_into_hooks_json(path)
    bse = json.loads(open(path, encoding="utf-8").read())["hooks"]["beforeShellExecution"]
    assert all(b["failClosed"] is True for b in bse if b["command"] == HOOK_COMMAND)
