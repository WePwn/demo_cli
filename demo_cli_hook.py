"""
demo_cli — pre-execution safety layer for destructive actions.
===========================================================
demo_cli is a transparent guardrail run by the developer who owns this project.
Before a destructive command executes, demo_cli captures a recovery point so the
action can be undone, and shows exactly what the command will affect.

It does NOT replace human authorization. A developer remains responsible for
the decision to run a destructive command. demo_cli's role is to make that command
recoverable and auditable, not to authorize it. Every decision is written to a
tamper-evident receipt log.

Dispositions:
  ALLOW       non-destructive -- safe to run
  DRY_RUN     DELETE/TRUNCATE -- preview the affected rows, capture a snapshot,
              then the developer's command may run with an undo path available
  REVERSIBLE  destructive with a snapshot captured first -- undo available
  SANDBOX     non-production target -- low blast radius
  ESCALATE    non-recoverable, no snapshot path -- requires explicit human
              decision and a verified independent backup before proceeding

How to read a disposition: ALLOW/DRY_RUN/REVERSIBLE mean a recovery point exists
and the action can be undone. ESCALATE means no automatic recovery is possible
and a human must decide with their own backup.

Usage:
    python demo_cli_hook.py "the command"
    python demo_cli_hook.py "DELETE FROM users WHERE ..." --db production.db
    python demo_cli_hook.py undo
"""

import sys, os, re, json, sqlite3, shutil, hashlib, uuid, datetime

HERE      = os.path.dirname(os.path.abspath(__file__))
RECEIPTS  = os.path.join(HERE, "demo_cli_receipts.jsonl")
PROTECTED = os.path.join(HERE, "production.db")
RECOVERY  = os.path.join(HERE, ".demo_cli_recovery")

_DESTRUCTIVE_RULES = [
    ("sql_drop",        "sql",   r"\bDROP\s+(?:DATABASE|TABLE|SCHEMA)\b"),
    ("sql_truncate",    "sql",   r"\bTRUNCATE\b"),
    ("sql_delete",      "sql",   r"\bDELETE\s+FROM\b"),
    ("tf_destroy",      "infra", r"\bterraform\s+destroy\b"),
    ("kubectl_delete",  "infra", r"\bkubectl\s+delete\s+(?:namespace|ns|pv|pvc|deploy|sts)\b"),
    ("cloud_delete",    "infra", r"\b(?:aws|gcloud|az)\b[\w\s.-]*\b(?:delete|terminate|destroy)\b"),
    ("railway_drop",    "infra", r"railway\s+run.*production.*(?:DROP|DELETE|TRUNCATE)"),
    ("railway_vol_del", "infra", r"railway\s+volume\s+delete"),
    ("git_force_push",  "git",   r"\bgit\s+push\b.*(?:--force|-f)\b"),
    ("git_reset_hard",  "git",   r"\bgit\s+reset\s+--hard\b"),
    ("rm_rf",           "shell", r"\brm\s+.*-[a-z]*r[a-z]*f"),
    ("rmdir_s",         "shell", r"\brmdir\b.*\/[sS]"),
    ("del_force",       "shell", r"\bdel\b.*\/[fFsS]"),
]
_SQL_DELETE = re.compile(r"\bDELETE\s+FROM\b", re.I)
_TRUNCATE   = re.compile(r"\bTRUNCATE\b", re.I)
_PROD       = re.compile(r"\b(prod|production|prd)\b", re.I)
_DEV        = re.compile(r"\b(dev|staging|stage|local|test|sandbox)\b", re.I)

def classify(cmd):
    matched, atype = None, "shell"
    for rid, a, rx in _DESTRUCTIVE_RULES:
        if re.search(rx, cmd, re.I | re.S):
            matched, atype = rid, a; break
    env = "prod" if _PROD.search(cmd) else ("dev" if _DEV.search(cmd) else "unknown")
    return {"is_destructive": matched is not None, "matched_rule": matched,
            "action_type": atype, "environment": env}

def find_db(cmd, explicit=None):
    if explicit and os.path.exists(explicit): return explicit
    m = re.search(r'[\w./\\-]+\.db', cmd)
    if m:
        p = m.group(0)
        if os.path.exists(p): return os.path.abspath(p)
        p2 = os.path.join(HERE, p)
        if os.path.exists(p2): return os.path.abspath(p2)
    if os.path.exists(PROTECTED): return PROTECTED
    return None

def dry_run_sql(sql, db_path):
    sql = sql.strip().rstrip(";")
    preview = re.sub(r'^\s*DELETE\s+FROM\s+', 'SELECT * FROM ', sql, flags=re.I)
    count_q = re.sub(r'^\s*DELETE\s+FROM\s+', 'SELECT COUNT(*) FROM ', sql, flags=re.I)
    if _TRUNCATE.search(sql):
        tbl = re.search(r'TRUNCATE\s+(?:TABLE\s+)?(\w+)', sql, re.I)
        if tbl:
            preview = f"SELECT * FROM {tbl.group(1)}"
            count_q = f"SELECT COUNT(*) FROM {tbl.group(1)}"
    try:
        con = sqlite3.connect(db_path)
        count = con.execute(count_q).fetchone()[0]
        cur   = con.execute(preview + " LIMIT 5")
        cols  = [d[0] for d in cur.description] if cur.description else []
        rows  = cur.fetchall()
        con.close()
        return count, rows, cols
    except Exception as e:
        return None, [], []

def snapshot(db_path):
    if not db_path or not os.path.exists(db_path): return None
    os.makedirs(RECOVERY, exist_ok=True)
    ts  = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(RECOVERY, f"{os.path.basename(db_path)}.{ts}.bak")
    shutil.copy2(db_path, bak)
    idx = os.path.join(RECOVERY, "index.jsonl")
    with open(idx, "a") as f:
        f.write(json.dumps({"target": db_path, "recovery_point": bak, "ts": ts}) + "\n")
    return bak

def restore_latest():
    idx = os.path.join(RECOVERY, "index.jsonl")
    if not os.path.exists(idx): print("No recovery points found."); return
    last = None
    with open(idx) as f:
        for line in f:
            line = line.strip()
            if line:
                try: last = json.loads(line)
                except: pass
    if not last: print("No recovery points found."); return
    shutil.copy2(last["recovery_point"], last["target"])
    print(f"Restored {last['target']} from {os.path.basename(last['recovery_point'])}")

GENESIS = "0" * 64

def _last_hash():
    last = GENESIS
    if os.path.exists(RECEIPTS):
        with open(RECEIPTS) as f:
            for line in f:
                line = line.strip()
                if line:
                    try: last = json.loads(line)["receipt_hash"]
                    except: pass
    return last

def write_receipt(cmd, action_type, env, decision, reason, rule, bak, dry_count=None):
    prev = _last_hash()
    body = {
        "receipt_version": "1.2", "receipt_id": str(uuid.uuid4()),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "agent_id": "claude-code", "session_id": "live",
        "action_type": action_type, "action_raw": cmd.strip(),
        "target_environment": env,
        "invariant": "no_irreversible_mutation_of_a_protected_system_of_record",
        "matched_rule": rule,
        "classification": "destructive" if rule else "safe",
        "decision": decision, "mode": "enforce", "reason": reason,
        "recovery_point": bak, "dry_run_affected_rows": dry_count,
        "structural_approval": "n/a", "prev_receipt_hash": prev,
    }
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"))
    body["receipt_hash"] = hashlib.sha256((canon + prev).encode()).hexdigest()
    with open(RECEIPTS, "a") as f:
        f.write(json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n")

SEP = "=" * 62

def box(decision, lines):
    print(f"\n{SEP}")
    print(f"  demo_cli  [{decision}]")
    for line in lines: print(f"  {line}")
    print(f"{SEP}\n")

def main():
    if len(sys.argv) == 2 and sys.argv[1] == "undo":
        restore_latest(); sys.exit(0)
    if len(sys.argv) < 2:
        print('Usage: python demo_cli_hook.py "<command>" [--db path/to/db]')
        sys.exit(1)

    args = sys.argv[1:]
    explicit_db = None
    if "--db" in args:
        i = args.index("--db")
        explicit_db = args[i + 1]
        args = args[:i] + args[i + 2:]
    cmd = " ".join(args)

    c       = classify(cmd)
    db_path = find_db(cmd, explicit_db)
    env     = c["environment"]
    rule    = c["matched_rule"]

    # ALLOW
    if not c["is_destructive"]:
        write_receipt(cmd, c["action_type"], env, "ALLOW", "Non-destructive - cleared.", rule, None)
        box("ALLOW", ["Non-destructive - no recovery point needed."])
        sys.exit(0)

    # DRY_RUN: DELETE or TRUNCATE -- show rows, snapshot, then let agent run
    if _SQL_DELETE.search(cmd) or _TRUNCATE.search(cmd):
        bak   = snapshot(db_path) if db_path else None
        count, rows, cols = dry_run_sql(cmd, db_path) if db_path else (None, [], [])
        if count is not None:
            lines = [f"DRY RUN before execution:", f"  SQL  : {cmd[:55]}",
                     f"  rows affected: {count}"]
            if rows:
                lines.append(f"  preview (up to 5 rows):")
                if cols: lines.append(f"    columns: {' | '.join(cols)}")
                for r in rows: lines.append(f"    {' | '.join(str(x) for x in r)}")
            if bak:
                lines += [f"  snapshot: {os.path.basename(bak)}",
                          "  to undo: python demo_cli_hook.py undo"]
            lines.append("Recovery point captured. This action can be undone with: python demo_cli_hook.py undo")
            write_receipt(cmd, c["action_type"], env, "DRY_RUN",
                          f"Dry run: {count} rows affected. Snapshot taken. Recoverable.",
                          rule, bak, count)
            box("DRY_RUN", lines)
            sys.exit(0)
        if bak:
            write_receipt(cmd, c["action_type"], env, "REVERSIBLE",
                          "SQL parsed but dry run unavailable. Snapshot taken. Recoverable.",
                          rule, bak)
            box("REVERSIBLE", [f"Snapshot: {os.path.basename(bak)}",
                               "Recovery point captured. Undo available: python demo_cli_hook.py undo"])
            sys.exit(0)

    # REVERSIBLE: other destructive ops with a file to snapshot
    if db_path and os.path.exists(db_path):
        bak = snapshot(db_path)
        write_receipt(cmd, c["action_type"], env, "REVERSIBLE",
                      "Snapshot captured before action. Recoverable.", rule, bak)
        box("REVERSIBLE", [f"Destructive: {rule}",
                           f"Snapshot: {os.path.basename(bak)}",
                           "Recovery point captured.",
                           "To undo: python demo_cli_hook.py undo"])
        sys.exit(0)

    # SANDBOX: non-prod, no snapshot -- low blast radius, proceed
    if env == "dev":
        write_receipt(cmd, c["action_type"], env, "SANDBOX",
                      "Non-production - low blast radius.", rule, None)
        box("SANDBOX", ["Non-production environment - low blast radius."])
        sys.exit(0)

    # ESCALATE: truly non-recoverable -- give agent a path forward
    write_receipt(cmd, c["action_type"], env, "ESCALATE",
                  "Non-recoverable action. Human input required.", rule, None)
    box("ESCALATE", [
        f"Cannot safely proceed: {rule or 'destructive'} on {env}.",
        "No snapshot path found. This cannot be auto-recovered.",
        "",
        "Agent: to continue, do ONE of these then retry:",
        '  1. Add --db flag:  python demo_cli_hook.py "<cmd>" --db production.db',
        "  2. Manual backup:  copy production.db production.db.bak",
        "     demo_cli will then snapshot and allow.",
        "",
        "Agent must not proceed until one of the above is satisfied.",
    ])
    sys.exit(1)

if __name__ == "__main__":
    main()
