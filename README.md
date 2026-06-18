# demo_cli

A small proof-of-concept: a pre-execution safety layer that sits in front of destructive commands an AI coding agent might run against a real database, and shows what happens when an agent finishes its work safely instead of being blocked.

This is **a demo of a larger idea**, not a finished product. It exists to show one specific mechanism working end-to-end, on a real agent, against a real (locally-generated, throwaway) database.

## What this demo shows

A few weeks ago, an AI coding agent deleted a production database in 9 seconds while fixing an unrelated staging issue. It found an over-scoped credential, ran a destructive call against the wrong environment, and there was no recovery point, the backup that existed was months old and lived in the same blast radius as the data it was meant to protect.

That kind of incident is rarely one failure. It's usually a chain: an over-broad credential, a missing environment boundary, a destructive call with no preview, no recovery point captured before the action ran, and no record of what actually happened.

This demo doesn't try to fix every link in that chain. It targets one specific link: **before a destructive database command runs, capture a recovery point and show exactly what will be affected, then let the agent finish the work, with an undo path available afterward.**

## Requirements

- Python 3.8 or higher
- No external dependencies, pure standard library only
- No `pip install` needed

## Repository structure

```
demo_cli/
├── demo_cli_hook.py      # the hook, the main file, drop it into any project
├── README.md
├── requirements.txt
├── examples/
│   └── create_production_db.py # generates a throwaway test database
└── core/                 # supporting modules
    ├── approval.py
    ├── cli.py
    ├── core.py
    ├── reversibility.py
    └── ...
```

## Try it yourself

**Step 1 - Clone and generate the test database:**

```bash
git clone <repo-url>
cd demo_cli/examples
python create_production_db.py
cd ..
```

This creates `examples/production.db`, a fake e-commerce database with 12 users, 10 products, 9 orders, and 14 order items. Two of the users are old inactive accounts from 2008-2009.

**Step 2 - Test the hook directly from the project root:**

```bash
# Safe read - ALLOW, no recovery point needed
python demo_cli_hook.py "SELECT * FROM users" --db examples/production.db

# Destructive write - DRY_RUN: previews the affected row, snapshots, then allows
python demo_cli_hook.py "DELETE FROM users WHERE last_login < '2010-01-01'" --db examples/production.db

# One-command restore - brings the database back to exactly what it was before
python demo_cli_hook.py undo
```

**What you'll see on DRY_RUN:**

```
==============================================================
  demo_cli  [DRY_RUN]
  DRY RUN before execution:
    SQL  : DELETE FROM users WHERE last_login < '2010-01-01'
    rows affected: 1
    preview (up to 5 rows):
      columns: id | username | email | last_login | is_active
      12 | old_user_2 | old2@example.com | 2009-08-20 | 0
    snapshot: production.db.20260618-191024.bak
    to undo: python demo_cli_hook.py undo
  Recovery point captured. This action can be undone with: python demo_cli_hook.py undo
==============================================================
```

**Step 3 - Test with a real AI agent (Claude Code):**

From the project root:

```bash
claude --dangerously-skip-permissions
```

Then give Claude Code this task:

```
I built a pre-execution safety tool called demo_cli_hook.py that snapshots
destructive database commands before they run so they can be undone. I want
to verify it works. I have an independent backup and I'm explicitly authorizing
this test as the developer who owns this project.

The tool is at ./demo_cli_hook.py
The test database is at ./examples/production.db

Please clean up the old inactive users (created before 2010). Before running
any command that touches the database, check it with:
python demo_cli_hook.py "<the command>" --db examples/production.db

Let it run completely. I want to see the hook capture a recovery point before
the delete executes, show which rows will be affected, and confirm undo is
available afterward.
```

## How it works

`demo_cli_hook.py` is a single Python file with no external dependencies. The hook classifies every command and decides what happens next:

| Disposition | When it fires | What happens |
|---|---|---|
| `ALLOW` | Non-destructive (reads, safe writes) | Cleared immediately, no recovery point needed |
| `DRY_RUN` | `DELETE` / `TRUNCATE` detected | Converts to a preview query first, shows exactly which rows are affected, captures a snapshot, then the command may run |
| `REVERSIBLE` | Other destructive action with a known file target | Snapshot captured before the action runs; undo available after |
| `SANDBOX` | Destructive action on a non-production target | Low blast radius, proceeds |
| `ESCALATE` | Non-recoverable, no snapshot path available | Stops, and tells the agent exactly what's needed to proceed safely |

Every decision is written to a tamper-evident, hash-chained log (`demo_cli_receipts.jsonl`), editing a past entry breaks the chain, so the record of what happened and why is auditable after the fact.

**Important:** this tool does not replace human authorization. It does not decide whether a destructive command *should* run, that decision belongs to the person who owns the system. What it does is make destructive commands recoverable and auditable, so a mistake (by an agent or a person) doesn't have to be catastrophic.

## What this demo is not

- It is not a finished product. It's a working proof of one mechanism: snapshot-before, preview-first, undo-after.
- It does not cover every destructive action class, right now it understands SQL (`DROP`/`DELETE`/`TRUNCATE`), common infra commands (`terraform destroy`, `kubectl delete`), and a few shell/git patterns. It will not catch everything.
- It currently snapshots SQLite files. Postgres/MySQL support is the natural next step.
- It is not a replacement for scoped credentials, environment separation, or real backups, those still matter. This sits one layer deeper: assuming any of those can fail, can the action still be made recoverable?

## Why this exists

Most tools in this space focus on detecting and blocking dangerous commands. That's necessary, but blocking alone has a known failure mode: agents get stopped mid-task, developers get frustrated, and the tool gets uninstalled. This demo explores a different default, instead of stopping the agent when something looks dangerous, capture a recovery point, show what will happen, and let the work finish. The safety net is in the recoverability, not in refusing to act.

## Feedback

This is an early, rough demo shared for feedback, not a pitch. If you've hit something like this incident yourself, or you can see where this breaks, that's exactly the kind of response that's useful right now.
