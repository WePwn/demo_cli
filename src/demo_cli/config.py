"""Project configuration: `.demo_cli.toml`.

This is what makes the tool *declared, not guessed*, and what keeps state
project-local instead of wired into the install directory. A project opts its
real targets into protection by declaring them here:

    mode = "shadow"            # observe-only by default; "enforce" to gate

    [workspace]
    dir = ".demo_cli"          # receipts + recovery points live here, per project

    [approval]
    key_env = "DEMO_CLI_APPROVER_KEY"   # env var holding the structural-approval key

    [[target]]
    match = "production"       # substring matched against the resolved target ref
    env   = "production"
    recovery = "snapshot"      # snapshot | attest | none

Nothing here is required: with no config file the tool runs with safe defaults
(shadow mode, `.demo_cli/` under the project root, heuristic env detection).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

try:  # Python 3.11+
    import tomllib as _toml

    def _load_toml(path):
        with open(path, "rb") as f:
            return _toml.load(f)
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.9/3.10
    try:
        import tomli as _toml

        def _load_toml(path):
            with open(path, "rb") as f:
                return _toml.load(f)
    except ModuleNotFoundError:  # pragma: no cover
        _toml = None

        def _load_toml(path):
            raise RuntimeError(
                "A .demo_cli.toml was found but no TOML parser is available. "
                "Install demo_cli on Python 3.11+, or `pip install tomli`."
            )

CONFIG_NAME = ".demo_cli.toml"
VALID_MODES = ("shadow", "enforce")
VALID_RECOVERY = ("snapshot", "attest", "none")


@dataclass
class TargetRule:
    match: str
    env: str = "unknown"
    recovery: str = "snapshot"

    def matches(self, ref: Optional[str]) -> bool:
        return bool(ref) and self.match.lower() in str(ref).lower()


@dataclass
class Config:
    mode: str = "shadow"
    project_root: str = field(default_factory=os.getcwd)
    workspace_dir: str = ".demo_cli"
    approval_key_env: Optional[str] = None
    targets: List[TargetRule] = field(default_factory=list)
    source_path: Optional[str] = None  # path of the loaded config, if any

    # ---- resolved paths (always project-local, never install-local) ----
    @property
    def workspace(self) -> str:
        return os.path.join(self.project_root, self.workspace_dir)

    @property
    def receipts_path(self) -> str:
        return os.path.join(self.workspace, "receipts.jsonl")

    @property
    def recovery_dir(self) -> str:
        return os.path.join(self.workspace, "recovery")

    @property
    def approver_key(self) -> Optional[str]:
        if not self.approval_key_env:
            return None
        return os.environ.get(self.approval_key_env) or None

    def match_target(self, ref: Optional[str]) -> Optional[TargetRule]:
        for t in self.targets:
            if t.matches(ref):
                return t
        return None

    def declared_env(self, ref: Optional[str]) -> Optional[str]:
        t = self.match_target(ref)
        return t.env if t else None


def find_project_root(start: Optional[str] = None) -> str:
    """Walk up from `start` looking for a .demo_cli.toml or a .git directory.
    Falls back to CLAUDE_PROJECT_DIR, then the start directory."""
    start = os.path.abspath(start or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    cur = start
    while True:
        if os.path.exists(os.path.join(cur, CONFIG_NAME)) or os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return start
        cur = parent


def load_config(start: Optional[str] = None) -> Config:
    """Load configuration from the nearest .demo_cli.toml, or return defaults."""
    root = find_project_root(start)
    cfg = Config(project_root=root)
    path = os.path.join(root, CONFIG_NAME)
    if not os.path.exists(path):
        return cfg

    data = _load_toml(path)
    cfg.source_path = path

    mode = str(data.get("mode", cfg.mode)).strip().lower()
    if mode in VALID_MODES:
        cfg.mode = mode

    ws = data.get("workspace", {})
    if isinstance(ws, dict) and ws.get("dir"):
        cfg.workspace_dir = str(ws["dir"])

    appr = data.get("approval", {})
    if isinstance(appr, dict) and appr.get("key_env"):
        cfg.approval_key_env = str(appr["key_env"])

    for raw in data.get("target", []) or []:
        if not isinstance(raw, dict) or "match" not in raw:
            continue
        recovery = str(raw.get("recovery", "snapshot")).strip().lower()
        if recovery not in VALID_RECOVERY:
            recovery = "snapshot"
        cfg.targets.append(TargetRule(
            match=str(raw["match"]),
            env=str(raw.get("env", "unknown")).strip().lower(),
            recovery=recovery,
        ))
    return cfg
