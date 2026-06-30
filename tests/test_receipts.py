import json

from demo_cli.receipts import Receipt, append_receipt, verify_chain


def _write(path, decision="ALLOW", cmd="SELECT 1"):
    append_receipt(str(path), Receipt(
        action_raw=cmd, action_type="sql", target_environment="production",
        decision=decision, reason="r", mode="enforce",
    ))


def test_chain_intact(tmp_path):
    log = tmp_path / "receipts.jsonl"
    for i in range(5):
        _write(log, cmd=f"SELECT {i}")
    v = verify_chain(str(log))
    assert v.ok and v.entries == 5


def test_tamper_breaks_chain(tmp_path):
    log = tmp_path / "receipts.jsonl"
    for i in range(4):
        _write(log, cmd=f"SELECT {i}")
    lines = log.read_text().splitlines()
    r = json.loads(lines[1])
    r["action_raw"] = "SELECT tampered"
    lines[1] = json.dumps(r, sort_keys=True, separators=(",", ":"))
    log.write_text("\n".join(lines) + "\n")

    v = verify_chain(str(log))
    assert not v.ok
    assert v.broken_at == 2


def test_reorder_breaks_chain(tmp_path):
    log = tmp_path / "receipts.jsonl"
    for i in range(3):
        _write(log, cmd=f"SELECT {i}")
    lines = log.read_text().splitlines()
    lines[0], lines[1] = lines[1], lines[0]
    log.write_text("\n".join(lines) + "\n")
    assert not verify_chain(str(log)).ok


def test_redacts_connection_password(tmp_path):
    log = tmp_path / "receipts.jsonl"
    append_receipt(str(log), Receipt(
        action_raw="psql postgres://user:secret@host/db -c 'DELETE FROM t'",
        action_type="sql", target_environment="production",
        decision="ESCALATE", reason="r", mode="enforce",
    ))
    content = log.read_text()
    assert "secret" not in content and "***" in content
