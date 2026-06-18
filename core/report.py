"""Shadow/enforce reporting + tamper-evidence verification."""
import json, os, hashlib
from collections import Counter

def load_receipts(path="demo_cli_receipts.jsonl"):
    rows = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line: rows.append(json.loads(line))
    return rows

def verify_chain(rows):
    prev = "0"*64
    for r in rows:
        body = {k:v for k,v in r.items() if k != "receipt_hash"}
        h = hashlib.sha256((json.dumps(body, sort_keys=True, separators=(",",":")) + prev).encode()).hexdigest()
        if h != r["receipt_hash"]: return False
        prev = r["receipt_hash"]
    return True

def shadow_report(path="demo_cli_receipts.jsonl"):
    rows = load_receipts(path)
    if not rows: return "No receipts yet."
    by = Counter(r["decision"] for r in rows)
    recovered = [r for r in rows if r.get("recovery_point")]
    caught = [r for r in rows if r["decision"] in ("BLOCK","ESCALATE")]
    L = ["="*68, "demo_cli — decision report (would-have-caught AND could-have-recovered)", "="*68,
         f"actions observed : {len(rows)}",
         f"allowed          : {by.get('ALLOW',0)}",
         f"reversible       : {by.get('REVERSIBLE',0)}   (recovery points captured: {len(recovered)})",
         f"escalated        : {by.get('ESCALATE',0)}",
         f"blocked          : {by.get('BLOCK',0)}",
         f"receipt chain    : {'INTACT' if verify_chain(rows) else 'TAMPERED'}", ""]
    if recovered:
        L.append("RECOVERABLE — a recovery point was captured before the action ran:")
        for r in recovered:
            L.append(f"   [{r['decision']}] {r['action_raw'][:60]}  ->  undo available")
    if caught:
        L.append("CONTAINED — required structural approval or blocked:")
        for r in caught:
            L.append(f"   [{r['decision']}] ({r['target_environment']}/{r['structural_approval']}) {r['action_raw'][:54]}")
    L.append("="*68)
    return "\n".join(L)
