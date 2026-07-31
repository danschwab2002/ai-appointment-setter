"""Hermes agent client for cart-abandonment recovery reasoning.

Separate from HermesShadowProcessor (which handles Chatwoot conversation
qualification) — this client sends a SituationReport to the agent and expects
a recovery-proposal JSON back, using the cart-abandonment-recovery skill
contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import fcntl
from pathlib import Path

import httpx


# ── Recovery proposal validation ────────────────────────────────────

_VALID_ACTIONS = {"send_first_touch", "hold", "handoff", "abort"}

_VALID_REASON_CODES = {
    "contact_blocked",
    "no_phone_available",
    "active_conversation_exists",
    "stale_conversation_review",
    "human_takeover_active",
    "recovery_case_active",
    "recovery_case_reopened",
    "first_touch",
    "insufficient_context",
}

_VALID_LEAD_STAGES = {"new", "discovery", "qualifying"}


def is_valid_recovery_proposal(proposal: dict[str, object]) -> bool:
    """Return whether a recovery proposal matches the skill contract."""
    if set(proposal) != {
        "action",
        "reason_code",
        "message",
        "lead_stage",
        "current_goal",
    }:
        return False

    action = proposal["action"]
    if not isinstance(action, str) or action not in _VALID_ACTIONS:
        return False

    reason_code = proposal["reason_code"]
    if not isinstance(reason_code, str) or reason_code not in _VALID_REASON_CODES:
        return False

    message = proposal["message"]
    if action == "send_first_touch":
        if not isinstance(message, str) or not message.strip() or len(message) > 500:
            return False
    else:
        if message is not None:
            return False

    lead_stage = proposal["lead_stage"]
    if not isinstance(lead_stage, str) or lead_stage not in _VALID_LEAD_STAGES:
        return False

    current_goal = proposal["current_goal"]
    if not isinstance(current_goal, str) or not current_goal.strip():
        return False
    if len(current_goal) > 200:
        return False

    return True


# ── Client ──────────────────────────────────────────────────────────


class RecoveryAgentClient:
    """Send a SituationReport to the agent and return its recovery proposal.

    The proposal is persisted to disk for audit, same pattern as
    HermesShadowProcessor — but with a separate contract and output dir.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_name: str,
        proposals_dir: Path,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model_name = model_name
        self._proposals_dir = proposals_dir
        self._transport = transport
        self._timeout_seconds = timeout_seconds

    async def request_proposal(
        self,
        *,
        event_id: str,
        situation_report: dict[str, object],
    ) -> dict[str, object] | None:
        """Send the situation_report to the agent and return its proposal.

        Returns ``None`` if the agent is unavailable or returns an invalid
        proposal.  The result is persisted to disk regardless of validity.
        """
        digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()

        context = {
            "skill": "cart-abandonment-recovery",
            "situation_report": situation_report,
            "instructions": (
                "Usá la skill cart-abandonment-recovery para analizar el "
                "situation_report y decidir qué acción tomar. Respondé "
                "únicamente con el JSON del contrato de salida de esa skill."
            ),
        }

        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout_seconds,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Idempotency-Key": digest,
                    },
                    json={
                        "model": self._model_name,
                        "stream": False,
                        "messages": [
                            {
                                "role": "user",
                                "content": json.dumps(
                                    context, ensure_ascii=False
                                ),
                            }
                        ],
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError:
            self._persist(
                digest=digest,
                result={
                    "status": "failed",
                    "event_id": event_id,
                    "reason": "agent_unavailable",
                },
            )
            return None

        try:
            body = response.json()
            proposal_text = body["choices"][0]["message"]["content"]
            proposal = json.loads(proposal_text)
        except (
            KeyError,
            IndexError,
            TypeError,
            UnicodeError,
            json.JSONDecodeError,
        ):
            proposal = None

        if not isinstance(proposal, dict) or not is_valid_recovery_proposal(
            proposal
        ):
            self._persist(
                digest=digest,
                result={
                    "status": "failed",
                    "event_id": event_id,
                    "reason": "invalid_agent_output",
                },
            )
            return None

        self._persist(
            digest=digest,
            result={
                "status": "completed",
                "event_id": event_id,
                "proposal": proposal,
            },
        )
        return proposal

    def _persist(self, *, digest: str, result: dict[str, object]) -> None:
        """Persist a result to disk with file locking, same as HermesShadow."""
        self._proposals_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._proposals_dir.chmod(0o700)
        result_path = self._proposals_dir / f"{digest}.json"
        lock_path = self._proposals_dir / f".{digest}.lock"
        serialized = (
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )

        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(lock_fd, 0o600)
        with os.fdopen(lock_fd) as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            if result_path.exists():
                return  # Already persisted
            temporary_fd, temporary_name = tempfile.mkstemp(
                prefix=f".{digest}.", suffix=".tmp", dir=self._proposals_dir
            )
            try:
                os.fchmod(temporary_fd, 0o600)
                with os.fdopen(temporary_fd, "w", encoding="utf-8") as handle:
                    handle.write(serialized)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, result_path)
            finally:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
