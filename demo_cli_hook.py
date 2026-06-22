"""
demo_cli - pre-execution safety layer for destructive and context-sensitive actions.

This is a local demo hook. It does not replace human authorization. Its job is to
make risky actions recoverable and auditable before they run.

What it demonstrates:
- safe reads are allowed
- destructive SQL gets a dry-run preview and a snapshot
- other mutating actions get a recovery point when possible
- context mismatch is treated as an invariant problem, not only as danger detection
- chained / piped commands are split and classified segment by segment
- opaque remote execution (curl | bash) is treated as non-previewable and escalated
- after the action runs, `diff` shows exactly what changed
- the receipt chain can be checked end to end with `verify`
- every decision is written to a hash-chained receipt log

Usage:
  python demo_cli_hook.py "SELECT * FROM users" --db examples/production.db
  python demo_cli_hook.py "DELETE FROM users WHERE ..." --db examples/production.db
  python demo_cli_hook.py "UPDATE users SET ..." --db examples/production.db --intent-env staging
  python demo_cli_hook.py "DELETE FROM users WHERE ..." --db-url postgres://user:pass@host/db
  python demo_cli_hook.py "npx prettier --write ." --target src
  python demo_cli_hook.py undo
  python demo_cli_hook.py diff
  python demo_cli_hook.py verify
"""

import datetime
import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
RECEIPTS = os.path.join(HERE, "demo_cli_receipts.jsonl")
RECOVERY = os.path.join(HERE, ".demo_cli_recovery")
DEFAULT_DB = os.path.join(HERE, "examples", "production.db")
LEGACY_DB = os.path.join(HERE, "production.db")
GENESIS = "0" * 64
VERSION = "v0.3"
FEEDBACK_URL = "https://github.com/WePwn/demo_cli/issues"
SEP = "=" * 62

# start.sh sets this so the guided demo prints the feedback link exactly once
# at the end instead of after every box. Direct invocations still show it.
SUPPRESS_FEEDBACK = os.environ.get("DEMO_CLI_SUPPRESS_FEEDBACK") is not None

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[91m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "cyan": "\033[96m",
    "gray": "\033[90m",
}

DECISION_COLOR = {
    "ALLOW": "green",
    "DRY_RUN": "yellow",
    "REVERSIBLE": "yellow",
    "CONTEXT_MISMATCH": "red",
    "SANDBOX": "green",
    "ESCALATE": "red",
    "RESTORED": "green",
    "VERIFIED": "green",
    "TAMPERED": "red",
    "DIFF": "cyan",
}

_DESTRUCTIVE_RULES = [
    ("sql_drop", "sql", r"\bDROP\s+(?:DATABASE|TABLE|SCHEMA)\b"),
    ("sql_truncate", "sql", r"\bTRUNCATE\b"),
    ("sql_delete", "sql", r"\bDELETE\s+FROM\b"),
    ("tf_destroy", "infra", r"\bterraform\s+destroy\b"),
    ("kubectl_delete", "infra", r"\bkubectl\s+delete\s+(?:namespace|ns|pv|pvc|deploy|sts)\b"),
    ("cloud_delete", "infra", r"\b(?:aws|gcloud|az)\b[\w\s.-]*\b(?:delete|terminate|destroy)\b"),
    ("railway_drop", "infra", r"railway\s+run.*production.*(?:DROP|DELETE|TRUNCATE)"),
    ("railway_vol_del", "infra", r"railway\s+volume\s+delete"),
    ("git_force_push", "git", r"\bgit\s+push\b.*(?:--force|-f)\b"),
    ("git_reset_hard", "git", r"\bgit\s+reset\s+--hard\b"),
    # rm with both recursive and force, in either flag order (-rf or -fr)
    ("rm_rf", "shell", r"\brm\b(?=[^|;&]*\b-?[a-z]*r[a-z]*\b)(?=[^|;&]*\b-?[a-z]*f[a-z]*\b)[^|;&]*"),
    ("rmdir_s", "shell", r"\brmdir\b.*\/[sS]"),
    ("del_force", "shell", r"\bdel\b.*\/[fFsS]"),
    # destructive overwrite / move over existing paths shows up in chains
    ("mv_overwrite", "shell", r"\bmv\s+(?:-[a-z]*f[a-z]*\s+)?\S+\s+\S+"),
]

# Opaque remote execution: code is fetched and run in one step. It cannot be
# previewed or snapshotted because the payload is not known before it runs.
_REMOTE_EXEC = re.compile(
    r"(?:curl|wget|fetch)\b[^|]*\|\s*(?:sudo\s+)?(?:bash|sh|zsh|python\d?|node|ruby|perl)\b"
    r"|base64\s+-d[^|]*\|\s*(?:bash|sh)\b"
    r"|\beval\b"
    r"|\|\s*(?:bash|sh)\s+-c\b",
    re.I,
)

# Tools that mutate files indirectly (formatters, generators, package managers).
# These rarely look destructive, but they rewrite the working tree, which is the
# exact case raised in feedback: snapshot the path first so the change is visible.
_FILE_WRITERS = re.compile(
    r"\bprettier\b[^|;&]*--write"
    r"|\beslint\b[^|;&]*--fix"
    r"|\b(?:black|isort|gofmt|rustfmt)\b"
    r"|\b(?:npm|yarn|pnpm)\s+(?:install|add|remove|i)\b"
    r"|\bpip\s+install\b"
    r"|\b(?:npx|node)\b[^|;&]*(?:codegen|generate|migrate)\b",
    re.I,
)

_SQL_DELETE = re.compile(r"\bDELETE\s+FROM\b", re.I)
_SQL_TRUNCATE = re.compile(r"\bTRUNCATE\b", re.I)
_SQL_UPDATE = re.compile(r"\bUPDATE\s+\w+\s+SET\b", re.I)
_SQL_MUTATING = re.compile(r"\b(UPDATE|INSERT|DELETE|DROP|TRUNCATE|ALTER|CREATE|REPLACE)\b", re.I)
_SQL_READ = re.compile(r"^\s*SELECT\b", re.I)
_PG_URL = re.compile(r"\bpostgres(?:ql)?://\S+", re.I)
_PROD = re.compile(r"\b(prod|production|prd)\b", re.I)
_STAGE = re.compile(r"\b(staging|stage|stg)\b", re.I)
_DEV = re.compile(r"\b(dev|development|local|test|sandbox)\b", re.I)

ENV_ALIASES = {
    "prod": "production",
    "prd": "production",
    "production": "production",
    "stage": "staging",
    "stg": "staging",
    "staging": "staging",
    "dev": "development",
    "development": "development",
    "local": "development",
    "test": "test",
    "sandbox": "sandbox",
}


def color(text, name):
    if not USE_COLOR:
        return text
    return f"{COLORS.get(name, '')}{text}{COLORS['reset']}"


def decision_label(decision):
    return color(decision, DECISION_COLOR.get(decision, "gray"))


def redact(text):
    """Hide passwords inside connection strings before printing or logging."""
    if not text:
        return text
    return re.sub(r"(://[^:/@\s]+:)[^@/\s]+(@)", r"\1***\2", str(text))


def normalize_env(value):
    if not value:
        return "unknown"
    return ENV_ALIASES.get(str(value).strip().lower(), str(value).strip().lower())


def detect_env_from_text(text):
    if not text:
        return "unknown"
    if _PROD.search(text):
        return "production"
    if _STAGE.search(text):
        return "staging"
    if _DEV.search(text):
        return "development"
    return "unknown"


def detect_env(cmd, db_path=None, actual_env=None):
    explicit = normalize_env(actual_env)
    if explicit != "unknown":
        return explicit
    db_env = detect_env_from_text(os.path.basename(db_path or "") or (db_path or ""))
    if db_env != "unknown":
        return db_env
    cmd_env = detect_env_from_text(cmd)
    if cmd_env != "unknown":
        return cmd_env
    return "unknown"


def git_value(*args):
    try:
        p = subprocess.run(
            ["git", *args],
            cwd=HERE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
        value = p.stdout.strip()
        return value or "unknown"
    except Exception:
        return "unknown"


def context_fingerprint(cmd, db_path=None, actual_env=None):
    ctx = {
        "cwd": os.getcwd(),
        "repo_root": git_value("rev-parse", "--show-toplevel"),
        "branch": git_value("rev-parse", "--abbrev-ref", "HEAD"),
        "remote": git_value("config", "--get", "remote.origin.url"),
        "db_path": redact(db_path) if db_path else "unknown",
        "environment": detect_env(cmd, db_path, actual_env),
        "aws_profile": os.environ.get("AWS_PROFILE", "unknown"),
        "gcloud_project": os.environ.get("CLOUDSDK_CORE_PROJECT", "unknown"),
        "azure_subscription": os.environ.get("AZURE_SUBSCRIPTION_ID", "unknown"),
    }
    canon = json.dumps(ctx, sort_keys=True, separators=(",", ":"))
    ctx["fingerprint"] = hashlib.sha256(canon.encode()).hexdigest()[:16]
    return ctx


def compare_intent_to_context(intent, ctx):
    mismatches = []

    wanted_env = normalize_env(intent.get("env"))
    actual_env = normalize_env(ctx.get("environment"))
    if wanted_env != "unknown" and actual_env != "unknown" and wanted_env != actual_env:
        mismatches.append(("environment", wanted_env, actual_env))

    wanted_branch = intent.get("branch")
    actual_branch = ctx.get("branch")
    if wanted_branch and actual_branch and actual_branch != "unknown" and wanted_branch != actual_branch:
        mismatches.append(("branch", wanted_branch, actual_branch))

    wanted_remote = intent.get("remote")
    actual_remote = ctx.get("remote")
    if wanted_remote and actual_remote and actual_remote != "unknown" and wanted_remote not in actual_remote:
        mismatches.append(("remote", wanted_remote, actual_remote))

    wanted_cwd = intent.get("cwd")
    actual_cwd = ctx.get("cwd")
    if wanted_cwd and actual_cwd and not actual_cwd.endswith(wanted_cwd):
        mismatches.append(("cwd", wanted_cwd, actual_cwd))

    return mismatches


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def split_segments(cmd):
    """Split a command line on shell separators (| || && ; and newlines),
    while respecting single and double quotes. Returns a list of trimmed,
    non-empty segments. This is a demo-grade splitter, not a full shell parser."""
    segments = []
    buf = []
    quote = None
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        nxt = cmd[i + 1] if i + 1 < n else ""
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch in ("&", "|") and nxt == ch:  # && or ||
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in ("|", ";", "\n"):  # single pipe, semicolon, newline
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]


def classify(cmd):
    matched, atype = None, "shell"
    for rid, action_type, rx in _DESTRUCTIVE_RULES:
        if re.search(rx, cmd, re.I | re.S):
            matched, atype = rid, action_type
            break

    is_sql_read = bool(_SQL_READ.search(cmd))
    is_sql_mutating = bool(_SQL_MUTATING.search(cmd))
    is_file_writer = bool(_FILE_WRITERS.search(cmd))
    is_destructive = matched is not None
    is_mutating = is_destructive or is_sql_mutating or is_file_writer

    if is_sql_read or is_sql_mutating:
        atype = "sql"
    elif is_file_writer:
        atype = "filewrite"

    return {
        "is_destructive": is_destructive,
        "is_mutating": is_mutating,
        "is_sql_read": is_sql_read,
        "is_sql_mutating": is_sql_mutating,
        "is_file_writer": is_file_writer,
        "matched_rule": matched,
        "action_type": atype,
    }


def classify_pipeline(cmd):
    """Classify a possibly chained/piped command. Each segment is classified
    on its own; the pipeline inherits the strongest signal. Opaque remote
    execution is detected on the full string because the pipe is the payload."""
    segments = split_segments(cmd)
    remote_exec = bool(_REMOTE_EXEC.search(cmd))
    seg_results = []
    for seg in segments:
        sc = classify(seg)
        sc["segment"] = seg
        seg_results.append(sc)

    agg = {
        "is_destructive": any(s["is_destructive"] for s in seg_results),
        "is_mutating": any(s["is_mutating"] for s in seg_results),
        "is_sql_read": any(s["is_sql_read"] for s in seg_results),
        "is_sql_mutating": any(s["is_sql_mutating"] for s in seg_results),
        "is_file_writer": any(s.get("is_file_writer") for s in seg_results),
        "matched_rule": next((s["matched_rule"] for s in seg_results if s["matched_rule"]), None),
        "action_type": next((s["action_type"] for s in seg_results if s["is_destructive"]),
                            seg_results[0]["action_type"] if seg_results else "shell"),
        "segments": seg_results,
        "is_pipeline": len(seg_results) > 1,
        "remote_exec": remote_exec,
    }
    return agg


# --------------------------------------------------------------------------
# Target resolution: sqlite file, postgres URL, or arbitrary file / dir
# --------------------------------------------------------------------------

def find_db(cmd, explicit=None):
    candidates = []
    if explicit:
        candidates.append(explicit)
    m = re.search(r"[\w./\\-]+\.db", cmd)
    if m:
        candidates.append(m.group(0))
    candidates.extend([DEFAULT_DB, LEGACY_DB])

    for candidate in candidates:
        if not candidate:
            continue
        paths = [candidate, os.path.join(HERE, candidate)]
        for path in paths:
            if os.path.exists(path):
                return os.path.abspath(path)

    if explicit:
        return os.path.abspath(explicit)
    return None


def resolve_target(cmd, explicit_db=None, db_url=None, target_path=None):
    """Return a target descriptor: {'kind', 'ref', 'label'} or None.
    kind is one of: sqlite, postgres, file, dir."""
    url = db_url
    if not url:
        m = _PG_URL.search(cmd)
        if m:
            url = m.group(0)
    if url:
        return {"kind": "postgres", "ref": url, "label": redact(url)}

    if target_path:
        ap = os.path.abspath(target_path)
        if os.path.isdir(ap):
            return {"kind": "dir", "ref": ap, "label": ap}
        return {"kind": "file", "ref": ap, "label": ap}

    db = find_db(cmd, explicit_db)
    if db:
        return {"kind": "sqlite", "ref": db, "label": db}
    return None


# --------------------------------------------------------------------------
# Preview (sqlite + best-effort postgres)
# --------------------------------------------------------------------------

def parse_table_and_where(sql, verb):
    sql = sql.strip().rstrip(";")
    if verb == "DELETE":
        m = re.search(r"^\s*DELETE\s+FROM\s+(\w+)\s*(?:WHERE\s+(.+))?$", sql, re.I | re.S)
    elif verb == "UPDATE":
        m = re.search(r"^\s*UPDATE\s+(\w+)\s+SET\s+.+?(?:\s+WHERE\s+(.+))?$", sql, re.I | re.S)
    else:
        return None, None
    if not m:
        return None, None
    table = m.group(1)
    where = m.group(2).strip() if m.group(2) else None
    return table, where


def _preview_queries(sql):
    sql = sql.strip().rstrip(";")
    verb = sql.split()[0].upper() if sql.split() else ""
    if _SQL_TRUNCATE.search(sql):
        tbl = re.search(r"TRUNCATE\s+(?:TABLE\s+)?(\w+)", sql, re.I)
        if not tbl:
            return None, None
        table = tbl.group(1)
        return f"SELECT COUNT(*) FROM {table}", f"SELECT * FROM {table} LIMIT 5"
    if verb in {"DELETE", "UPDATE"}:
        table, where = parse_table_and_where(sql, verb)
        if not table:
            return None, None
        where_sql = f" WHERE {where}" if where else ""
        return f"SELECT COUNT(*) FROM {table}{where_sql}", f"SELECT * FROM {table}{where_sql} LIMIT 5"
    return None, None


def preview_sql(sql, db_path):
    count_q, preview_q = _preview_queries(sql)
    if not count_q:
        return None, [], []
    try:
        con = sqlite3.connect(db_path)
        count = con.execute(count_q).fetchone()[0]
        cur = con.execute(preview_q)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        con.close()
        return count, rows, cols
    except Exception:
        return None, [], []


def preview_pg(sql, url):
    """Best-effort preview using the psql client. No Python dependency."""
    if not shutil.which("psql"):
        return None, [], []
    count_q, preview_q = _preview_queries(sql)
    if not count_q:
        return None, [], []
    try:
        count_out = subprocess.run(
            ["psql", url, "-tAc", count_q],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10,
        ).stdout.strip()
        count = int(count_out) if count_out.isdigit() else None
        prev = subprocess.run(
            ["psql", url, "-A", "-F", " | ", "-c", preview_q + " LIMIT 5"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10,
        ).stdout.strip().splitlines()
        cols = [prev[0]] if prev else []
        rows = [[r] for r in prev[1:6]] if len(prev) > 1 else []
        return count, rows, cols
    except Exception:
        return None, [], []


# --------------------------------------------------------------------------
# Snapshots / recovery (type-aware)
# --------------------------------------------------------------------------

def _index_path():
    return os.path.join(RECOVERY, "index.jsonl")


def _record_recovery(entry):
    os.makedirs(RECOVERY, exist_ok=True)
    with open(_index_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def snapshot_target(target):
    """Capture a recovery point for any supported target kind.
    Returns the recovery entry dict, or None if not capturable."""
    if not target:
        return None
    kind, ref = target["kind"], target["ref"]
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    os.makedirs(RECOVERY, exist_ok=True)

    if kind == "sqlite" or kind == "file":
        if not os.path.exists(ref):
            return None
        bak = os.path.join(RECOVERY, f"{os.path.basename(ref)}.{ts}.bak")
        shutil.copy2(ref, bak)
        entry = {"kind": kind, "target": ref, "recovery_point": bak, "ts": ts}
        _record_recovery(entry)
        return entry

    if kind == "dir":
        if not os.path.isdir(ref):
            return None
        snap = os.path.join(RECOVERY, f"{os.path.basename(ref.rstrip('/'))}.{ts}.snapdir")
        shutil.copytree(ref, snap, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git", "node_modules",
                                                      ".demo_cli_recovery", "__pycache__"))
        entry = {"kind": "dir", "target": ref, "recovery_point": snap, "ts": ts}
        _record_recovery(entry)
        return entry

    if kind == "postgres":
        if not shutil.which("pg_dump"):
            return None
        dump = os.path.join(RECOVERY, f"pg.{ts}.dump")
        try:
            r = subprocess.run(["pg_dump", "-Fc", "-f", dump, ref],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            if r.returncode != 0 or not os.path.exists(dump):
                return None
        except Exception:
            return None
        entry = {"kind": "postgres", "target": ref, "recovery_point": dump, "ts": ts}
        _record_recovery(entry)
        return entry

    return None


def _load_recovery_entries():
    idx = _index_path()
    entries = []
    if os.path.exists(idx):
        with open(idx, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    return entries


def _latest_recovery(target_ref=None):
    entries = _load_recovery_entries()
    if target_ref:
        entries = [e for e in entries if e.get("target") == target_ref]
    return entries[-1] if entries else None


def restore_entry(entry):
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
        # restore snapshotted files over the target (additive restore for the demo)
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


def restore_latest():
    entry = _latest_recovery()
    if not entry:
        box("ESCALATE", [color("  No recovery points found.", "red")], feedback=True)
        return
    ok = restore_entry(entry)
    if ok:
        box("RESTORED", [
            title("Recovery", "green"),
            kv("kind", entry.get("kind", "sqlite")),
            kv("target", redact(entry["target"])),
            kv("from", os.path.basename(entry["recovery_point"])),
            color("  Restored from the latest recovery point.", "green"),
        ], feedback=True)
    else:
        box("ESCALATE", [
            title("Recovery", "red"),
            color("  Recovery point could not be restored.", "red"),
            kv("kind", entry.get("kind", "sqlite")),
            kv("from", os.path.basename(entry.get("recovery_point", "unknown"))),
        ], feedback=True)


# --------------------------------------------------------------------------
# diff: what actually changed since the latest recovery point
# --------------------------------------------------------------------------

def _sqlite_tables(path):
    con = sqlite3.connect(path)
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    finally:
        con.close()
    return names


def _sqlite_rows(path, table):
    con = sqlite3.connect(path)
    try:
        cur = con.execute(f"SELECT * FROM {table}")
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [tuple(r) for r in cur.fetchall()]
    finally:
        con.close()
    return cols, rows


def diff_sqlite(before, after, limit=8):
    lines = []
    try:
        t_before = set(_sqlite_tables(before))
        t_after = set(_sqlite_tables(after))
    except Exception:
        return [color("  Could not open one of the databases for diff.", "red")]

    for t in sorted(t_after - t_before):
        lines.append(kv(t, color("table created", "green")))
    for t in sorted(t_before - t_after):
        lines.append(kv(t, color("table dropped", "red")))

    for t in sorted(t_before & t_after):
        try:
            _, rb = _sqlite_rows(before, t)
            _, ra = _sqlite_rows(after, t)
        except Exception:
            continue
        sb, sa = set(rb), set(ra)
        removed = [r for r in rb if r not in sa]
        added = [r for r in ra if r not in sb]
        if not removed and not added:
            continue
        lines.append(kv(t, f"{color('-' + str(len(removed)), 'red')}  "
                           f"{color('+' + str(len(added)), 'green')}  rows"))
        for r in removed[:limit]:
            lines.append("    " + color("- " + " | ".join(str(x) for x in r), "red"))
        for r in added[:limit]:
            lines.append("    " + color("+ " + " | ".join(str(x) for x in r), "green"))
    if not lines:
        lines.append(color("  No row-level changes detected.", "dim"))
    return lines


def _dir_manifest(root):
    manifest = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "node_modules", "__pycache__", ".demo_cli_recovery")]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            try:
                with open(full, "rb") as f:
                    manifest[rel] = hashlib.sha256(f.read()).hexdigest()
            except Exception:
                manifest[rel] = "unreadable"
    return manifest


def _text_diff(path_before, path_after, rel):
    try:
        with open(path_before, encoding="utf-8") as f:
            a = f.read().splitlines()
        with open(path_after, encoding="utf-8") as f:
            b = f.read().splitlines()
    except Exception:
        return []
    out = []
    for ln in difflib.unified_diff(a, b, fromfile="before/" + rel, tofile="after/" + rel, lineterm=""):
        if ln.startswith("+") and not ln.startswith("+++"):
            out.append("    " + color(ln, "green"))
        elif ln.startswith("-") and not ln.startswith("---"):
            out.append("    " + color(ln, "red"))
        elif ln.startswith("@@"):
            out.append("    " + color(ln, "cyan"))
    return out[:40]


def diff_dir(snap, current):
    before = _dir_manifest(snap)
    after = _dir_manifest(current)
    lines = []
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    for k in added:
        lines.append(kv(k, color("added", "green")))
    for k in removed:
        lines.append(kv(k, color("deleted", "red")))
    for k in modified:
        lines.append(kv(k, color("modified", "yellow")))
        lines += _text_diff(os.path.join(snap, k), os.path.join(current, k), k)
    if not lines:
        lines.append(color("  No file changes detected.", "dim"))
    return lines


def diff_file(snap, current):
    if not os.path.exists(snap) or not os.path.exists(current):
        return [color("  Missing file for diff.", "red")]
    out = _text_diff(snap, current, os.path.basename(current))
    if out:
        return out
    h1 = hashlib.sha256(open(snap, "rb").read()).hexdigest()
    h2 = hashlib.sha256(open(current, "rb").read()).hexdigest()
    if h1 == h2:
        return [color("  File is unchanged.", "dim")]
    return [kv("before sha256", h1[:16]), kv("after sha256", h2[:16]),
            color("  Binary file changed.", "yellow")]


def run_diff(target_ref=None):
    entry = _latest_recovery(target_ref)
    if not entry:
        box("ESCALATE", [color("  No recovery points to diff against.", "red")], feedback=True)
        return
    kind = entry.get("kind", "sqlite")
    rp, target = entry["recovery_point"], entry["target"]
    header = [
        title("Diff", "cyan"),
        kv("kind", kind),
        kv("target", redact(target)),
        kv("baseline", os.path.basename(rp)),
        "",
        title("What changed", "cyan"),
    ]
    if kind == "sqlite":
        body = diff_sqlite(rp, target)
    elif kind == "dir":
        body = diff_dir(rp, target)
    elif kind == "file":
        body = diff_file(rp, target)
    elif kind == "postgres":
        body = [color("  Postgres diff compares dumps; restore into a scratch DB to inspect rows.", "dim"),
                kv("dump", os.path.basename(rp))]
    else:
        body = [color("  Unsupported target kind for diff.", "red")]
    box("DIFF", header + body, feedback=True)


# --------------------------------------------------------------------------
# Receipts + verify
# --------------------------------------------------------------------------

def _last_hash():
    last = GENESIS
    if os.path.exists(RECEIPTS):
        with open(RECEIPTS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = json.loads(line)["receipt_hash"]
                except Exception:
                    pass
    return last


def write_receipt(cmd, action_type, env, decision, reason, rule, bak,
                  dry_count=None, context=None, intent=None, mismatches=None,
                  segments=None, remote_exec=False):
    prev = _last_hash()
    body = {
        "receipt_version": "1.4",
        "receipt_id": str(uuid.uuid4()),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "agent_id": "local-demo",
        "session_id": "live",
        "action_type": action_type,
        "action_raw": redact(cmd.strip()),
        "target_environment": env,
        "invariant": "mutating_actions_must_be_recoverable_and_match_the_declared_context",
        "matched_rule": rule,
        "classification": "destructive" if rule else ("mutating" if decision in {"REVERSIBLE", "CONTEXT_MISMATCH"} else "safe"),
        "decision": decision,
        "mode": "enforce",
        "reason": reason,
        "recovery_point": bak,
        "dry_run_affected_rows": dry_count,
        "context": context or {},
        "declared_intent": intent or {},
        "context_mismatches": mismatches or [],
        "pipeline_segments": segments or [],
        "remote_exec": bool(remote_exec),
        "prev_receipt_hash": prev,
    }
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"))
    body["receipt_hash"] = hashlib.sha256((canon + prev).encode()).hexdigest()
    with open(RECEIPTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n")


def verify_chain():
    """Walk the receipt log end to end and report whether the hash chain holds."""
    if not os.path.exists(RECEIPTS):
        box("ESCALATE", [color("  No receipt log found yet.", "red")], feedback=True)
        return 1

    rows = []
    with open(RECEIPTS, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append((n, json.loads(line)))
            except Exception:
                box("TAMPERED", [
                    title("Receipt chain", "red"),
                    kv("line", n),
                    color("  Line is not valid JSON. The log was altered.", "red"),
                ], feedback=True)
                return 1

    prev = GENESIS
    for n, r in rows:
        stored = r.get("receipt_hash", "")
        body = {k: v for k, v in r.items() if k != "receipt_hash"}
        canon = json.dumps(body, sort_keys=True, separators=(",", ":"))
        recomputed = hashlib.sha256((canon + r.get("prev_receipt_hash", "")).encode()).hexdigest()

        if r.get("prev_receipt_hash") != prev:
            box("TAMPERED", [
                title("Receipt chain", "red"),
                kv("broken at", f"entry {rows.index((n, r)) + 1} (line {n})"),
                kv("expected prev", prev[:16] + "..."),
                kv("found prev", str(r.get("prev_receipt_hash"))[:16] + "..."),
                color("  An entry was inserted, removed, or reordered.", "red"),
            ], feedback=True)
            return 1
        if recomputed != stored:
            box("TAMPERED", [
                title("Receipt chain", "red"),
                kv("broken at", f"entry {rows.index((n, r)) + 1} (line {n})"),
                kv("action", str(r.get("action_raw"))[:80]),
                kv("expected hash", recomputed[:16] + "..."),
                kv("stored hash", stored[:16] + "..."),
                color("  A field in this entry was edited after it was written.", "red"),
            ], feedback=True)
            return 1
        prev = stored

    decisions = {}
    for _, r in rows:
        decisions[r.get("decision")] = decisions.get(r.get("decision"), 0) + 1
    summary = ", ".join(f"{k}:{v}" for k, v in sorted(decisions.items()))
    box("VERIFIED", [
        title("Receipt chain", "green"),
        kv("entries", len(rows)),
        kv("head", prev[:16] + "..."),
        kv("decisions", summary or "none"),
        color("  Chain intact. Every entry links to the one before it.", "green"),
    ], feedback=True)
    return 0


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

def kv(label, value):
    return f"  {color(str(label).ljust(18), 'dim')}{value}"


def title(text, tone="cyan"):
    return color(text, tone)


def feedback_line():
    return f"Feedback? Open an issue: {FEEDBACK_URL}"


def box(decision, lines, feedback=False):
    print()
    print(color(SEP, "gray"))
    print(f" demo_cli {VERSION} [{decision_label(decision)}]")
    print(color(SEP, "gray"))
    for line in lines:
        print(f" {line}")
    if feedback and not SUPPRESS_FEEDBACK:
        print()
        print(f" {color(feedback_line(), 'dim')}")
    print(color(SEP, "gray"))
    print()


def segment_lines(c):
    """Render the per-segment breakdown for chained / piped commands."""
    if not c.get("is_pipeline") and not c.get("remote_exec"):
        return []
    lines = ["", title("Pipeline", "cyan")]
    if c.get("remote_exec"):
        lines.append(kv("remote exec", color("fetch-and-run detected", "red")))
    for s in c.get("segments", []):
        tag = s["matched_rule"] or ("sql_mutation" if s["is_sql_mutating"]
                                    else ("sql_read" if s["is_sql_read"] else "plain"))
        tone = "red" if s["is_destructive"] else ("yellow" if s["is_mutating"] else "green")
        lines.append(kv(color(tag, tone), s["segment"][:80]))
    return lines


def format_preview_lines(cmd, count, rows, cols, bak, ctx=None):
    lines = [
        title("Action", "cyan"),
        kv("SQL", cmd[:110]),
        kv("type", "mutating SQL with preview"),
        kv("rows affected", count),
    ]
    if ctx:
        lines += [
            "",
            title("Context fingerprint", "cyan"),
            kv("environment", ctx.get("environment", "unknown")),
            kv("database", ctx.get("db_path", "unknown")),
            kv("fingerprint", ctx.get("fingerprint", "unknown")),
        ]
    if rows:
        lines += ["", title("Preview", "yellow")]
        if cols:
            lines.append(kv("columns", " | ".join(cols)))
        for r in rows:
            lines.append("  " + " | ".join(str(x) for x in r))
    if bak:
        lines += [
            "",
            title("Recovery", "green"),
            kv("snapshot", os.path.basename(bak)),
            kv("undo", "python demo_cli_hook.py undo"),
            kv("diff", "python demo_cli_hook.py diff"),
            color("  Recovery point captured. This action can be undone.", "green"),
        ]
    return lines


def extract_flag(args, name):
    if name not in args:
        return None, args
    i = args.index(name)
    if i + 1 >= len(args):
        print(f"Missing value for {name}")
        sys.exit(2)
    value = args[i + 1]
    return value, args[:i] + args[i + 2:]


def usage():
    print('Usage: python demo_cli_hook.py "command" [--db path] [--db-url url] [--target path] [--intent-env staging]')
    print('       python demo_cli_hook.py undo')
    print('       python demo_cli_hook.py diff   [--target path]')
    print('       python demo_cli_hook.py verify')


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    global USE_COLOR

    if len(sys.argv) >= 2 and sys.argv[1] == "undo":
        restore_latest()
        sys.exit(0)

    if len(sys.argv) >= 2 and sys.argv[1] == "verify":
        sys.exit(verify_chain())

    if len(sys.argv) >= 2 and sys.argv[1] == "diff":
        args = sys.argv[2:]
        tgt, _ = extract_flag(args, "--target")
        ref = os.path.abspath(tgt) if tgt else None
        run_diff(ref)
        sys.exit(0)

    if len(sys.argv) < 2:
        usage()
        sys.exit(1)

    args = sys.argv[1:]
    if "--no-color" in args:
        USE_COLOR = False
        args.remove("--no-color")

    explicit_db, args = extract_flag(args, "--db")
    db_url, args = extract_flag(args, "--db-url")
    target_path, args = extract_flag(args, "--target")
    intent_env, args = extract_flag(args, "--intent-env")
    intent_branch, args = extract_flag(args, "--intent-branch")
    intent_cwd, args = extract_flag(args, "--intent-cwd")
    intent_remote, args = extract_flag(args, "--intent-remote")
    intent_scope, args = extract_flag(args, "--intent-scope")
    actual_env, args = extract_flag(args, "--actual-env")

    cmd = " ".join(args).strip()
    if not cmd:
        usage()
        sys.exit(1)

    c = classify_pipeline(cmd)
    target = resolve_target(cmd, explicit_db, db_url, target_path)
    explicit_target = bool(target_path) and target is not None and target["kind"] in ("file", "dir")
    db_path = target["ref"] if target else None
    db_label = target["label"] if target else None
    ctx = context_fingerprint(cmd, db_label, actual_env)
    env = ctx["environment"]
    rule = c["matched_rule"]
    intent = {
        "env": normalize_env(intent_env),
        "branch": intent_branch,
        "cwd": intent_cwd,
        "remote": intent_remote,
        "scope": intent_scope,
    }
    mismatches = compare_intent_to_context(intent, ctx)
    seg_summary = [s["segment"] for s in c.get("segments", [])] if c.get("is_pipeline") else []

    # --- Opaque remote execution: fetch-and-run cannot be previewed or snapshotted.
    if c["remote_exec"]:
        write_receipt(cmd, "shell", env, "ESCALATE",
                      "Opaque remote execution (fetch-and-run). Payload unknown before it runs.",
                      rule, None, None, ctx, intent, [], seg_summary, True)
        box("ESCALATE", [
            title("Action", "cyan"),
            kv("command", cmd[:110]),
            kv("type", "remote fetch-and-run"),
        ] + segment_lines(c) + [
            "",
            title("Decision", "red"),
            color("  Cannot preview or snapshot: the code is downloaded and run in one step.", "red"),
            "",
            title("Safe next step", "yellow"),
            "  1. Download to a file first, review it, then run it.",
            "  2. Or pin a known checksum and verify before executing.",
            "",
            "  The agent should not pipe a remote script straight into a shell.",
        ], feedback=True)
        sys.exit(1)

    # --- Context mismatch: reasonable mutating action pointed at the wrong context.
    if c["is_mutating"] and mismatches:
        bak_entry = snapshot_target(target) if target else None
        bak = bak_entry["recovery_point"] if bak_entry else None
        lines = [
            title("Action", "cyan"),
            kv("command", cmd[:110]),
            kv("type", "mutating action"),
        ] + segment_lines(c) + [
            "",
            title("Context fingerprint", "cyan"),
            kv("intended env", intent.get("env") or "unknown"),
            kv("actual env", ctx.get("environment", "unknown")),
            kv("database", ctx.get("db_path", "unknown")),
            kv("git branch", ctx.get("branch", "unknown")),
            kv("git remote", ctx.get("remote", "unknown")),
            kv("fingerprint", ctx.get("fingerprint", "unknown")),
            "",
            title("Decision", "red"),
            color("  CONTEXT_MISMATCH detected before execution.", "red"),
        ]
        for name, wanted, actual in mismatches:
            lines.append(kv(name, f"intended {wanted}, actual {actual}"))
        if intent_scope:
            lines.append(kv("task scope", intent_scope))
        if bak:
            lines += [
                "",
                title("Recovery", "green"),
                kv("snapshot", os.path.basename(bak)),
                kv("undo", "python demo_cli_hook.py undo"),
                kv("diff", "python demo_cli_hook.py diff"),
                color("  Recovery point captured before continuing.", "green"),
            ]
            reason = "Mutating action did not match declared context. Snapshot taken first."
        else:
            lines += [
                "",
                title("Recovery", "red"),
                color("  No snapshot path found. Human review required before proceeding.", "red"),
            ]
            reason = "Mutating action did not match declared context and no snapshot path was available."

        write_receipt(cmd, c["action_type"], env, "CONTEXT_MISMATCH", reason, rule, bak,
                      None, ctx, intent, mismatches, seg_summary, False)
        box("CONTEXT_MISMATCH", lines, feedback=True)
        sys.exit(0 if bak else 1)

    # --- Safe read. An explicit --target opts the path into recoverability even
    # when the command text looks benign (the formatter / generator case).
    if not c["is_mutating"] and not c["is_destructive"] and not explicit_target:
        write_receipt(cmd, c["action_type"], env, "ALLOW", "Non-mutating action cleared.",
                      rule, None, None, ctx, intent, [], seg_summary, False)
        box("ALLOW", [
            title("Decision", "green"),
            color("  Safe read or non-mutating action.", "green"),
            kv("recovery", "not needed"),
            kv("fingerprint", ctx["fingerprint"]),
        ] + segment_lines(c))
        sys.exit(0)

    # --- DELETE / UPDATE / TRUNCATE: show what would be affected when possible.
    if _SQL_DELETE.search(cmd) or _SQL_UPDATE.search(cmd) or _SQL_TRUNCATE.search(cmd):
        bak_entry = snapshot_target(target) if target else None
        bak = bak_entry["recovery_point"] if bak_entry else None

        if target and target["kind"] == "postgres":
            count, rows, cols = preview_pg(cmd, target["ref"])
        elif target and target["kind"] == "sqlite":
            count, rows, cols = preview_sql(cmd, db_path)
        else:
            count, rows, cols = None, [], []

        if count is not None:
            lines = format_preview_lines(cmd, count, rows, cols, bak, ctx) + segment_lines(c)
            write_receipt(cmd, c["action_type"], env, "DRY_RUN",
                          f"Dry run: {count} rows affected. Snapshot taken. Recoverable.",
                          rule, bak, count, ctx, intent, [], seg_summary, False)
            box("DRY_RUN", lines, feedback=True)
            sys.exit(0)

        if bak:
            write_receipt(cmd, c["action_type"], env, "REVERSIBLE",
                          "SQL parsed but preview unavailable. Snapshot taken.",
                          rule, bak, None, ctx, intent, [], seg_summary, False)
            box("REVERSIBLE", [
                title("Recovery", "green"),
                kv("reason", "SQL mutation detected, but preview was unavailable."),
                kv("snapshot", os.path.basename(bak)),
                kv("undo", "python demo_cli_hook.py undo"),
                kv("diff", "python demo_cli_hook.py diff"),
                color("  Recovery point captured.", "green"),
            ] + segment_lines(c), feedback=True)
            sys.exit(0)

    # --- Other mutating / destructive ops with a snapshottable target (file, dir, db).
    if target:
        bak_entry = snapshot_target(target)
        if bak_entry:
            bak = bak_entry["recovery_point"]
            write_receipt(cmd, c["action_type"], env, "REVERSIBLE",
                          "Snapshot captured before action. Recoverable.",
                          rule, bak, None, ctx, intent, [], seg_summary, False)
            box("REVERSIBLE", [
                title("Action", "cyan"),
                kv("command", cmd[:110]),
                kv("target kind", target["kind"]),
            ] + segment_lines(c) + [
                "",
                title("Recovery", "green"),
                kv("action", rule or "mutation"),
                kv("snapshot", os.path.basename(bak)),
                kv("undo", "python demo_cli_hook.py undo"),
                kv("diff", "python demo_cli_hook.py diff"),
                color("  Recovery point captured.", "green"),
            ], feedback=True)
            sys.exit(0)

    # --- Low-blast-radius non-production target.
    if env in {"development", "test", "sandbox", "staging"}:
        write_receipt(cmd, c["action_type"], env, "SANDBOX",
                      "Non-production target. Low blast radius.",
                      rule, None, None, ctx, intent, [], seg_summary, False)
        box("SANDBOX", [
            title("Decision", "green"),
            color("  Non-production target detected.", "green"),
            kv("environment", env),
            kv("recovery", "not required for this demo"),
        ] + segment_lines(c))
        sys.exit(0)

    # --- Non-recoverable.
    write_receipt(cmd, c["action_type"], env, "ESCALATE",
                  "No automatic recovery path found. Human input required.",
                  rule, None, None, ctx, intent, [], seg_summary, False)
    box("ESCALATE", [
        title("Decision", "red"),
        kv("reason", f"Cannot safely proceed: {rule or 'mutating action'} on {env}."),
        kv("problem", "No snapshot path found. This cannot be auto-recovered."),
    ] + segment_lines(c) + [
        "",
        title("Safe next step", "yellow"),
        '  1. Add --db / --db-url / --target so a recovery point can be captured.',
        "  2. Create an independent backup first.",
        "",
        "  The agent should not proceed until one of the above is satisfied.",
    ], feedback=True)
    sys.exit(1)


if __name__ == "__main__":
    main()
