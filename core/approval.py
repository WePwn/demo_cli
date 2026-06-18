"""V0 structural authorization (2-of-2). The catastrophic, non-recoverable action
needs an approval token. The APPROVER KEY lives out-of-band with a separate party;
the agent does not hold it, so the agent cannot forge approval (Kerckhoffs-safe)."""
import hmac, hashlib

def sign(action: str, approver_key: str) -> str:
    return hmac.new(approver_key.encode(), action.encode(), hashlib.sha256).hexdigest()

def verify(action: str, token: str, approver_key: str) -> bool:
    if not token or not approver_key:
        return False
    return hmac.compare_digest(token, sign(action, approver_key))
