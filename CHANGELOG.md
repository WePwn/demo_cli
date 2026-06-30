# Changelog

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
