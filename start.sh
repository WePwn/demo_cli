#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "Python 3 is required to run this demo."
  exit 1
fi

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'
  CYAN=$'\033[0;36m'; DIM=$'\033[2m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
else
  RED=''; GREEN=''; YELLOW=''; CYAN=''; DIM=''; BOLD=''; RESET=''
fi

FAST="${DEMO_FAST:-0}"
TYPE_DELAY="${TYPE_DELAY:-0.018}"
FEEDBACK_URL="https://github.com/WePwn/demo_cli/issues"

# The hook prints the feedback link at the end of its own output. During the
# guided demo we suppress that and print it once at the very end instead.
export DEMO_CLI_SUPPRESS_FEEDBACK=1

pause() {
  local seconds="$1"
  if [ "$FAST" = "1" ]; then sleep 0.2; else sleep "$seconds"; fi
}

type_line() {
  local text="$1"
  if [ "$FAST" = "1" ]; then printf "%s\n" "$text"; return; fi
  local i ch
  for ((i=0; i<${#text}; i++)); do ch="${text:i:1}"; printf "%s" "$ch"; sleep "$TYPE_DELAY"; done
  printf "\n"
}

section() {
  local title="$1"
  echo
  printf "%b\n" "${CYAN}==============================================================${RESET}"
  printf "%b\n" "${CYAN}${BOLD}${title}${RESET}"
  printf "%b\n" "${CYAN}==============================================================${RESET}"
}

say() { type_line "$1"; }

run_cmd() {
  local display="$1"; local wait_after="$2"; shift 2
  echo
  printf "%s" "$DIM"; type_line "\$ ${display}"; printf "%s" "$RESET"
  pause 0.7
  "$@"
  pause "$wait_after"
}

section "demo_cli v0.3 quick demo"
say "This script only touches the local throwaway demo database and a temp dir."
say "It creates examples/production.db, runs the hook, captures snapshots, shows what changed and undo."
say "Use DEMO_FAST=1 ./start.sh if you want to skip the typing effect."
pause 2

mkdir -p examples
rm -f examples/production.db production.db
rm -rf .demo_cli_recovery
rm -f demo_cli_receipts.jsonl

section "1/7 Create the throwaway production database"
run_cmd "cd examples && $PY create_production_db.py" 3 bash -lc "cd examples && $PY create_production_db.py"

section "2/7 Safe read"
say "A read-only action should pass without needing a recovery point."
run_cmd "$PY demo_cli_hook.py \"SELECT * FROM users\" --db examples/production.db" \
  4 "$PY" demo_cli_hook.py "SELECT * FROM users" --db examples/production.db

section "3/7 Chained / piped command"
say "A safe read followed by a destructive step. The hook splits the chain and"
say "still catches the rm. An opaque 'curl | bash' would be escalated outright."
run_cmd "$PY demo_cli_hook.py \"SELECT * FROM users && rm -fr ./build\" --db examples/production.db" \
  5 "$PY" demo_cli_hook.py "SELECT * FROM users && rm -fr ./build" --db examples/production.db

section "4/7 Destructive cleanup with preview"
say "This command is destructive, so demo_cli previews affected rows and captures a recovery point first."
run_cmd "$PY demo_cli_hook.py \"DELETE FROM users WHERE last_login < '2010-01-01'\" --db examples/production.db" \
  5 "$PY" demo_cli_hook.py "DELETE FROM users WHERE last_login < '2010-01-01'" --db examples/production.db

section "5/7 The agent runs it - then we see what changed"
say "The hook is pre-execution. Here the agent actually applies the delete,"
say "then 'diff' shows exactly which rows changed against the recovery point."
run_cmd "$PY -c \"apply the delete\"" 2 "$PY" -c \
  "import sqlite3; c=sqlite3.connect('examples/production.db'); c.execute(\"DELETE FROM users WHERE last_login < '2010-01-01'\"); c.commit(); c.close(); print('agent executed the DELETE')"
run_cmd "$PY demo_cli_hook.py diff" 5 "$PY" demo_cli_hook.py diff

section "6/7 Undo from the latest recovery point"
say "The last recovery point is restored with one command."
run_cmd "$PY demo_cli_hook.py undo" 4 "$PY" demo_cli_hook.py undo

section "7/7 Verify the receipt chain"
say "Every decision was written to a hash-chained log. 'verify' walks it end to end."
run_cmd "$PY demo_cli_hook.py verify" 4 "$PY" demo_cli_hook.py verify

section "Bonus - valid command, wrong context"
say "The command is reasonable, but the stated intent says staging while the target is production."
run_cmd "$PY demo_cli_hook.py \"UPDATE users SET is_active = 0 WHERE last_login < '2010-01-01'\" --db examples/production.db --intent-env staging --intent-scope \"clean old inactive users in staging\"" \
  6 "$PY" demo_cli_hook.py "UPDATE users SET is_active = 0 WHERE last_login < '2010-01-01'" \
  --db examples/production.db \
  --intent-env staging \
  --intent-scope "clean old inactive users in staging"

section "Demo complete"
printf "%b\n" "${GREEN}Demo completed.${RESET}"
echo "Receipts were written to demo_cli_receipts.jsonl  (check them with: $PY demo_cli_hook.py verify)"
echo "Snapshots were stored in .demo_cli_recovery/."
echo
echo "What we want feedback on:"
echo "- Did demo_cli snapshot something it should have allowed?"
echo "- Did demo_cli allow something it should have snapshotted first?"
echo "- Which target types matter most next: Postgres, MySQL, object storage, the git working tree?"
echo
echo "Feedback? Open an issue: $FEEDBACK_URL"
