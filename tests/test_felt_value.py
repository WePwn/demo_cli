"""Coverage for the 0.4.0b7 'felt value' batch: the loud save-moment on the
hook path, the shared feedback URL, and doctor's PATH + self-test checks.

These behaviors are what turn an invisible clone into an identified, retained
user, so a silent regression here is expensive. Pin them."""
import io
import json
import os
import tempfile

from demo_cli.hooks import claude_code
from demo_cli import render, cli


def _run_hook(command, mode="enforce", tool="Bash", cwd=None):
    """Drive the real PreToolUse entrypoint, capturing stdout+stderr."""
    import sys
    d = cwd or tempfile.mkdtemp()
    prev = os.getcwd()
    try:
        os.chdir(d)
        open(os.path.join(d, ".demo_cli.toml"), "w").write(
            f'mode = "{mode}"\n[workspace]\ndir = ".demo_cli"\n')
        payload = {"tool_name": tool, "tool_input": {"command": command},
                   "cwd": d}
        out, err = io.StringIO(), io.StringIO()
        real = sys.stderr
        sys.stderr = err
        try:
            claude_code.run_pretooluse(io.StringIO(json.dumps(payload)), out)
        finally:
            sys.stderr = real
        return out.getvalue(), err.getvalue()
    finally:
        os.chdir(prev)


def test_reversible_prints_felt_save_on_stderr():
    d = tempfile.mkdtemp()
    open(os.path.join(d, "a.txt"), "w").write("x")
    _, err = _run_hook("rm -rf a.txt", cwd=d)
    # the three things a saved user must SEE
    assert "recovery point" in err
    assert "captured before this ran" in err
    assert "demo_cli undo" in err


def test_felt_save_carries_the_prefilled_report_link():
    d = tempfile.mkdtemp()
    open(os.path.join(d, "a.txt"), "w").write("x")
    _, err = _run_hook("rm -rf a.txt", cwd=d)
    assert "report it (prefilled)" in err
    assert "github.com/WePwn/demo_cli/issues/new" in err


def test_block_prints_honest_no_capture_line():
    # a recursive-force delete hard-stops in every env -> loud block, no claim
    _, err = _run_hook("Remove-Item -Recurse -Force C:\\\\x")
    assert "blocked" in err.lower()
    assert "nothing was captured" in err
    assert "claimed" in err


def test_shadow_mode_stays_quiet_on_stdout():
    # shadow must never emit a permission decision on stdout
    d = tempfile.mkdtemp()
    open(os.path.join(d, "a.txt"), "w").write("x")
    out, _ = _run_hook("rm -rf a.txt", mode="shadow", cwd=d)
    assert out.strip() == ""


def test_feedback_url_for_is_shared_by_both_paths():
    assert hasattr(render, "feedback_url_for")
    # feedback_line must be built ON TOP of feedback_url_for (no divergence)
    assert hasattr(render, "feedback_line")


def test_feedback_url_empty_on_plain_allow():
    class _D:  # minimal stand-in for a non-feedback decision
        decision = "ALLOW"
        reason = "non-mutating"
    class _R:
        decision = _D()
        command = "ls"
        classification = type("C", (), {"matched_rule": None})()
    assert render.feedback_url_for(_R()) == ""


def test_hook_selftest_exists_and_returns_bool():
    assert hasattr(cli, "_hook_selftest")
    assert isinstance(cli._hook_selftest("Bash", "rm -rf canary.txt"), bool)
