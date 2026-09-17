from __future__ import annotations

from datetime import UTC, datetime
import json

from slack_correlation.app import _parse_command
from slack_correlation.catalog import (
    CorrelationRecommendation,
    CorrelationRecommendationEvidence,
    NotificationCommand,
)
from slack_correlation.producer import _serialize_command


def _command() -> NotificationCommand:
    return NotificationCommand(
        event_id="11111111-1111-4111-8111-111111111111",
        event_code="COR-003",
        dedupe_key="1" * 64,
        occurred_at=datetime(2026, 9, 16, 12, tzinfo=UTC),
        subject_ref="C-22222222-2222-4222-8222-222222222222",
        reason_code="email_phone_conflict",
        state="pending",
        count=2,
        recommendation=CorrelationRecommendation(
            recommendation_ref="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            candidate_id="33333333-3333-4333-8333-333333333333",
            candidate_label="Persona 2",
            evidence=(
                CorrelationRecommendationEvidence(
                    kind="precheckout_time_proximity_minutes",
                    value=4,
                ),
                CorrelationRecommendationEvidence(
                    kind="same_product_offer",
                ),
            ),
            evidence_fingerprint="f" * 64,
            model_name="resolver-model",
            prompt_version="correlation-preresolution-v1",
        ),
    )


def test_recommendation_round_trips_across_the_bridge_connector_contract() -> None:
    command = _command()

    serialized = _serialize_command(command)
    parsed = _parse_command(json.dumps(serialized).encode("utf-8"))

    assert parsed == command
    assert serialized["recommendation"] == {
        "recommendation_ref": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "candidate_id": "33333333-3333-4333-8333-333333333333",
        "candidate_label": "Persona 2",
        "evidence": [
            {"kind": "precheckout_time_proximity_minutes", "value": 4},
            {"kind": "same_product_offer", "value": None},
        ],
        "evidence_fingerprint": "f" * 64,
        "model_name": "resolver-model",
        "prompt_version": "correlation-preresolution-v1",
    }


def test_ingress_rejects_arbitrary_recommendation_text_and_unknown_evidence() -> None:
    payload = _serialize_command(_command())
    payload["recommendation"]["explanation"] = "trust me"

    try:
        _parse_command(json.dumps(payload).encode("utf-8"))
    except ValueError as exc:
        assert str(exc) == "invalid_notification"
    else:
        raise AssertionError("arbitrary recommendation fields must fail closed")

    payload = _serialize_command(_command())
    payload["recommendation"]["evidence"][0]["kind"] = "free_text"
    try:
        _parse_command(json.dumps(payload).encode("utf-8"))
    except ValueError as exc:
        assert str(exc) == "invalid_notification"
    else:
        raise AssertionError("unknown evidence kinds must fail closed")
