#!/usr/bin/env sh
# demo_cli one-command install (macOS / Linux).
#
#   curl -fsSL https://raw.githubusercontent.com/WePwn/demo_cli/beta/install.sh | sh
#
# Yes, this is curl-pipe-sh. demo_cli's whole point is that you read code
# before you run it, so read this first - it is ~90 lines and does exactly
# four things: install, init, hook, verify. It writes nothing outside your
# project and this tool's own workspace, and it phones nobody.
#
# The one failure mode it must eliminate: a hook installed but NOT on PATH,
# where Claude Code silently proceeds with no protection. This script fails
# LOUD instead - it runs `doctor` at the end and tells you plainly whether the
# next destructive command is actually covered.
set -eu

REPO="git+https://github.com/WePwn/demo_cli.git@beta"
say()  { printf '%s\n' "$*"; }
ok()   { printf '\033[32m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

say ""
say "demo_cli installer"
say "──────────────────"

# 1. python
if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else die "Python 3.9+ not found. Install it first, then re-run."
fi
VER=$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "?")
say "  python  $VER"

# 2. install via pipx (isolated, on PATH), bootstrapping pipx if needed
if ! command -v pipx >/dev/null 2>&1; then
  warn "  pipx not found - installing it ($PY -m pip install --user pipx)"
  "$PY" -m pip install --user -q pipx || die "could not install pipx"
  "$PY" -m pipx ensurepath >/dev/null 2>&1 || true
  # make pipx's bin dir visible to THIS shell so the checks below work now
  PIPX_BIN="$("$PY" -m site --user-base 2>/dev/null)/bin"
  case ":$PATH:" in *":$PIPX_BIN:"*) : ;; *) PATH="$PIPX_BIN:$PATH"; export PATH ;; esac
fi
say "  installing demo_cli (pipx)..."
pipx install --force "$REPO" >/dev/null 2>&1 || die "pipx install failed - try:  pipx install $REPO"

# 3. resolve the binary NOW, in this shell. If it isn't visible, say exactly why.
if ! command -v demo_cli >/dev/null 2>&1; then
  BIN="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || echo "$HOME/.local/bin")"
  warn ""
  warn "  demo_cli installed but not on PATH in this shell."
  warn "  add this to your shell profile, then open a NEW terminal:"
  warn "      export PATH=\"$BIN:\$PATH\""
  warn ""
  warn "  IMPORTANT: until demo_cli is on PATH in the shell where you launch"
  warn "  'claude', Claude Code will silently run with NO protection."
  exit 1
fi
ok  "  demo_cli on PATH: $(command -v demo_cli)"

# 4. init + hook in the current project (idempotent)
say "  wiring into this project..."
demo_cli init          >/dev/null 2>&1 || true   # shadow mode by default
demo_cli install-hook  >/dev/null 2>&1 || warn "  install-hook: check .claude/settings.json is writable"

# 5. the verdict - loud, honest, the whole reason this script exists
say ""
demo_cli doctor || true
say ""
if demo_cli status 2>/dev/null | grep -qi "hook installed.*yes"; then
  ok  "✅ hook live. Your next destructive command gets a recovery point."
  ok  "   (shadow mode: it observes and snapshots, never blocks. Flip to"
  ok  "    enforce in .demo_cli.toml once you trust it.)"
else
  warn "⚠  hook not confirmed. Run 'demo_cli doctor' above and fix what's red."
fi
say ""
say "  try it:   demo_cli check \"rm -rf ./build\""
say "  undo:     demo_cli undo"
say "  verify:   demo_cli verify"
say ""
