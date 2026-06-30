"""Recovery: resolve the real target of a command and snapshot / restore it.

The single most important property of this module is what it does *not* do: it
never falls back to a default target. If a destructive command's target cannot
be resolved to the thing it will actually affect, `resolve_target` returns
None, the orchestrator captures nothing, and the decision is escalated. That is
how the "snapshot something unrelated and call it recovered" failure is removed
by construction.

Supported target kinds: sqlite (.db file), postgres (connection URL),
file, dir.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from typing import List, Optional

from .context import redact

_PG_URL = re.compile(r"\bpostgres(?:ql)?://\S+", re.I)
_DB_FILE = re.compile(r"[\w./\\-]+\.db\b")
_IGNORE = shutil.ignore_patterns(".git", "node_modules", "__pycache__", ".demo_cli", ".demo_cli_recovery")

# Upper bound on what we will copy for a directory snapshot. A snapshot we
# cannot take quickly is not a recovery we should silently promise: above this
# the orchestrator escalates honestly instead of copying a multi-GB tree.
# Override with DEMO_CLI_MAX_SNAPSHOT_MB.
_DEFAULT_MAX_SNAPSHOT_MB = 256


@dataclass
class Target:
    kind: str   # sqlite | postgres | file | dir
    ref: str    # absolute path or connection URL
    label: str  # redacted, human-safe


def resolve_target(cmd: str, explicit_db: Optional[str] = None,
                   db_url: Optional[str] = None, target_path: Optional[str] = None) -> Optional[Target]:
    """Resolve the target a command will affect. No default fallback."""
    # Postgres URL: explicit flag, then a URL embedded in the command.
    url = db_url
    if not url:
        m = _PG_URL.search(cmd)
        if m:
            url = m.group(0)
    if url:
        return Target("postgres", url, redact(url))

    # Explicit file / dir target.
    if target_path:
        ap = os.path.abspath(target_path)
        return Target("dir", ap, ap) if os.path.isdir(ap) else Target("file", ap, ap)

    # Explicit sqlite db, or a .db path literally named in the command.
    candidates: List[str] = []
    if explicit_db:
        candidates.append(explicit_db)
    m = _DB_FILE.search(cmd)
    if m:
        candidates.append(m.group(0))
    for cand in candidates:
        if os.path.exists(cand):
            ap = os.path.abspath(cand)
            return Target("sqlite", ap, ap)
    # An explicit --db that does not exist yet is still a declared target.
    if explicit_db:
        ap = os.path.abspath(explicit_db)
        return Target("sqlite", ap, ap)
    return None


# --------------------------------------------------------------------------
# Filesystem-operand extraction (Trou 2): make the snapshot actually fire on
# the auto-fire path for rm / mv, where the target is named in the command
# rather than passed as a flag.
# --------------------------------------------------------------------------

_RM_RE = re.compile(r"^\s*(?:sudo\s+)?rm\b", re.I)
_MV_RE = re.compile(r"^\s*(?:sudo\s+)?mv\b", re.I)


def _path_operands(cmd: str) -> List[str]:
    """Crude operand extraction: drop the leading command word(s) and any flags,
    keep the rest as candidate paths. Not a shell parser - good enough to find
    the target of a simple rm / mv."""
    out: List[str] = []
    for tok in cmd.strip().split():
        if tok in ("sudo", "rm", "mv"):
            continue
        if tok.startswith("-"):
            continue
        out.append(tok.strip("'\""))
    return out


def extract_path_operand(cmd: str) -> Optional[str]:
    """Return the single filesystem path an rm / mv will affect, or None.

    Honesty rule (the invariant): if an rm names *several* existing paths we
    return None rather than snapshot only one and imply full recovery - the
    orchestrator then escalates instead of claiming reversibility it can't
    deliver. For mv we protect the destination when it already exists (the
    overwrite case), otherwise the source.
    """
    if _RM_RE.search(cmd):
        existing = [p for p in _path_operands(cmd) if os.path.exists(p)]
        return existing[0] if len(existing) == 1 else None
    if _MV_RE.search(cmd):
        ops = _path_operands(cmd)
        if len(ops) >= 2:
            dst, src = ops[-1], ops[-2]
            if os.path.exists(dst):
                return dst
            if os.path.exists(src):
                return src
    return None


def is_remote_pg(ref: str) -> bool:
    """True if a Postgres connection string points somewhere other than the
    local machine. A pg_dump over the wire is not a recovery point we can stand
    behind for a system we do not control, so the orchestrator escalates these
    honestly rather than claiming reversibility."""
    try:
        from urllib.parse import urlparse
        host = (urlparse(ref).hostname or "").lower()
    except Exception:
        return False
    return host not in ("", "localhost", "127.0.0.1", "::1")


# --------------------------------------------------------------------------
# Recovery index (project-local)
# --------------------------------------------------------------------------

def _index_path(recovery_dir: str) -> str:
    return os.path.join(recovery_dir, "index.jsonl")


def _record(recovery_dir: str, entry: dict) -> None:
    os.makedirs(recovery_dir, exist_ok=True)
    with open(_index_path(recovery_dir), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _ts() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _new_id() -> str:
    """Short, human-typable recovery-point id (prefix-matchable)."""
    return uuid.uuid4().hex[:8]


def _max_snapshot_bytes() -> int:
    try:
        mb = float(os.environ.get("DEMO_CLI_MAX_SNAPSHOT_MB", _DEFAULT_MAX_SNAPSHOT_MB))
    except ValueError:
        mb = _DEFAULT_MAX_SNAPSHOT_MB
    return int(mb * 1024 * 1024)


def _dir_size(path: str, cap: int) -> int:
    """Sum file sizes under `path`, ignoring the same noise as the copy, and
    short-circuiting as soon as `cap` is exceeded (so we never walk a huge tree
    just to find out it is huge)."""
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in
                   (".git", "node_modules", "__pycache__", ".demo_cli", ".demo_cli_recovery")]
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
            if total > cap:
                return total
    return total


def snapshot(target: Optional[Target], recovery_dir: str, strategy: str = "snapshot",
             action: Optional[str] = None) -> Optional[dict]:
    """Capture a recovery point for a target. Returns the recovery entry or None.

    `strategy` comes from config: "snapshot" captures; "none"/"attest" never
    capture (attestation of an externally managed recovery point is reserved
    for a later release and is treated as non-recoverable here, by design,
    rather than claiming a recovery we have not verified).

    `action` is the human-readable thing that prompted the snapshot (a command
    or a file-edit), stored on the entry so `demo_cli log` can show *why* each
    recovery point exists.
    """
    if not target or strategy in ("none", "attest"):
        return None

    os.makedirs(recovery_dir, exist_ok=True)
    kind, ref, ts, rid = target.kind, target.ref, _ts(), _new_id()
    action = redact(action) if action else None

    def _entry(recovery_point: str) -> dict:
        e = {"id": rid, "kind": kind, "target": ref,
             "recovery_point": recovery_point, "ts": ts, "action": action}
        _record(recovery_dir, e)
        return e

    if kind in ("sqlite", "file"):
        if not os.path.exists(ref):
            return None
        bak = os.path.join(recovery_dir, f"{os.path.basename(ref)}.{ts}.{rid}.bak")
        shutil.copy2(ref, bak)
        return _entry(bak)

    if kind == "dir":
        if not os.path.isdir(ref):
            return None
        # Honesty + safety: refuse to "recover" a tree we cannot copy quickly.
        cap = _max_snapshot_bytes()
        if _dir_size(ref, cap) > cap:
            return None
        snap = os.path.join(recovery_dir, f"{os.path.basename(ref.rstrip('/'))}.{ts}.{rid}.snapdir")
        shutil.copytree(ref, snap, dirs_exist_ok=True, ignore=_IGNORE)
        return _entry(snap)

    if kind == "postgres":
        if not shutil.which("pg_dump"):
            return None
        dump = os.path.join(recovery_dir, f"pg.{ts}.{rid}.dump")
        try:
            r = subprocess.run(["pg_dump", "-Fc", "-f", dump, ref],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            if r.returncode != 0 or not os.path.exists(dump):
                return None
        except Exception:
            return None
        return _entry(dump)

    return None


def load_entries(recovery_dir: str) -> List[dict]:
    idx = _index_path(recovery_dir)
    entries: List[dict] = []
    if os.path.exists(idx):
        with open(idx, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass
    return entries


def latest(recovery_dir: str, target_ref: Optional[str] = None) -> Optional[dict]:
    entries = load_entries(recovery_dir)
    if target_ref:
        entries = [e for e in entries if e.get("target") == target_ref]
    return entries[-1] if entries else None


def find(recovery_dir: str, rid: str) -> Optional[dict]:
    """Find a recovery point by id (exact or unique prefix). Returns the entry,
    or None if nothing matches or the prefix is ambiguous."""
    matches = [e for e in load_entries(recovery_dir)
               if str(e.get("id", "")).startswith(rid)]
    return matches[-1] if len(matches) == 1 else None


def entry_size(entry: dict) -> int:
    """On-disk size of a recovery point (file or directory tree)."""
    rp = entry.get("recovery_point")
    if not rp or not os.path.exists(rp):
        return 0
    if os.path.isfile(rp):
        try:
            return os.path.getsize(rp)
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(rp):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def prune(recovery_dir: str, keep: Optional[int] = None,
          older_than_days: Optional[int] = None) -> List[dict]:
    """Delete recovery-point artefacts (not receipts - those are the audit
    trail and must stay chain-intact) and rewrite the index.

    `keep`: retain the N most recent points, delete the rest.
    `older_than_days`: delete points whose timestamp is older than the cutoff.
    With neither argument, nothing is deleted. Returns the removed entries.
    """
    entries = load_entries(recovery_dir)
    if not entries:
        return []

    doomed: List[dict] = []
    survivors: List[dict] = entries

    if older_than_days is not None:
        cutoff = datetime.datetime.now() - datetime.timedelta(days=older_than_days)
        keep_set, drop = [], []
        for e in survivors:
            try:
                when = datetime.datetime.strptime(e.get("ts", ""), "%Y%m%d-%H%M%S")
            except ValueError:
                when = datetime.datetime.now()  # undated: treat as fresh, keep
            (drop if when < cutoff else keep_set).append(e)
        doomed += drop
        survivors = keep_set

    if keep is not None and len(survivors) > keep:
        doomed += survivors[:-keep] if keep > 0 else survivors[:]
        survivors = survivors[-keep:] if keep > 0 else []

    for e in doomed:
        e["_freed_bytes"] = entry_size(e)
        rp = e.get("recovery_point")
        if rp and os.path.exists(rp):
            try:
                shutil.rmtree(rp) if os.path.isdir(rp) else os.remove(rp)
            except OSError:
                pass

    idx = _index_path(recovery_dir)
    if os.path.exists(idx):
        with open(idx, "w", encoding="utf-8") as f:
            for e in survivors:
                f.write(json.dumps(e) + "\n")
    return doomed


def restore_entry(entry: dict) -> bool:
    kind = entry.get("kind", "sqlite")
    rp, target = entry.get("recovery_point"), entry.get("target")
    if kind in ("sqlite", "file"):
        if not rp or not os.path.exists(rp):
            return False
        shutil.copy2(rp, target)
        return True
    if kind == "dir":
        if not rp or not os.path.isdir(rp):
            return False
        shutil.copytree(rp, target, dirs_exist_ok=True)
        return True
    if kind == "postgres":
        if not shutil.which("pg_restore") or not rp or not os.path.exists(rp):
            return False
        try:
            r = subprocess.run(["pg_restore", "--clean", "--if-exists", "-d", target, rp],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            return r.returncode == 0
        except Exception:
            return False
    return False
