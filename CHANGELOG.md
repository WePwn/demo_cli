# Changelog

## 0.4.0b8 - honesty + release hardening

* **The shared-receipt install command is pinned to the release tag.** A
  `receipt --share` card tells the recipient how to verify the hash chain
  themselves; that command still pointed at `@beta`, the moving branch the
  README explicitly warns against. It now derives the tag from the package
  version via `version.release_tag()` (`0.4.0b8` → `v0.4.0-beta.8`), so a
  stranger verifies against exactly the code the receipt was written by, and
  the command follows the next version bump instead of going stale. Locked by
  a regression test — this was previously unasserted, which is how it survived.
* **Fixed a self-defeating trust check in the README.** The no-telemetry claim
  invited readers to run `grep -rn "requests\|urllib\|http\|socket" src/`,
  which *returns hits*: `urllib.parse` is imported twice for pure string
  handling (building the prefilled issue link; reading a hostname out of a DB
  connection string to tell local from remote). The grep now covers actual
  network I/O (`urlopen`, `requests`, `httpx`, `socket`, `http.client`) and
  returns nothing, with the two benign `urllib.parse` uses named explicitly
  rather than hidden.
* **Corrected the opening comparison.** The README claimed Claude Code and
  Cursor "neither snapshot first." Their checkpointing *does* cover the agent's
  own file edits; what it does not cover is shell commands and databases. The
  line now says that, which is both accurate and the actual gap.
* **Relicensed to MIT.** The project is now MIT-licensed across `LICENSE`,
  `pyproject.toml`, and the README (previously Apache-2.0). MIT is more
  permissive and drops Apache-2.0's explicit patent grant.
* **Escalate everywhere: removed the SANDBOX low-blast exception.** An
  unrecoverable, non-recursive mutation in a `development`/`test`/`sandbox`/
  `staging` workspace previously returned `SANDBOX` (posture SAFE) and proceeded
  with no recovery point. It now escalates like everywhere else — an
  unrecoverable mutation is never waved through on the strength of an environment
  label. The stated invariant ("recoverable or escalate") is now true without
  exception. The `SANDBOX` disposition is gone.
* **Install is pinned to a release tag, not the moving `beta` branch.** The
  recommended path is manual and verify-first (read source + confirm the
  published SHA-256 before anything runs). `install.sh` / `install.ps1` and every
  README install snippet now reference `v0.4.0-beta.8`. `curl | sh` remains
  available but is clearly labelled convenience, not the safe path.
* **Corrected the recovery-demo caption.** It previously implied the referenced
  incident's files were "gone for good"; file-carving tools recover *some*
  fraction. The caption now states the honest case — carving is partial and luck,
  a recovery point taken *before* the command restores exactly the captured state.
* **Fail-open is now loud on every path.** A bug in demo_cli must never brick the
  agent, so the hook fails open on its own internal errors — but the one
  remaining silent path (unparseable hook input) now writes a visible stderr
  warning instead of a silent allow. Decisions themselves remain fail-closed.
* **Documented two standing limitations** in the README: shell parsing is
  pattern-based (a full command AST is roadmap; ambiguous parses are treated as
  unrecoverable), and database coverage is SQLite + Postgres only (other engines
  escalate rather than being captured).
* **Added `SECURITY.md`** with a responsible-disclosure contact.
* **Windows drive-letter detection is now cross-platform** (via `ntpath`), so a
  multi-drive delete is recognised as having no common capture root on any host,
  and the corresponding test runs everywhere instead of being Windows-only.

## 0.4.0b7 - Windows: PowerShell hook coverage + honest Remove-Item recovery

**Also fixed - receipt-chain locking was POSIX-only (fcntl), a silent no-op on Windows**, so concurrent writers could fork the tamper-evident chain. Replaced with a cross-platform sidecar lock (msvcrt.locking on Windows, fcntl.flock on POSIX) held across the whole read-hash to append to fsync section, with bounded retry and a ReceiptLockError rather than an unlocked write. Verified under 4 threads x 10 receipts and separate spawned processes.

Confirmed real-world failure: Claude Code on Windows fired
`Remove-Item -Recurse -Force ".\victim"` under `tool_name="PowerShell"`. The
folder was deleted with **no receipt and no recovery point** - the debug log
showed `Hooks: Found 0 total hooks in registry`. Four root causes, all closed:

* **The installed hook only matched `Bash`.** `install-hook` now also installs
  a `PowerShell` matcher block; an existing Bash-only install is upgraded
  in place (the Bash block is left untouched, not duplicated) rather than
  left silently half-covered.
* **`run_pretooluse` only evaluated `tool_name == "Bash"`.** PowerShell
  commands now route through the same `tool_input.command` evaluation path.
* **`recovery.py` only extracted targets for Unix `rm`/`mv`.** Added
  conservative `Remove-Item` target extraction: `-Path`, `-LiteralPath`, or a
  single positional operand. Anything else - missing, ambiguous, multiple
  targets, multiple drives, or a wildcard - is left unresolved by design, so
  the caller escalates instead of guessing.
* **`ps_remove_item_rf` was unconditionally nonrecoverable**, so it could
  never receive a snapshot even when its target was perfectly resolvable.
  It is now snapshotted and treated as an ordinary recoverable mutation
  (`REVERSIBLE`) when the target exists inside the project root; it still
  hard-stops (`ESCALATE`) in every environment, including
  `development`/`staging`, when it does not. `rmdir /s` and `del /s|/f`
  keep the old unconditional hard-stop - their target extraction isn't
  implemented yet.

The `doctor` self-test now drives a synthetic `PowerShell` payload through
the real hook entrypoint in addition to the existing `Bash` one, so a
Windows install where only the Bash matcher registered is caught before an
agent hits it for real.

## 0.4.0b6 - multi-path recovery, brace expansion, affected-files preview

This is where the auto-fire recovery path changed. (0.4.0b5 correctly stated
that *its* changes left snapshot/recovery untouched; the work below lands on top
of it.) It closes a live data-loss incident and finishes the shell-expansion
story the snapshot depends on.

### Multi-path `rm` now captures the common directory (was: escalate)
Forced by a live incident (`claude-code#76626`): an agent ran
`rm -f Reports/report_*.txt Reports/report_*.png` meaning only to count the
files. Every path was local, bounded, and trivially copyable - precisely the
case where full capture is *provable* - yet the old rule returned `None` for any
multi-path `rm` and escalated, and the files were permanently gone. Now, several
paths that collapse into one capturable directory snapshot **that directory**: a
superset of the blast radius, so recovery stays provable, never partial. Paths
that do not collapse to one bounded directory still return `None` and still
escalate. The size cap in `snapshot()` and the project-root bound in `guard()`
both still apply on top.

### Shell expansion, done the way the shell does it
The command reaches the hook *before* the shell has touched it, so globs and
braces arrive literally and `os.path.exists()` on them is `False` - which is why
the extractor used to see zero operands and capture nothing.

* **Globs** (`Reports/*.png`) are expanded so the real operands are seen.
* **Brace expansion** (`file{1,2,3}.txt`, `{a..z}`, `{a,b}{1,2}`, nesting) is now
  handled too - the same failure mode as globs, and unconditional in bash, so a
  brace-delete now fires the snapshot instead of slipping through. Bounded by
  `_BRACE_MAX`; a pathological expansion falls back to the literal token (i.e.
  the honest escalate path), never an unbounded blow-up.

### Affected-files preview
`expanded_operands(cmd)` is now wired into the rendered output: `check` prints an
**Affected files (preview)** section listing the concrete files an `rm` / `mv`
will touch. That is the uKER insight made real - the agent wanted a file count;
the expansion *is* the file count - shown in the same operation, so an accidental
mass-delete is impossible to approve blind. Also surfaced in `--json`
(`affected_paths`).

### Project-root capture refusal
A two-file `rm` at the top level of a project collapses to a common root of the
whole project; silently deep-copying the entire tree on every such `rm` is
neither honest nor cheap. The project root itself is now refused as a capture
surface (same spirit as refusing `$HOME` / the filesystem root) and escalates
instead. A common root that is a real *subdirectory* (the incident case) still
captures and stays reversible.

### Honesty notes pinned in the code
`extract_path_operand` now documents that it is **not** the honesty boundary by
itself - when a single operand exists among several, the result collapses to that
path even outside the project, and it is the guard's `within` check that refuses
it; do not reuse the function without that bound. `restore_entry` documents that
directory restore is **coarse** (it reverts the whole captured directory to its
snapshot state), which is what keeps recovery a provable superset.

Tests: **101 passing** (+9: brace expansion, expanded-operands gating, the
project-root refusal, and the multi-path/glob capture cases).

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
