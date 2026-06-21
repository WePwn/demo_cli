"""
demo_cli - pre-execution safety layer for destructive and context-sensitive actions.

This is a local demo hook. It does not replace human authorization. Its job is to
make risky actions recoverable and auditable before they run.

What it demonstrates:
- safe reads are allowed
- destructive SQL gets a dry-run preview and snapshot
- other mutating actions get a recovery point when possible
- context mismatch is treated as an invariant problem, not only as danger detection
- every decision is written to a hash-chained receipt log

Usage:
  python demo_cli_hook.py "SELECT * FROM users" --db examples/production.db
  python demo_cli_hook.py "DELETE FROM users WHERE ..." --db examples/production.db
  python demo_cli_hook.py "UPDATE users SET ..." --db examples/production.db --intent-env staging
  python demo_cli_hook.py undo
"""

import datetime
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
VERSION = "v0.2"
FEEDBACK_URL = "https://github.com/WePwn/demo_cli/issues"
SEP = "=" * 62

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
    ("rm_rf", "shell", r"\brm\s+.*-[a-z]*r[a-z]*f"),
    ("rmdir_s", "shell", r"\brmdir\b.*\/[sS]"),
    ("del_force", "shell", r"\bdel\b.*\/[fFsS]"),
]

_SQL_DELETE = re.compile(r"\bDELETE\s+FROM\b", re.I)
_SQL_TRUNCATE = re.compile(r"\bTRUNCATE\b", re.I)
_SQL_UPDATE = re.compile(r"\bUPDATE\s+\w+\s+SET\b", re.I)
_SQL_MUTATING = re.compile(r"\b(UPDATE|INSERT|DELETE|DROP|TRUNCATE|ALTER|CREATE|REPLACE)\b", re.I)
_SQL_READ = re.compile(r"^\s*SELECT\b", re.I)
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
    db_env = detect_env_from_text(os.path.basename(db_path or ""))
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
        "db_path": os.path.abspath(db_path) if db_path else "unknown",
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


def classify(cmd):
    matched, atype = None, "shell"
    for rid, action_type, rx in _DESTRUCTIVE_RULES:
        if re.search(rx, cmd, re.I | re.S):
            matched, atype = rid, action_type
            break

    is_sql_read = bool(_SQL_READ.search(cmd))
    is_sql_mutating = bool(_SQL_MUTATING.search(cmd))
    is_destructive = matched is not None
    is_mutating = is_destructive or is_sql_mutating

    if is_sql_read or is_sql_mutating:
        atype = "sql"

    return {
        "is_destructive": is_destructive,
        "is_mutating": is_mutating,
        "is_sql_read": is_sql_read,
        "is_sql_mutating": is_sql_mutating,
        "matched_rule": matched,
        "action_type": atype,
    }


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


def preview_sql(sql, db_path):
    sql = sql.strip().rstrip(";")
    verb = sql.split()[0].upper() if sql.split() else ""

    if _SQL_TRUNCATE.search(sql):
        tbl = re.search(r"TRUNCATE\s+(?:TABLE\s+)?(\w+)", sql, re.I)
        if not tbl:
            return None, [], []
        table = tbl.group(1)
        count_q = f"SELECT COUNT(*) FROM {table}"
        preview_q = f"SELECT * FROM {table} LIMIT 5"
    elif verb in {"DELETE", "UPDATE"}:
        table, where = parse_table_and_where(sql, verb)
        if not table:
            return None, [], []
        where_sql = f" WHERE {where}" if where else ""
        count_q = f"SELECT COUNT(*) FROM {table}{where_sql}"
        preview_q = f"SELECT * FROM {table}{where_sql} LIMIT 5"
    else:
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


def snapshot(db_path):
    if not db_path or not os.path.exists(db_path):
        return None
    os.makedirs(RECOVERY, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(RECOVERY, f"{os.path.basename(db_path)}.{ts}.bak")
    shutil.copy2(db_path, bak)
    idx = os.path.join(RECOVERY, "index.jsonl")
    with open(idx, "a", encoding="utf-8") as f:
        f.write(json.dumps({"target": db_path, "recovery_point": bak, "ts": ts}) + "\n")
    return bak


def restore_latest():
    idx = os.path.join(RECOVERY, "index.jsonl")
    if not os.path.exists(idx):
        box("ESCALATE", ["No recovery points found."], feedback=True)
        return

    last = None
    with open(idx, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                last = json.loads(line)
            except Exception:
                pass

    if not last:
        box("ESCALATE", ["No recovery points found."], feedback=True)
        return

    shutil.copy2(last["recovery_point"], last["target"])
    box("RESTORED", [
        title("Recovery", "green"),
        kv("target", last["target"]),
        kv("from", os.path.basename(last["recovery_point"])),
        color("  Database restored from the latest recovery point.", "green"),
    ], feedback=True)


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


def write_receipt(cmd, action_type, env, decision, reason, rule, bak, dry_count=None, context=None, intent=None, mismatches=None):
    prev = _last_hash()
    body = {
        "receipt_version": "1.3",
        "receipt_id": str(uuid.uuid4()),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "agent_id": "local-demo",
        "session_id": "live",
        "action_type": action_type,
        "action_raw": cmd.strip(),
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
        "prev_receipt_hash": prev,
    }
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"))
    body["receipt_hash"] = hashlib.sha256((canon + prev).encode()).hexdigest()
    with open(RECEIPTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n")


def kv(label, value):
    return f"  {color(label.ljust(18), 'dim')}{value}"


def title(text, tone="cyan"):
    return color(text, tone)


def feedback_line():
    return f"Feedback? Open an issue: {FEEDBACK_URL}"


def box(decision, lines, feedback=False):
    decision_raw = decision
    print()
    print(color(SEP, "gray"))
    print(f" demo_cli {VERSION} [{decision_label(decision_raw)}]")
    print(color(SEP, "gray"))
    for line in lines:
        print(f" {line}")
    # Feedback is intentionally not printed by the hook.
    # The guided demo prints the feedback link once at the end.
    print(color(SEP, "gray"))
    print()


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
        lines += [
            "",
            title("Preview", "yellow"),
        ]
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
    print('Usage: python demo_cli_hook.py "command" [--db path/to/db] [--intent-env staging]')
    print('       python demo_cli_hook.py undo')


def main():
    global USE_COLOR

    if len(sys.argv) == 2 and sys.argv[1] == "undo":
        restore_latest()
        sys.exit(0)

    if len(sys.argv) < 2:
        usage()
        sys.exit(1)

    args = sys.argv[1:]
    if "--no-color" in args:
        USE_COLOR = False
        args.remove("--no-color")

    explicit_db, args = extract_flag(args, "--db")
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

    c = classify(cmd)
    db_path = find_db(cmd, explicit_db)
    ctx = context_fingerprint(cmd, db_path, actual_env)
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

    # Context mismatch is handled before danger-classification. This catches
    # reasonable mutating actions pointed at the wrong environment.
    if c["is_mutating"] and mismatches:
        bak = snapshot(db_path) if db_path else None
        lines = [
            title("Action", "cyan"),
            kv("command", cmd[:110]),
            kv("type", "mutating action"),
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

        write_receipt(cmd, c["action_type"], env, "CONTEXT_MISMATCH", reason, rule, bak, None, ctx, intent, mismatches)
        box("CONTEXT_MISMATCH", lines, feedback=True)
        sys.exit(0 if bak else 1)

    # Safe read.
    if not c["is_mutating"] and not c["is_destructive"]:
        write_receipt(cmd, c["action_type"], env, "ALLOW", "Non-mutating action cleared.", rule, None, None, ctx, intent, [])
        box("ALLOW", [
            title("Decision", "green"),
            color("  Safe read or non-mutating action.", "green"),
            kv("recovery", "not needed"),
            kv("fingerprint", ctx["fingerprint"]),
        ])
        sys.exit(0)

    # DELETE, UPDATE, TRUNCATE: show what would be affected when possible.
    if _SQL_DELETE.search(cmd) or _SQL_UPDATE.search(cmd) or _SQL_TRUNCATE.search(cmd):
        bak = snapshot(db_path) if db_path else None
        count, rows, cols = preview_sql(cmd, db_path) if db_path else (None, [], [])

        if count is not None:
            lines = format_preview_lines(cmd, count, rows, cols, bak, ctx)
            write_receipt(
                cmd, c["action_type"], env, "DRY_RUN",
                f"Dry run: {count} rows affected. Snapshot taken. Recoverable.",
                rule, bak, count, ctx, intent, []
            )
            box("DRY_RUN", lines, feedback=True)
            sys.exit(0)

        if bak:
            write_receipt(cmd, c["action_type"], env, "REVERSIBLE", "SQL parsed but preview unavailable. Snapshot taken.", rule, bak, None, ctx, intent, [])
            box("REVERSIBLE", [
                title("Recovery", "green"),
                kv("reason", "SQL mutation detected, but preview was unavailable."),
                kv("snapshot", os.path.basename(bak)),
                kv("undo", "python demo_cli_hook.py undo"),
                color("  Recovery point captured.", "green"),
            ], feedback=True)
            sys.exit(0)

    # Other mutating SQL or destructive ops with a file target.
    if db_path and os.path.exists(db_path):
        bak = snapshot(db_path)
        write_receipt(cmd, c["action_type"], env, "REVERSIBLE", "Snapshot captured before action. Recoverable.", rule, bak, None, ctx, intent, [])
        box("REVERSIBLE", [
            title("Recovery", "green"),
            kv("action", rule or "sql_mutation"),
            kv("snapshot", os.path.basename(bak)),
            kv("undo", "python demo_cli_hook.py undo"),
            color("  Recovery point captured.", "green"),
        ], feedback=True)
        sys.exit(0)

    # Low-blast-radius non-production target.
    if env in {"development", "test", "sandbox", "staging"}:
        write_receipt(cmd, c["action_type"], env, "SANDBOX", "Non-production target. Low blast radius.", rule, None, None, ctx, intent, [])
        box("SANDBOX", [
            title("Decision", "green"),
            color("  Non-production target detected.", "green"),
            kv("environment", env),
            kv("recovery", "not required for this demo"),
        ])
        sys.exit(0)

    # Non-recoverable.
    write_receipt(cmd, c["action_type"], env, "ESCALATE", "No automatic recovery path found. Human input required.", rule, None, None, ctx, intent, [])
    box("ESCALATE", [
        title("Decision", "red"),
        kv("reason", f"Cannot safely proceed: {rule or 'mutating action'} on {env}."),
        kv("problem", "No snapshot path found. This cannot be auto-recovered."),
        "",
        title("Safe next step", "yellow"),
        '  1. Add --db: python demo_cli_hook.py "..." --db examples/production.db',
        "  2. Create an independent backup first.",
        "",
        "  The agent should not proceed until one of the above is satisfied.",
    ], feedback=True)
    sys.exit(1)


if __name__ == "__main__":
    main()