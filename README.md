# demo_cli

**Claude Code and Cursor *ask* you to confirm dangerous commands, or *block* them. Neither snapshots first, and their undo doesn't cover shell commands or databases.**

demo_cli snapshots the real target **before** a destructive command runs, so a wrong call is reversible with one command. **Confirming ≠ recovering.**

Before `rm -rf`, `rmdir /s /q`, `Remove-Item -Recurse -Force`, `git reset --hard`, or `DROP TABLE` executes, demo_cli captures what's about to be destroyed and writes a tamper-evident receipt of the decision. If the agent gets it wrong, `demo_cli undo` brings it back. When it *can't* prove recovery (`terraform destroy`, `git push --force`, remote/cloud resources, or a recursive-force delete with no recoverable target), it hard-blocks instead of faking safety.

The whole design in one line: **recovery is the default; blocking is the fallback for the truly unrecoverable, not the default for everything.**

![demo_cli snapshots before an agent's rm, lets it run, and undoes it in one command](https://cdn.wepwn.ma/images/demo/demo_recover.gif)

*A real session. The agent's delete is **not** blocked, it runs, the files really are gone, and one command brings them back. This is [claude-code#76626](https://github.com/anthropics/claude-code/issues/76626): an agent ran `rm -f Reports/report_*.txt Reports/report_*.png` intending only to count the files. That user's screenshots, referenced across 57 open tickets, are gone for good. Not in the recycle bin (no CLI delete ever is), not in git, not in a shadow copy.*

### Start in shadow mode, it changes nothing

Default mode is observe-only: it logs what it *would* have caught and touches nothing. Run it a week on a low-stakes project, read the receipts, then decide whether to let it act.

```bash
pipx install git+https://github.com/WePwn/demo_cli.git@beta
demo_cli init        # shadow mode by default, observes, never blocks
demo_cli doctor      # verify the install
```

### Why you can trust it

- **No telemetry, it phones nobody.** Verify it yourself: `grep -rn "requests\|urllib\|http\|socket" src/`
- **One small, readable, MIT-licensed codebase**, read exactly what it does before you run it.
- **Tamper-evident receipts**, every decision is hash-chained and independently verifiable (`demo_cli verify`).

### "Why not just use git / Claude Code checkpoints?"

git and Claude Code's rewind can't recover an `rm -rf` outside the repo, a dropped database, or an overwrite of an untracked file, they don't snapshot before shell commands run. That's the exact gap demo_cli fills.

> **Threat model:** cooperative agents making mistakes, not adversarial evasion. An agent actively trying to evade protection is out of scope, no hook solves that.

`0.4.0b6`, public beta.

Built around one invariant:

> A mutating action must be recoverable **and** match its declared context, otherwise it is escalated, never silently allowed, and never falsely reported as "recovered".

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

### And when it cannot recover, it says so

![demo_cli refuses an rsync to a remote host and explicitly refuses to claim a recovery point](https://cdn.wepwn.ma/images/demo/demo_honest.gif)

*An `rsync --delete` to a production host over SSH. No snapshot on your machine
can reach the far end of that connection, so demo_cli does not take one and does
not pretend it did. The most important line a safety tool can print is the one
admitting what it did not do.*

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

**Shell expansion is resolved before the decision is made.** The command reaches
the hook *before* the shell has touched it, so `Reports/*.png` and
`file{1,2,3}.txt` arrive as literal strings, and a naive `os.path.exists()` on
them returns false. demo_cli expands globs and braces itself (`{a..z}`, `{a,b}{1,2}`,
nested, bounded), so it sees the operands the shell will actually pass to `rm`.
Skipping this is how a mass delete slips through with no snapshot taken.

**Affected files are printed before the action runs.** `check` renders an
**Affected files (preview)** section listing the concrete paths an `rm` / `mv`
will touch (also in `--json` as `affected_paths`). In the incident above, the
agent's stated goal was to *count* the files matching `Reports/report_*`, and the
expansion of that glob **is** that count, so the preview answers the question the
agent was asking and makes an accidental mass delete impossible to approve blind,
in the same operation.

**Multi-path deletes are captured when full capture is provable.** Several paths
that collapse into one capturable *subdirectory* snapshot **that directory**, a
superset of the blast radius, so recovery stays provable rather than partial.
Paths that do not collapse to one bounded directory still escalate.

**Capture surfaces that are always refused,** however small they measure:
`$HOME`, the filesystem root, a Windows drive root, and the **project root
itself**. A two-file `rm` at the top of a repo collapses to a common root of the
whole project, and silently deep-copying the entire tree on every such delete is
neither honest nor cheap, so it escalates instead.

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
- **Directory restore is coarse.** Undoing a directory snapshot reverts the
  **whole captured directory** to its snapshot state. That coarseness is exactly
  what keeps recovery a provable superset rather than a partial guess, but it
  also means unrelated edits made inside that directory *after* the snapshot are
  reverted too. Run `demo_cli diff <id>` before `undo <id>`.
- **A multi-path `rm` at the project top level escalates.** If the paths collapse
  to a common root that is the project root itself (`rm a.py b.py` at the top of
  your repo), demo_cli refuses to deep-copy the whole tree and escalates. Paths
  that collapse to a real subdirectory (`rm Reports/*.txt Reports/*.png`) are
  captured and stay reversible.
- **`mv` is conservative:** most two-argument `mv` commands are flagged. Safe
  renames inside the project workspace will be narrowed in a future release.
- **A pathological brace expansion falls back to the literal token,** which means
  the honest escalate path rather than an unbounded expansion. Bounded by
  `_BRACE_MAX`.

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
# 101 tests, passing on Python 3.9 – 3.14
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
one-directional, a link handed to you, nothing phones home. It stays silent on
plain `ALLOW`s so it never becomes noise. `demo_cli receipt --share` turns any
receipt into a plain-text proof card you can paste into an issue or thread.
