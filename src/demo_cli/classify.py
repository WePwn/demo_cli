"""Command classification.

Pure, dependency-free. Given a (possibly chained / piped) command string,
decide what kind of action it is: destructive, mutating, a safe read, an
opaque remote fetch-and-run, or a mutation of a surface that a local snapshot
cannot cover (external side effects, credentials, migrations, ...).

Classification never decides what to *do* - that is `decide.py`. It only
describes the command. The regex rules here encode real failure modes raised
by users during the validation sprint (piped/chained commands hiding a
destructive step, formatters rewriting whole trees, curl | bash).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

# --------------------------------------------------------------------------
# Rule tables
# --------------------------------------------------------------------------

# (rule_id, action_type, pattern). First match wins, order matters.
_DESTRUCTIVE_RULES = [
    ("sql_drop", "sql", r"\bDROP\s+(?:DATABASE|TABLE|SCHEMA)\b"),
    ("sql_truncate", "sql", r"\bTRUNCATE\b"),
    ("sql_delete", "sql", r"\bDELETE\s+FROM\b"),
    ("tf_destroy", "infra", r"\bterraform\s+destroy\b"),
    ("kubectl_delete", "infra", r"\bkubectl\s+delete\s+(?:namespace|ns|pv|pvc|deploy(?:ment)?|sts)\b"),
    ("cloud_delete", "infra", r"\b(?:aws|gcloud|az)\b[\w\s.-]*\b(?:delete|terminate|destroy|rb)\b"),
    ("railway_drop", "infra", r"railway\s+run.*production.*(?:DROP|DELETE|TRUNCATE)"),
    ("railway_vol_del", "infra", r"railway\s+volume\s+delete"),
    ("git_force_push", "git", r"\bgit\s+push\b.*(?:--force|-f)\b"),
    ("git_reset_hard", "git", r"\bgit\s+reset\s+--hard\b"),
    # rm with both recursive and force, in either flag order (-rf or -fr),
    # bounded so it does not leak across a pipe / chain separator. Listed first
    # so this specific, higher-signal id wins for the -rf case.
    ("rm_rf", "shell", r"\brm\b(?=[^|;&]*\b-?[a-z]*r[a-z]*\b)(?=[^|;&]*\b-?[a-z]*f[a-z]*\b)[^|;&]*"),
    # Any *top-level* rm, not only -rf. A plain `rm app.db` deletes a file just
    # as irrecoverably from the shell's point of view, and "delete this file" is
    # the single most common destructive thing an agent does. Anchored to the
    # start of the segment (like the operand extractor) so subcommands such as
    # `git rm`, `docker rm`, `npm rm` do NOT match - those are not local-file
    # deletions and would only produce false escalations.
    ("rm_local", "shell", r"^\s*(?:sudo\s+)?rm\b[^|;&]*"),
    ("rmdir_s", "shell", r"\brmdir\b.*\/[sS]"),
    ("del_force", "shell", r"\bdel\b.*\/[fFsS]"),
    ("mv_overwrite", "shell", r"\bmv\s+(?:-[a-z]*f[a-z]*\s+)?\S+\s+\S+"),
    # A small, bounded set of other local data-destroyers a cooperative agent
    # can run by mistake. These are LOCAL (no external blast radius): they are
    # snapshotted when the target can be resolved, and escalated honestly when
    # it cannot. This is deliberately NOT an attempt to enumerate every
    # dangerous command - string-level coverage is explicitly out of scope.
    ("fs_shred", "shell", r"\bshred\b"),
    ("fs_truncate", "shell", r"\btruncate\b[^|;&]*\s-s\b"),
    ("fs_dd_of", "shell", r"\bdd\b[^|;&]*\bof=\S+"),
    ("fs_find_delete", "shell", r"\bfind\b[^|;&]*\s-delete\b"),
    ("git_clean", "git", r"\bgit\s+clean\b[^|;&]*-[a-z]*d"),
]
_DESTRUCTIVE = [(rid, a, re.compile(rx, re.I | re.S)) for rid, a, rx in _DESTRUCTIVE_RULES]

# Destructive rules whose blast radius is EXTERNAL / remote. A local snapshot
# can never truthfully cover them, so they are treated as non-recoverable
# surfaces and escalated regardless of the local environment label. (Trou 1:
# without this, a force-push or terraform destroy in a project whose env
# resolves to "development" was downgraded to SANDBOX and allowed unattended.)
# Local destructive rules (rm/mv/reset --hard/shred/...) are intentionally NOT
# here - those are recoverable by a local snapshot.
_EXTERNAL_IRREVERSIBLE = {
    "tf_destroy": "infra_destroy",
    "kubectl_delete": "cluster_resource_delete",
    "cloud_delete": "cloud_resource_delete",
    "railway_drop": "remote_database",
    "railway_vol_del": "remote_volume",
    "git_force_push": "remote_vcs_history",
}

# Opaque remote execution: code is fetched and run in one step. It cannot be
# previewed or snapshotted because the payload is not known before it runs.
_REMOTE_EXEC = re.compile(
    r"(?:curl|wget|fetch)\b[^|]*\|\s*(?:sudo\s+)?(?:bash|sh|zsh|python\d?|node|ruby|perl)\b"
    r"|base64\s+-d[^|]*\|\s*(?:bash|sh)\b"
    r"|\beval\b"
    r"|\|\s*(?:bash|sh)\s+-c\b"
    # Opaque dynamic execution via a one-liner interpreter call. We do NOT try
    # to defeat obfuscation (that arms race is out of scope); we only recognise
    # that a `python -c` carrying os.system/eval/exec/__import__/subprocess/pty
    # cannot be previewed, exactly like curl|bash, and therefore must escalate.
    r"|\bpython\d?\s+-c\b[^|]*(?:os\.system|os\.popen|subprocess|__import__|\beval\b|\bexec\b|pty\.spawn|commands\.get)",
    re.I,
)

# Tools that mutate files indirectly (formatters, generators, package managers).
# They rarely look destructive, but they rewrite the working tree - snapshot the
# path first so the change is visible and reversible.
_FILE_WRITERS = re.compile(
    r"\bprettier\b[^|;&]*--write"
    r"|\beslint\b[^|;&]*--fix"
    r"|\b(?:black|isort|gofmt|rustfmt)\b"
    r"|\b(?:npm|yarn|pnpm)\s+(?:install|add|remove|i)\b"
    r"|\bpip\s+install\b"
    r"|\b(?:npx|node)\b[^|;&]*(?:codegen|generate|migrate)\b",
    re.I,
)

# Surfaces a local snapshot cannot truthfully cover. A mutation here is NOT
# made "reversible" by copying a file - it must be escalated honestly (P2).
# (label, pattern)
_NONRECOVERABLE_SURFACES = [
    ("external_email", r"\b(sendgrid|mailgun|ses\s+send-email|smtp)\b|\bmail\s+-s\b"),
    ("external_payment", r"\b(stripe|paypal|braintree)\b[^|;&]*\b(charge|refund|payout|capture)\b"),
    ("external_message", r"\b(slack|discord|twilio)\b[^|;&]*\b(post|send|message|webhook)\b"
                         r"|hooks\.slack\.com|chat\.postMessage"),
    ("vcs_remote_state", r"\b(gh|hub)\s+(pr|issue|release)\s+(close|merge|delete|create)\b"),
    ("credential_rotation", r"\brotat(?:e|ing)\b[^|;&]*\b(key|secret|credential|token)\b"
                            r"|\b(?:aws\s+iam|gcloud\s+iam|az\s+role)\b"),
    ("secret_write", r"\b(vault|aws\s+secretsmanager|aws\s+ssm)\b[^|;&]*\b(put|write|delete|set)\b"),
    ("object_storage", r"\b(?:aws\s+s3|gsutil|az\s+storage\s+blob)\b[^|;&]*\b(rm|delete|rb)\b"),
    ("message_queue", r"\b(?:aws\s+sqs|rabbitmqadmin|kafka-topics)\b[^|;&]*\b(purge|delete)\b"),
    ("remote_filesystem", r"\bssh\b[^|;&]*\brm\b|\brsync\b[^|;&]*--delete\b"),
    ("schema_migration", r"\b(alembic|flyway|liquibase|prisma\s+migrate|knex\s+migrate|sequelize\s+db:migrate)\b"
                         r"|\brails\s+db:migrate\b|\bmanage\.py\s+migrate\b"),
]
_NONRECOVERABLE = [(label, re.compile(rx, re.I)) for label, rx in _NONRECOVERABLE_SURFACES]

_SQL_READ = re.compile(r"^\s*SELECT\b", re.I)
_SQL_MUTATING = re.compile(r"\b(UPDATE|INSERT|DELETE|DROP|TRUNCATE|ALTER|CREATE|REPLACE)\b", re.I)
_SQL_DELETE = re.compile(r"\bDELETE\s+FROM\b", re.I)
_SQL_UPDATE = re.compile(r"\bUPDATE\s+\w+\s+SET\b", re.I)
_SQL_TRUNCATE = re.compile(r"\bTRUNCATE\b", re.I)


# --------------------------------------------------------------------------
# Pipeline splitting
# --------------------------------------------------------------------------

def split_segments(cmd: str) -> List[str]:
    """Split a command line on shell separators (| || && ; newline) while
    respecting single and double quotes. Returns trimmed, non-empty segments.

    This is a pragmatic splitter, not a full shell parser; it exists so a
    destructive step hidden after a safe one in a chain is still classified.
    """
    segments: List[str] = []
    buf: List[str] = []
    quote = None
    i, n = 0, len(cmd)
    while i < n:
        ch = cmd[i]
        nxt = cmd[i + 1] if i + 1 < n else ""
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch in ("&", "|") and nxt == ch:  # && or ||
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in ("|", ";", "\n"):  # single pipe, semicolon, newline
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]


# --------------------------------------------------------------------------
# Classification result
# --------------------------------------------------------------------------

@dataclass
class Classification:
    """What a command is. Describes; does not decide."""
    is_destructive: bool = False
    is_mutating: bool = False
    is_sql_read: bool = False
    is_sql_mutating: bool = False
    is_file_writer: bool = False
    remote_exec: bool = False
    is_pipeline: bool = False
    matched_rule: Optional[str] = None
    action_type: str = "shell"
    nonrecoverable_surface: Optional[str] = None
    segments: List[str] = field(default_factory=list)

    @property
    def needs_recovery(self) -> bool:
        """True when proceeding should be gated on a recovery point."""
        return self.is_mutating or self.is_destructive


def _classify_segment(cmd: str) -> dict:
    matched, atype = None, "shell"
    for rid, action_type, rx in _DESTRUCTIVE:
        if rx.search(cmd):
            matched, atype = rid, action_type
            break

    is_sql_read = bool(_SQL_READ.search(cmd))
    is_sql_mutating = bool(_SQL_MUTATING.search(cmd))
    is_file_writer = bool(_FILE_WRITERS.search(cmd))
    is_destructive = matched is not None

    # A non-recoverable surface (external effect, migration, credential, ...)
    # is itself a mutation, even when no other rule fires.
    surface = None
    for label, rx in _NONRECOVERABLE:
        if rx.search(cmd):
            surface = label
            break
    # External/remote destructive rules cannot be covered by a local snapshot.
    if surface is None and matched in _EXTERNAL_IRREVERSIBLE:
        surface = _EXTERNAL_IRREVERSIBLE[matched]

    is_mutating = is_destructive or is_sql_mutating or is_file_writer or (surface is not None)

    if is_sql_read or is_sql_mutating:
        atype = "sql"
    elif is_file_writer:
        atype = "filewrite"

    return {
        "is_destructive": is_destructive,
        "is_mutating": is_mutating,
        "is_sql_read": is_sql_read,
        "is_sql_mutating": is_sql_mutating,
        "is_file_writer": is_file_writer,
        "matched_rule": matched,
        "action_type": atype,
        "nonrecoverable_surface": surface,
    }


def classify(cmd: str) -> Classification:
    """Classify a single command (no pipeline awareness)."""
    s = _classify_segment(cmd)
    return Classification(
        is_destructive=s["is_destructive"],
        is_mutating=s["is_mutating"],
        is_sql_read=s["is_sql_read"],
        is_sql_mutating=s["is_sql_mutating"],
        is_file_writer=s["is_file_writer"],
        remote_exec=bool(_REMOTE_EXEC.search(cmd)),
        is_pipeline=False,
        matched_rule=s["matched_rule"],
        action_type=s["action_type"],
        nonrecoverable_surface=s["nonrecoverable_surface"],
        segments=[cmd.strip()],
    )


def classify_pipeline(cmd: str) -> Classification:
    """Classify a possibly chained / piped command. Each segment is classified
    on its own and the pipeline inherits the strongest signal. Opaque remote
    execution is detected on the full string because the pipe *is* the payload.
    """
    segments = split_segments(cmd)
    seg_results = [_classify_segment(seg) for seg in segments]
    if not seg_results:
        seg_results = [_classify_segment(cmd)]

    def any_of(key):
        return any(s[key] for s in seg_results)

    matched_rule = next((s["matched_rule"] for s in seg_results if s["matched_rule"]), None)
    surface = next((s["nonrecoverable_surface"] for s in seg_results if s["nonrecoverable_surface"]), None)
    action_type = next((s["action_type"] for s in seg_results if s["is_destructive"]),
                       seg_results[0]["action_type"])

    return Classification(
        is_destructive=any_of("is_destructive"),
        is_mutating=any_of("is_mutating"),
        is_sql_read=any_of("is_sql_read"),
        is_sql_mutating=any_of("is_sql_mutating"),
        is_file_writer=any_of("is_file_writer"),
        remote_exec=bool(_REMOTE_EXEC.search(cmd)),
        is_pipeline=len(seg_results) > 1,
        matched_rule=matched_rule,
        action_type=action_type,
        nonrecoverable_surface=surface,
        segments=[s.strip() for s in segments] or [cmd.strip()],
    )


def is_sql_preview_candidate(cmd: str) -> bool:
    """True if the command is a DELETE / UPDATE / TRUNCATE we can preview."""
    return bool(_SQL_DELETE.search(cmd) or _SQL_UPDATE.search(cmd) or _SQL_TRUNCATE.search(cmd))
