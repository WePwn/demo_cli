"""The receipt ledger: an append-only, hash-chained, tamper-evident record of
every decision. Each receipt commits to the one before it, so any edit,
insertion, removal, or reordering breaks the chain and is detectable by
`verify_chain`.

Beyond the *what* (command, decision, recovery point), each receipt also
records the *why*: the agent's declared intent and stated reasoning. That is
the audit-grade artefact - not just that a mutation happened, but the context
and rationale it happened under.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from .context import redact
from .decide import INVARIANT

RECEIPT_VERSION = "2.0"
GENESIS = "0" * 64


def _canon(d: dict) -> str:
    return json.dumps(d, sort_keys=True, separators=(",", ":"))


import contextlib


@contextlib.contextmanager
def _chain_lock(path: str):
    """Exclusive lock guarding the read-last-hash / append pair. POSIX flock on
    a sidecar `.lock`; a no-op where fcntl is unavailable (e.g. Windows)."""
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX fallback
        yield
        return
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclass
class Receipt:
    action_raw: str
    action_type: str
    target_environment: str
    decision: str
    reason: str
    mode: str
    matched_rule: Optional[str] = None
    classification: str = "safe"
    recovery_point: Optional[str] = None
    nonrecoverable_surface: Optional[str] = None
    dry_run_affected_rows: Optional[int] = None
    context: Dict = field(default_factory=dict)
    declared_intent: Dict = field(default_factory=dict)
    context_mismatches: List = field(default_factory=list)
    pipeline_segments: List = field(default_factory=list)
    remote_exec: bool = False
    agent_id: str = "unknown"
    session_id: str = "unknown"
    invariant: str = INVARIANT
    receipt_version: str = RECEIPT_VERSION
    receipt_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=_now)
    prev_receipt_hash: str = GENESIS
    receipt_hash: str = ""

    def finalize(self) -> "Receipt":
        body = asdict(self)
        body.pop("receipt_hash")
        self.receipt_hash = hashlib.sha256(
            (_canon(body) + self.prev_receipt_hash).encode()).hexdigest()
        return self


def last_hash(path: str) -> str:
    last = GENESIS
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        last = json.loads(line)["receipt_hash"]
                    except Exception:
                        pass
    return last


def append_receipt(path: str, receipt: Receipt) -> Receipt:
    """Chain `receipt` to the log at `path` and persist it.

    The read-last-hash / write pair is held under an exclusive file lock so two
    agents firing at once cannot interleave and break the chain. The lock is a
    sidecar file (POSIX flock); on platforms without fcntl it degrades to a
    best-effort no-op rather than failing.
    """
    receipt.action_raw = redact(receipt.action_raw)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    with _chain_lock(path):
        receipt.prev_receipt_hash = last_hash(path)
        receipt.finalize()
        with open(path, "a", encoding="utf-8") as f:
            f.write(_canon(asdict(receipt)) + "\n")
    return receipt


@dataclass
class VerifyResult:
    ok: bool
    entries: int = 0
    head: str = GENESIS
    decisions: Dict[str, int] = field(default_factory=dict)
    broken_at: Optional[int] = None   # 1-based line number
    detail: Optional[str] = None


def verify_chain(path: str) -> VerifyResult:
    """Walk the receipt log end to end and report whether the hash chain holds."""
    if not os.path.exists(path):
        return VerifyResult(ok=False, detail="No receipt log found yet.")

    rows = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append((n, json.loads(line)))
            except Exception:
                return VerifyResult(ok=False, broken_at=n,
                                    detail="Line is not valid JSON; the log was altered.")

    prev = GENESIS
    for n, r in rows:
        stored = r.get("receipt_hash", "")
        body = {k: v for k, v in r.items() if k != "receipt_hash"}
        recomputed = hashlib.sha256(
            (_canon(body) + r.get("prev_receipt_hash", "")).encode()).hexdigest()
        if r.get("prev_receipt_hash") != prev:
            return VerifyResult(ok=False, broken_at=n,
                                detail="An entry was inserted, removed, or reordered.")
        if recomputed != stored:
            return VerifyResult(ok=False, broken_at=n,
                                detail="A field in this entry was edited after it was written.")
        prev = stored

    decisions: Dict[str, int] = {}
    for _, r in rows:
        d = r.get("decision", "?")
        decisions[d] = decisions.get(d, 0) + 1
    return VerifyResult(ok=True, entries=len(rows), head=prev, decisions=decisions)
