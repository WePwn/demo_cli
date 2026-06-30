"""Execution context: fingerprint, environment resolution, and intent matching.

The principle here is *declared, not guessed*. Environment is resolved in this
order:

    1. an explicit `--actual-env` / hook-provided value          (declared)
    2. a target matched in .demo_cli.toml                        (declared)
    3. a heuristic over the connection string / command text     (guessed)

The heuristic is a last resort, not the source of truth, because a real
production database is reached through a connection string or secret, not a
file literally named "production.db".
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

_PROD = re.compile(r"\b(prod|production|prd)\b", re.I)
_STAGE = re.compile(r"\b(staging|stage|stg)\b", re.I)
_DEV = re.compile(r"\b(dev|development|local|test|sandbox)\b", re.I)

ENV_ALIASES = {
    "prod": "production", "prd": "production", "production": "production",
    "stage": "staging", "stg": "staging", "staging": "staging",
    "dev": "development", "development": "development", "local": "development",
    "test": "test", "sandbox": "sandbox",
}

_REDACT = re.compile(r"(://[^:/@\s]+:)[^@/\s]+(@)")


def redact(text) -> str:
    """Mask the password inside a connection string before printing or logging."""
    if not text:
        return text
    return _REDACT.sub(r"\1***\2", str(text))


def normalize_env(value) -> str:
    if not value:
        return "unknown"
    v = str(value).strip().lower()
    return ENV_ALIASES.get(v, v)


def detect_env_from_text(text) -> str:
    if not text:
        return "unknown"
    if _PROD.search(text):
        return "production"
    if _STAGE.search(text):
        return "staging"
    if _DEV.search(text):
        return "development"
    return "unknown"


def resolve_env(cmd: str, target_label: Optional[str] = None,
                declared_env: Optional[str] = None,
                config_env: Optional[str] = None) -> Tuple[str, str]:
    """Return (environment, source). source is one of:
    'declared', 'config', 'heuristic', 'unknown'."""
    declared = normalize_env(declared_env)
    if declared != "unknown":
        return declared, "declared"
    cfg = normalize_env(config_env)
    if cfg != "unknown":
        return cfg, "config"
    guess = detect_env_from_text(os.path.basename(target_label or "") or (target_label or ""))
    if guess == "unknown":
        guess = detect_env_from_text(cmd)
    if guess != "unknown":
        return guess, "heuristic"
    return "unknown", "unknown"


def _git_value(*args, cwd=None) -> str:
    try:
        p = subprocess.run(
            ["git", *args], cwd=cwd or os.getcwd(),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=2,
        )
        return p.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


@dataclass
class Context:
    cwd: str
    repo_root: str
    branch: str
    remote: str
    target_label: str
    environment: str
    environment_source: str
    aws_profile: str
    gcloud_project: str
    azure_subscription: str
    fingerprint: str = ""

    def as_dict(self) -> Dict:
        d = {
            "cwd": self.cwd,
            "repo_root": self.repo_root,
            "branch": self.branch,
            "remote": self.remote,
            "target_label": self.target_label,
            "environment": self.environment,
            "environment_source": self.environment_source,
            "aws_profile": self.aws_profile,
            "gcloud_project": self.gcloud_project,
            "azure_subscription": self.azure_subscription,
        }
        d["fingerprint"] = self.fingerprint
        return d


def build_context(cmd: str, target_label: Optional[str] = None,
                  declared_env: Optional[str] = None, config_env: Optional[str] = None,
                  cwd: Optional[str] = None) -> Context:
    cwd = cwd or os.getcwd()
    env, source = resolve_env(cmd, target_label, declared_env, config_env)
    ctx = Context(
        cwd=cwd,
        repo_root=_git_value("rev-parse", "--show-toplevel", cwd=cwd),
        branch=_git_value("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd),
        remote=_git_value("config", "--get", "remote.origin.url", cwd=cwd),
        target_label=redact(target_label) if target_label else "unknown",
        environment=env,
        environment_source=source,
        aws_profile=os.environ.get("AWS_PROFILE", "unknown"),
        gcloud_project=os.environ.get("CLOUDSDK_CORE_PROJECT", "unknown"),
        azure_subscription=os.environ.get("AZURE_SUBSCRIPTION_ID", "unknown"),
    )
    canon = json.dumps({k: v for k, v in ctx.as_dict().items() if k != "fingerprint"},
                       sort_keys=True, separators=(",", ":"))
    ctx.fingerprint = hashlib.sha256(canon.encode()).hexdigest()[:16]
    return ctx


@dataclass
class Intent:
    env: str = "unknown"
    branch: Optional[str] = None
    cwd: Optional[str] = None
    remote: Optional[str] = None
    scope: Optional[str] = None
    reasoning: Optional[str] = None  # the agent's stated "why", for the why-ledger

    def as_dict(self) -> Dict:
        return {
            "env": self.env, "branch": self.branch, "cwd": self.cwd,
            "remote": self.remote, "scope": self.scope, "reasoning": self.reasoning,
        }


def compare_intent(intent: Intent, ctx: Context) -> List[Tuple[str, str, str]]:
    """Return [(field, intended, actual)] for every context invariant that the
    declared intent and the live context disagree on."""
    out: List[Tuple[str, str, str]] = []

    wanted_env = normalize_env(intent.env)
    actual_env = normalize_env(ctx.environment)
    if wanted_env != "unknown" and actual_env != "unknown" and wanted_env != actual_env:
        out.append(("environment", wanted_env, actual_env))

    if intent.branch and ctx.branch and ctx.branch != "unknown" and intent.branch != ctx.branch:
        out.append(("branch", intent.branch, ctx.branch))

    if intent.remote and ctx.remote and ctx.remote != "unknown" and intent.remote not in ctx.remote:
        out.append(("remote", intent.remote, ctx.remote))

    if intent.cwd and ctx.cwd and not ctx.cwd.endswith(intent.cwd):
        out.append(("cwd", intent.cwd, ctx.cwd))

    return out
