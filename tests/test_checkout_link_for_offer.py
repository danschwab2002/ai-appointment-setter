"""El cliente de la base para el link con la oferta de la campana (E-410).

Con ``offer_code`` el bridge pega en reserve_portable_checkout_issuance_for_offer_v1
(20261110000500) con el funnel, la oferta y la clave de la campana. Sin
``offer_code`` el pedido es byte a byte el de siempre.

Dato real: la respuesta de la base es la del link que arma la RPC para la oferta
de Black de ATT1, con la URL que se verifico en el checkout publico de Hotmart
(tests/fixtures/hotmart_checkout_offer_black_att1_20261110.json, F1): la oferta
wmkq4ba9 carga en D98014973Y con checkoutMode=10, src=hermes y el marcador. Lo
que hace la RPC con la cadena real lo prueba el caso 15 de
tests/sql/followup_engine/validate_att1_portable_chain.mjs.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from bridge.supabase import SupabaseClient, SupabaseCommittedResponseError

F1 = json.loads(
    (Path(__file__).parent / "fixtures" / "hotmart_checkout_offer_black_att1_20261110.json")
    .read_text(encoding="utf-8")
)
ULID = "01TEST0000000000000000000X"
RESERVE_ARGS = {
    "commercial_case_id": "00000000-0000-0000-0000-000000000300",
    "external_user_id": "5215512345678",
    "chatwoot_account_id": 1,
    "chatwoot_inbox_id": 11,
    "chatwoot_conversation_id": 200,
    "trigger_external_message_id": "501",
    "issuance_ulid": ULID,
    "now": "2026-11-10T22:00:00+00:00",
}
RESERVED_CAMPAIGN = {
    "outcome": "reserved",
    "issuance_id": "00000000-0000-0000-0000-000000000301",
    "issuance_ulid": ULID,
    "purchase_intent_id": "00000000-0000-0000-0000-000000000302",
    "source_kind": "inbound_request",
    "checkout_url_final": F1["link_de_campana_esperado"],
    "source_value": "hermes",
    "sck_value": f"hermes~v1~{ULID}",
}
BLOCKED_CAMPAIGN_OFFER = {
    "outcome": "blocked_campaign_offer",
    **{key: None for key in RESERVED_CAMPAIGN if key != "outcome"},
}


def _client(seen: list[httpx.Request], row: dict[str, object]) -> SupabaseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[row], request=request)

    return SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="test-key",
        transport=httpx.MockTransport(handler),
    )


def test_the_fixture_is_the_black_offer_of_att1() -> None:
    assert F1["oferta_de_la_campana"] == "wmkq4ba9"
    assert F1["link_de_campana_esperado"].startswith(
        "https://pay.hotmart.com/D98014973Y?off=wmkq4ba9&checkoutMode=10&src=hermes&sck=hermes~v1~"
    )
    measured = {row["url"]: row for row in F1["medido"]}
    assert measured[F1["link_de_campana_esperado"]]["oferta_cargada"] == "wmkq4ba9"


def test_with_an_offer_the_reserve_is_the_one_of_the_campaign() -> None:
    seen: list[httpx.Request] = []

    result = asyncio.run(_client(seen, RESERVED_CAMPAIGN).reserve_chatwoot_checkout_issuance_v2(
        **RESERVE_ARGS, phone_equivalence=True, funnel_ref="att1",
        offer_code="wmkq4ba9", campaign_ref="black-november-2026",
    ))

    [request] = seen
    assert request.url.path == "/rest/v1/rpc/reserve_portable_checkout_issuance_for_offer_v1"
    assert json.loads(request.content) == {
        "p_commercial_case_id": "00000000-0000-0000-0000-000000000300",
        "p_external_user_id": "5215512345678",
        "p_chatwoot_account_id": 1,
        "p_chatwoot_inbox_id": 11,
        "p_chatwoot_conversation_id": 200,
        "p_trigger_external_message_id": "501",
        "p_issuance_ulid": ULID,
        "p_now": "2026-11-10T22:00:00+00:00",
        "p_funnel_ref": "att1",
        "p_offer_code": "wmkq4ba9",
        "p_campaign_ref": "black-november-2026",
    }
    assert result.outcome == "reserved"
    assert result.checkout_url_final == F1["link_de_campana_esperado"]


def test_blocked_campaign_offer_is_an_answer_only_of_the_offer_reserve() -> None:
    seen: list[httpx.Request] = []

    result = asyncio.run(
        _client(seen, BLOCKED_CAMPAIGN_OFFER).reserve_chatwoot_checkout_issuance_v2(
            **RESERVE_ARGS, phone_equivalence=True, funnel_ref="att1",
            offer_code="wmkq4ba9", campaign_ref="black-november-2026",
        )
    )
    assert result.outcome == "blocked_campaign_offer"
    assert result.issuance_id is None

    for kwargs in ({"phone_equivalence": True}, {"phone_equivalence": True, "funnel_ref": "att1"}):
        with pytest.raises(SupabaseCommittedResponseError):
            asyncio.run(_client(seen, BLOCKED_CAMPAIGN_OFFER).reserve_chatwoot_checkout_issuance_v2(
                **RESERVE_ARGS, **kwargs
            ))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"funnel_ref": "att1", "offer_code": "wmkq4ba9", "campaign_ref": "black-november-2026"},
         "phone_equivalence"),
        ({"phone_equivalence": True, "offer_code": "wmkq4ba9", "campaign_ref": "black-november-2026"},
         "funnel_ref"),
        ({"phone_equivalence": True, "funnel_ref": "att1", "offer_code": "wmkq4ba9"},
         "campaign_ref"),
        ({"phone_equivalence": True, "funnel_ref": "att1", "campaign_ref": "black-november-2026"},
         "offer_code"),
    ],
)
def test_an_incomplete_offer_reserve_never_reaches_the_base(
    kwargs: dict[str, object], message: str
) -> None:
    seen: list[httpx.Request] = []
    with pytest.raises(ValueError, match=message):
        asyncio.run(_client(seen, RESERVED_CAMPAIGN).reserve_chatwoot_checkout_issuance_v2(
            **RESERVE_ARGS, **kwargs
        ))
    assert seen == []


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"phone_equivalence": True}, {"phone_equivalence": True, "funnel_ref": "att1"}],
)
def test_without_an_offer_the_request_is_byte_for_byte_the_one_of_always(
    kwargs: dict[str, object]
) -> None:
    plain: list[httpx.Request] = []
    explicit: list[httpx.Request] = []
    row = {**RESERVED_CAMPAIGN, "checkout_url_final": RESERVED_CAMPAIGN["checkout_url_final"]}
    asyncio.run(_client(plain, row).reserve_chatwoot_checkout_issuance_v2(**RESERVE_ARGS, **kwargs))
    asyncio.run(_client(explicit, row).reserve_chatwoot_checkout_issuance_v2(
        **RESERVE_ARGS, **kwargs, offer_code=None, campaign_ref=None
    ))
    [before] = plain
    [after] = explicit
    assert after.url == before.url
    assert after.content == before.content
    # El pedido de antes de E-410, escrito a mano: las ocho claves de siempre
    # en su orden, mas el funnel si lo hay.
    expected = {f"p_{key}": value for key, value in RESERVE_ARGS.items()}
    expected = {key: expected[key] for key in (
        "p_commercial_case_id", "p_external_user_id", "p_chatwoot_account_id",
        "p_chatwoot_inbox_id", "p_chatwoot_conversation_id", "p_trigger_external_message_id",
        "p_issuance_ulid", "p_now",
    )}
    if "funnel_ref" in kwargs:
        expected["p_funnel_ref"] = kwargs["funnel_ref"]
    assert before.content == json.dumps(expected, ensure_ascii=False).encode()
    rpc = (
        "reserve_portable_checkout_issuance_for_funnel_v1" if "funnel_ref" in kwargs
        else "reserve_portable_checkout_issuance_v2" if kwargs
        else "reserve_chatwoot_checkout_issuance_v2"
    )
    assert before.url.path == f"/rest/v1/rpc/{rpc}"
