"""Tests for the beta hardening fixes:

Trou 1  external / remote destructive commands escalate regardless of the
        local environment label (never downgraded to SANDBOX).
Trou 2  rm / mv resolve their real filesystem operand so a snapshot can fire,
        and multi-path rm refuses to imply partial recovery.
opaque  a `python -c` carrying dynamic execution escalates like curl | bash.
remote  a managed/remote Postgres URL is a non-recoverable surface.
"""
import os

from demo_cli.classify import classify_pipeline
from demo_cli.decide import decide, ESCALATE, SANDBOX
from demo_cli import recovery


# --- Trou 1 ----------------------------------------------------------------

EXTERNAL_IRREVERSIBLE = [
    "git push --force origin main",
    "terraform destroy -auto-approve",
    "kubectl delete deployment api",
    "aws ec2 terminate-instances --instance-ids i-123",
    "aws s3 rm s3://bucket/data --recursive",
]


def test_external_irreversible_escalates_even_in_dev():
    for cmd in EXTERNAL_IRREVERSIBLE:
        c = classify_pipeline(cmd)
        assert c.nonrecoverable_surface is not None, cmd
        d = decide(c, "development", recovery_captured=False)
        assert d.decision == ESCALATE, f"{cmd} -> {d.decision}"


def test_local_destructive_not_marked_external():
    for cmd in ["rm -rf ./build", "git reset --hard HEAD~1", "mv a b"]:
        c = classify_pipeline(cmd)
        assert c.nonrecoverable_surface is None, cmd


def test_structural_approval_lets_external_through():
    c = classify_pipeline("terraform destroy -auto-approve")
    d = decide(c, "production", recovery_captured=False, approval_ok=True)
    assert d.decision != ESCALATE


# --- new bounded local-destructive bash rules ------------------------------

def test_new_local_destroyers_are_recognised():
    for cmd in ["shred secret.txt", "truncate -s 0 app.log",
                "dd if=/dev/zero of=disk.img", "find . -name '*.tmp' -delete",
                "git clean -fdx"]:
        c = classify_pipeline(cmd)
        assert c.is_destructive, cmd


# --- opaque execution ------------------------------------------------------

def test_opaque_python_exec_escalates():
    for cmd in [
        "python3 -c \"import os; os.system('ls')\"",
        "python -c \"__import__('os').system('ls')\"",
        "python -c \"import subprocess; subprocess.call('ls', shell=True)\"",
    ]:
        c = classify_pipeline(cmd)
        assert c.remote_exec, cmd
        assert decide(c, "development", recovery_captured=False).decision == ESCALATE


def test_benign_python_c_not_flagged():
    c = classify_pipeline("python -c \"print('hello')\"")
    assert not c.remote_exec


# --- Trou 2 ----------------------------------------------------------------

def test_extract_single_rm_target(tmp_path):
    f = tmp_path / "important.txt"
    f.write_text("data")
    assert recovery.extract_path_operand(f"rm -rf {f}") == str(f)


def test_multi_path_rm_captures_the_common_directory(tmp_path):
    """v0.4 change, forced by a live incident (claude-code#76626).

    The old rule returned None for ANY multi-path rm, so the orchestrator
    escalated. That is right when we can only capture a subset - but it also
    refused the cases where we can capture EVERYTHING. An agent ran
    `rm -f Reports/report_*.txt Reports/report_*.png` meaning only to count the
    files. All local, all bounded, all trivially copyable. We escalated, and the
    files are permanently gone.

    Now: several paths under one capturable directory snapshot that DIRECTORY.
    A superset of the blast radius, so recovery stays PROVABLE, never partial.
    """
    a = tmp_path / "a.txt"; a.write_text("1")
    b = tmp_path / "b.txt"; b.write_text("2")
    assert recovery.extract_path_operand(f"rm -rf {a} {b}") == str(tmp_path)


def test_glob_is_expanded_before_the_shell_sees_it(monkeypatch, tmp_path):
    """The command has NOT been through the shell yet, so `Reports/*.png`
    arrives literally and os.path.exists() on it is False. If we do not expand
    it ourselves we see zero operands and snapshot nothing."""
    reports = tmp_path / "Reports"; reports.mkdir()
    for i in range(3):
        (reports / f"report_2026070{i}.txt").write_text("x")
        (reports / f"report_2026070{i}.png").write_text("x")
    (reports / "keep.md").write_text("unrelated")
    monkeypatch.chdir(tmp_path)

    cmd = "rm -f Reports/report_*.txt Reports/report_*.png"
    # the expansion IS the file count the agent was asking for
    assert len(recovery.expanded_operands(cmd)) == 6
    assert recovery.extract_path_operand(cmd) == str(reports)


def test_multi_path_rm_still_refuses_when_capture_would_be_absurd(tmp_path,
                                                                  monkeypatch):
    """The honesty invariant survives: a common root of $HOME (or / or a drive
    root) is NOT a capture surface, however small it happens to measure.
    `rm ~/a ~/b` must never quietly become "snapshot the whole home directory"."""
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("os.path.expanduser", lambda p: p.replace("~", str(home)))
    a = home / "a.txt"; a.write_text("1")
    b = home / "b.txt"; b.write_text("2")
    assert recovery.extract_path_operand(f"rm -rf {a} {b}") is None


def test_multi_path_rm_that_cannot_collapse_still_escalates(tmp_path):
    """Paths with no bounded common directory still return None -> escalate."""
    a = tmp_path / "a.txt"; a.write_text("1")
    assert recovery.extract_path_operand(f"rm -rf {a} /etc/hosts") is None


def test_extract_mv_protects_destination(tmp_path):
    src = tmp_path / "src.txt"; src.write_text("new")
    dst = tmp_path / "dst.txt"; dst.write_text("will be overwritten")
    assert recovery.extract_path_operand(f"mv {src} {dst}") == str(dst)


# --- remote Postgres -------------------------------------------------------

def test_remote_pg_detection():
    assert recovery.is_remote_pg("postgres://u:p@db.example.com:5432/app")
    assert recovery.is_remote_pg("postgresql://u:p@10.0.0.5/app")
    assert not recovery.is_remote_pg("postgres://u:p@localhost/app")
    assert not recovery.is_remote_pg("postgresql://u:p@127.0.0.1:5432/app")


# --- brace expansion (v0.4.0b6) --------------------------------------------
# Same failure mode as the glob one: the shell expands `{a,b}` and `{1..3}`
# BEFORE the command runs and UNCONDITIONALLY, so if we do not expand braces
# ourselves the snapshot never fires for a brace-delete.

def test_expand_braces_comma_list():
    assert recovery._expand_braces("file{1,2,3}.txt") == \
        ["file1.txt", "file2.txt", "file3.txt"]


def test_expand_braces_numeric_and_alpha_range():
    assert recovery._expand_braces("f{1..3}") == ["f1", "f2", "f3"]
    assert recovery._expand_braces("f{a..c}") == ["fa", "fb", "fc"]


def test_expand_braces_cartesian_and_nested():
    assert recovery._expand_braces("{a,b}{1,2}") == ["a1", "a2", "b1", "b2"]
    assert recovery._expand_braces("x{a,b{c,d}}") == ["xa", "xbc", "xbd"]


def test_expand_braces_no_comma_stays_literal():
    # A group with no top-level comma and no range is left untouched, as bash does.
    assert recovery._expand_braces("file{foo}.txt") == ["file{foo}.txt"]
    assert recovery._expand_braces("plain.txt") == ["plain.txt"]


def test_brace_delete_snapshots_the_common_directory(tmp_path, monkeypatch):
    """`rm -f file{1,2,3}.txt` deletes three files the shell names by expansion;
    demo_cli must see all three and capture their common directory."""
    for n in (1, 2, 3):
        (tmp_path / f"file{n}.txt").write_text("x")
    monkeypatch.chdir(tmp_path)
    cmd = "rm -f file{1,2,3}.txt"
    assert len(recovery.expanded_operands(cmd)) == 3
    assert recovery.extract_path_operand(cmd) == str(tmp_path)


def test_brace_and_glob_combined(tmp_path, monkeypatch):
    reports = tmp_path / "Reports"; reports.mkdir()
    for i in range(3):
        (reports / f"report_{i}.txt").write_text("x")
        (reports / f"report_{i}.png").write_text("x")
    monkeypatch.chdir(tmp_path)
    # braces expand first (-> two globs), then each glob expands to 3 files
    cmd = "rm -f Reports/report_*.{txt,png}"
    assert len(recovery.expanded_operands(cmd)) == 6
    assert recovery.extract_path_operand(cmd) == str(reports)


def test_expanded_operands_empty_for_non_rm_mv():
    # The crude operand split is only meaningful for rm / mv, so callers can
    # invoke expanded_operands unconditionally without getting garbage.
    assert recovery.expanded_operands("git commit -m message") == []
    assert recovery.expanded_operands("ls -la /tmp") == []


# --- project-root capture refusal (v0.4.0b6) -------------------------------

def _guard_in(root):
    from demo_cli.config import Config
    from demo_cli.guard import Guard
    cfg = Config(); cfg.project_root = str(root); cfg.mode = "enforce"
    return Guard(config=cfg)


def test_multi_file_rm_at_project_root_does_not_snapshot_whole_project(tmp_path,
                                                                       monkeypatch):
    """A two-file rm whose common root is the project root must NOT silently
    deep-copy the entire tree; it escalates instead."""
    (tmp_path / "a.txt").write_text("1")
    (tmp_path / "src").mkdir(); (tmp_path / "src" / "b.txt").write_text("2")
    monkeypatch.chdir(tmp_path)
    r = _guard_in(tmp_path).evaluate("rm -f a.txt src/b.txt")
    assert r.recovery_entry is None
    assert r.decision.decision == ESCALATE


def test_multi_file_rm_in_subdir_still_snapshots(tmp_path, monkeypatch):
    """The refusal is scoped to the project root itself: a common root that is a
    real SUBDIRECTORY (the incident case) still captures and stays reversible."""
    reports = tmp_path / "Reports"; reports.mkdir()
    (reports / "a.txt").write_text("1"); (reports / "b.txt").write_text("2")
    monkeypatch.chdir(tmp_path)
    r = _guard_in(tmp_path).evaluate("rm -rf Reports/a.txt Reports/b.txt")
    assert r.recovery_entry is not None
    assert r.affected_paths and len(r.affected_paths) == 2
