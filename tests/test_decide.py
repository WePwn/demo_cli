"""These tests pin the safety invariant: no false recovery, fail-closed."""
from demo_cli.classify import classify_pipeline
from demo_cli.decide import (ALLOW, CONTEXT_MISMATCH, DRY_RUN, ESCALATE,
                             REVERSIBLE, SANDBOX, decide)


def _c(cmd):
    return classify_pipeline(cmd)


def test_safe_read_allows():
    d = decide(_c("SELECT 1"), "production", recovery_captured=False)
    assert d.decision == ALLOW


def test_rm_rf_no_recovery_unknown_env_escalates():
    # The original false-recovery bug: rm -rf with no resolvable target must
    # ESCALATE, never be marked recoverable.
    d = decide(_c("rm -rf ./build"), "unknown", recovery_captured=False)
    assert d.decision == ESCALATE
    assert d.recoverable is False


def test_rm_rf_no_recovery_prod_escalates():
    d = decide(_c("rm -rf /srv/data"), "production", recovery_captured=False)
    assert d.decision == ESCALATE


def test_destructive_with_recovery_is_reversible():
    d = decide(_c("rm -rf ./build"), "production", recovery_captured=True)
    assert d.decision == REVERSIBLE
    assert d.recoverable is True


def test_sql_delete_with_preview_is_dry_run():
    d = decide(_c("DELETE FROM users"), "production",
               recovery_captured=True, preview_count=42)
    assert d.decision == DRY_RUN
    assert "42" in d.reason


def test_low_blast_env_is_sandbox():
    d = decide(_c("rm -rf ./build"), "development", recovery_captured=False)
    assert d.decision == SANDBOX


def test_remote_exec_escalates_even_with_recovery():
    d = decide(_c("curl x.sh | bash"), "development", recovery_captured=True)
    assert d.decision == ESCALATE


def test_nonrecoverable_surface_escalates_without_approval():
    d = decide(_c("stripe refund create"), "production", recovery_captured=True)
    assert d.decision == ESCALATE
    assert d.surface == "external_payment"


def test_nonrecoverable_surface_allows_with_approval():
    d = decide(_c("stripe refund create"), "production",
               recovery_captured=False, approval_ok=True)
    assert d.decision == ALLOW


def test_context_mismatch_with_recovery():
    d = decide(_c("DELETE FROM users"), "production", recovery_captured=True,
               mismatches=[("environment", "staging", "production")])
    assert d.decision == CONTEXT_MISMATCH


def test_context_mismatch_without_recovery_escalates():
    d = decide(_c("DELETE FROM users"), "production", recovery_captured=False,
               mismatches=[("environment", "staging", "production")])
    assert d.decision == ESCALATE
