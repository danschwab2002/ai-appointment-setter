"""Bounded AI pre-resolution for unresolved commercial identity cases.

The model may recommend one existing candidate or abstain. It cannot create an
identity, resolve a case, authorize contact, or execute a commercial effect.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import re
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx


logger = logging.getLogger(__name__)

_PROMPT_VERSION = "correlation-preresolution-v2"
_PROPOSAL_KEYS = frozenset(
    {
        "decision",
        "recommended_candidate_id",
        "confidence",
        "supporting_evidence_ids",
        "contradicting_evidence_ids",
        "missing_information",
    }
)
_SUPPORTED_EVENT_TYPES = frozenset(
    {
        "PURCHASE_APPROVED",
        "PURCHASE_OUT_OF_SHOPPING_CART",
        "PURCHASE_CANCELED",
    }
)
_SUPPORTED_OUTCOMES = frozenset({"ambiguous", "conflict"})
_SUPPORTED_FACT_KINDS = frozenset(
    {
        "precheckout_time_proximity_minutes",
        "nearest_precheckout_by_at_least_5m",
        "same_product_offer",
        "chatwoot_phone_exact_match",
        "chatwoot_email_exact_match",
        "customer_confirmed_identity",
        "prior_verified_identity",
        "event_email_exact_match",
        "event_phone_exact_match",
    }
)
_MACHINE_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
_EVIDENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_SAFE_LABEL = re.compile(r"^Persona [1-9][0-9]{0,2}$")
_DECISION_REASON_CODES = frozenset(
    {
        "model_abstained",
        "model_abstained_missing_information",
        "proposal_keys_invalid",
        "proposal_shape_invalid",
        "abstention_payload_invalid",
        "confidence_below_high",
        "contradicting_evidence_present",
        "candidate_not_allowed",
        "supporting_evidence_missing",
        "supporting_evidence_unknown",
        "supporting_evidence_candidate_mismatch",
        "independent_discriminating_evidence_missing",
        "independent_discriminating_candidate_not_unique",
    }
)
_TIMEOUT = httpx.Timeout(connect=5.0, read=45.0, write=10.0, pool=5.0)

_SYSTEM_PROMPT_V1 = """You review a bounded identity-correlation evidence packet.
Return exactly one JSON object with these keys and no others:
decision, recommended_candidate_id, confidence, supporting_evidence_ids,
contradicting_evidence_ids, missing_information.
You may only recommend one candidate_id present in the packet. Evidence fields
must contain only evidence_id values present in the packet. Recommend only when
independent evidence clearly distinguishes that candidate; otherwise abstain.
Never resolve the case, authorize contact, invent facts, or return personal data.
"""
_SYSTEM_PROMPT_V2 = """You review a bounded identity-correlation evidence packet.
Return exactly one JSON object with these keys and no others:
decision, recommended_candidate_id, confidence, supporting_evidence_ids,
contradicting_evidence_ids, missing_information.
You may only recommend one candidate_id present in the packet. Evidence fields
must contain only evidence_id values present in the packet. Recommend only when
independent evidence clearly distinguishes that candidate; otherwise abstain.
A fact marked independent=true and discriminating=true is bridge-verified
evidence that clearly distinguishes its candidate. When exactly one candidate
has such evidence and there is no contradicting evidence, recommend that
candidate with high confidence and cite the discriminating evidence_id.
Never resolve the case, authorize contact, invent facts, or return personal data.
"""
_SYSTEM_PROMPTS = {
    "correlation-preresolution-v1": _SYSTEM_PROMPT_V1,
    "correlation-preresolution-v2": _SYSTEM_PROMPT_V2,
}


class CorrelationPreresolutionProviderError(RuntimeError):
    """Transient or malformed provider response that must use durable retry."""


def _canonical_uuid(value: object, *, error: str) -> str:
    if not isinstance(value, str):
        raise ValueError(error)
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(error) from exc
    if str(parsed) != value:
        raise ValueError(error)
    return value


@dataclass(frozen=True)
class CorrelationCandidate:
    candidate_id: str
    label: str

    def __post_init__(self) -> None:
        _canonical_uuid(self.candidate_id, error="invalid_correlation_candidate_id")
        if not isinstance(self.label, str) or _SAFE_LABEL.fullmatch(self.label) is None:
            raise ValueError("invalid_correlation_candidate_label")


@dataclass(frozen=True)
class CorrelationEvidenceFact:
    evidence_id: str
    candidate_id: str
    kind: str
    value: int | None
    independent: bool
    discriminating: bool

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_id, str) or _EVIDENCE_ID.fullmatch(self.evidence_id) is None:
            raise ValueError("invalid_correlation_evidence_id")
        _canonical_uuid(self.candidate_id, error="invalid_correlation_evidence_candidate")
        if self.kind not in _SUPPORTED_FACT_KINDS:
            raise ValueError("invalid_correlation_evidence_kind")
        if type(self.independent) is not bool:
            raise ValueError("invalid_correlation_evidence_independence")
        if self.kind in {
            "precheckout_time_proximity_minutes",
            "nearest_precheckout_by_at_least_5m",
        }:
            if isinstance(self.value, bool) or not isinstance(self.value, int) or not 0 <= self.value <= 1440:
                raise ValueError("invalid_correlation_evidence_value")
        elif self.value is not None:
            raise ValueError("invalid_correlation_evidence_value")
        if not isinstance(self.independent, bool) or not isinstance(
            self.discriminating, bool
        ):
            raise ValueError("invalid_correlation_evidence_policy")
        if self.discriminating and not self.independent:
            raise ValueError("invalid_correlation_evidence_policy")


@dataclass(frozen=True)
class CorrelationEvidence:
    case_id: str
    event_type: str
    outcome: str
    candidates: tuple[CorrelationCandidate, ...]
    facts: tuple[CorrelationEvidenceFact, ...]

    def __post_init__(self) -> None:
        _canonical_uuid(self.case_id, error="invalid_correlation_case_id")
        if self.event_type not in _SUPPORTED_EVENT_TYPES:
            raise ValueError("unsupported_correlation_event_type")
        if self.outcome not in _SUPPORTED_OUTCOMES or len(self.candidates) < 1:
            raise ValueError("correlation_preresolution_requires_candidates")
        if len(self.candidates) > 50 or len(self.facts) > 200:
            raise ValueError("correlation_preresolution_evidence_too_large")
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("duplicate_correlation_candidate")
        labels = [candidate.label for candidate in self.candidates]
        if len(labels) != len(set(labels)):
            raise ValueError("duplicate_correlation_candidate_label")
        evidence_ids = [fact.evidence_id for fact in self.facts]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("duplicate_correlation_evidence")
        if any(fact.candidate_id not in candidate_ids for fact in self.facts):
            raise ValueError("correlation_evidence_candidate_mismatch")

    def to_model_context(self) -> dict[str, object]:
        return {
            "case": {
                "case_id": self.case_id,
                "event_type": self.event_type,
                "outcome": self.outcome,
            },
            "candidates": [
                {"candidate_id": item.candidate_id, "label": item.label}
                for item in self.candidates
            ],
            "evidence_facts": [
                {
                    "evidence_id": fact.evidence_id,
                    "candidate_id": fact.candidate_id,
                    "kind": fact.kind,
                    "value": fact.value,
                    "independent": fact.independent,
                    "discriminating": fact.discriminating,
                }
                for fact in self.facts
            ],
        }


@dataclass(frozen=True)
class PreresolutionRecommendation:
    status: str
    candidate_id: str | None
    candidate_label: str | None
    supporting_facts: tuple[CorrelationEvidenceFact, ...]
    model_name: str
    prompt_version: str
    decision_reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.status == "recommended":
            if self.decision_reason_code is not None:
                raise ValueError("recommended_preresolution_cannot_have_reason")
            return
        if self.status != "abstained":
            raise ValueError("invalid_preresolution_status")
        if self.decision_reason_code not in _DECISION_REASON_CODES:
            raise ValueError("invalid_preresolution_decision_reason")

    @classmethod
    def abstained(
        cls,
        *,
        model_name: str,
        prompt_version: str,
        decision_reason_code: str,
    ) -> PreresolutionRecommendation:
        return cls(
            status="abstained",
            candidate_id=None,
            candidate_label=None,
            supporting_facts=(),
            model_name=model_name,
            prompt_version=prompt_version,
            decision_reason_code=decision_reason_code,
        )

    @classmethod
    def from_proposal(
        cls,
        *,
        evidence: CorrelationEvidence,
        proposal: dict[str, object],
        model_name: str,
        prompt_version: str,
    ) -> PreresolutionRecommendation:
        def abstain(reason: str) -> PreresolutionRecommendation:
            return cls.abstained(
                model_name=model_name,
                prompt_version=prompt_version,
                decision_reason_code=reason,
            )

        if set(proposal) != _PROPOSAL_KEYS:
            return abstain("proposal_keys_invalid")
        decision = proposal.get("decision")
        candidate_id = proposal.get("recommended_candidate_id")
        confidence = proposal.get("confidence")
        supporting_ids = proposal.get("supporting_evidence_ids")
        contradicting_ids = proposal.get("contradicting_evidence_ids")
        missing = proposal.get("missing_information")
        if (
            decision not in {"recommend_candidate", "abstain"}
            or confidence not in {"high", "medium", "low"}
            or not _valid_token_list(supporting_ids, evidence_ids=True)
            or not _valid_token_list(contradicting_ids, evidence_ids=True)
            or not _valid_token_list(missing, evidence_ids=False)
        ):
            return abstain("proposal_shape_invalid")
        if decision == "abstain":
            if candidate_id is not None or supporting_ids or contradicting_ids:
                return abstain("abstention_payload_invalid")
            return abstain(
                "model_abstained_missing_information"
                if missing
                else "model_abstained"
            )
        if not isinstance(candidate_id, str):
            return abstain("proposal_shape_invalid")
        if confidence != "high":
            return abstain("confidence_below_high")
        if contradicting_ids:
            return abstain("contradicting_evidence_present")
        if not isinstance(supporting_ids, list) or not isinstance(
            contradicting_ids, list
        ):
            return abstain("proposal_shape_invalid")
        supporting_keys = [item for item in supporting_ids if isinstance(item, str)]
        contradicting_keys = [item for item in contradicting_ids if isinstance(item, str)]
        candidates = {candidate.candidate_id: candidate for candidate in evidence.candidates}
        selected = candidates.get(candidate_id)
        facts = {fact.evidence_id: fact for fact in evidence.facts}
        if selected is None:
            return abstain("candidate_not_allowed")
        if not supporting_keys:
            return abstain("supporting_evidence_missing")
        if any(evidence_id not in facts for evidence_id in supporting_keys):
            return abstain("supporting_evidence_unknown")
        if any(evidence_id not in facts for evidence_id in contradicting_keys):
            return abstain("contradicting_evidence_present")
        supporting = tuple(facts[evidence_id] for evidence_id in supporting_keys)
        if any(fact.candidate_id != candidate_id for fact in supporting):
            return abstain("supporting_evidence_candidate_mismatch")
        if not any(
            fact.independent and fact.discriminating for fact in supporting
        ):
            return abstain("independent_discriminating_evidence_missing")
        discriminating_candidates = {
            fact.candidate_id
            for fact in evidence.facts
            if fact.independent and fact.discriminating
        }
        if discriminating_candidates != {candidate_id}:
            return abstain("independent_discriminating_candidate_not_unique")
        return cls(
            status="recommended",
            candidate_id=candidate_id,
            candidate_label=selected.label,
            supporting_facts=supporting,
            model_name=model_name,
            prompt_version=prompt_version,
        )


def _valid_token_list(value: object, *, evidence_ids: bool) -> bool:
    pattern = _EVIDENCE_ID if evidence_ids else _MACHINE_TOKEN
    return (
        isinstance(value, list)
        and len(value) <= 20
        and len(value) == len(set(value))
        and all(isinstance(item, str) and pattern.fullmatch(item) is not None for item in value)
    )


def parse_preresolution_proposal(content: object) -> dict[str, object] | None:
    if not isinstance(content, str):
        return None
    stripped = content.strip()
    candidates = [stripped]
    if stripped.startswith("```"):
        opening_end = stripped.find("\n")
        closing_start = stripped.rfind("```")
        opening = stripped[:opening_end].strip().lower() if opening_end >= 0 else ""
        if (
            opening in {"```", "```json"}
            and closing_start > opening_end
            and closing_start == len(stripped) - 3
        ):
            candidates.append(stripped[opening_end + 1 : closing_start].strip())
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


class CorrelationPreresolutionClient:
    """Call a model for a recommendation; every uncertain result is an abstention."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_name: str,
        prompt_version: str = _PROMPT_VERSION,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = _validate_base_url(base_url)
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("correlation_preresolution_api_key_required")
        if not isinstance(model_name, str) or not model_name or len(model_name) > 200:
            raise ValueError("invalid_correlation_preresolution_model")
        if prompt_version not in _SYSTEM_PROMPTS:
            raise ValueError("unsupported_correlation_preresolution_prompt")
        self._api_key = api_key
        self._model_name = model_name
        self._prompt_version = prompt_version
        self._system_prompt = _SYSTEM_PROMPTS[prompt_version]
        self._transport = transport

    async def recommend(self, evidence: CorrelationEvidence) -> PreresolutionRecommendation:
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=_TIMEOUT,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Idempotency-Key": evidence.case_id,
                    },
                    json={
                        "model": self._model_name,
                        "stream": False,
                        "messages": [
                            {"role": "system", "content": self._system_prompt},
                            {
                                "role": "user",
                                "content": json.dumps(
                                    evidence.to_model_context(),
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                    sort_keys=True,
                                ),
                            },
                        ],
                    },
                )
                if response.status_code != 200:
                    raise CorrelationPreresolutionProviderError(
                        "correlation_preresolution_provider_http_error"
                    )
                body: Any = response.json()
                content = body["choices"][0]["message"]["content"]
        except CorrelationPreresolutionProviderError:
            raise
        except httpx.HTTPError as exc:
            raise CorrelationPreresolutionProviderError(
                "correlation_preresolution_provider_transport_error"
            ) from exc
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise CorrelationPreresolutionProviderError(
                "correlation_preresolution_provider_invalid_response"
            ) from exc
        proposal = parse_preresolution_proposal(content)
        if proposal is None:
            raise CorrelationPreresolutionProviderError(
                "correlation_preresolution_provider_invalid_response"
            )
        return PreresolutionRecommendation.from_proposal(
            evidence=evidence,
            proposal=proposal,
            model_name=self._model_name,
            prompt_version=self._prompt_version,
        )


@dataclass(frozen=True)
class CorrelationPreresolutionClaim:
    case_id: str
    event_type: str
    outcome: str
    claim_token: str
    lease_generation: int

    def __post_init__(self) -> None:
        _canonical_uuid(self.case_id, error="invalid_correlation_case_id")
        _canonical_uuid(self.claim_token, error="invalid_correlation_claim_token")
        if self.event_type not in _SUPPORTED_EVENT_TYPES:
            raise ValueError("unsupported_correlation_event_type")
        if self.outcome not in {"unmatched", *_SUPPORTED_OUTCOMES}:
            raise ValueError("unsupported_correlation_outcome")
        if (
            isinstance(self.lease_generation, bool)
            or not isinstance(self.lease_generation, int)
            or self.lease_generation < 1
        ):
            raise ValueError("invalid_correlation_lease_generation")


class CorrelationPreresolutionModel(Protocol):
    async def recommend(
        self, evidence: CorrelationEvidence
    ) -> PreresolutionRecommendation: ...


class CorrelationPreresolutionStore(Protocol):
    async def claim_correlation_preresolution(
        self,
        *,
        tenant_ref: str,
        funnel_ref: str,
        worker_id: str,
    ) -> CorrelationPreresolutionClaim | None: ...

    async def load_correlation_evidence(
        self,
        *,
        tenant_ref: str,
        funnel_ref: str,
        case_id: str,
        claim_token: str,
        lease_generation: int,
    ) -> CorrelationEvidence: ...

    async def complete_correlation_preresolution(
        self,
        *,
        case_id: str,
        claim_token: str,
        lease_generation: int,
        disposition: str,
        recommendation: PreresolutionRecommendation | None,
    ) -> None: ...

    async def release_correlation_preresolution(
        self,
        *,
        case_id: str,
        claim_token: str,
        lease_generation: int,
        failure_code: str,
    ) -> None: ...


class CorrelationPreresolutionWorker:
    """Persist a bounded recommendation or abstention before Slack projection."""

    def __init__(
        self,
        *,
        store: CorrelationPreresolutionStore,
        model: CorrelationPreresolutionModel,
        tenant_ref: str,
        funnel_ref: str,
        worker_id: str,
        poll_interval_seconds: float = 5.0,
    ) -> None:
        for value, error in (
            (tenant_ref, "invalid_correlation_preresolution_scope"),
            (funnel_ref, "invalid_correlation_preresolution_scope"),
            (worker_id, "invalid_correlation_preresolution_worker_id"),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ValueError(error)
        self._store = store
        self._model = model
        self._tenant_ref = tenant_ref
        self._funnel_ref = funnel_ref
        self._worker_id = worker_id
        if poll_interval_seconds <= 0:
            raise ValueError("invalid_correlation_preresolution_poll_interval")
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._healthy = True

    @property
    def healthy(self) -> bool:
        return self._healthy

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run(), name="correlation-preresolution-worker"
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        task = self._task
        self._task = None
        await task

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                processed = await self.run_once()
                self._healthy = True
            except Exception as exc:
                self._healthy = False
                logger.warning(
                    "correlation_preresolution_worker_failed error_type=%s",
                    type(exc).__name__,
                )
                processed = 0
            if processed:
                continue
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._poll_interval_seconds
                )
            except TimeoutError:
                pass

    async def run_once(self) -> int:
        claim = await self._store.claim_correlation_preresolution(
            tenant_ref=self._tenant_ref,
            funnel_ref=self._funnel_ref,
            worker_id=self._worker_id,
        )
        if claim is None:
            return 0
        if claim.outcome == "unmatched":
            await self._complete(
                claim,
                disposition="suppressed_unmatched",
                recommendation=None,
            )
            return 1
        try:
            evidence = await self._store.load_correlation_evidence(
                tenant_ref=self._tenant_ref,
                funnel_ref=self._funnel_ref,
                case_id=claim.case_id,
                claim_token=claim.claim_token,
                lease_generation=claim.lease_generation,
            )
            if (
                evidence.case_id != claim.case_id
                or evidence.event_type != claim.event_type
                or evidence.outcome != claim.outcome
            ):
                raise ValueError("correlation_preresolution_evidence_mismatch")
            recommendation = await self._model.recommend(evidence)
            if not isinstance(recommendation, PreresolutionRecommendation):
                raise ValueError("invalid_correlation_preresolution_result")
            disposition = recommendation.status
            if disposition not in {"recommended", "abstained"}:
                raise ValueError("invalid_correlation_preresolution_result")
            await self._complete(
                claim,
                disposition=disposition,
                recommendation=recommendation,
            )
        except Exception:
            await self._store.release_correlation_preresolution(
                case_id=claim.case_id,
                claim_token=claim.claim_token,
                lease_generation=claim.lease_generation,
                failure_code="correlation_preresolution_failed",
            )
            return 0
        return 1

    async def _complete(
        self,
        claim: CorrelationPreresolutionClaim,
        *,
        disposition: str,
        recommendation: PreresolutionRecommendation | None,
    ) -> None:
        await self._store.complete_correlation_preresolution(
            case_id=claim.case_id,
            claim_token=claim.claim_token,
            lease_generation=claim.lease_generation,
            disposition=disposition,
            recommendation=recommendation,
        )


def _validate_base_url(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("invalid_correlation_preresolution_base_url")
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError:
        raise ValueError("invalid_correlation_preresolution_base_url") from None
    trusted_http_hosts = {"hermes", "localhost", "127.0.0.1", "::1"}
    trusted_internal_http = (
        parsed.scheme == "http" and parsed.hostname in trusted_http_hosts
    )
    if (
        (parsed.scheme != "https" and not trusted_internal_http)
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/", "/v1"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid_correlation_preresolution_base_url")
    return value.rstrip("/")
