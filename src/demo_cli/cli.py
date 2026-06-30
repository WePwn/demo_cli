"""Command-line interface.

Thin by design: parse arguments, call into the library, render the result.
No safety logic lives here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from . import recovery, render
from .config import CONFIG_NAME, load_config
from .context import Intent, normalize_env
from .decide import CONTEXT_MISMATCH, ESCALATE
from .diff import diff_entry
from .guard import Guard
from .receipts import verify_chain
from .version import __version__

_EXIT = {ESCALATE: 2, CONTEXT_MISMATCH: 1}

_CONFIG_TEMPLATE = """\
# demo_cli configuration. All fields are optional; defaults are safe.
# Docs: https://github.com/WePwn/demo_cli

mode = "shadow"            # "shadow" observes only; "enforce" gates actions

[workspace]
dir = ".demo_cli"          # receipts + recovery points live here (per project)

# [approval]
# key_env = "DEMO_CLI_APPROVER_KEY"   # env var holding the structural-approval key

# Declare your real targets so environment is known, not guessed.
# [[target]]
# match = "production"     # substring matched against the resolved target ref
# env = "production"
# recovery = "snapshot"    # snapshot | none   (attest is reserved)
"""


def _build_intent(a) -> Intent:
    return Intent(
        env=normalize_env(getattr(a, "intent_env", None)),
        branch=getattr(a, "intent_branch", None),
        cwd=getattr(a, "intent_cwd", None),
        remote=getattr(a, "intent_remote", None),
        scope=getattr(a, "intent_scope", None),
        reasoning=getattr(a, "reason", None),
    )


def cmd_check(a) -> int:
    if getattr(a, "no_color", False):
        render.set_color(False)
    guard = Guard(mode=a.mode)
    result = guard.evaluate(
        a.command,
        target_path=a.target, explicit_db=a.db, db_url=a.db_url,
        intent=_build_intent(a), actual_env=a.actual_env,
        approval_token=a.approval_token,
        agent_id="cli", session_id="cli",
    )
    if getattr(a, "json", False):
        print(json.dumps(render.result_json(result, __version__), indent=2))
    elif getattr(a, "quiet", False):
        print(render.quiet_line(result))
    else:
        render.render_result(result, __version__)
    return _EXIT.get(result.decision.decision, 0)


def _resolve_entry(cfg, a):
    """Pick a recovery entry: by id if given, else scoped by --target, else latest."""
    rid = getattr(a, "id", None)
    if rid:
        return recovery.find(cfg.recovery_dir, rid)
    ref = os.path.abspath(a.target) if getattr(a, "target", None) else None
    return recovery.latest(cfg.recovery_dir, ref)


def cmd_undo(a) -> int:
    cfg = load_config()
    entry = _resolve_entry(cfg, a)
    ok = recovery.restore_entry(entry) if entry else False
    render.render_restore(entry, ok, __version__)
    return 0 if ok else 1


def cmd_diff(a) -> int:
    cfg = load_config()
    entry = _resolve_entry(cfg, a)
    if not entry:
        print(render.c("No recovery point matched. Try `demo_cli log`.", "red"))
        return 1
    render.render_diff(entry, diff_entry(entry), __version__)
    return 0


def cmd_log(a) -> int:
    cfg = load_config()
    render.render_log(recovery.load_entries(cfg.recovery_dir), __version__)
    return 0


def cmd_verify(a) -> int:
    cfg = load_config()
    v = verify_chain(cfg.receipts_path)
    render.render_verify(v, __version__)
    return 0 if v.ok else 1


def cmd_report(a) -> int:
    cfg = load_config()
    v = verify_chain(cfg.receipts_path)
    if not os.path.exists(cfg.receipts_path):
        print("No receipts yet. Run some commands through `demo_cli check` first.")
        return 0
    total = 0
    by_decision = {}
    recovered = 0
    with open(cfg.receipts_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            total += 1
            by_decision[r.get("decision", "?")] = by_decision.get(r.get("decision", "?"), 0) + 1
            if r.get("recovery_point"):
                recovered += 1
    print(render.c(f"\ndemo_cli {__version__}  shadow report\n", "dim"))
    print(render.kv("receipts", total))
    print(render.kv("chain", "intact" if v.ok else f"TAMPERED at line {v.broken_at}"))
    print(render.kv("recovery points", recovered))
    for k, n in sorted(by_decision.items()):
        print(render.kv("  " + k, n))
    print()
    return 0


def _hook_installed(path) -> bool:
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False
    for block in (data.get("hooks", {}) or {}).get("PreToolUse", []) or []:
        for h in (block or {}).get("hooks", []) or []:
            if h.get("command") == "demo_cli hook":
                return True
    return False


def _any_hook_installed(cfg) -> bool:
    project = os.path.join(cfg.project_root, ".claude", "settings.json")
    glob = os.path.expanduser("~/.claude/settings.json")
    return _hook_installed(project) or _hook_installed(glob)


def cmd_doctor(a) -> int:
    import shutil
    cfg = load_config()
    checks = []

    pyok = sys.version_info >= (3, 9)
    checks.append(("ok" if pyok else "fail", "python >= 3.9",
                   f"{sys.version_info.major}.{sys.version_info.minor}"))
    checks.append(("ok", "mode", cfg.mode))
    checks.append(("ok" if cfg.source_path else "warn", "config",
                   cfg.source_path or "using defaults (run: demo_cli init)"))

    ws = cfg.workspace
    writable = True
    try:
        os.makedirs(ws, exist_ok=True)
        probe = os.path.join(ws, ".write_probe")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except Exception:
        writable = False
    checks.append(("ok" if writable else "fail", "workspace writable", ws))

    pg = bool(shutil.which("pg_dump") and shutil.which("pg_restore"))
    checks.append(("ok" if pg else "warn", "postgres tools",
                   "pg_dump/pg_restore found" if pg else "missing (postgres snapshot disabled)"))
    git = bool(shutil.which("git"))
    checks.append(("ok" if git else "warn", "git", "found" if git else "missing (branch/remote context off)"))

    hook = _any_hook_installed(cfg)
    checks.append(("ok" if hook else "warn", "claude code hook",
                   "installed" if hook else "not installed (run: demo_cli install-hook)"))

    render.render_doctor(checks, __version__)
    return 0 if all(s != "fail" for s, _, _ in checks) else 1


def cmd_prune(a) -> int:
    cfg = load_config()
    if a.keep is None and a.older_than is None:
        print("Specify --keep N or --older-than DAYS. Receipts are never pruned (audit trail).")
        return 1
    removed = recovery.prune(cfg.recovery_dir, keep=a.keep, older_than_days=a.older_than)
    freed = sum(e.get("_freed_bytes", 0) for e in removed)
    print(render.c(f"\ndemo_cli {__version__}  prune\n", "dim"))
    print(render.kv("removed", f"{len(removed)} recovery point(s)"))
    print(render.kv("freed", render._size(freed)))
    print(render.kv("note", "receipts untouched (tamper-evident audit trail)"))
    print()
    return 0


def cmd_status(a) -> int:
    cfg = load_config()
    v = verify_chain(cfg.receipts_path)
    total = v.entries if v.ok else 0
    if not v.ok and os.path.exists(cfg.receipts_path):
        with open(cfg.receipts_path, encoding="utf-8") as f:
            total = sum(1 for line in f if line.strip())
    info = {
        "mode": cfg.mode,
        "hook": _any_hook_installed(cfg),
        "config": cfg.source_path,
        "receipts": total,
        "chain": "intact" if v.ok else (f"TAMPERED at line {v.broken_at}"
                                        if os.path.exists(cfg.receipts_path) else "none yet"),
        "recovery_points": len(recovery.load_entries(cfg.recovery_dir)),
        "workspace": cfg.workspace,
    }
    render.render_status(info, __version__)
    return 0


def cmd_init(a) -> int:
    path = os.path.join(os.getcwd(), CONFIG_NAME)
    if os.path.exists(path) and not a.force:
        print(f"{CONFIG_NAME} already exists. Use --force to overwrite.")
        return 1
    with open(path, "w", encoding="utf-8") as f:
        f.write(_CONFIG_TEMPLATE)
    print(f"Wrote {path}")
    return 0


def cmd_install_hook(a) -> int:
    from .hooks.claude_code import settings_snippet, install_into_settings
    snippet = settings_snippet()
    if a.print:
        print(json.dumps(snippet, indent=2))
        return 0
    target = (os.path.expanduser("~/.claude/settings.json") if a.scope == "global"
              else os.path.join(os.getcwd(), ".claude", "settings.json"))
    install_into_settings(target)
    print(f"Installed PreToolUse hook into {target}")
    print("demo_cli will now fire automatically before each Bash command.")
    return 0


def cmd_hook(a) -> int:
    from .hooks.claude_code import run_pretooluse
    return run_pretooluse(sys.stdin, sys.stdout)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="demo_cli",
        description="A pre-execution safety layer for AI coding agents: "
                    "preview, snapshot, undo, and a tamper-evident receipt of every decision.",
    )
    p.add_argument("--version", action="version", version=f"demo_cli {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--no-color", action="store_true", help="disable coloured output")
    sub = p.add_subparsers(dest="cmd")

    ch = sub.add_parser("check", parents=[common], help="evaluate one command before it runs")
    ch.add_argument("command")
    ch.add_argument("--target", default=None, help="file or directory to snapshot")
    ch.add_argument("--db", default=None, help="sqlite database path")
    ch.add_argument("--db-url", default=None, help="postgres connection url")
    ch.add_argument("--mode", choices=("shadow", "enforce"), default=None)
    ch.add_argument("--actual-env", default=None, help="assert the real environment")
    ch.add_argument("--intent-env", default=None, help="environment the agent believes it is in")
    ch.add_argument("--intent-branch", default=None)
    ch.add_argument("--intent-cwd", default=None)
    ch.add_argument("--intent-remote", default=None)
    ch.add_argument("--intent-scope", default=None)
    ch.add_argument("--reason", default=None, help="the agent's stated reasoning (why-ledger)")
    ch.add_argument("--approval-token", default=None, help="structural-approval token")
    ch.add_argument("--json", action="store_true", help="machine-readable output")
    ch.add_argument("--quiet", action="store_true", help="one-line summary")
    ch.set_defaults(func=cmd_check)

    un = sub.add_parser("undo", parents=[common], help="restore a recovery point (latest, or by id)")
    un.add_argument("id", nargs="?", default=None, help="recovery point id (see `demo_cli log`)")
    un.add_argument("--target", default=None)
    un.set_defaults(func=cmd_undo)

    df = sub.add_parser("diff", parents=[common], help="show what changed since a recovery point")
    df.add_argument("id", nargs="?", default=None, help="recovery point id (see `demo_cli log`)")
    df.add_argument("--target", default=None)
    df.set_defaults(func=cmd_diff)

    lg = sub.add_parser("log", parents=[common], help="list captured recovery points")
    lg.set_defaults(func=cmd_log)

    vf = sub.add_parser("verify", parents=[common], help="verify the receipt hash-chain")
    vf.set_defaults(func=cmd_verify)

    rp = sub.add_parser("report", parents=[common], help="summarise recorded decisions (shadow report)")
    rp.set_defaults(func=cmd_report)

    st = sub.add_parser("status", parents=[common], help="show mode, hook, receipts, recovery points")
    st.set_defaults(func=cmd_status)

    dc = sub.add_parser("doctor", parents=[common], help="check the install across this environment")
    dc.set_defaults(func=cmd_doctor)

    pr = sub.add_parser("prune", parents=[common], help="delete old recovery points (receipts kept)")
    pr.add_argument("--keep", type=int, default=None, help="retain the N most recent points")
    pr.add_argument("--older-than", type=int, default=None, metavar="DAYS",
                    help="delete points older than DAYS")
    pr.set_defaults(func=cmd_prune)

    it = sub.add_parser("init", parents=[common], help=f"write a starter {CONFIG_NAME}")
    it.add_argument("--force", action="store_true")
    it.set_defaults(func=cmd_init)

    ih = sub.add_parser("install-hook", parents=[common], help="wire the PreToolUse hook into Claude Code")
    ih.add_argument("--scope", choices=("project", "global"), default="project")
    ih.add_argument("--print", action="store_true", help="print the settings snippet instead of writing")
    ih.set_defaults(func=cmd_install_hook)

    hk = sub.add_parser("hook", parents=[common], help="(internal) PreToolUse entrypoint; reads JSON on stdin")
    hk.set_defaults(func=cmd_hook)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "no_color", False):
        render.set_color(False)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
