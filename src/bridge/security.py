"""Security helpers for Chatwoot webhooks."""

import hashlib
import hmac


def verify_chatwoot_signature(
    *, raw_body: bytes, timestamp: str, received_signature: str, secret: str
) -> bool:
    """Return whether a Chatwoot HMAC signature matches the raw request."""
    signed_payload = timestamp.encode("ascii") + b"." + raw_body
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), signed_payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, received_signature)
