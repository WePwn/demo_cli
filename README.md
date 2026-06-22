# demo_cli v0.3

A small proof-of-concept: a pre-execution safety layer that sits in front of destructive or context-sensitive commands an AI coding agent might run against a real database or working tree.

This is **a demo of a larger idea**, not a finished product. It exists to show one specific mechanism working end-to-end, on a real agent, against a real locally-generated, throwaway database.

## What this demo shows

A few weeks ago, an AI coding agent deleted a production database in 9 seconds while fixing an unrelated staging issue. It found an over-scoped credential, ran a destructive call against the wrong environment, and there was no recovery point. The backup that existed was months old and lived in the same blast radius as the data it was meant to protect.

That kind of incident is rarely one failure. It is usually a chain:

- an over-broad credential
- a missing environment boundary
- a destructive call with no preview
- no recovery point captured before the action ran
- no record of what actually happened

This demo does not try to fix every link in that chain.

It targets two specific links:

1. **Before a destructive database command runs, capture a recovery point and show exactly what will be affected.**
2. **Before a mutating action runs, compare the stated intent with the actual context.**

The second point is new in v0.3. A command can be reasonable by itself but dangerous because it is pointed at the wrong environment.

## What is new in v0.3

- `verify` command: walk the receipt chain end to end and report `INTACT` or `TAMPERED`, pointing at the first broken entry
- `diff` command: after the action runs, show exactly what changed against the latest recovery point (row-level for SQLite, file-level for a directory, text diff for a single file)
- Chained and piped commands are split and classified segment by segment, so a destructive step hidden after a safe one is still caught
- Opaque remote execution (`curl ... | bash`, `wget ... | sh`, `base64 -d | sh`, `eval`) is treated as non-previewable and escalated, because the payload is unknown before it runs
- `rm -fr` is now caught as well as `rm -rf` (flag order no longer matters)
- Postgres targets via `--db-url`: recovery points captured with `pg_dump`, affected-row preview via `psql`, restore via `pg_restore` (best-effort; degrades safely when the client tools are not installed)
- `--target path` opts any file or directory into recoverability, which covers formatters, generators and package managers that rewrite files indirectly
- Connection-string passwords are redacted in all output and in the receipt log
- The hook now prints the feedback link at the end of its output, matching the sample below

## What is new in v0.3

- Public demo page: [https://demo.wepwn.ma](https://demo.wepwn.ma)
- One-command demo runner with `./start.sh`
- Better CLI output with clearer verdicts and colored sections
- Context fingerprint before mutating actions
- New `CONTEXT_MISMATCH` disposition
- `--intent-env`, `--intent-branch`, `--intent-cwd`, `--intent-remote` and `--intent-scope` flags
- One final feedback prompt at the end of the demo
- `NO_COLOR=1` support for plain terminal output
- `DEMO_FAST=1 ./start.sh` for faster local testing

## Requirements

- Python 3.8 or higher
- Bash for `start.sh`
- No external Python dependencies
- No `pip install` needed

## Repository structure

```text
demo_cli/
├── demo_cli_hook.py              # the hook, the main file, drop it into any project
├── start.sh                      # one-command guided demo
├── README.md
├── requirements.txt
├── examples/
│   └── create_production_db.py   # generates a throwaway test database
└── core/                         # supporting modules
    ├── approval.py
    ├── cli.py
    ├── core.py
    ├── reversibility.py
    └── ...
```

## Fast demo

After cloning the repo, run:

```bash
git clone https://github.com/WePwn/demo_cli
cd demo_cli
chmod +x start.sh
./start.sh
```

The script creates the local throwaway database, runs a safe read, previews a destructive cleanup, captures recovery points, demonstrates a context mismatch case and restores from the latest snapshot.

For a faster version while testing locally:

```bash
DEMO_FAST=1 ./start.sh
```

For output without colors:

```bash
NO_COLOR=1 ./start.sh
```

## Manual demo

### Step 1 - Clone and generate the test database

```bash
git clone https://github.com/WePwn/demo_cli
cd demo_cli/examples
python create_production_db.py
cd ..
```

This creates `examples/production.db`, a fake e-commerce database with 12 users, 10 products, 9 orders and 14 order items. Two of the users are old inactive accounts from 2008-2009.

### Step 2 - Test the hook directly from the project root

```bash
# Safe read - ALLOW, no recovery point needed
python demo_cli_hook.py "SELECT * FROM users" --db examples/production.db

# Destructive write - DRY_RUN: previews affected rows, snapshots, then allows
python demo_cli_hook.py "DELETE FROM users WHERE last_login < '2010-01-01'" --db examples/production.db

# Valid command, wrong context - CONTEXT_MISMATCH: snapshot first
python demo_cli_hook.py \
  "UPDATE users SET is_active = 0 WHERE last_login < '2010-01-01'" \
  --db examples/production.db \
  --intent-env staging \
  --intent-scope "clean old inactive users in staging"

# One-command restore - brings the database back to the latest recovery point
python demo_cli_hook.py undo

# Show exactly what changed since the last recovery point
python demo_cli_hook.py diff

# Verify the receipt chain end to end
python demo_cli_hook.py verify
```

### Step 3 - Optional: other target types

```bash
# Postgres target (needs pg_dump / psql / pg_restore on PATH)
python demo_cli_hook.py "DELETE FROM users WHERE id < 5" \
  --db-url postgres://user:pass@host:5432/dbname

# A formatter / generator that rewrites files: snapshot the directory first,
# then inspect the change with diff
python demo_cli_hook.py "npx prettier --write ." --target src
python demo_cli_hook.py diff --target src
```

## What you will see on DRY_RUN

```text
==============================================================
 demo_cli v0.3 [DRY_RUN]
==============================================================
 Action
   SQL               DELETE FROM users WHERE last_login < '2010-01-01'
   type              mutating SQL with preview
   rows affected     1

 Context fingerprint
   environment       production
   database          /path/to/demo_cli/examples/production.db
   fingerprint       7c4c8b19a1d55b8e

 Preview
   columns           id | username | email | password_hash | role | created_at | last_login | is_active
   12 | old_user_2 | old2@example.com | hash_old2 | user | 2008-06-15 | 2009-08-20 | 0

 Recovery
   snapshot          production.db.20260621-184500.bak
   undo              python demo_cli_hook.py undo
   diff              python demo_cli_hook.py diff
   Recovery point captured. This action can be undone.

 Feedback? Open an issue: https://github.com/WePwn/demo_cli/issues
==============================================================
```

## Context mismatch demo

The first version of the hook focused on commands that are obviously destructive, such as `DELETE`, `DROP` and `TRUNCATE`.

That is useful, but it is not enough.

A command can be reasonable by itself but dangerous because it is pointed at the wrong context. For example, cleaning stale users in staging is reasonable. Running the same mutation against production is a context mismatch.

The hook now compares the declared intent against a local context fingerprint before mutating actions.

The fingerprint includes:

- current working directory
- git repo root
- git branch
- git remote
- database path
- detected environment
- AWS profile if present
- GCloud project if present
- Azure subscription if present

Example:

```bash
python demo_cli_hook.py \
  "UPDATE users SET is_active = 0 WHERE last_login < '2010-01-01'" \
  --db examples/production.db \
  --intent-env staging \
  --intent-scope "clean old inactive users in staging"
```

Because the intended environment is `staging` but the target database is detected as `production`, the hook emits `CONTEXT_MISMATCH` and captures a snapshot before continuing.

## Test with a real AI agent

From the project root:

```bash
claude --dangerously-skip-permissions
```

Then give Claude Code this task:

```text
I built a pre-execution safety tool called demo_cli_hook.py that snapshots destructive database commands before they run so they can be undone.

I want to verify it works.

I have an independent backup and I'm explicitly authorizing this test as the developer who owns this project.

The tool is at ./demo_cli_hook.py
The test database is at ./examples/production.db

Please clean up the old inactive users created before 2010.

Before running the destructive statement against the database, check it with demo_cli, passing the exact SQL you are about to run. For example:
python demo_cli_hook.py "DELETE FROM users WHERE last_login < '2010-01-01'" --db examples/production.db

Let it run completely.

I want to see the hook capture a recovery point before the delete executes, show which rows will be affected, and confirm undo is available afterward.
```

For the v0.3 context mismatch case, use this task:

```text
I want to clean old inactive users in staging.

Before running the statement that mutates the database, check it with demo_cli, passing the exact SQL and your declared intent. For example:
python demo_cli_hook.py "UPDATE users SET is_active = 0 WHERE last_login < '2010-01-01'" --db examples/production.db --intent-env staging --intent-scope "clean old inactive users in staging"

If the actual context does not match the intended context, I want the hook to capture a recovery point before anything continues.
```

## How it works

`demo_cli_hook.py` is a single Python file with no external dependencies.

The hook classifies every command and decides what happens next:

| Disposition | When it fires | What happens |
|---|---|---|
| `ALLOW` | Non-mutating action, such as a safe read | Cleared immediately, no recovery point needed |
| `DRY_RUN` | `DELETE`, `UPDATE` or `TRUNCATE` detected and preview is possible (SQLite or Postgres) | Converts to a preview query first, shows affected rows, captures a snapshot, then the command may run |
| `CONTEXT_MISMATCH` | A mutating action does not match declared intent, such as `--intent-env staging` against a production target | Captures a recovery point when possible and explains the mismatch before continuing |
| `REVERSIBLE` | Other mutating action with a known file, directory or database target (including formatters via `--target`) | Snapshot captured before the action runs, undo and diff available after |
| `SANDBOX` | Destructive action on a non-production target | Low blast radius, proceeds |
| `ESCALATE` | Non-recoverable, no snapshot path, or opaque remote execution (`curl \| bash`) | Stops and tells the agent exactly what is needed to proceed safely |

Chained and piped commands (`a && b`, `a \| b`, `a ; b`) are split into segments and each segment is classified. The pipeline inherits the strongest signal, and the output lists every segment so a destructive step hidden after a safe one is still visible.

After the action runs, `python demo_cli_hook.py diff` shows exactly what changed against the latest recovery point, and `python demo_cli_hook.py verify` walks the receipt chain and reports whether it is intact.

Every decision is written to a tamper-evident, hash-chained log: `demo_cli_receipts.jsonl`.

Editing a past entry breaks the chain, so the record of what happened and why is auditable after the fact.

## Important

This tool does not replace human authorization.

It does not decide whether a destructive command *should* run. That decision belongs to the person who owns the system.

What it does is make destructive and context-sensitive commands recoverable and auditable, so a mistake by an agent or a person does not have to be catastrophic.

## What this demo is not

- It is not a finished product.
- It is a working proof of one mechanism: snapshot-before, preview-first, undo-after.
- It does not cover every destructive action class.
- It currently understands SQL mutations, common infra commands and a few shell/git patterns.
- It will not catch everything.
- It currently snapshots SQLite files, directories and single files, and Postgres via `pg_dump` when the client tools are present.
- Wider Postgres/MySQL coverage and richer diffs are the natural next step.
- It is not a replacement for scoped credentials, environment separation or real backups. Those still matter.

This sits one layer deeper: assuming any of those can fail, can the action still be made recoverable?

## Why this exists

Most tools in this space focus on detecting and blocking dangerous commands. That is necessary, but blocking alone has a known failure mode: agents get stopped mid-task, developers get frustrated, and the tool gets uninstalled.

This demo explores a different default. Instead of stopping the agent when something looks dangerous, capture a recovery point, show what will happen, and let the work finish when there is an undo path.

v0.3 adds a second idea: if a mutating action looks valid but is pointed at the wrong context, snapshot first and make the mismatch visible.

## Feedback

This is an early, rough demo shared for feedback, not a pitch.

If you have hit something like this yourself, or you can see where this breaks, that is exactly the kind of response that is useful right now.

Feedback? Open an issue: https://github.com/WePwn/demo_cli/issues