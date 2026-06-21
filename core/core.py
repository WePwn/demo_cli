"""
demo_cli V0 - proof before permission. The action is the unit of trust.

Now led by the two differentiators the field lacks:
  • REVERSIBILITY  - capture a recovery point before a destructive action, undo in one command.
  • STRUCTURAL AUTH - the catastrophic, non-recoverable action needs an approval the agent can't forge.
The gate is table stakes; these two + the receipt ledger are the edge.
"""
from __future__ import annotations
import re, json, hashlib, uuid, datetime, os
from dataclasses import dataclass, asdict
from typing import Optional
from . import reversibility, approval

INVARIANT = "no_irreversible_mutation_of_a_protected_system_of_record"

_DESTRUCTIVE = [(rid, a, re.compile(rx, re.IGNORECASE | re.DOTALL)) for rid, a, rx in [
    ("sql_drop",        "sql",   r"\bDROP\s+(?:DATABASE|SCHEMA|TABLE)\b"),
    ("sql_truncate",    "sql",   r"\bTRUNCATE\b"),
    ("tf_destroy",      "infra", r"\bterraform\s+destroy\b"),
    ("kubectl_delete",  "infra", r"\bkubectl\s+delete\s+(?:namespace|ns|pv|pvc|deployment|deploy|sts)\b"),
    ("cloud_terminate", "infra", r"\b(?:aws|gcloud|az)\b[\w\s.-]*\b(?:delete|terminate|destroy)\b"),
    ("key_rotation",    "secret",r"\brotat(?:e|ing)\b[\w\s-]*\b(?:key|secret|credential)\b"),
    ("git_force_push",  "git",   r"\bgit\s+push\b[^\n]*(?:--force\b|-f\b)"),
    ("git_reset_hard",  "git",   r"\bgit\s+reset\s+--hard\b"),
]]
_PROD       = re.compile(r"\b(prod|production|prd)\b", re.IGNORECASE)
_SAFE_ENV   = re.compile(r"\b(dev|develop|local|localhost|staging|stage|test|sandbox|qa)\b", re.IGNORECASE)
_DELETE_NW  = re.compile(r"\bDELETE\s+FROM\b(?!.*\bWHERE\b)", re.IGNORECASE | re.DOTALL)
_RM         = re.compile(r"\brm\b", re.IGNORECASE)

def _rm_rf(cmd):
    for m in re.finditer(r"\brm\s+((?:-{1,2}[\w-]+\s*)+)", cmd, re.IGNORECASE):
        f = m.group(1).lower().replace("-", "")
        if ("r" in f or "recursive" in f) and ("f" in f or "force" in f):
            return True
    return False

def classify(command: str) -> dict:
    cmd = command.strip()
    matched, atype = None, "shell"
    for rid, a, rx in _DESTRUCTIVE:
        if rx.search(cmd):
            matched, atype = rid, a; break
    if not matched and _DELETE_NW.search(cmd):
        matched, atype = "sql_delete_no_where", "sql"
    if not matched and _RM.search(cmd) and _rm_rf(cmd):
        matched, atype = "fs_rm_rf", "shell"
    env = "prod" if _PROD.search(cmd) else ("dev" if _SAFE_ENV.search(cmd) else "unknown")
    return {"is_destructive": matched is not None, "matched_rule": matched,
            "action_type": atype, "environment": env}

ALLOW, REVERSIBLE, SANDBOX, ESCALATE, BLOCK = "ALLOW", "REVERSIBLE", "SANDBOX", "ESCALATE", "BLOCK"

def decide(c: dict, recoverable: bool, approved: bool) -> tuple[str, str]:
    """Disposition, led by reversibility, fail-closed on the unknown."""
    if not c["is_destructive"]:
        return ALLOW, "Non-destructive; outside the invariant."
    if recoverable:
        return REVERSIBLE, "Destructive, but a recovery point was captured first - reversible."
    if c["environment"] == "dev":
        return ALLOW, "Destructive but non-production; low blast radius."
    if c["environment"] == "prod":
        if approved:
            return ALLOW, "Catastrophic + non-recoverable, carried a valid structural approval the agent could not forge."
        return ESCALATE, "Catastrophic + non-recoverable on production; requires K-of-N approval the agent cannot produce."
    return ESCALATE, "Destructive with an undetermined target; fail-closed."

RECEIPT_VERSION = "1.1"
GENESIS = "0" * 64
def _canon(d): return json.dumps(d, sort_keys=True, separators=(",", ":"))
def _now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()

@dataclass
class Receipt:
    receipt_version: str; receipt_id: str; timestamp: str; session_id: str; agent_id: str
    action_type: str; action_raw: str; target_environment: str; invariant: str
    matched_rule: Optional[str]; classification: str; decision: str; mode: str; reason: str
    recovery_point: Optional[str]; structural_approval: str
    prev_receipt_hash: str; receipt_hash: str = ""
    def finalize(self):
        body = asdict(self); body.pop("receipt_hash")
        self.receipt_hash = hashlib.sha256((_canon(body) + self.prev_receipt_hash).encode()).hexdigest()
        return self

class Guard:
    """Out-of-band reference monitor (V0): classify -> capture recovery point /
    verify structural approval -> disposition -> bureau-grade receipt."""
    def __init__(self, mode="shadow", receipts_path="demo_cli_receipts.jsonl"):
        assert mode in ("shadow", "enforce")
        self.mode, self.receipts_path = mode, receipts_path

    def _last_hash(self):
        last = GENESIS
        if os.path.exists(self.receipts_path):
            with open(self.receipts_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try: last = json.loads(line)["receipt_hash"]
                        except Exception: pass
        return last

    def evaluate(self, command, target_path=None, approval_token=None, approver_key=None,
                 agent_id="agent-0", session_id="session-0"):
        c = classify(command)
        recoverable = c["is_destructive"] and reversibility.can_capture(target_path)
        recovery = reversibility.capture(target_path) if recoverable else None
        approved = bool(approval_token and approver_key and
                        approval.verify(command.strip(), approval_token, approver_key))
        decision, reason = decide(c, recoverable, approved)
        needs_approval = c["is_destructive"] and not recoverable and c["environment"] == "prod"
        sa = "approved" if approved else ("required-missing" if needs_approval else "n/a")
        r = Receipt(RECEIPT_VERSION, str(uuid.uuid4()), _now(), session_id, agent_id,
                    c["action_type"], command.strip(), c["environment"], INVARIANT,
                    c["matched_rule"], "destructive" if c["is_destructive"] else "safe",
                    decision, self.mode, reason,
                    recovery["recovery_point"] if recovery else None, sa,
                    self._last_hash()).finalize()
        with open(self.receipts_path, "a") as f:
            f.write(_canon(asdict(r)) + "\n")
        return r, recovery

    def allowed(self, command, **kw):
        r, _ = self.evaluate(command, **kw)
        return True if self.mode == "shadow" else r.decision in (ALLOW, REVERSIBLE)
