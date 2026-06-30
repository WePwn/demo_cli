"""Structural authorisation (2-of-2).

For the catastrophic, non-recoverable action, recoverability is not available
as a safety net - so it must carry an approval the agent cannot forge. The
approver key lives out-of-band (a separate party / secret store, referenced by
`approval.key_env` in config); the agent never holds it, so it cannot produce a
valid token (Kerckhoffs-safe: the scheme is public, only the key is secret).
"""
from __future__ import annotations

import hashlib
import hmac


def sign(action: str, approver_key: str) -> str:
    """Produce the approval token for an exact action string."""
    return hmac.new(approver_key.encode(), action.encode(), hashlib.sha256).hexdigest()


def verify(action: str, token: str, approver_key: str) -> bool:
    """Constant-time check that `token` approves exactly this `action`."""
    if not token or not approver_key:
        return False
    return hmac.compare_digest(token, sign(action, approver_key))
