import asyncio
import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from bridge.chatwoot import ChatwootClient, ChatwootProtocolError
from bridge.worker import _equivalent_conversation_jid


def test_scoped_reply_uses_expected_jid_without_fixed_allowed_jid(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    scoped_jid = "12025550999@s.whatsapp.net"
    reply_hash = hashlib.sha256(b"2:10").hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/conversations/2"):
            return httpx.Response(
                200,
                json={
                    "id": 2,
                    "meta": {
                        "sender": {"identifier": scoped_jid},
                        "assignee": None,
                    },
                },
            )
        if request.method == "GET" and request.url.path.endswith("/labels"):
            return httpx.Response(200, json={"payload": []})
        if request.method == "GET" and request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "payload": [
                        {
                            "id": 10,
                            "message_type": 0,
                            "private": False,
                            "content": "Hola",
                            "sender": {"type": "contact", "id": 20},
                        }
                    ]
                },
            )
        assert request.method == "POST"
        assert request.headers["api_access_token"] == "agent-bot-token"
        assert json.loads(request.content)["content_attributes"] == {
            "appointment_setter_reply_hash": reply_hash,
        }
        return httpx.Response(
            200,
            json={
                "id": 11,
                "conversation_id": 2,
                "message_type": 1,
                "private": False,
                "content": "Respuesta",
                "content_attributes": {
                    "appointment_setter_reply_hash": reply_hash,
                },
                "sender": {"type": "agent_bot", "id": 1},
            },
        )

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid=None,
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(
        client.send_agent_bot_reply(
            conversation_id=2,
            trigger_message_id=10,
            delivery_id="dynamic-recipient",
            content="Respuesta",
            expected_jid=scoped_jid,
        )
    )

    assert result == {"status": "sent", "message_id": 11}
    assert [request.method for request in requests].count("POST") == 1


# ── La otra forma del mismo movil (solo el runtime portable) ────────
#
# Una fila durable guarda la identidad como la tiene la base (52 + 10 digitos
# si la creo el formulario) y la conversacion de Chatwoot es del wa_id (521 +
# 10). Las proyecciones (opt-out, derivacion) validan la conversacion contra
# el JID que le pasan: con la equivalencia prendida prueban la forma guardada
# y, si la identidad no coincide, la otra.
#
# La conversacion es la captura chatwoot_conversation_api_show_conv_158_20260924
# (GET /conversations/158 de produccion, inbox 9 de WhatsApp Cloud): no trae
# contact_inbox en la raiz y meta.sender.identifier es null, asi que la
# identidad sale de meta.sender.phone_number. Se cambia solo ese telefono (el
# capturado es colombiano, de una sola forma) por uno mexicano de prueba.

_SHOW_158 = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "chatwoot_conversation_api_show_conv_158_20260924.json"
    ).read_text(encoding="utf-8")
)["conversation"]
MX_FORM = "525512345678"
MX_WHATSAPP = "5215512345678"


def _client_for_conversation_of(phone_number: str) -> tuple[ChatwootClient, list[str]]:
    conversation = copy.deepcopy(_SHOW_158)
    conversation["meta"]["sender"]["phone_number"] = phone_number
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(f"{request.method} {request.url.path}")
        assert request.method == "GET"
        assert request.url.path == "/api/v1/accounts/1/conversations/158"
        return httpx.Response(200, json=conversation)

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid=None,
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        transport=httpx.MockTransport(handler),
    )
    return client, paths


def _jid(client: ChatwootClient, external_user_id: str) -> str:
    return asyncio.run(
        _equivalent_conversation_jid(
            client,
            conversation_id=158,
            expected_inbox_id=9,
            external_user_id=external_user_id,
        )
    )


def test_the_conversation_is_found_under_the_other_form_of_the_stored_phone() -> None:
    client, paths = _client_for_conversation_of(f"+{MX_WHATSAPP}")

    # La validacion exacta de hoy rechaza la forma guardada...
    with pytest.raises(ChatwootProtocolError, match="conversation_identity_mismatch"):
        asyncio.run(client.validate_conversation_authority(
            conversation_id=158,
            expected_inbox_id=9,
            expected_jid=f"{MX_FORM}@s.whatsapp.net",
        ))
    # ...y con la equivalencia se llega al JID real de la conversacion.
    jid = _jid(client, MX_FORM)

    assert jid == f"{MX_WHATSAPP}@s.whatsapp.net"
    asyncio.run(client.validate_conversation_authority(
        conversation_id=158,
        expected_inbox_id=9,
        expected_jid=jid,
    ))


def test_the_stored_form_wins_when_the_conversation_already_has_it() -> None:
    client, paths = _client_for_conversation_of(f"+{MX_FORM}")

    assert _jid(client, MX_FORM) == f"{MX_FORM}@s.whatsapp.net"
    assert len(paths) == 1


def test_a_conversation_of_a_third_number_stays_refused() -> None:
    client, _ = _client_for_conversation_of("+5215599999999")

    jid = _jid(client, MX_FORM)

    # Devuelve la otra forma, y quien la use la valida otra vez: no pasa.
    assert jid == f"{MX_WHATSAPP}@s.whatsapp.net"
    with pytest.raises(ChatwootProtocolError, match="conversation_identity_mismatch"):
        asyncio.run(client.validate_conversation_authority(
            conversation_id=158,
            expected_inbox_id=9,
            expected_jid=jid,
        ))


def test_a_number_with_a_single_form_costs_no_request() -> None:
    client, paths = _client_for_conversation_of("+573000000158")

    assert _jid(client, "573000000158") == "573000000158@s.whatsapp.net"
    assert paths == []


def test_another_inbox_is_not_mistaken_for_an_identity_mismatch() -> None:
    client, _ = _client_for_conversation_of(f"+{MX_WHATSAPP}")

    with pytest.raises(ChatwootProtocolError, match="invalid_conversation_authority"):
        asyncio.run(
            _equivalent_conversation_jid(
                client,
                conversation_id=158,
                expected_inbox_id=11,
                external_user_id=MX_FORM,
            )
        )
