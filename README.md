# demo_cli

A pre-execution safety layer for AI coding agents.

Before a destructive command runs, demo_cli **previews** what it would do,
**snapshots** the real target so it can be **undone**, and writes a
**tamper-evident receipt** of every decision, including the agent's stated
reasoning.

Built around one invariant:

> A mutating action must be recoverable **and** match its declared context
> otherwise it is escalated, never silently allowed, and never falsely reported
> as "recovered".

`0.4.0b5` - public beta. Cooperative-agent threat model (mistakes, not evasion).

---

## Why this is not just another blocking hook

Most safety hooks for AI agents do one thing: pattern-match a dangerous command
and **block** it. That stops the obvious disasters, but it also stops the agent
mid-task, so you either loosen the rules until they stop catching things, or
you babysit the session.

demo_cli starts from the opposite default: **recovery, not blocking.**

- For anything it can prove it captured (a file, a directory, a local DB), it
  **snapshots first and lets the agent keep working.** If the agent gets it
  wrong, `demo_cli undo <id>` brings it back. The work finishes; the mistake is
  reversible.
- It only **blocks** when something genuinely *can't* be recovered: an external
  or irreversible effect (`terraform destroy`, `git push --force`, an object-store
  delete), or a recursive-force delete that leaves no recoverable target
  (`Remove-Item -Recurse -Force`, `rmdir /s`, `del /s`). There, it escalates
  honestly instead of pretending it captured a recovery point.

Blocking is the fallback for the un-recoverable, not the default for everything.
That's the whole design.

---

## How it works with Claude Code

Claude Code supports [PreToolUse hooks](https://docs.claude.com/en/docs/claude-code/hooks):
a shell command that runs **before each tool call**, receives the full tool input
as JSON on stdin, and returns a permission decision. demo_cli is that command.

```
Claude Code is about to run: Bash(rm -rf dist/)
                              ↓
              demo_cli hook  (fires automatically)
                              ↓
         classify → resolve target → snapshot → decide
                              ↓
        { "permissionDecision": "allow" }   ← allow, with a recovery point
        { "permissionDecision": "deny"  }   ← blocked, reason surfaced to agent
        { "permissionDecision": "ask"   }   ← paused, human decides
```

**One property worth knowing:** a PreToolUse hook returning `deny` stops the tool
**even when the session is running `--dangerously-skip-permissions` or
`bypassPermissions`**, that mode skips the interactive prompts, not the hooks.
So the recovery-and-escalation layer holds even in a fully autonomous run. (The
one way it does *not* fire: if the `demo_cli` binary isn't on PATH in the shell
where `claude` launched, Claude Code silently proceeds with no check, see
[Known issues](#known-issues-beta). Run `demo_cli doctor` to confirm.)

demo_cli gates **both** tool categories Claude Code uses to modify your project:
- `Bash` - shell commands (`rm`, `git reset --hard`, `terraform destroy`, …)
- `Edit` / `Write` / `MultiEdit` / `NotebookEdit` - direct file mutations

A file or directory is snapshotted **before** it is touched. If the agent
destroys something, `demo_cli undo <id>` brings it back.

---

## How it works with Cursor

Cursor supports [hooks](https://cursor.com/docs/hooks): scripts it runs at named
points in the agent loop. demo_cli registers a `beforeShellExecution` hook that
receives the command as JSON on stdin (`command`, `cwd`, …) and returns a
permission decision.

```bash
demo_cli install-hook --cursor          # writes .cursor/hooks.json (project)
demo_cli install-hook --cursor --scope global   # or ~/.cursor/hooks.json
demo_cli install-hook --cursor --print  # inspect the snippet without writing
```

The installed hook looks like this:

```json
{
  "version": 1,
  "hooks": {
    "beforeShellExecution": [
      { "command": "demo_cli hook-cursor", "failClosed": true }
    ]
  }
}
```

Two Cursor-specific properties, both handled deliberately:

- **`failClosed: true` is mandatory.** By default Cursor *fails open*: a hook
  that crashes, times out, or emits invalid JSON lets the command through.
  `failClosed: true` inverts that, a guard that cannot run blocks instead. The
  adapter also fails closed *inside* the script: if it holds a real command it
  cannot evaluate, it returns `deny` (the opposite of the Claude Code adapter's
  fail-open on internal error). The one exception is Cursor's known empty-stdin
  defect on some remote workspaces, where there is no command to judge, so it
  steps aside rather than brick the session.
- **Only `deny` is reliably honored today.** Cursor's own allow-list can
  override an `allow`/`ask` from a hook. That is fine here: the whole move is to
  *deny the unrecoverable*, which is exactly the decision Cursor respects. So a
  recursive-force delete on Cursor is stopped by the same rule that stops it in
  Claude Code.

Scope for this beta: the Cursor adapter gates **shell commands** only
(`beforeShellExecution`). File-edit gating is Claude Code only for now.

---

## Install

**With pipx (recommended)**, puts `demo_cli` in your global PATH so Claude
Code can find it from any project directory:

```bash
pipx install git+https://github.com/WePwn/demo_cli.git@beta
demo_cli --version
```

**From a clone:**

```bash
git clone -b beta https://github.com/WePwn/demo_cli.git
cd demo_cli
pipx install -e .
demo_cli --version
```

> **Note:** if you install inside a virtualenv, that venv must be active in
> every shell where you launch `claude`. Otherwise the `demo_cli hook` command
> is not found and Claude Code silently proceeds without a safety check. Use
> `demo_cli doctor` to verify the install.

Requires Python 3.9+.

---

## Quickstart

```bash
# inside your project
demo_cli init            # write .demo_cli.toml (shadow mode by default)
demo_cli install-hook    # register the PreToolUse hook in .claude/settings.json
demo_cli status          # confirm: hook installed, mode shadow, 0 receipts
```

Now launch Claude Code and ask it to do something destructive. demo_cli fires
automatically, no extra commands needed. Afterwards:

```bash
demo_cli log             # see every recovery point: id, when, kind, size, action
demo_cli diff <id>       # what exactly changed
demo_cli undo <id>       # restore to the state before that action
demo_cli verify          # confirm the receipt log was not tampered with
```

To evaluate a command manually (useful for testing or CI):

```bash
demo_cli check "DELETE FROM users WHERE plan = 'free'" --db app.db
demo_cli check "rm -rf dist/" --quiet
demo_cli check "terraform destroy" --actual-env production --json
```

---

## Modes

**shadow** (default), observe, snapshot, and record, but never block. The
recommended way to start: prove the value with zero workflow disruption. In
shadow mode the hook writes to stderr when it captures a snapshot or sees a
blocking decision, so the value is visible without affecting Claude Code's flow.

**enforce** - the hook actively gates tool calls:

| Disposition | Hook response | When |
|---|---|---|
| `ESCALATE` | `deny` | non-recoverable blast radius (infra destroy, force push, …) |
| `CONTEXT_MISMATCH` | `ask` | declared env does not match the resolved env |
| `REVERSIBLE` / `DRY_RUN` | `allow` | snapshotted first, recoverable |
| `ALLOW` | `allow` | non-mutating, no action needed |

Switch mode in `.demo_cli.toml` or per call:

```bash
demo_cli check "rm -rf dist" --mode enforce
```

Start in shadow. Move to enforce on a project once you trust what it's doing.

---

## Configuration

Run `demo_cli init` to scaffold `.demo_cli.toml` at the project root.

```toml
mode = "shadow"

[workspace]
dir = ".demo_cli"          # receipts + recovery points (gitignored)

[approval]
key_env = "DEMO_CLI_APPROVER_KEY"

[[target]]
match = "production"       # substring matched against the resolved target ref
env = "production"
recovery = "snapshot"      # snapshot | none
```

Environment is resolved in priority order: an explicit `--actual-env` flag,
then a `[[target]]` match in this file, then a heuristic over the command text.
A production database is reached via a connection string, not by a file called
`prod.db`, the declared target is always the source of truth.

---

## Command reference

| Command | What it does |
|---|---|
| `check "<cmd>"` | evaluate a command; flags: `--db`, `--db-url`, `--target`, `--mode`, `--intent-env`, `--actual-env`, `--reason`, `--approval-token`, `--json`, `--quiet` |
| `log` | list captured recovery points (id, when, kind, size, action) |
| `undo [id]` | restore a recovery point by id, or the latest |
| `diff [id]` | show what changed since a recovery point |
| `verify` | walk the receipt hash-chain → INTACT or TAMPERED |
| `report` | summarise recorded decisions |
| `receipt [id]` | print a copy-pasteable, tamper-evident proof card for a receipt (latest, or by id); `--list` shows recent receipt ids |
| `status` | mode, hook state, receipts, chain integrity, recovery count |
| `doctor` | check python version, config, pg tools, hook registration, PATH |
| `prune` | delete old recovery artefacts (`--keep N`, `--older-than DAYS`); receipts are never pruned |
| `init` | scaffold `.demo_cli.toml` |
| `install-hook` | write PreToolUse entries into `.claude/settings.json` (add `--cursor` for `.cursor/hooks.json`) |
| `hook` | (internal) called by Claude Code; reads tool JSON on stdin, writes permission decision on stdout |
| `hook-cursor` | (internal) called by Cursor; reads `beforeShellExecution` JSON on stdin, writes permission decision on stdout |

Exit codes for `check`: `0` allow, `1` context mismatch, `2` escalate.

---

## What it covers, and what it does not

**Snapshot targets:** sqlite files, postgres (via `pg_dump`/`pg_restore`),
individual files, directories (capped at `DEMO_CLI_MAX_SNAPSHOT_MB`, default 256 MB).

**Escalated honestly, never falsely snapshotted:** terraform/kubectl/cloud
destroy commands, `git push --force`, remote filesystem changes, object-storage
deletions, external side effects (email, payments, webhooks), credential rotation,
and any database reached over a non-local connection string. demo_cli will not
claim a recovery it cannot provide.

**Recursive-force deletes are a hard-stop in every environment:**
`Remove-Item -Recurse -Force` (PowerShell, including the `ri` alias and
abbreviated `-r`/`-fo` flags in any order), `rmdir /s`, and `del /s|/f`. These
wipe a whole tree with no recycle bin and expose no target the snapshot layer
can capture, so there is no honest recovery point to stand behind. They are
denied even in a `development`/`staging` workspace, where an ordinary,
snapshottable delete (`rm -rf ./build`) would instead be captured and allowed.
A human structural-approval token is the one legitimate override; an agent
cannot forge it.

**Out of scope for this beta:**
- Adversarial agents deliberately evading classification
- Reversing already-sent external effects
- Multi-agent concurrent sessions (receipt locking is POSIX; Windows degrades to best-effort)
- Adapters for agents other than Claude Code and Cursor (Aider, Cline, planned)

---

## Known issues (beta)

- **PATH:** `demo_cli` must be resolvable in the shell where `claude` runs.
  Install with `pipx`, or keep the virtualenv active. Run `demo_cli doctor`
  to check. If the binary is not found, Claude Code silently skips the hook.
- **Single-path `rm`:** `rm a.py b.py` (multiple targets) escalates rather than
  partially snapshotting one file. This is intentional - partial recovery is
  not honest recovery.
- **`mv` is conservative:** most two-argument `mv` commands are flagged. Safe
  renames inside the project workspace will be narrowed in a future release.

---

## Library use

```python
from demo_cli import Guard

result = Guard(mode="enforce").evaluate("DELETE FROM users", explicit_db="app.db")
print(result.decision.decision)   # REVERSIBLE
print(result.permission)          # allow
```

---

## Tests

```bash
pip install -e ".[dev]"
pytest -q
# 85 tests, passing on Python 3.9 – 3.14
```

---

## Contributing

This is a public beta. The most useful reports are **real commands from real
agent sessions** that produced a wrong decision (false block or missed snapshot).
Open an issue with the command, your `.demo_cli.toml` (redact credentials), and
what you expected. Bug reports found through dogfooding, like the `rm app.db`
case that shaped 0.4.0b3, are exactly what the project needs right now.

To make that trivial, an in-context feedback prompt fires on a wrong-call-worthy
decision (a block, a context mismatch, or a snapshot): the tool prints a
`Wrong call? → report it` line with a **prefilled** GitHub issue (decision,
reason, matched rule, command already filled in). It is consent-based and
one-directional — a link handed to you, nothing phones home. It stays silent on
plain `ALLOW`s so it never becomes noise. `demo_cli receipt --share` turns any
receipt into a plain-text proof card you can paste into an issue or thread.
