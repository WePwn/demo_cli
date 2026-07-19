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

import contextlib
import datetime
import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from .context import redact
from .decide import INVARIANT
from .version import release_tag

RECEIPT_VERSION = "2.0"
GENESIS = "0" * 64


def _canon(d: dict) -> str:
    return json.dumps(d, sort_keys=True, separators=(",", ":"))


class ReceiptLockError(RuntimeError):
    """Raised when the sidecar receipt lock cannot be acquired in time."""


# Bounded so a stuck lock fails loudly instead of hanging a hook forever.
_LOCK_TIMEOUT_SECONDS = float(os.environ.get("DEMO_CLI_LOCK_TIMEOUT", "10"))
_LOCK_POLL_INTERVAL = 0.05


def _acquire(fh) -> None:
    """Take an exclusive, non-blocking lock on byte 0 of `fh`, retrying until
    `_LOCK_TIMEOUT_SECONDS` elapses. Raises ReceiptLockError rather than
    letting a caller proceed unlocked."""
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    if os.name == "nt":
        import msvcrt
        while True:
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise ReceiptLockError(
                        f"Could not acquire receipt lock on {fh.name!r} "
                        f"within {_LOCK_TIMEOUT_SECONDS}s.")
                time.sleep(_LOCK_POLL_INTERVAL)
    else:
        import fcntl
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise ReceiptLockError(
                        f"Could not acquire receipt lock on {fh.name!r} "
                        f"within {_LOCK_TIMEOUT_SECONDS}s.")
                time.sleep(_LOCK_POLL_INTERVAL)


def _release(fh) -> None:
    if os.name == "nt":
        import msvcrt
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _chain_lock(path: str):
    """Exclusive lock guarding the read-last-hash / append pair, held across
    the full critical section (read last hash -> finalize -> append -> flush
    -> fsync). POSIX uses fcntl.flock; Windows uses msvcrt.locking on one byte
    of the sidecar `.lock` file. Both sides poll with a bounded retry loop and
    raise ReceiptLockError instead of silently proceeding unlocked."""
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)

    fh = open(lock_path, "a+b")
    try:
        # msvcrt.locking needs at least one byte to lock; keep it non-empty
        # either way so byte-0 locking is always well defined.
        if os.fstat(fh.fileno()).st_size == 0:
            fh.write(b"\0")
            fh.flush()
            os.fsync(fh.fileno())

        _acquire(fh)
        try:
            yield
        finally:
            _release(fh)
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

    The read-last-hash / finalize / append / flush / fsync sequence is held
    under a single exclusive cross-platform file lock (see `_chain_lock`) so
    concurrent writers - threads, or separate processes such as real hooks -
    cannot read the same last hash and append competing receipts.
    """
    receipt.action_raw = redact(receipt.action_raw)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    with _chain_lock(path):
        receipt.prev_receipt_hash = last_hash(path)
        receipt.finalize()
        with open(path, "a", encoding="utf-8") as f:
            f.write(_canon(asdict(receipt)) + "\n")
            f.flush()
            os.fsync(f.fileno())
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


# writes and verifies) plus the shareable proof-card builder. They reuse the
# same _canon / hashing already defined above, so nothing else changes.


def load_receipts(path: str) -> List[dict]:
    """Return every receipt in the log, oldest first. Read-only."""
    rows: List[dict] = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def find_receipt(path: str, receipt_id: Optional[str] = None) -> Optional[dict]:
    """Pick a receipt: by (full or prefix) id if given, else the most recent.

    Matching by prefix mirrors how `log`/`undo` show short ids, so a user can
    paste the 8-char id they see rather than the full uuid.
    """
    rows = load_receipts(path)
    if not rows:
        return None
    if not receipt_id:
        return rows[-1]
    rid = receipt_id.strip()
    # exact first, then unique prefix
    for r in rows:
        if r.get("receipt_id") == rid:
            return r
    matches = [r for r in rows if str(r.get("receipt_id", "")).startswith(rid)]
    return matches[-1] if matches else None


def share_card(receipt: dict, *, repo: str = "github.com/WePwn/demo_cli") -> str:
    """Build a copy-pasteable, plain-text (markdown-safe) proof card for a
    single receipt, plus a one-line command anyone can run to verify the chain
    this receipt belongs to.

    Deliberately colour-free: the shared artifact is meant to be pasted into a
    forum/PR/issue, where ANSI codes would be noise. `action_raw` is already
    redacted at write time, so the card inherits that privacy property.
    """
    rid = str(receipt.get("receipt_id", "?"))
    short = rid[:8]
    decision = receipt.get("decision", "?")
    reason = receipt.get("reason", "")
    action = receipt.get("action_raw", "")
    rule = receipt.get("matched_rule")
    surface = receipt.get("nonrecoverable_surface")
    env = receipt.get("target_environment", "unknown")
    recovered = bool(receipt.get("recovery_point"))
    rhash = str(receipt.get("receipt_hash", ""))
    phash = str(receipt.get("prev_receipt_hash", ""))
    ts = receipt.get("timestamp", "")

    intent = receipt.get("declared_intent") or {}
    reasoning = ""
    if isinstance(intent, dict):
        reasoning = intent.get("reasoning") or ""

    outcome = ("recovery point captured — reversible with one command"
               if recovered else
               "hard-stopped before it ran — no honest recovery point exists for this")

    lines = [
        "```",
        "demo_cli — pre-execution receipt",
        "",
        f"  command      {action}",
        f"  decision     {decision}",
    ]
    if rule:
        lines.append(f"  matched      {rule}")
    if surface:
        lines.append(f"  surface      {surface}")
    lines += [
        f"  environment  {env}",
        f"  outcome      {outcome}",
    ]
    if reasoning:
        lines.append(f"  agent's why  {reasoning[:200]}")
    lines += [
        "",
        f"  receipt      {short}   {ts}",
        f"  hash         {rhash[:32]}…",
        f"  prev         {phash[:32]}…",
        "",
        "  tamper-evident: this receipt links to the one before it.",
        "  verify the chain yourself:",
        f"    pipx install git+https://{repo}.git@{release_tag()} && demo_cli verify",
        "```",
    ]
    return "\n".join(lines)
