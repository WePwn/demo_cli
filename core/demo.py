"""
demo_cli V0 - recovery, not blocking.

A real SQLite 'production' database. The agent, under a code freeze:
  1) reads staging                      -> ALLOW
  2) DROP TABLE users on production      -> demo_cli snapshots FIRST, allows, then we UNDO in one command
  3) rotate the production master key    -> non-recoverable: agent CANNOT self-approve; only a
                                            separate party's signature (2-of-2) lets it proceed
Every step writes a tamper-evident, bureau-grade receipt.
"""
import os, sqlite3, tempfile
from .core import Guard
from .report import shadow_report
from . import reversibility, approval

APPROVER_KEY = "approver-key-held-out-of-band"   # a SEPARATE party holds this; the agent never does

def _make_db(path):
    con = sqlite3.connect(path); cur = con.cursor()
    cur.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    cur.executemany("INSERT INTO users (name) VALUES (?)", [("alice",), ("bob",), ("carol",)])
    con.commit(); con.close()

def _rows(path):
    try:
        con = sqlite3.connect(path); cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM users"); n = cur.fetchone()[0]; con.close(); return n
    except Exception:
        return "— (table gone)"

def run():
    work = tempfile.mkdtemp(prefix="demo_cli-demo-")
    db, rec = os.path.join(work, "prod.db"), os.path.join(work, "receipts.jsonl")
    _make_db(db)
    g = Guard(mode="enforce", receipts_path=rec)
    print("\ndemo_cli V0 — reversibility + structural-approval demo")
    print(f"production DB: prod.db   (users rows: {_rows(db)})")
    print("agent directive: 'CODE FREEZE — do not touch production.'\n")

    r, _ = g.evaluate('psql staging -c "SELECT count(*) FROM users"', agent_id="coding-agent", session_id="demo")
    print(f"  .  [{r.decision:<10}] read on staging")

    print("\n  >> agent attempts: DROP TABLE users  (production)")
    r, rp = g.evaluate('psql prod -c "DROP TABLE users"', target_path=db, agent_id="coding-agent", session_id="demo")
    print(f"  ~  [{r.decision:<10}] recovery point captured BEFORE allowing -> {os.path.basename(r.recovery_point)}")
    con = sqlite3.connect(db); con.execute("DROP TABLE users"); con.commit(); con.close()  # the allowed action runs
    print(f"        the agent runs it   -> users rows: {_rows(db)}")
    reversibility.restore(rp)
    print(f"        $ demo_cli undo        -> users rows: {_rows(db)}   ✓ restored in one command")

    print("\n  >> agent attempts: rotate production master signing key  (non-recoverable)")
    r, _ = g.evaluate("rotate production master signing key --yes", agent_id="coding-agent", session_id="demo")
    print(f"  !  [{r.decision:<10}] structural_approval={r.structural_approval}  -> agent cannot self-approve, blocked")
    token = approval.sign("rotate production master signing key --yes", APPROVER_KEY)   # a SEPARATE party signs
    r, _ = g.evaluate("rotate production master signing key --yes",
                      approval_token=token, approver_key=APPROVER_KEY, agent_id="coding-agent", session_id="demo")
    print(f"  ✓  [{r.decision:<10}] structural_approval={r.structural_approval}  -> proceeds only with independent 2-of-2 approval")

    print("\n" + shadow_report(rec) + "\n")

if __name__ == "__main__":
    run()
