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
import glob as _glob
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
_PS_REMOVE_RE = re.compile(r"^\s*(?:Remove-Item|ri)\b", re.I)

# Brace expansion. The shell expands `{a,b}` and `{1..3}` BEFORE globbing and
# *unconditionally* - independent of what exists on disk - so `rm file{1,2,3}.txt`
# deletes three files even though none of them is named literally anywhere. This
# is the same failure mode as the glob one: the command reaches us as the literal
# string, so if we do not expand braces ourselves we see one non-existent operand
# and snapshot nothing. Bounded by _BRACE_MAX: a pathological expansion falls back
# to the literal token (we capture nothing for it -> the honest escalate path),
# never an unbounded blow-up.
_BRACE_MAX = 1024


def _split_top_commas(s: str) -> List[str]:
    """Split on commas at brace-depth 0 only, so nested groups stay intact."""
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch == "{":
            depth += 1
            cur.append(ch)
        elif ch == "}":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _expand_range(inner: str) -> Optional[List[str]]:
    """Expand a `{m..n}` numeric or `{a..z}` single-char range, or None."""
    m = re.fullmatch(r"(-?\d+)\.\.(-?\d+)", inner)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        step = 1 if b >= a else -1
        vals = range(a, b + step, step)
        return None if len(vals) > _BRACE_MAX else [str(v) for v in vals]
    m = re.fullmatch(r"([a-zA-Z])\.\.([a-zA-Z])", inner)
    if m:
        a, b = ord(m.group(1)), ord(m.group(2))
        step = 1 if b >= a else -1
        vals = range(a, b + step, step)
        return None if len(vals) > _BRACE_MAX else [chr(v) for v in vals]
    return None


def _first_brace_group(s: str) -> Optional[tuple]:
    """Index span (start, end) of the first balanced top-level {...}, or None."""
    depth, start = 0, -1
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                return start, i
    return None


def _expand_braces(token: str) -> List[str]:
    """Expand shell brace syntax in one operand, bash-style: `{a,b,c}` lists and
    `{m..n}` / `{a..z}` ranges, including nesting and cartesian products
    (`{a,b}{1,2}` -> a1 a2 b1 b2). A group with no top-level comma and no valid
    range (e.g. `{foo}`) stays literal, exactly as the shell leaves it. Falls
    back to the unexpanded token if the expansion would exceed _BRACE_MAX."""
    grp = _first_brace_group(token)
    if grp is None:
        return [token]
    a, b = grp
    pre, inner, post = token[:a], token[a + 1:b], token[b + 1:]
    options = _split_top_commas(inner)
    if len(options) <= 1:
        rng = _expand_range(inner)
        if rng is None:
            # Not expandable: keep this group literal, expand anything after it.
            return [token[:b + 1] + tail for tail in _expand_braces(post)]
        options = rng
    result: List[str] = []
    for opt in options:
        for opt_x in _expand_braces(opt):          # options may nest
            for tail in _expand_braces(post):
                result.append(pre + opt_x + tail)
                if len(result) > _BRACE_MAX:
                    return [token]                 # pathological -> literal
    return result


def _path_operands(cmd: str) -> List[str]:
    """Crude operand extraction: drop the leading command word(s) and any flags,
    keep the rest as candidate paths. Not a shell parser - good enough to find
    the target of a simple rm / mv.

    Braces and globs are expanded here the way the shell would expand them, in
    that order (braces first, then glob each result). The command we are handed
    has NOT been through the shell yet: `rm -f Reports/*.png` and
    `rm file{1,2,3}.txt` both arrive as literal strings, so os.path.exists() on
    them is False and the operand extractor would see nothing at all. Expanding
    them ourselves is the only way to know what the command will actually
    destroy - which is also, exactly, the thing the user needed to see.
    """
    out: List[str] = []
    for tok in cmd.strip().split():
        if tok in ("sudo", "rm", "mv"):
            continue
        if tok.startswith("-"):
            continue
        tok = tok.strip("'\"")
        for piece in _expand_braces(tok):
            if any(ch in piece for ch in "*?["):
                out.extend(sorted(_glob.glob(piece)))
            else:
                out.append(piece)
    return out


_PS_PATH_FLAGS = {"-literalpath", "-path"}


def _ps_remove_item_operand(cmd: str) -> Optional[str]:
    """Conservative PowerShell `Remove-Item` target extraction. Supports:

        Remove-Item -Recurse -Force ".\\victim"
        Remove-Item -LiteralPath ".\\victim" -Recurse -Force
        Remove-Item -Path ".\\victim" -Recurse -Force

    Not a shell parser - like `_path_operands`, good enough to find the single
    target of a simple call. Any flag other than -Path/-LiteralPath is skipped
    without consuming a value, so a command carrying an unsupported flag with
    its own argument (e.g. `-ErrorAction Stop`) leaves that argument looking
    like a second positional operand - deliberately, so it is treated as
    ambiguous below rather than guessed at. Returns None (never a target) when
    the operand count is not exactly one, or the operand contains a wildcard:
    the honesty rule is that a target this function cannot pin down exactly
    must not be snapshotted at all.
    """
    if not _PS_REMOVE_RE.search(cmd):
        return None
    tokens = cmd.strip().split()[1:]  # drop the leading Remove-Item / ri
    targets: List[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.lower() in _PS_PATH_FLAGS:
            i += 1
            if i < len(tokens):
                targets.append(tokens[i].strip("'\""))
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        targets.append(tok.strip("'\""))
        i += 1
    if len(targets) != 1:
        return None
    target = targets[0]
    if not target or any(ch in target for ch in "*?[]"):
        return None
    return target


def _too_broad(path: str) -> bool:
    """Capture surfaces we refuse however small they measure: the filesystem
    root, a Windows drive root, and $HOME. `rm ~/a ~/b` must never quietly
    become "snapshot the entire home directory"."""
    ap = os.path.abspath(path)
    if os.path.dirname(ap) == ap:                       # "/" or "C:\\"
        return True
    return ap == os.path.abspath(os.path.expanduser("~"))


def _common_capture_root(paths: List[str]) -> Optional[str]:
    """The one directory that provably contains every path the command can
    affect. Snapshotting it captures a SUPERSET of the damage, so the recovery
    stays provable rather than partial - which is the whole invariant. Returns
    None when no such directory exists, or when it would be absurdly broad."""
    # Two distinct Windows drive letters have no common root at all. Detect this
    # with ntpath (available on every OS) so it is caught even when the check
    # runs on a POSIX host, where backslash paths would otherwise be treated as
    # literal filenames and collapse to a bogus common root under the cwd.
    import ntpath
    drives = {ntpath.splitdrive(p)[0].upper() for p in paths}
    drives.discard("")
    if len(drives) > 1:
        return None
    try:
        root = os.path.commonpath([os.path.abspath(p) for p in paths])
    except ValueError:                                  # different drives (Windows)
        return None
    if not os.path.isdir(root):
        root = os.path.dirname(root)
    if not root or not os.path.isdir(root) or _too_broad(root):
        return None
    return root


def expanded_operands(cmd: str) -> List[str]:
    """The concrete list of existing paths an rm / mv will touch.

    Exposed so the preview can PRINT it. claude-code#76626: an agent ran
    `rm -f Reports/report_*.txt Reports/report_*.png` intending "just to check
    the current file count". The glob expansion IS the file count. Showing this
    list answers the question the agent was asking and makes the deletion
    impossible to approve by accident, in the same operation. The preview is not
    friction here - the preview is the task.

    Returns [] for anything that is not an rm / mv, so callers can invoke it
    unconditionally: the crude operand split is only meaningful for those two.
    """
    if not (_RM_RE.search(cmd) or _MV_RE.search(cmd)):
        return []
    return [p for p in _path_operands(cmd) if os.path.exists(p)]


def extract_path_operand(cmd: str) -> Optional[str]:
    """Return the filesystem path an rm / mv will affect, or None.

    Honesty rule (the invariant): never snapshot a SUBSET and imply full
    recovery.

    v0.4 - a multi-path rm used to return None unconditionally. That was too
    blunt, and it cost someone their work. Live incident, claude-code#76626: an
    agent ran

        rm -f Reports/report_*.txt Reports/report_*.png

    intending only to count the files. Every path was local, bounded and cheap
    to copy - precisely the case where full capture is PROVABLE - and the old
    rule escalated instead of capturing. Not in the recycle bin (no CLI delete
    ever is), not git-tracked, not in shadow copies. Permanently gone.

    So: several paths that collapse into one capturable directory now snapshot
    that DIRECTORY. It is a superset of everything the command can touch, so the
    recovery is still provable, never partial. Anything that does NOT collapse
    to one bounded directory still returns None and still escalates honestly.
    The size cap in snapshot() and the project-root bound in guard() both still
    apply on top of this.

    Two cautions for anyone touching this later:

    * This function is NOT the honesty boundary by itself. When only one operand
      exists among several, the result collapses to that single path even if it
      lies outside the project; it is the guard's `within` project-root check
      (guard.evaluate) that refuses to snapshot or claim reversibility for it.
      Do not reuse extract_path_operand at a new call site without that bound.
    * The directory return means undo is COARSE: restoring a multi-path rm
      restores the whole captured directory to its snapshot state (see
      restore_entry). That is what keeps recovery a provable superset, but it
      also rolls back unrelated edits made to other files in that directory
      after the snapshot. Immediately after the rm (the flagship flow) this is a
      non-issue; the window only matters if other writes land before undo.
    """
    if _RM_RE.search(cmd):
        # Collect every operand BEFORE deciding anything. Filtering to
        # os.path.exists() first and only then counting was the bug: an
        # operand that is a real, literal path but happens not to exist on
        # THIS machine (e.g. a Unix path like /etc/hosts checked from a
        # Windows host) would silently vanish from the count, so a genuinely
        # multi-target rm looked like a single-target one and collapsed to
        # that one target instead of escalating. A non-wildcard operand is a
        # real rm argument regardless of whether it currently exists; only
        # wildcard operands are existence-filtered (by the glob expansion
        # inside _path_operands itself, since a glob that matches nothing
        # touches nothing).
        ops = _path_operands(cmd)
        if len(ops) == 1:
            return ops[0] if os.path.exists(ops[0]) else None
        if len(ops) > 1:
            return _common_capture_root(ops)
        return None
    if _MV_RE.search(cmd):
        ops = _path_operands(cmd)
        if len(ops) >= 2:
            dst, src = ops[-1], ops[-2]
            if os.path.exists(dst):
                return dst
            if os.path.exists(src):
                return src
        return None
    if _PS_REMOVE_RE.search(cmd):
        target = _ps_remove_item_operand(cmd)
        return target if target and os.path.exists(target) else None
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
        # Coarse by design: copytree overlays the snapshot back onto the target,
        # bringing deleted files back and reverting modified ones to their
        # snapshot state, while leaving files created after the snapshot in
        # place. For a multi-path rm captured as its common directory this
        # restores the whole directory, which is exactly what makes the recovery
        # a provable superset - and also why an edit made to an unrelated file in
        # that directory after the snapshot would be rolled back here.
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
