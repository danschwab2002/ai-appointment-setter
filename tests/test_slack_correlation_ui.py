from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import tomllib

import httpx
import pytest

from slack_correlation.client import SlackClient, SlackProtocolError, SlackRejectedError
from slack_correlation.security import InvalidSlackSignature, verify_slack_signature
from slack_correlation.ui import build_pending_message, build_review_modal


def test_slack_correlation_is_included_in_the_installable_package() -> None:
    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert "src/slack_correlation" in project["tool"]["hatch"]["build"]["targets"][
        "wheel"
    ]["packages"]


def _case() -> dict[str, object]:
    return {
        "case_id": "11111111-1111-4111-8111-111111111111",
        "outcome": "conflict",
        "reason_code": "email_phone_conflict",
        "reason": "El email y el teléfono apuntan a intenciones diferentes.",
        "candidate_count": 2,
        "automation_blocked": True,
        "identity": {
            "email_present": True,
            "phone_present": True,
            "masked_email": "b***r@example.com",
            "masked_phone": "********4567",
        },
        "candidates": [],
    }


def test_pending_message_is_native_correlated_and_pii_minimized() -> None:
    payload = build_pending_message(
        _case(),
        review_due_at="2026-09-07T15:00:00Z",
    )

    assert payload["text"] == "Correlación pendiente · Caso C-11111111"
    assert payload["metadata"] == {
        "event_type": "operator_correlation_case",
        "event_payload": {
            "case_id": "11111111-1111-4111-8111-111111111111",
        },
    }
    action = payload["blocks"][-1]["elements"][0]
    assert action == {
        "type": "button",
        "action_id": "review_operator_correlation",
        "text": {"type": "plain_text", "text": "Revisar caso"},
        "style": "primary",
        "value": "11111111-1111-4111-8111-111111111111",
    }
    rendered = repr(payload)
    assert "b***r@example.com" in rendered
    assert "********4567" in rendered
    assert "buyer@example.com" not in rendered
    assert "593991234567" not in rendered
    assert "automatización permanece bloqueada" in rendered


def test_review_modal_offers_only_projected_candidates_and_no_match() -> None:
    case = _case()
    case["candidates"] = [
        {
            "purchase_intent_id": "22222222-2222-4222-8222-222222222222",
            "matched_by": ["email"],
            "submitted_at": "2026-09-06T14:00:00Z",
            "lifecycle_state": "waiting_for_purchase",
            "masked_email": "b***r@example.com",
            "masked_phone": "********9999",
        },
        {
            "purchase_intent_id": "33333333-3333-4333-8333-333333333333",
            "matched_by": ["phone"],
            "submitted_at": "2026-09-06T14:05:00Z",
            "lifecycle_state": "waiting_for_purchase",
            "masked_email": "o***r@example.com",
            "masked_phone": "********4567",
        },
    ]
    review_token = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    modal = build_review_modal(case, review_token=review_token)

    assert modal["type"] == "modal"
    assert modal["callback_id"] == "prepare_operator_correlation_resolution"
    assert json.loads(modal["private_metadata"]) == {"review_token": review_token}
    assert modal["submit"] == {"type": "plain_text", "text": "Preparar resolución"}
    choice_block = next(
        block for block in modal["blocks"] if block.get("block_id") == "resolution"
    )
    options = choice_block["element"]["options"]
    assert [option["value"] for option in options] == [
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
        "close_without_match",
    ]
    rendered = repr(modal)
    assert "b***r@example.com" in rendered
    assert "********4567" in rendered
    assert "buyer@example.com" not in rendered
    assert "593991234567" not in rendered


def test_review_modal_rejects_unmasked_candidate_email_without_rendering_it() -> None:
    case = _case()
    case["candidate_count"] = 1
    case["candidates"] = [{
        "purchase_intent_id": "22222222-2222-4222-8222-222222222222",
        "lifecycle_state": "waiting_for_purchase",
        "masked_email": "buyer@example.com",
        "masked_phone": "********4567",
    }]
    with pytest.raises(ValueError, match="invalid_candidate_identity") as error:
        build_review_modal(case, review_token="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert "buyer@example.com" not in str(error.value)


def test_pending_message_rejects_raw_top_level_identity_without_rendering_it() -> None:
    case = _case()
    case["email"] = "buyer@example.com"
    with pytest.raises(ValueError, match="raw_identity_forbidden") as error:
        build_pending_message(case, review_due_at="2026-09-07T15:00:00Z")
    assert "buyer@example.com" not in str(error.value)


@pytest.mark.parametrize("value", ["b***r@example.com\n<!channel>", "b***r@exam`ple.com", "*******<1234"])
def test_slack_surfaces_reject_control_or_mrkdwn_in_identity(value: str) -> None:
    case = _case()
    case["identity"] = {"masked_email": value, "masked_phone": None}
    with pytest.raises(ValueError):
        build_pending_message(case, review_due_at="2026-09-07T15:00:00Z")


def test_slack_signature_uses_raw_body_and_rejects_stale_or_modified_requests() -> None:
    secret = "test-signing-secret"
    timestamp = "1788700000"
    body = b"payload=%7B%22type%22%3A%22block_actions%22%7D"
    base = b"v0:" + timestamp.encode("ascii") + b":" + body
    signature = "v0=" + hmac.new(
        secret.encode("utf-8"), base, hashlib.sha256
    ).hexdigest()

    verify_slack_signature(
        signing_secret=secret,
        raw_body=body,
        timestamp=timestamp,
        signature=signature,
        now_epoch=1788700000,
    )

    with pytest.raises(InvalidSlackSignature, match="signature_mismatch"):
        verify_slack_signature(
            signing_secret=secret,
            raw_body=body + b"x",
            timestamp=timestamp,
            signature=signature,
            now_epoch=1788700000,
        )
    with pytest.raises(InvalidSlackSignature, match="stale_request"):
        verify_slack_signature(
            signing_secret=secret,
            raw_body=body,
            timestamp=timestamp,
            signature=signature,
            now_epoch=1788700301,
        )


def test_slack_client_posts_to_exact_channel_and_validates_message_identity() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer test-bot-token"
        return httpx.Response(
            200,
            json={"ok": True, "channel": "C-OPERATIONS", "ts": "1788700000.123456"},
        )

    payload = build_pending_message(
        _case(), review_due_at="2026-09-07T15:00:00Z"
    )
    client = SlackClient(
        bot_token="test-bot-token",
        transport=httpx.MockTransport(handler),
    )

    reference = asyncio.run(
        client.post_message(channel_id="C-OPERATIONS", message=payload)
    )

    assert reference.channel_id == "C-OPERATIONS"
    assert reference.message_ts == "1788700000.123456"
    assert requests == [{"channel": "C-OPERATIONS", **payload}]

    def mismatch(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "channel": "C-OTHER", "ts": "1788700000.123456"},
        )

    mismatched_client = SlackClient(
        bot_token="test-bot-token",
        transport=httpx.MockTransport(mismatch),
    )
    with pytest.raises(SlackProtocolError, match="message_identity_mismatch"):
        asyncio.run(
            mismatched_client.post_message(
                channel_id="C-OPERATIONS", message=payload
            )
        )


def test_slack_client_opens_view_and_updates_exact_message_identity() -> None:
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path.endswith("views.open"):
            return httpx.Response(200, json={
                "ok": True,
                "view": {"id": "V12345678", "team_id": "T12345678", "callback_id": "prepare_operator_correlation_resolution"},
            })
        if request.url.path.endswith("views.update"):
            return httpx.Response(200, json={
                "ok": True,
                "view": {"id": "V12345678", "team_id": "T12345678", "callback_id": "confirm_operator_correlation_resolution"},
            })
        return httpx.Response(200, json={
            "ok": True, "channel": "C0C0YEACVT2", "ts": "1788700000.123456"
        })

    client = SlackClient(bot_token="test-bot-token", transport=httpx.MockTransport(handler))
    view = build_review_modal(
        {**_case(), "candidate_count": 0},
        review_token="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    asyncio.run(client.open_view(trigger_id="123.456.valid", view=view, expected_team_id="T12345678"))
    confirmation = {**view, "callback_id": "confirm_operator_correlation_resolution"}
    asyncio.run(client.update_view(
        view_id="V12345678", view_hash="1788700000.abc", view=confirmation,
        expected_team_id="T12345678",
    ))
    asyncio.run(client.update_message(
        channel_id="C0C0YEACVT2",
        message_ts="1788700000.123456",
        message={"text": "terminal", "blocks": []},
    ))
    assert [path for path, _body in requests] == [
        "/api/views.open", "/api/views.update", "/api/chat.update"
    ]
    assert requests[1][1]["view_id"] == "V12345678"
    assert requests[1][1]["hash"] == "1788700000.abc"
    assert requests[2][1]["channel"] == "C0C0YEACVT2"
    assert requests[2][1]["ts"] == "1788700000.123456"


def test_slack_client_classifies_explicit_update_rejection() -> None:
    client = SlackClient(
        bot_token="test-bot-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": False, "error": "message_not_found"})),
    )
    with pytest.raises(SlackRejectedError):
        asyncio.run(client.update_message(
            channel_id="C0C0YEACVT2", message_ts="1788700000.123456", message={"text": "terminal"}
        ))
