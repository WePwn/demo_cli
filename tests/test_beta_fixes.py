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


def test_extract_multi_path_rm_refuses(tmp_path):
    a = tmp_path / "a.txt"; a.write_text("1")
    b = tmp_path / "b.txt"; b.write_text("2")
    # two existing targets -> None, so the orchestrator escalates rather than
    # snapshotting only one and implying full recovery.
    assert recovery.extract_path_operand(f"rm -rf {a} {b}") is None


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
