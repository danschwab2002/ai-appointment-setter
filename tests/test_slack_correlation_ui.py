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
from slack_correlation.ui import (
    build_confirmation_modal,
    build_pending_message,
    build_processing_modal,
    build_review_modal,
    build_safe_error_modal,
    build_success_modal,
    build_terminal_message,
    build_verification_modal,
)


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

    assert payload["text"] == "Necesitamos confirmar una compra"
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
        "text": {"type": "plain_text", "text": "Revisar compra"},
        "style": "primary",
        "value": "11111111-1111-4111-8111-111111111111",
    }
    rendered = repr(payload["blocks"])
    assert "Necesitamos confirmar una compra" in rendered
    assert "El email y el teléfono no conducen a la misma persona." in rendered
    assert "Personas posibles: 2" in rendered
    assert "b***r@example.com" in rendered
    assert "********4567" in rendered
    assert "buyer@example.com" not in rendered
    assert "593991234567" not in rendered
    assert "La compra seguirá en espera hasta que alguien la revise." in rendered
    assert "Correlación" not in rendered
    assert "email_phone_conflict" not in rendered
    assert "C-11111111" not in rendered


def test_review_modal_asks_one_plain_language_question_with_three_safe_outcomes() -> None:
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
    assert modal["callback_id"] == "select_operator_correlation_resolution"
    assert json.loads(modal["private_metadata"]) == {"review_token": review_token}
    assert modal["title"] == {"type": "plain_text", "text": "Revisar compra"}
    assert modal["submit"] == {"type": "plain_text", "text": "Continuar"}
    choice_block = next(
        block for block in modal["blocks"] if block.get("block_id") == "decision"
    )
    options = choice_block["element"]["options"]
    assert [option["value"] for option in options] == [
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
        "close_without_match",
        "leave_pending",
    ]
    assert [option["text"]["text"] for option in options] == [
        "Es la persona 1",
        "Es la persona 2",
        "Revisé los datos: no corresponde a ninguna",
        "No puedo determinarlo",
    ]
    rendered = repr(modal)
    assert "¿A cuál persona pertenece esta compra?" in rendered
    assert "Compra recibida" in rendered
    assert "Persona 1" in rendered
    assert "Coincide por: email" in rendered
    assert "Coincide por: teléfono" in rendered
    assert "2026-09-06T14:00:00Z" in rendered
    assert "b***r@example.com" in rendered
    assert "********4567" in rendered
    assert "buyer@example.com" not in rendered
    assert "593991234567" not in rendered
    assert "email_phone_conflict" not in rendered
    assert "Resultado determinístico" not in rendered
    assert "Evidencia utilizada" not in rendered


def test_single_candidate_modal_asks_if_purchase_belongs_to_this_person() -> None:
    case = _case()
    case["candidate_count"] = 1
    case["candidates"] = [{
        "purchase_intent_id": "22222222-2222-4222-8222-222222222222",
        "matched_by": ["email"],
        "submitted_at": "2026-09-06T14:00:00Z",
        "lifecycle_state": "waiting_for_purchase",
        "masked_email": "b***r@example.com",
        "masked_phone": "********9999",
    }]

    modal = build_review_modal(
        case, review_token="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )

    rendered = repr(modal)
    assert "¿Esta compra pertenece a esta persona?" in rendered
    decision = next(block for block in modal["blocks"] if block.get("block_id") == "decision")
    assert decision["element"]["options"][0]["text"]["text"] == "Sí, es esta persona"


def test_no_candidate_modal_asks_whether_to_close_or_leave_pending() -> None:
    case = _case()
    case["outcome"] = "unmatched"
    case["reason_code"] = "identity_not_found"
    case["candidate_count"] = 0
    case["candidates"] = []

    modal = build_review_modal(
        case, review_token="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )

    rendered = repr(modal)
    assert "No encontramos una persona. ¿Qué querés hacer?" in rendered
    decision = next(block for block in modal["blocks"] if block.get("block_id") == "decision")
    assert [option["value"] for option in decision["element"]["options"]] == [
        "close_without_match",
        "leave_pending",
    ]


@pytest.mark.parametrize(
    ("candidate_count", "expected_element"),
    [(8, "radio_buttons"), (9, "static_select")],
)
def test_decision_control_respects_slacks_ten_radio_option_limit(
    candidate_count: int, expected_element: str
) -> None:
    case = _case()
    case["outcome"] = "ambiguous"
    case["candidate_count"] = candidate_count
    case["candidates"] = [
        {
            "purchase_intent_id": f"{index:08x}-2222-4222-8222-222222222222",
            "matched_by": ["email"],
            "submitted_at": "2026-09-06T14:00:00Z",
            "lifecycle_state": "waiting_for_purchase",
            "masked_email": "b***r@example.com",
            "masked_phone": "********9999",
        }
        for index in range(1, candidate_count + 1)
    ]

    modal = build_review_modal(
        case, review_token="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )

    decision = next(
        block for block in modal["blocks"] if block.get("block_id") == "decision"
    )
    assert decision["element"]["type"] == expected_element
    assert len(decision["element"]["options"]) == candidate_count + 2


def test_verification_is_requested_only_after_selecting_a_person() -> None:
    modal = build_verification_modal(
        review_token="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        candidate_id="22222222-2222-4222-8222-222222222222",
    )

    assert modal["callback_id"] == "prepare_operator_correlation_resolution"
    assert json.loads(modal["private_metadata"]) == {
        "review_token": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "candidate_id": "22222222-2222-4222-8222-222222222222",
    }
    assert modal["submit"] == {"type": "plain_text", "text": "Continuar"}
    assert [block.get("block_id") for block in modal["blocks"]] == [None, "verification"]
    rendered = repr(modal)
    assert "¿Cómo lo confirmaste?" in rendered
    assert "Revisé la compra o transacción" in rendered
    assert "Revisé el registro del cliente" in rendered
    assert "El cliente lo confirmó" in rendered


def test_confirmation_explains_the_business_consequence_without_internal_codes() -> None:
    modal = build_confirmation_modal(
        review_token="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        action="resolve_with_candidate",
        verification_basis="customer_confirmation",
    )

    rendered = repr(modal)
    assert modal["title"]["text"] == "Confirmar asociación"
    assert modal["submit"]["text"] == "Confirmar asociación"
    assert "Vas a asociar esta compra con la persona seleccionada." in rendered
    assert "El cliente confirmó que es su compra." in rendered
    assert "resolve_with_candidate" not in rendered



def test_no_match_confirmation_requires_an_explicit_irreversible_choice() -> None:
    modal = build_confirmation_modal(
        review_token="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        action="close_without_match",
        verification_basis="no_valid_candidate_after_review",
    )

    rendered = repr(modal)
    assert modal["title"]["text"] == "Confirmar cierre"
    assert modal["submit"]["text"] == "Confirmar cierre"
    assert "Vas a cerrar esta compra sin asociarla a ninguna persona." in rendered
    assert "Confirmá únicamente si revisaste todas las opciones." in rendered


@pytest.mark.parametrize(
    ("outcome", "headline"),
    [
        ("linked_candidate", "Compra asociada"),
        ("closed_without_match", "Compra cerrada sin asociación"),
    ],
)
def test_terminal_card_is_plain_language_but_keeps_audit_metadata(
    outcome: str, headline: str
) -> None:
    message = build_terminal_message(
        case_id="11111111-1111-4111-8111-111111111111",
        outcome=outcome,
        actor_id="U12345678",
        applied_at="2026-09-09T12:10:00Z",
    )

    assert message["text"] == headline
    assert message["metadata"]["event_payload"] == {
        "case_id": "11111111-1111-4111-8111-111111111111",
        "state": outcome,
    }
    visible = repr(message["blocks"])
    assert headline in visible
    assert "Revisado por: <@U12345678>" in visible
    assert "C-11111111" not in visible
    assert "Actor Slack" not in visible
    assert "linked_candidate" not in visible


def test_progress_success_and_error_surfaces_use_plain_decision_language() -> None:
    processing = build_processing_modal(
        review_token="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", phase="prepare"
    )
    success = build_success_modal()
    error = build_safe_error_modal()

    assert processing["title"]["text"] == "Guardando decisión"
    assert "Estamos comprobando tu decisión" in repr(processing)
    assert success["title"]["text"] == "Decisión guardada"
    assert "La compra fue actualizada" in repr(success)
    assert error["title"]["text"] == "No pudimos guardar"
    assert "La compra sigue pendiente" in repr(error)
    visible = repr([
        {"title": item["title"], "blocks": item["blocks"]}
        for item in (processing, success, error)
    ])
    assert "resolución" not in visible.lower()


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


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("matched_by", [["email"]], "invalid_candidate_match"),
        ("submitted_at", "tomorrow-ish", "invalid_candidate_submitted_at"),
        ("submitted_at", "2026-09-06T14:00:00Z\x7f", "invalid_candidate_submitted_at"),
    ],
)
def test_review_modal_rejects_malformed_candidate_comparison_data(
    field: str, value: object, reason: str
) -> None:
    case = _case()
    candidate = {
        "purchase_intent_id": "22222222-2222-4222-8222-222222222222",
        "matched_by": ["email"],
        "submitted_at": "2026-09-06T14:00:00Z",
        "lifecycle_state": "waiting_for_purchase",
        "masked_email": "b***r@example.com",
        "masked_phone": "********9999",
    }
    candidate[field] = value
    case["candidate_count"] = 1
    case["candidates"] = [candidate]

    with pytest.raises(ValueError, match=reason):
        build_review_modal(
            case, review_token="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        )


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
    assert requests == [{
        "channel": "C-OPERATIONS",
        **payload,
        "unfurl_links": False,
        "unfurl_media": False,
    }]

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
                "view": {"id": "V12345678", "team_id": "T12345678", "callback_id": body["view"]["callback_id"]},
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
