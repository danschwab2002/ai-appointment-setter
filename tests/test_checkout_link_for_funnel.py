"""El link del producto que nombra el agente (varios productos por aliada, 2c-3a).

Con dos productos el agente dice de cual es el link (2b) y el bridge reserva con
reserve_portable_checkout_issuance_for_funnel_v1 y el funnel de ese producto: su
oferta por defecto, su hotlink y su intencion. Con un solo producto la reserva es
la de siempre (reserve_portable_checkout_issuance_v2).

Dato real: el catalogo y el binding de att1-db, leidos en solo lectura el
2026-10-16. Las cuatro ofertas de ATT1 tienen funnel_ref att1 y una sola es la
por defecto del scope entrante; el binding att1 esta activo en la cuenta y el
inbox de la instancia. Con el funnel del principal, la reserva nueva elige la
misma oferta que la de hoy: lo prueba el caso 12 de
tests/sql/followup_engine/validate_att1_portable_chain.mjs con las RPC reales.
El segundo producto es el de prueba de test_multi_product_manifest: ATT2 nunca
mando nada a att1-db.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import tomllib
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from bridge.instance_manifest import InstanceManifest
from bridge.supabase import SupabaseClient, SupabaseCommittedResponseError
from test_agent_link_product import _LinkSupabase, _link_turn
from test_multi_product_manifest import SECOND
from test_person_activity import _products_settings
from test_product_label import _by_product
from test_whatsapp_inbound_equivalence import (
    FORM_ID,
    PAYMENT_LINK_PROPOSAL,
    _process,
    _text_webhook,
)

ATT1_TOML = Path(__file__).parent / "fixtures" / "instances" / "att1" / "instancia.toml"
ULID = "01K3F8QW7N2VYB4M6X9CDPTZRA"
RESERVE_ARGS = {
    "commercial_case_id": "00000000-0000-0000-0000-000000000300",
    "external_user_id": FORM_ID,
    "chatwoot_account_id": 1,
    "chatwoot_inbox_id": 9,
    "chatwoot_conversation_id": 200,
    "trigger_external_message_id": "501",
    "issuance_ulid": ULID,
    "now": "2026-10-16T22:00:00+00:00",
}


# ------------------------------------------------------- el pedido a la base


def _client(seen: list[httpx.Request], row: dict[str, object]) -> SupabaseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[row], request=request)

    return SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="test-key",
        transport=httpx.MockTransport(handler),
    )


RESERVED_SECOND_PRODUCT = {
    "outcome": "reserved",
    "issuance_id": "00000000-0000-0000-0000-000000000301",
    "issuance_ulid": ULID,
    "purchase_intent_id": "00000000-0000-0000-0000-000000000302",
    "source_kind": "inbound_request",
    "checkout_url_final": (
        "https://pay.hotmart.com/P8437428PRUEBA?off=prueba2a&checkoutMode=10"
        f"&src=hermes&sck=hermes~v1~{ULID}"
    ),
    "source_value": "hermes",
    "sck_value": f"hermes~v1~{ULID}",
}
BLOCKED_FUNNEL = {
    "outcome": "blocked_funnel",
    **{key: None for key in RESERVED_SECOND_PRODUCT if key != "outcome"},
}


def test_with_a_funnel_the_reserve_is_the_one_of_that_funnel() -> None:
    seen: list[httpx.Request] = []
    client = _client(seen, RESERVED_SECOND_PRODUCT)

    result = asyncio.run(client.reserve_chatwoot_checkout_issuance_v2(
        **RESERVE_ARGS, phone_equivalence=True, funnel_ref="att2"
    ))

    [request] = seen
    assert request.url.path == "/rest/v1/rpc/reserve_portable_checkout_issuance_for_funnel_v1"
    # El payload de la portable, con el funnel.
    assert json.loads(request.content) == {
        "p_commercial_case_id": "00000000-0000-0000-0000-000000000300",
        "p_external_user_id": FORM_ID,
        "p_chatwoot_account_id": 1,
        "p_chatwoot_inbox_id": 9,
        "p_chatwoot_conversation_id": 200,
        "p_trigger_external_message_id": "501",
        "p_issuance_ulid": ULID,
        "p_now": "2026-10-16T22:00:00+00:00",
        "p_funnel_ref": "att2",
    }
    assert result.outcome == "reserved"
    assert result.checkout_url_final.startswith("https://pay.hotmart.com/P8437428PRUEBA?")


def test_blocked_funnel_is_an_answer_only_of_the_funnel_reserve() -> None:
    seen: list[httpx.Request] = []

    result = asyncio.run(_client(seen, BLOCKED_FUNNEL).reserve_chatwoot_checkout_issuance_v2(
        **RESERVE_ARGS, phone_equivalence=True, funnel_ref="att9"
    ))
    assert result.outcome == "blocked_funnel"
    assert result.issuance_id is None

    # La portable de siempre no lo devuelve nunca: si llega, la respuesta no
    # es la esperada.
    with pytest.raises(SupabaseCommittedResponseError):
        asyncio.run(_client(seen, BLOCKED_FUNNEL).reserve_chatwoot_checkout_issuance_v2(
            **RESERVE_ARGS, phone_equivalence=True
        ))


def test_a_funnel_reserve_without_the_portable_never_reaches_the_base() -> None:
    seen: list[httpx.Request] = []

    with pytest.raises(ValueError, match="phone_equivalence"):
        asyncio.run(_client(seen, RESERVED_SECOND_PRODUCT).reserve_chatwoot_checkout_issuance_v2(
            **RESERVE_ARGS, funnel_ref="att2"
        ))
    assert seen == []


# --------------------------------------------------------- el bridge


def _two_products_settings(tmp_path: Path):
    """El manifiesto de prueba de ATT1 con el segundo producto de prueba."""

    settings = _products_settings(tmp_path)
    payload = tomllib.loads(ATT1_TOML.read_text(encoding="utf-8"))
    payload["productos"] = [{"clave": "att1"}, copy.deepcopy(SECOND)]
    for product in payload["productos"]:
        # Sin etiquetas: la etiqueta por producto es la 2c-3b.
        product.pop("etiqueta", None)
        product.pop("macro_etiqueta", None)
    products = InstanceManifest.from_mapping(payload).products
    assert settings.instance_manifest is not None
    return replace(
        settings,
        instance_manifest=replace(settings.instance_manifest, products=products),
        commercial_knowledge=_by_product("att1", "att2"),
    )


def test_with_two_products_the_link_is_the_one_of_the_product_the_agent_named(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        chatwoot, supabase = _link_turn(
            tmp_path,
            {**PAYMENT_LINK_PROPOSAL, "product": "att2"},
            _two_products_settings(tmp_path),
        )

    assert "payment_link_product product=att2 source=agent" in caplog.text
    [reservation] = supabase.candidate_calls
    assert reservation["funnel_ref"] == "att2"
    assert reservation["phone_equivalence"] is True
    [reply] = chatwoot.reply_calls
    assert "src=hermes" in str(reply["content"])


def test_with_two_products_the_principal_also_goes_by_its_funnel(
    tmp_path: Path
) -> None:
    _, supabase = _link_turn(
        tmp_path,
        {**PAYMENT_LINK_PROPOSAL, "product": "att1"},
        _two_products_settings(tmp_path),
    )

    [reservation] = supabase.candidate_calls
    assert reservation["funnel_ref"] == "att1"


def test_a_funnel_without_binding_in_the_inbox_goes_to_a_person(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # La base contesta blocked_funnel: el binding del segundo producto no esta
    # activo en el inbox de la instancia.
    supabase = _LinkSupabase()
    supabase.candidate_outcome = "blocked_funnel"
    webhook, history = _text_webhook("Envíame el enlace")
    with caplog.at_level(logging.INFO):
        chatwoot, _, _ = _process(
            tmp_path,
            supabase,
            settings=replace(_two_products_settings(tmp_path), payment_link_enabled=True),
            webhook=webhook,
            history=history,
            proposal={**PAYMENT_LINK_PROPOSAL, "product": "att2"},
        )

    assert "payment_link_handoff reason=blocked_funnel" in caplog.text
    # La derivacion, con el motivo normalizado de todo link bloqueado; el
    # motivo fino queda en la linea de payment_link_handoff.
    [handoff] = supabase.handoff_calls
    assert handoff["reason_code"] == "commercial_exception"
    assert not any("src=hermes" in str(reply["content"]) for reply in chatwoot.reply_calls)


def test_with_one_product_the_reserve_is_the_one_of_always(tmp_path: Path) -> None:
    _, supabase = _link_turn(
        tmp_path, {**PAYMENT_LINK_PROPOSAL, "product": "att1"}, _products_settings(tmp_path)
    )

    [reservation] = supabase.candidate_calls
    assert "funnel_ref" not in reservation
    assert reservation["phone_equivalence"] is True
