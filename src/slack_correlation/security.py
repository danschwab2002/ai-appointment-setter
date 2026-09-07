"""Authenticate Slack interactions over their exact raw request bytes."""

from __future__ import annotations

import hashlib
import hmac


class InvalidSlackSignature(ValueError):
    """Raised when a Slack request is unauthenticated or outside its time window."""


def verify_slack_signature(
    *,
    signing_secret: str,
    raw_body: bytes,
    timestamp: str,
    signature: str,
    now_epoch: int,
    tolerance_seconds: int = 300,
) -> None:
    """Verify Slack's v0 HMAC and timestamp without normalizing the body."""
    if not isinstance(signing_secret, str) or not signing_secret:
        raise InvalidSlackSignature("signing_secret_unavailable")
    if not isinstance(raw_body, bytes):
        raise InvalidSlackSignature("invalid_raw_body")
    if not isinstance(timestamp, str) or not timestamp.isascii() or not timestamp.isdigit():
        raise InvalidSlackSignature("invalid_timestamp")
    if (
        not isinstance(now_epoch, int)
        or isinstance(now_epoch, bool)
        or not isinstance(tolerance_seconds, int)
        or isinstance(tolerance_seconds, bool)
        or tolerance_seconds < 1
    ):
        raise InvalidSlackSignature("invalid_verification_clock")
    request_epoch = int(timestamp)
    if abs(now_epoch - request_epoch) > tolerance_seconds:
        raise InvalidSlackSignature("stale_request")
    if (
        not isinstance(signature, str)
        or len(signature) != 67
        or not signature.startswith("v0=")
        or any(character not in "0123456789abcdef" for character in signature[3:])
    ):
        raise InvalidSlackSignature("invalid_signature")
    signing_base = b"v0:" + timestamp.encode("ascii") + b":" + raw_body
    expected = "v0=" + hmac.new(
        signing_secret.encode("utf-8"),
        signing_base,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise InvalidSlackSignature("signature_mismatch")
