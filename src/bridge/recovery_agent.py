"""Hermes agent client for cart-abandonment recovery reasoning.

Separate from HermesShadowProcessor (which handles Chatwoot conversation
qualification) — this client sends a SituationReport to the agent and expects
a recovery-proposal JSON back, using the cart-abandonment-recovery skill
contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import fcntl
from dataclasses import dataclass
from pathlib import Path

import httpx

from bridge.supabase import FollowupExecutionContext


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
_EXPECTED_PROPOSAL_KEYS = {
    "action",
    "reason_code",
    "message",
    "lead_stage",
    "current_goal",
}
_FOLLOWUP_MESSAGE_PROPOSAL_KEYS = {"strategy", "message"}
_FOLLOWUP_HANDOFF_PROPOSAL_KEYS = {"proposal", "reason_code"}
_VALID_HANDOFF_REASON_CODES = {
    "explicit_human_request",
    "commercial_exception",
    "policy_requires_human",
}


@dataclass(frozen=True)
class FollowupMessageProposal:
    """Untrusted draft accepted by the bridge's bounded output contract."""

    strategy: str
    message: str


@dataclass(frozen=True)
class FollowupHandoffSuggestion:
    """Untrusted bounded suggestion; the bridge remains execution authority."""

    reason_code: str


_RECOVERY_DECISION_POLICY: dict[str, object] = {
    "authoritative_fields": [
        "authoritative_context_complete",
        "contact_blocked",
        "phone_available",
        "any_conversation_human_takeover",
        "has_active_conversation",
        "has_open_recovery_case",
    ],
    "rules": [
        {
            "when": {"authoritative_context_complete": False},
            "action": "handoff",
            "reason_code": "insufficient_context",
        },
        {
            "when": {"contact_blocked": True},
            "action": "abort",
            "reason_code": "contact_blocked",
        },
        {
            "when": {"phone_available": False},
            "action": "abort",
            "reason_code": "no_phone_available",
        },
        {
            "when": {"any_conversation_human_takeover": True},
            "action": "abort",
            "reason_code": "human_takeover_active",
        },
        {
            "when": {"has_active_conversation": True},
            "action": "hold",
            "reason_code": "active_conversation_exists",
        },
        {
            "when": {"has_open_recovery_case": True},
            "action": "hold",
            "reason_code": "recovery_case_active",
        },
    ],
    "default": {
        "action": "send_first_touch",
        "reason_code": "first_touch",
    },
    "on_missing_authoritative_field": {
        "action": "handoff",
        "reason_code": "insufficient_context",
    },
    "constraints": [
        "Apply the first matching rule; otherwise apply default.",
        "Do not recompute or contradict authoritative_fields from nested data.",
        "The current recovery case is excluded by has_open_recovery_case.",
        "Cancelled, closed, or lost cases do not block the default action.",
        "Use only the action and reason_code pairs defined in this policy.",
    ],
}


def required_recovery_decision(
    situation_report: dict[str, object],
) -> dict[str, str]:
    """Calculate the required action from bridge-authoritative guard fields."""
    authoritative_fields = _RECOVERY_DECISION_POLICY["authoritative_fields"]
    assert isinstance(authoritative_fields, list)
    if any(
        not isinstance(situation_report.get(field), bool)
        for field in authoritative_fields
    ):
        return {"action": "handoff", "reason_code": "insufficient_context"}

    if situation_report["authoritative_context_complete"] is False:
        return {"action": "handoff", "reason_code": "insufficient_context"}
    if situation_report["contact_blocked"] is True:
        return {"action": "abort", "reason_code": "contact_blocked"}
    if situation_report["phone_available"] is False:
        return {"action": "abort", "reason_code": "no_phone_available"}
    if situation_report["any_conversation_human_takeover"] is True:
        return {"action": "abort", "reason_code": "human_takeover_active"}
    if situation_report["has_active_conversation"] is True:
        return {
            "action": "hold",
            "reason_code": "active_conversation_exists",
        }
    if situation_report["has_open_recovery_case"] is True:
        return {"action": "hold", "reason_code": "recovery_case_active"}
    return {"action": "send_first_touch", "reason_code": "first_touch"}


def _strip_code_fence(text: str) -> str | None:
    """Return the body inside a leading markdown code fence, or None.

    Pure linear string scanning (no regex) so a malformed/unclosed fence
    followed by a long run of whitespace cannot trigger catastrophic
    backtracking (ReDoS) on attacker-influenced agent output.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return None
    newline = stripped.find("\n")
    if newline == -1:
        return None
    body = stripped[newline + 1 :]
    closing = body.rfind("```")
    if closing != -1:
        body = body[:closing]
    return body.strip()


def _parse_agent_json_object(text: str) -> dict[str, object] | None:
    """Parse a JSON object from an agent reply, tolerating markdown fences.

    Chat LLMs frequently wrap JSON in ```json ... ``` fences or emit a small
    amount of surrounding prose. Accept the direct JSON first; otherwise pull
    the object out of a fenced block or the outermost brace pair. Returns the
    parsed dict, or ``None`` when nothing decodes to a JSON object.
    """
    candidates: list[str] = [text]
    fenced = _strip_code_fence(text)
    if fenced is not None:
        candidates.append(fenced)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def is_valid_followup_message_proposal(proposal: dict[str, object]) -> bool:
    """Validate a bounded durable follow-up drafting proposal."""
    if set(proposal) == _FOLLOWUP_HANDOFF_PROPOSAL_KEYS:
        return (
            proposal.get("proposal") == "suggest_handoff"
            and proposal.get("reason_code") in _VALID_HANDOFF_REASON_CODES
        )
    if set(proposal) != _FOLLOWUP_MESSAGE_PROPOSAL_KEYS:
        return False
    strategy = proposal["strategy"]
    message = proposal["message"]
    return (
        isinstance(strategy, str)
        and bool(strategy.strip())
        and len(strategy) <= 120
        and isinstance(message, str)
        and bool(message.strip())
        and len(message) <= 500
    )


def is_valid_recovery_proposal(proposal: dict[str, object]) -> bool:
    """Return whether a recovery proposal matches the skill contract."""
    if set(proposal) != _EXPECTED_PROPOSAL_KEYS:
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


def _safe_proposal_diagnostics(proposal: object) -> dict[str, object]:
    """Describe contract failures without persisting generated text or PII."""
    diagnostics: dict[str, object] = {
        "proposal_type": type(proposal).__name__,
    }
    if not isinstance(proposal, dict):
        diagnostics["validation_errors"] = ["not_an_object"]
        return diagnostics

    proposal_keys = set(proposal)
    missing_keys = sorted(_EXPECTED_PROPOSAL_KEYS - proposal_keys)
    extra_key_count = len(proposal_keys - _EXPECTED_PROPOSAL_KEYS)
    diagnostics.update(
        {
            "expected_keys_present": sorted(
                _EXPECTED_PROPOSAL_KEYS & proposal_keys
            ),
            "missing_keys": missing_keys,
            "extra_key_count": extra_key_count,
        }
    )

    validation_errors: list[str] = []
    if missing_keys:
        validation_errors.append("missing_keys")
    if extra_key_count:
        validation_errors.append("unexpected_keys")

    action = proposal.get("action")
    action_valid = isinstance(action, str) and action in _VALID_ACTIONS
    if action_valid:
        diagnostics["action"] = action
    else:
        validation_errors.append("invalid_action")

    reason_code = proposal.get("reason_code")
    reason_valid = (
        isinstance(reason_code, str) and reason_code in _VALID_REASON_CODES
    )
    if reason_valid:
        diagnostics["reason_code"] = reason_code
    else:
        validation_errors.append("invalid_reason_code")

    lead_stage = proposal.get("lead_stage")
    lead_stage_valid = (
        isinstance(lead_stage, str) and lead_stage in _VALID_LEAD_STAGES
    )
    if lead_stage_valid:
        diagnostics["lead_stage"] = lead_stage
    else:
        validation_errors.append("invalid_lead_stage")

    message = proposal.get("message")
    diagnostics["message_type"] = type(message).__name__
    if isinstance(message, str):
        diagnostics["message_length"] = len(message)
    message_valid = (
        isinstance(message, str)
        and bool(message.strip())
        and len(message) <= 500
        if action == "send_first_touch"
        else message is None
    )
    if not message_valid:
        validation_errors.append("invalid_message")

    current_goal = proposal.get("current_goal")
    diagnostics["current_goal_type"] = type(current_goal).__name__
    if isinstance(current_goal, str):
        diagnostics["current_goal_length"] = len(current_goal)
    if not (
        isinstance(current_goal, str)
        and bool(current_goal.strip())
        and len(current_goal) <= 200
    ):
        validation_errors.append("invalid_current_goal")

    diagnostics["validation_errors"] = validation_errors
    return diagnostics


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

    async def request_followup_message(
        self,
        *,
        attempt_id: str,
        execution_context: FollowupExecutionContext,
    ) -> FollowupMessageProposal | FollowupHandoffSuggestion | None:
        """Serialize one durable Hermes evaluation per delivery attempt."""
        digest = hashlib.sha256(f"followup:{attempt_id}".encode("utf-8")).hexdigest()
        self._proposals_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._proposals_dir.chmod(0o700)
        evaluation_lock_fd = os.open(
            self._proposals_dir / f"{digest}.evaluation.lock",
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        try:
            lock_task = asyncio.create_task(
                asyncio.to_thread(fcntl.flock, evaluation_lock_fd, fcntl.LOCK_EX)
            )
            cancellation_requested = False
            while not lock_task.done():
                try:
                    await asyncio.shield(lock_task)
                except asyncio.CancelledError:
                    cancellation_requested = True
            lock_task.result()
            if cancellation_requested:
                raise asyncio.CancelledError
            return await self._request_followup_message_locked(
                attempt_id=attempt_id,
                execution_context=execution_context,
            )
        finally:
            fcntl.flock(evaluation_lock_fd, fcntl.LOCK_UN)
            os.close(evaluation_lock_fd)

    async def _request_followup_message_locked(
        self,
        *,
        attempt_id: str,
        execution_context: FollowupExecutionContext,
    ) -> FollowupMessageProposal | FollowupHandoffSuggestion | None:
        """Request only strategy and copy while holding the attempt lock."""
        digest = hashlib.sha256(f"followup:{attempt_id}".encode("utf-8")).hexdigest()
        cached = self._load_completed_followup_proposal(
            digest=digest,
            attempt_id=attempt_id,
        )
        if cached is not None:
            return cached
        context = {
            "skill": "cart-abandonment-recovery",
            "execution_context": {
                "action_type": execution_context.action_type,
                "step_key": execution_context.step_key,
                "buyer_name": execution_context.buyer_name,
                "product_name": execution_context.product_name,
                "offer_code": execution_context.offer_code,
                "current_goal": execution_context.current_goal,
                "lead_stage": execution_context.lead_stage,
            },
            "required_output": {
                "one_of": [
                    {
                        "strategy": "non-empty string, max 120 characters",
                        "message": "non-empty string, max 500 characters",
                    },
                    {
                        "proposal": "suggest_handoff",
                        "reason_code": sorted(_VALID_HANDOFF_REASON_CODES),
                    },
                ],
            },
            "instructions": (
                "La autorización para contactar ya fue evaluada fuera del modelo. "
                "No decidas si enviar ni propongas destinatario, canal o acción. "
                "Usá la skill para devolver un borrador o suggest_handoff. "
                "La sugerencia no ejecuta nada: el bridge valida la política. "
                "Respondé únicamente con uno de los objetos JSON de one_of."
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
                        "messages": [{
                            "role": "user",
                            "content": json.dumps(context, ensure_ascii=False),
                        }],
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError:
            self._persist(
                digest=digest,
                result={
                    "status": "failed",
                    "attempt_id": attempt_id,
                    "reason": "agent_unavailable",
                },
                replace_nonterminal=True,
            )
            return None

        try:
            body = response.json()
            proposal_text = body["choices"][0]["message"]["content"]
            proposal = _parse_agent_json_object(proposal_text)
        except (
            KeyError,
            IndexError,
            TypeError,
            UnicodeError,
            json.JSONDecodeError,
        ):
            proposal = None

        if not isinstance(proposal, dict) or not is_valid_followup_message_proposal(
            proposal
        ):
            self._persist(
                digest=digest,
                result={
                    "status": "failed",
                    "attempt_id": attempt_id,
                    "reason": "invalid_followup_message_proposal",
                },
                replace_nonterminal=True,
            )
            return None

        accepted: FollowupMessageProposal | FollowupHandoffSuggestion
        if proposal.get("proposal") == "suggest_handoff":
            accepted = FollowupHandoffSuggestion(
                reason_code=str(proposal["reason_code"])
            )
            persisted_proposal: dict[str, object] = {
                "proposal": "suggest_handoff",
                "reason_code": accepted.reason_code,
            }
        else:
            accepted = FollowupMessageProposal(
                strategy=str(proposal["strategy"]),
                message=str(proposal["message"]),
            )
            persisted_proposal = {
                "strategy": accepted.strategy,
                "message": accepted.message,
            }
        self._persist(
            digest=digest,
            result={
                "status": "completed",
                "attempt_id": attempt_id,
                "proposal": persisted_proposal,
            },
            replace_nonterminal=True,
        )
        return accepted

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
        required_decision = required_recovery_decision(situation_report)

        context = {
            "skill": "cart-abandonment-recovery",
            "situation_report": situation_report,
            "decision_policy": _RECOVERY_DECISION_POLICY,
            "required_decision": required_decision,
            "instructions": (
                "Aplicá decision_policy de forma estricta sobre el "
                "situation_report. No recalcules los authoritative_fields. "
                "Copiá action y reason_code exactamente desde "
                "required_decision. "
                "Usá la skill cart-abandonment-recovery únicamente para las "
                "reglas de redacción del message. Respondé únicamente con el "
                "JSON del contrato de salida, sin campos adicionales."
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
            proposal = _parse_agent_json_object(proposal_text)
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
                    "diagnostics": _safe_proposal_diagnostics(proposal),
                },
            )
            return None

        if (
            proposal["action"] != required_decision["action"]
            or proposal["reason_code"] != required_decision["reason_code"]
        ):
            self._persist(
                digest=digest,
                result={
                    "status": "failed",
                    "event_id": event_id,
                    "reason": "proposal_decision_mismatch",
                    "required_decision": required_decision,
                    "diagnostics": _safe_proposal_diagnostics(proposal),
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

    def _load_completed_followup_proposal(
        self,
        *,
        digest: str,
        attempt_id: str,
    ) -> FollowupMessageProposal | FollowupHandoffSuggestion | None:
        """Reuse only a complete artifact for the same durable attempt."""
        result_path = self._proposals_dir / f"{digest}.json"
        try:
            persisted = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(persisted, dict):
            return None
        if (
            persisted.get("status") != "completed"
            or persisted.get("attempt_id") != attempt_id
        ):
            return None
        proposal = persisted.get("proposal")
        if not isinstance(proposal, dict) or not is_valid_followup_message_proposal(
            proposal
        ):
            return None
        if proposal.get("proposal") == "suggest_handoff":
            return FollowupHandoffSuggestion(
                reason_code=str(proposal["reason_code"])
            )
        return FollowupMessageProposal(
            strategy=str(proposal["strategy"]),
            message=str(proposal["message"]),
        )

    def _persist(
        self,
        *,
        digest: str,
        result: dict[str, object],
        replace_nonterminal: bool = False,
    ) -> None:
        """Persist a result atomically; completed artifacts are immutable."""
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
                if not replace_nonterminal:
                    return
                try:
                    existing = json.loads(result_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    existing = None
                if isinstance(existing, dict) and existing.get("status") == "completed":
                    existing_proposal = existing.get("proposal")
                    if (
                        existing.get("attempt_id") == result.get("attempt_id")
                        and isinstance(existing_proposal, dict)
                        and is_valid_followup_message_proposal(existing_proposal)
                    ):
                        return
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
                directory_fd = os.open(
                    self._proposals_dir,
                    os.O_RDONLY | os.O_DIRECTORY,
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
