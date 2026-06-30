"""Terminal rendering. Presentation only; all logic lives elsewhere.

Honours NO_COLOR and non-tty output. Colours follow the product's disposition
palette (green allow, amber recoverable, red escalate, cyan diff).
"""
from __future__ import annotations

import os
import sys
from typing import List

from .context import redact
from .decide import (ALLOW, CONTEXT_MISMATCH, DRY_RUN, ESCALATE, REVERSIBLE, SANDBOX,
                     SAFE, REVIEW, BLOCKED, posture)
from .diff import DiffLine
from .guard import GuardResult
from .receipts import VerifyResult

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

_C = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[91m", "green": "\033[92m", "yellow": "\033[93m",
    "cyan": "\033[96m", "gray": "\033[90m",
}

_DECISION_COLOR = {
    ALLOW: "green", DRY_RUN: "yellow", REVERSIBLE: "yellow",
    CONTEXT_MISMATCH: "red", SANDBOX: "green", ESCALATE: "red",
    "RESTORED": "green", "VERIFIED": "green", "TAMPERED": "red", "DIFF": "cyan",
}
_TONE_COLOR = {"add": "green", "del": "red", "mod": "yellow", "meta": "cyan", "info": "dim"}

# Three-tier posture -> (glyph, colour). The glyph + colour carry the stance at
# a glance; the precise disposition rides along as a subtitle.
_POSTURE_META = {
    SAFE: ("[+] SAFE", "green"),
    REVIEW: ("[!] REVIEW", "yellow"),
    BLOCKED: ("[x] BLOCKED", "red"),
}


def posture_meta(disposition: str):
    return _POSTURE_META.get(posture(disposition), ("[?] REVIEW", "yellow"))


def set_color(enabled: bool) -> None:
    global _USE_COLOR
    _USE_COLOR = enabled


def c(text, name) -> str:
    if not _USE_COLOR:
        return str(text)
    return f"{_C.get(name, '')}{text}{_C['reset']}"


def kv(label, value) -> str:
    return f"  {c(str(label).ljust(20), 'dim')}{value}"


def _label(decision: str) -> str:
    return c(decision, _DECISION_COLOR.get(decision, "gray"))


def _print(lines: List[str]) -> None:
    print("\n".join(lines))


def render_result(r: GuardResult, version: str) -> None:
    d = r.decision
    glyph, pcolor = posture_meta(d.decision)
    head = (c(f"demo_cli {version}", "dim") + "  " + c(glyph, pcolor)
            + c(f"  ·  {d.decision}", "dim"))
    lines: List[str] = ["", head, ""]

    lines.append(c("Action", "cyan"))
    lines.append(kv("command", r.command[:110]))
    lines.append(kv("type", r.classification.action_type
                    + (" · pipeline" if r.classification.is_pipeline else "")
                    + (" · remote-exec" if r.classification.remote_exec else "")))
    if r.classification.matched_rule:
        lines.append(kv("matched rule", r.classification.matched_rule))
    if r.classification.nonrecoverable_surface:
        lines.append(kv("surface", r.classification.nonrecoverable_surface))
    if r.classification.is_pipeline:
        for seg in r.classification.segments:
            lines.append("    " + c("» " + seg[:96], "gray"))

    lines += ["", c("Context", "cyan")]
    ctx = r.context
    env_line = ctx.environment if ctx.environment_source == "unknown" \
        else f"{ctx.environment}  ({ctx.environment_source})"
    lines.append(kv("environment", env_line))
    lines.append(kv("target", ctx.target_label))
    branch = ctx.branch
    if branch in ("unknown", "HEAD", ""):
        branch = "- (no git branch)"
    lines.append(kv("git branch", branch))
    lines.append(kv("fingerprint", ctx.fingerprint))

    if r.mismatches:
        lines += ["", c("Context mismatch", "red")]
        for name, wanted, actual in r.mismatches:
            lines.append(kv(name, f"intended {wanted}, actual {actual}"))

    if r.preview_count is not None:
        lines += ["", c("Preview (dry run)", "yellow"),
                  kv("rows affected", r.preview_count)]
        if r.preview_cols:
            lines.append("    " + c(" | ".join(str(x) for x in r.preview_cols), "dim"))
        for row in r.preview_rows[:5]:
            lines.append("    " + " | ".join(str(x) for x in row))

    if r.recovery_entry:
        lines += ["", c("Recovery", "green"),
                  kv("snapshot", os.path.basename(r.recovery_entry["recovery_point"])),
                  kv("undo", "demo_cli undo"),
                  kv("diff", "demo_cli diff")]

    lines += ["", c("Decision", _DECISION_COLOR.get(d.decision, "gray")),
              "  " + c(d.reason, _DECISION_COLOR.get(d.decision, "gray"))]
    if d.next_steps:
        lines += ["", c("Safe next step", "yellow")]
        for i, step in enumerate(d.next_steps, 1):
            lines.append(f"  {i}. {step}")

    lines.append("")
    lines.append(c(f"  mode: {r.mode}"
                   + ("" if r.mode == "enforce" else "  (observe-only; nothing was blocked)"), "dim"))
    lines.append("")
    _print(lines)


def render_verify(v: VerifyResult, version: str) -> None:
    if v.ok:
        summary = ", ".join(f"{k}:{n}" for k, n in sorted(v.decisions.items())) or "none"
        _print(["", c(f"demo_cli {version}", "dim") + "  " + _label("VERIFIED"), "",
                c("Receipt chain", "green"),
                kv("entries", v.entries),
                kv("head", v.head[:16] + "..."),
                kv("decisions", summary),
                "  " + c("Chain intact. Every entry links to the one before it.", "green"), ""])
    else:
        lines = ["", c(f"demo_cli {version}", "dim") + "  " + _label("TAMPERED"), "",
                 c("Receipt chain", "red")]
        if v.broken_at:
            lines.append(kv("broken at", f"line {v.broken_at}"))
        lines.append("  " + c(v.detail or "Chain verification failed.", "red"))
        lines.append("")
        _print(lines)


def render_diff(entry: dict, lines: List[DiffLine], version: str) -> None:
    out = ["", c(f"demo_cli {version}", "dim") + "  " + _label("DIFF"), "",
           c("Diff", "cyan"),
           kv("kind", entry.get("kind", "sqlite")),
           kv("target", redact(entry.get("target", "unknown"))),
           kv("baseline", os.path.basename(entry.get("recovery_point", "unknown"))),
           "", c("What changed", "cyan")]
    for ln in lines:
        out.append("  " + c(ln.text, _TONE_COLOR.get(ln.tone, "info")))
    out.append("")
    _print(out)


def render_restore(entry, ok: bool, version: str) -> None:
    if ok and entry:
        _print(["", c(f"demo_cli {version}", "dim") + "  " + _label("RESTORED"), "",
                c("Recovery", "green"),
                kv("kind", entry.get("kind", "sqlite")),
                kv("target", redact(entry["target"])),
                kv("from", os.path.basename(entry["recovery_point"])),
                "  " + c("Restored from the latest recovery point.", "green"), ""])
    else:
        _print(["", c(f"demo_cli {version}", "dim") + "  " + _label("ESCALATE"), "",
                "  " + c("No recovery point could be restored." if entry
                         else "No recovery points found.", "red"), ""])


def _size(n: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == "GB":
            return f"{int(f)}{u}" if u == "B" else f"{f:.1f}{u}"
        f /= 1024
    return f"{int(n)}B"


def quiet_line(r: GuardResult) -> str:
    """One-line summary for CI / hook-style consumers."""
    glyph, pcolor = posture_meta(r.decision.decision)
    rp = ""
    if r.recovery_entry:
        rp = f"  recovery={r.recovery_entry.get('id', '')}"
    return c(glyph, pcolor) + c(f" {r.decision.decision}", "dim") + f"  {r.decision.reason}{rp}"


def result_json(r: GuardResult, version: str) -> dict:
    """Machine-readable view of a check, for pipelines and other environments."""
    return {
        "version": version,
        "posture": posture(r.decision.decision),
        "disposition": r.decision.decision,
        "reason": r.decision.reason,
        "recoverable": r.decision.recoverable,
        "mode": r.mode,
        "allowed": r.allowed,
        "permission": r.permission,
        "command": r.command,
        "action_type": r.classification.action_type,
        "matched_rule": r.classification.matched_rule,
        "nonrecoverable_surface": r.classification.nonrecoverable_surface,
        "environment": r.context.environment,
        "environment_source": r.context.environment_source,
        "target": r.context.target_label,
        "recovery_point": (r.recovery_entry or {}).get("recovery_point"),
        "recovery_id": (r.recovery_entry or {}).get("id"),
        "preview_affected_rows": r.preview_count,
        "context_mismatches": [list(m) for m in r.mismatches],
        "receipt_hash": r.receipt.receipt_hash if r.receipt else None,
        "next_steps": r.decision.next_steps,
    }


def render_log(entries, version: str) -> None:
    if not entries:
        print(c(f"\ndemo_cli {version}", "dim") + "  recovery log\n")
        print("  No recovery points yet. Run some mutating commands first.\n")
        return
    out = ["", c(f"demo_cli {version}", "dim") + "  recovery log", ""]
    out.append("  " + c(f"{'id':<10}{'when':<17}{'kind':<9}{'size':<8}action", "dim"))
    for e in reversed(entries):
        rid = str(e.get("id", "?"))[:8]
        ts = e.get("ts", "?")
        kind = e.get("kind", "?")
        size = _size(_entry_size_safe(e))
        action = redact(e.get("action") or e.get("target", ""))
        if len(action) > 46:
            action = action[:43] + "..."
        out.append(f"  {rid:<10}{ts:<17}{kind:<9}{size:<8}{action}")
    out += ["", c("  undo <id> to restore · diff <id> to inspect", "dim"), ""]
    _print(out)


def _entry_size_safe(e) -> int:
    from . import recovery
    try:
        return recovery.entry_size(e)
    except Exception:
        return 0


def render_doctor(checks, version: str) -> None:
    out = ["", c(f"demo_cli {version}", "dim") + "  doctor", ""]
    glyphs = {"ok": ("[+]", "green"), "warn": ("[!]", "yellow"), "fail": ("[x]", "red")}
    for status, label, detail in checks:
        g, col = glyphs.get(status, ("[?]", "gray"))
        line = "  " + c(g, col) + " " + label.ljust(26) + c(detail, "dim")
        out.append(line)
    fails = sum(1 for s, _, _ in checks if s == "fail")
    warns = sum(1 for s, _, _ in checks if s == "warn")
    out.append("")
    summary = "all good" if (fails == 0 and warns == 0) else f"{fails} fail, {warns} warn"
    out.append("  " + c(summary, "green" if fails == 0 else "red"))
    out.append("")
    _print(out)


def render_status(info, version: str) -> None:
    out = ["", c(f"demo_cli {version}", "dim") + "  status", ""]
    out.append(kv("mode", info["mode"] + ("" if info["mode"] == "enforce"
                  else "  (observe-only)")))
    out.append(kv("hook installed", "yes" if info["hook"] else "no  (run: demo_cli install-hook)"))
    out.append(kv("config", info["config"] or "defaults (no .demo_cli.toml)"))
    out.append(kv("receipts", info["receipts"]))
    out.append(kv("chain", info["chain"]))
    out.append(kv("recovery points", info["recovery_points"]))
    out.append(kv("workspace", info["workspace"]))
    out.append("")
    _print(out)
