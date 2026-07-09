# Changelog

## 0.4.0b5 - shareable proof cards + in-context feedback prompt

Two adoption-phase features. No telemetry: the only feedback channel is what a
user chooses to send. Both additions honour that — nothing phones home.

### `receipt --share` — a shareable proof card
`demo_cli receipt [id] --share` renders a single receipt as a plain-text,
copy-pasteable card (the exact command caught, the decision, the hash chain,
and a one-line command anyone can run to verify the chain). `--list` shows
recent receipt ids; the id argument accepts the same 8-char prefix shown by
`log`/`undo`. The card is deliberately colour-free so it pastes cleanly into a
forum, PR, or issue, and it inherits the write-time redaction of `action_raw`.

Its correctness invariant carries into the shared artifact: an `ESCALATE` card
states *hard-stopped before it ran — no honest recovery point exists*, a
`REVERSIBLE` card states *recovery point captured*. The card never claims a
recovery it does not hold.

`receipts.py` gains read-side access to the ledger (`load_receipts`,
`find_receipt`) plus the card builder (`share_card`), all reusing the existing
`_canon`/hashing. `cli.py` gains the `receipt` subcommand.

### In-context feedback prompt
After a wrong-call-worthy decision (`ESCALATE`, `CONTEXT_MISMATCH`,
`REVERSIBLE`, `DRY_RUN`), `demo_cli check` prints one `Wrong call? → report it`
line carrying a **prefilled** GitHub issue (decision, reason, matched rule, and
command already filled in). Consent-based and one-directional — a link handed to
the user, no telemetry. Silent on plain `ALLOW`/`SANDBOX` so it never becomes
background noise. `render.py` gains `feedback_line`; `render_result` prints it
just above the mode line. (The hook/quiet stderr path is intentionally left
terse — the prompt rides the interactive `check` path only.)

Zero new dependencies. Scope closed to these two features; classifier, adapters,
snapshot, and recovery are untouched.

Tests: **89 passing** (4 new; on Windows,
`test_concurrent_appends_keep_chain_intact` remains a pre-existing best-effort
lock limitation, not from these changes — see Known issues).

---

## 0.4.0b4 - recursive-force hard-stop + Cursor adapter

Two builds shipped together.

### Recursive-force delete is a hard-stop in every environment
`Remove-Item -Recurse -Force` slipped through entirely (classified
non-mutating, allowed), and `rmdir /s` / `del /s` were waved through by the
low-blast SANDBOX path in a dev/staging workspace - the exact place an agent
runs. Both gaps are closed to match what is stated publicly.

`classify.py`: new `ps_remove_item_rf` rule matches `Remove-Item` (alias `ri`)
carrying both a Recurse-like and a Force-like flag, in any order, full or
abbreviated (`-r`/`-fo`); two disambiguating lookaheads mean a lone `-Force`
does not trigger it. A new `_LOCAL_UNRECOVERABLE` map gives `ps_remove_item_rf`,
`rmdir_s`, and `del_force` the `recursive_force_delete` surface, so the decision
engine escalates them in **every** environment. A human structural-approval
token remains the one legitimate override. `rm -rf` is unchanged - it still
snapshots its operand and stays reversible.

### Cursor adapter (`beforeShellExecution`)
`install-hook --cursor` writes a `beforeShellExecution` hook into
`.cursor/hooks.json` with `failClosed: true`. `demo_cli hook-cursor` reads
Cursor's stdin JSON (`command`, `cwd`) and returns
`{"continue": true, "permission": "allow" | "deny" | "ask"}`.

Cursor defaults to fail-open and reliably honors only `deny`, so the adapter
leans on deny-the-unrecoverable and fails **closed** on its own evaluation
error (the deliberate opposite of the Claude Code adapter). It steps aside only
when Cursor delivers no command at all (a known empty-stdin defect), so a
harness bug never bricks the session. Shell-command gating only in this beta;
file-edit gating stays Claude Code.

Tests: **85 passing** (22 new: 10 for the hard-stop, 12 for the Cursor adapter).

---

## 0.4.0b3 - dogfooding fix: plain `rm` now gated

Found during live testing against a real Claude Code session: an agent issuing
`rm app.db` (no `-rf`) slipped through undetected - classified non-mutating,
no snapshot, no recovery. Only `rm -rf` was previously caught.

**Fix** (`classify.py`): `rm_local` rule matches any top-level `rm` (plain,
`-f`, `-r`, `sudo rm`), anchored to the segment start so `git rm` / `docker rm`
/ `npm rm` produce no false positives. `rm -rf` still matches the more specific
`rm_rf` id. Operand is snapshotted before deletion → reversible.

Tests: **63 passing** (4 new).

---

## 0.4.0b2 - from "a check" to a safety net you can see

Three things kept the value invisible in earlier betas: only Bash was gated,
the recovery loop was not drivable, and shadow mode surfaced nothing.

### File edits are now gated (second door)
An agent can destroy data with `Edit`/`Write`/`MultiEdit`/`NotebookEdit` just
as easily as with `rm`. `Guard.evaluate_file_edit()` snapshots an existing file
before it is overwritten so the change is reversible. Creating a new file
proceeds without a snapshot. If a file cannot be snapshotted, the action
escalates honestly. `install-hook` now writes both matchers (`Bash` and
`Edit|Write|MultiEdit|NotebookEdit`).

### Drivable recovery loop
Every recovery point carries a short `id` and the action that caused it.
`demo_cli log` lists all points; `undo <id>` and `diff <id>` target a specific
one. `prune --keep N` / `--older-than DAYS` reclaims disk without touching the
receipt chain.

### Shadow mode surfaces its value
An ESCALATE / CONTEXT_MISMATCH decision, or a fresh snapshot, is written to
stderr (with the undo id) so it is not silently buried.

### UX
Three-tier posture band: `[+] SAFE` / `[!] REVIEW` / `[x] BLOCKED`.
`check --json` for pipelines, `check --quiet` for one-liners.
`status`, `doctor`, cosmetic fixes (branch, env source display).

### Robustness
POSIX flock on receipt append (parallel agents keep chain intact).
Directory snapshots bounded by `DEMO_CLI_MAX_SNAPSHOT_MB` (default 256 MB).
Recovery filenames include the id (fixes latent same-second filename collision).

Tests: **59 passing** (13 new over 0.4.0b1).

---

## 0.4.0b1 - initial beta

Core pipeline: classify → resolve target → build context → snapshot → decide →
receipt. Shadow mode by default. Claude Code PreToolUse hook (Bash only at this
stage). Hash-chained tamper-evident receipt log. sqlite / postgres / file / dir
snapshot and restore. Declared-first environment resolution.

Tests: **46 passing**.
