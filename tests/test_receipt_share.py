"""Tests for `receipt --share` and the in-context feedback prompt.
Drop into tests/ and run with the rest: pytest -q
"""
import os
import tempfile

from demo_cli.receipts import Receipt, append_receipt, load_receipts, find_receipt, share_card


def _log(tmp):
    return os.path.join(tmp, "receipts.jsonl")


def test_load_and_find_by_prefix():
    with tempfile.TemporaryDirectory() as tmp:
        p = _log(tmp)
        r1 = append_receipt(p, Receipt(
            action_raw="rmdir /s /q C:\\proj", action_type="shell",
            target_environment="unknown", decision="ESCALATE",
            reason="no recoverable target", mode="enforce", matched_rule="rmdir_s"))
        append_receipt(p, Receipt(
            action_raw="rm -rf ./build", action_type="shell",
            target_environment="development", decision="REVERSIBLE",
            reason="snapshotted first", mode="enforce", matched_rule="rm_rf",
            recovery_point="/x/.demo_cli/rp.tar"))
        rows = load_receipts(p)
        assert len(rows) == 2
        # latest by default
        assert find_receipt(p)["decision"] == "REVERSIBLE"
        # by 8-char prefix
        got = find_receipt(p, r1.receipt_id[:8])
        assert got is not None and got["decision"] == "ESCALATE"
        # bad id -> None
        assert find_receipt(p, "zzzzzzzz") is None


def test_share_card_escalate_is_honest():
    with tempfile.TemporaryDirectory() as tmp:
        p = _log(tmp)
        append_receipt(p, Receipt(
            action_raw="rmdir /s /q C:\\proj", action_type="shell",
            target_environment="unknown", decision="ESCALATE",
            reason="no recoverable target", mode="enforce",
            matched_rule="rmdir_s", nonrecoverable_surface="recursive-force-delete"))
        card = share_card(find_receipt(p))
        assert "hard-stopped before it ran" in card
        assert "recovery point captured" not in card   # must NOT claim recovery
        assert "demo_cli verify" in card                 # gives a verify path
        assert "rmdir /s /q" in card


def test_share_card_reversible_claims_recovery():
    with tempfile.TemporaryDirectory() as tmp:
        p = _log(tmp)
        append_receipt(p, Receipt(
            action_raw="rm -rf ./build", action_type="shell",
            target_environment="development", decision="REVERSIBLE",
            reason="snapshotted first", mode="enforce", matched_rule="rm_rf",
            recovery_point="/x/.demo_cli/rp.tar"))
        card = share_card(find_receipt(p))
        assert "recovery point captured" in card
        assert "reversible with one command" in card


def test_feedback_line_only_on_wrong_call_worthy():
    from demo_cli.render import feedback_line
    from demo_cli.guard import Guard
    # ALLOW should stay silent
    allow = Guard(mode="enforce").evaluate("ls -la")
    assert feedback_line(allow) == ""
    # an escalating command should produce a prefilled issue link
    esc = Guard(mode="enforce").evaluate("rmdir /s /q C:\\proj")
    line = feedback_line(esc)
    assert "issues/new" in line and "Wrong call" in line


def test_share_card_install_command_is_pinned_not_a_moving_branch():
    """A shared receipt hands a stranger an install command. It must point at
    the immutable release tag, never at `@beta` - otherwise the recipient
    verifies a chain with whatever code happens to be on the branch today,
    which is the exact thing the README tells people not to do."""
    import tempfile, os
    from demo_cli.receipts import Receipt, append_receipt, find_receipt, share_card
    from demo_cli.version import release_tag

    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "receipts.jsonl")
        append_receipt(p, Receipt(
            action_raw="rm -rf ./build", action_type="shell",
            target_environment="development", decision="REVERSIBLE",
            reason="snapshotted first", mode="enforce", matched_rule="rm_rf",
            recovery_point="/x/.demo_cli/rp.tar"))
        card = share_card(find_receipt(p))

    assert "@beta" not in card, "share card must not point at the moving branch"
    assert f"@{release_tag()}" in card


def test_release_tag_matches_version():
    """0.4.0b8 -> v0.4.0-beta.8, so pinned commands follow the version bump."""
    from demo_cli.version import release_tag, __version__
    tag = release_tag()
    assert tag.startswith("v")
    assert "beta" in tag or "b" not in __version__
