from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from bridge.correlation_preresolution import (
    CorrelationCandidate,
    CorrelationEvidence,
    CorrelationEvidenceFact,
    CorrelationPreresolutionClient,
    CorrelationPreresolutionClaim,
    CorrelationPreresolutionProviderError,
    CorrelationPreresolutionWorker,
    PreresolutionRecommendation,
    parse_preresolution_proposal,
)


_CANDIDATE_1 = "11111111-1111-4111-8111-111111111111"
_CANDIDATE_2 = "22222222-2222-4222-8222-222222222222"


def _evidence() -> CorrelationEvidence:
    return CorrelationEvidence(
        case_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        event_type="PURCHASE_APPROVED",
        outcome="ambiguous",
        candidates=(
            CorrelationCandidate(candidate_id=_CANDIDATE_1, label="Persona 1"),
            CorrelationCandidate(candidate_id=_CANDIDATE_2, label="Persona 2"),
        ),
        facts=(
            CorrelationEvidenceFact(
                evidence_id="fact-1",
                candidate_id=_CANDIDATE_2,
                kind="precheckout_time_proximity_minutes",
                value=4,
                independent=True,
                discriminating=True,
            ),
            CorrelationEvidenceFact(
                evidence_id="fact-2",
                candidate_id=_CANDIDATE_2,
                kind="same_product_offer",
                value=None,
                independent=False,
                discriminating=False,
            ),
            CorrelationEvidenceFact(
                evidence_id="fact-3",
                candidate_id=_CANDIDATE_1,
                kind="event_email_exact_match",
                value=None,
                independent=False,
                discriminating=False,
            ),
        ),
    )


def _proposal(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "decision": "recommend_candidate",
        "recommended_candidate_id": _CANDIDATE_2,
        "confidence": "high",
        "supporting_evidence_ids": ["fact-1", "fact-2"],
        "contradicting_evidence_ids": [],
        "missing_information": [],
    }
    payload.update(overrides)
    return payload


def test_parse_accepts_only_a_supported_complete_json_envelope() -> None:
    proposal = _proposal()

    assert parse_preresolution_proposal(json.dumps(proposal)) == proposal
    assert parse_preresolution_proposal(
        "```json\n" + json.dumps(proposal) + "\n```"
    ) == proposal
    assert parse_preresolution_proposal(
        "```json\n" + json.dumps(proposal) + "\n``` trailing"
    ) is None
    assert parse_preresolution_proposal("[]") is None


def test_high_confidence_recommendation_requires_independent_bounded_evidence() -> None:
    recommendation = PreresolutionRecommendation.from_proposal(
        evidence=_evidence(),
        proposal=_proposal(),
        model_name="resolver-model",
        prompt_version="correlation-preresolution-v1",
    )

    assert recommendation.status == "recommended"
    assert recommendation.candidate_id == _CANDIDATE_2
    assert recommendation.candidate_label == "Persona 2"
    assert recommendation.supporting_facts == (
        CorrelationEvidenceFact(
            evidence_id="fact-1",
            candidate_id=_CANDIDATE_2,
            kind="precheckout_time_proximity_minutes",
            value=4,
            independent=True,
            discriminating=True,
        ),
        CorrelationEvidenceFact(
            evidence_id="fact-2",
            candidate_id=_CANDIDATE_2,
            kind="same_product_offer",
            value=None,
            independent=False,
            discriminating=False,
        ),
    )
    assert recommendation.model_name == "resolver-model"
    assert recommendation.prompt_version == "correlation-preresolution-v1"


@pytest.mark.parametrize(
    "proposal",
    [
        _proposal(confidence="medium"),
        _proposal(supporting_evidence_ids=["fact-3"]),
        _proposal(supporting_evidence_ids=["unknown"]),
        _proposal(recommended_candidate_id="33333333-3333-4333-8333-333333333333"),
        _proposal(contradicting_evidence_ids=["fact-3"]),
        _proposal(extra="not-allowed"),
    ],
)
def test_unsafe_or_unverifiable_model_choices_become_abstentions(
    proposal: dict[str, object],
) -> None:
    recommendation = PreresolutionRecommendation.from_proposal(
        evidence=_evidence(),
        proposal=proposal,
        model_name="resolver-model",
        prompt_version="correlation-preresolution-v1",
    )

    assert recommendation.status == "abstained"
    assert recommendation.candidate_id is None
    assert recommendation.supporting_facts == ()


def test_explicit_abstention_is_valid_and_never_selects_a_candidate() -> None:
    recommendation = PreresolutionRecommendation.from_proposal(
        evidence=_evidence(),
        proposal=_proposal(
            decision="abstain",
            recommended_candidate_id=None,
            confidence="low",
            supporting_evidence_ids=[],
            missing_information=["independent_identity_evidence"],
        ),
        model_name="resolver-model",
        prompt_version="correlation-preresolution-v1",
    )

    assert recommendation.status == "abstained"
    assert recommendation.candidate_id is None


def test_unmatched_cases_are_rejected_before_any_model_request() -> None:
    with pytest.raises(ValueError, match="correlation_preresolution_requires_candidates"):
        CorrelationEvidence(
            case_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            event_type="PURCHASE_APPROVED",
            outcome="unmatched",
            candidates=(),
            facts=(),
        )


def test_client_sends_bounded_structured_context_and_accepts_a_safe_recommendation() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_proposal())}}]},
        )

    client = CorrelationPreresolutionClient(
        base_url="https://hermes.internal.example",
        api_key="secret-value",
        model_name="resolver-model",
        prompt_version="correlation-preresolution-v1",
        transport=httpx.MockTransport(handler),
    )

    recommendation = asyncio.run(client.recommend(_evidence()))

    assert recommendation.status == "recommended"
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/chat/completions"
    body = json.loads(request.content)
    assert body["model"] == "resolver-model"
    context = json.loads(body["messages"][1]["content"])
    assert set(context) == {"case", "candidates", "evidence_facts"}
    assert "secret-value" not in request.content.decode()


def test_client_accepts_the_existing_trusted_hermes_internal_http_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503)

    client = CorrelationPreresolutionClient(
        base_url="http://hermes:8642/v1",
        api_key="secret-value",
        model_name="resolver-model",
        prompt_version="correlation-preresolution-v1",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(
        CorrelationPreresolutionProviderError,
        match="correlation_preresolution_provider_http_error",
    ):
        asyncio.run(client.recommend(_evidence()))
    assert len(requests) == 1
    assert str(requests[0].url) == "http://hermes:8642/v1/chat/completions"


def test_client_retries_http_or_protocol_errors() -> None:
    for response in (
        httpx.Response(503, json={"detail": "provider failed"}),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(
            200,
            json={"choices": [{"message": {"content": "not-json"}}]},
        ),
    ):
        client = CorrelationPreresolutionClient(
            base_url="https://hermes.internal.example",
            api_key="secret-value",
            model_name="resolver-model",
            prompt_version="correlation-preresolution-v1",
            transport=httpx.MockTransport(
                lambda request, response=response: response
            ),
        )

        with pytest.raises(CorrelationPreresolutionProviderError):
            asyncio.run(client.recommend(_evidence()))


def test_preresolution_worker_persists_only_a_validated_recommendation() -> None:
    claim = CorrelationPreresolutionClaim(
        case_id=_evidence().case_id,
        event_type="PURCHASE_APPROVED",
        outcome="ambiguous",
        claim_token="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        lease_generation=1,
    )

    class Store:
        completed: list[dict[str, object]] = []
        released: list[dict[str, object]] = []

        async def claim_correlation_preresolution(self, **kwargs: object):
            return claim

        async def load_correlation_evidence(self, **kwargs: object):
            return _evidence()

        async def complete_correlation_preresolution(self, **kwargs: object):
            self.completed.append(kwargs)

        async def release_correlation_preresolution(self, **kwargs: object):
            self.released.append(kwargs)

    class Model:
        async def recommend(self, evidence: CorrelationEvidence):
            return PreresolutionRecommendation.from_proposal(
                evidence=evidence,
                proposal=_proposal(),
                model_name="resolver-model",
                prompt_version="correlation-preresolution-v1",
            )

    store = Store()
    worker = CorrelationPreresolutionWorker(
        store=store,
        model=Model(),
        tenant_ref="lancemos",
        funnel_ref="psicologajohanna",
        worker_id="correlation-ai-1",
    )

    assert asyncio.run(worker.run_once()) == 1
    assert store.released == []
    assert store.completed[0]["disposition"] == "recommended"
    recommendation = store.completed[0]["recommendation"]
    assert isinstance(recommendation, PreresolutionRecommendation)
    assert recommendation.candidate_id == _CANDIDATE_2


def test_preresolution_worker_suppresses_unmatched_without_calling_the_model() -> None:
    claim = CorrelationPreresolutionClaim(
        case_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        event_type="PURCHASE_APPROVED",
        outcome="unmatched",
        claim_token="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        lease_generation=1,
    )

    class Store:
        completed: list[dict[str, object]] = []

        async def claim_correlation_preresolution(self, **kwargs: object):
            return claim

        async def load_correlation_evidence(self, **kwargs: object):
            raise AssertionError("unmatched must not load model evidence")

        async def complete_correlation_preresolution(self, **kwargs: object):
            self.completed.append(kwargs)

        async def release_correlation_preresolution(self, **kwargs: object):
            raise AssertionError("suppression is terminal, not retryable")

    class Model:
        async def recommend(self, evidence: CorrelationEvidence):
            raise AssertionError("unmatched must not invoke the model")

    store = Store()
    worker = CorrelationPreresolutionWorker(
        store=store,
        model=Model(),
        tenant_ref="lancemos",
        funnel_ref="psicologajohanna",
        worker_id="correlation-ai-1",
    )

    assert asyncio.run(worker.run_once()) == 1
    assert store.completed == [
        {
            "case_id": claim.case_id,
            "claim_token": claim.claim_token,
            "lease_generation": 1,
            "disposition": "suppressed_unmatched",
            "recommendation": None,
        }
    ]


def test_preresolution_worker_persists_abstention_instead_of_notifying() -> None:
    claim = CorrelationPreresolutionClaim(
        case_id=_evidence().case_id,
        event_type="PURCHASE_APPROVED",
        outcome="ambiguous",
        claim_token="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        lease_generation=1,
    )

    class Store:
        completed: list[dict[str, object]] = []

        async def claim_correlation_preresolution(self, **kwargs: object):
            return claim

        async def load_correlation_evidence(self, **kwargs: object):
            return _evidence()

        async def complete_correlation_preresolution(self, **kwargs: object):
            self.completed.append(kwargs)

        async def release_correlation_preresolution(self, **kwargs: object):
            raise AssertionError("valid abstention must be terminal")

    class Model:
        async def recommend(self, evidence: CorrelationEvidence):
            return PreresolutionRecommendation.abstained(
                model_name="resolver-model",
                prompt_version="correlation-preresolution-v1",
            )

    store = Store()
    worker = CorrelationPreresolutionWorker(
        store=store,
        model=Model(),
        tenant_ref="lancemos",
        funnel_ref="psicologajohanna",
        worker_id="correlation-ai-1",
    )

    assert asyncio.run(worker.run_once()) == 1
    assert store.completed[0]["disposition"] == "abstained"


def test_preresolution_worker_releases_transient_failures_without_a_slack_effect() -> None:
    claim = CorrelationPreresolutionClaim(
        case_id=_evidence().case_id,
        event_type="PURCHASE_APPROVED",
        outcome="conflict",
        claim_token="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        lease_generation=2,
    )

    class Store:
        released: list[dict[str, object]] = []

        async def claim_correlation_preresolution(self, **kwargs: object):
            return claim

        async def load_correlation_evidence(self, **kwargs: object):
            return _evidence()

        async def complete_correlation_preresolution(self, **kwargs: object):
            raise AssertionError("failed model call must not complete")

        async def release_correlation_preresolution(self, **kwargs: object):
            self.released.append(kwargs)

    class Model:
        async def recommend(self, evidence: CorrelationEvidence):
            raise RuntimeError("provider unavailable")

    store = Store()
    worker = CorrelationPreresolutionWorker(
        store=store,
        model=Model(),
        tenant_ref="lancemos",
        funnel_ref="psicologajohanna",
        worker_id="correlation-ai-1",
    )

    assert asyncio.run(worker.run_once()) == 0
    assert store.released == [
        {
            "case_id": claim.case_id,
            "claim_token": claim.claim_token,
            "lease_generation": 2,
            "failure_code": "correlation_preresolution_failed",
        }
    ]
