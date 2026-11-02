"""El agente sabe que la persona es cliente (E-297, P-11; diseno E-278, PR 3).

Lo de la base (las fuentes link, telefono y mail, sin ventana y con los
reembolsos) lo prueba ``tests/sql/followup_engine/validate_customer_status.mjs``
contra la cadena real de migraciones (PR de la migracion 20261103000700). Aca va
lo del bridge:

* la entrada ``customer_status``: un item por producto comprado, con el nombre
  de ``[hotmart]`` cuando no hay ``[[productos]]`` (ATT1 y Maribel hoy), y la
  etiqueta ``cliente`` de la conversacion como cuarta fuente;
* el cliente de la base: lo que pide y lo que acepta;
* el webhook: con el flag, la entrada lo trae; el link del producto comprado
  no sale y el turno se deriva con ``customer_link_request`` (D6); el cupon no
  se renueva ni se ofrece (D4, D5); sin compras, sin el flag o con la base
  caida, todo como antes;
* el arranque: apagado por defecto, portable, y sin manifiesto (Johanna) no
  arranca.

Datos: el webhook es la captura de la conversacion 200 del inbox 9 con
``content`` reemplazado (``_text_webhook``), y la etiqueta en
``conversation.labels``, donde la manda Chatwoot (captura
``chatwoot_message_created_inbox_9_conv_158_20260923.json``). La forma de lo que
devuelve la RPC es la que fija la migracion: no hay captura de PostgREST
contestandola porque no esta desplegada.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_recovery_coupon_reply import ATT1_517, _a2_message, _CouponConversation
from test_recovery_coupon_reply import _settings as _coupon_settings
from test_whatsapp_inbound_equivalence import (
    FORM_ID,
    PAYMENT_LINK_PROPOSAL,
    WA_ID,
    _johanna_settings,
    _manifest_settings,
    _process,
    _text_webhook,
)

from bridge import app as app_module
from bridge.app import Settings, create_app
from bridge.customer_status import (
    CUSTOMER_LINK_REQUEST_REASON,
    conversation_labels,
    customer_entry_for,
    customer_status_entries,
)
from bridge.product_routes import ProductRoutes
from bridge.supabase import (
    CustomerStatus,
    RecoveryCouponForConversation,
    SupabaseClient,
    SupabaseError,
    SupabaseRpcMissing,
)

BOUGHT = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
PRODUCT = "Alimenta tu Tiroides"  # product_name de [hotmart] en la instancia de ATT1


def _status(funnel_ref: str = "att1", **overrides: Any) -> CustomerStatus:
    values: dict[str, Any] = {
        "funnel_ref": funnel_ref,
        "status": "cliente",
        "first_purchase_at": BOUGHT,
        "last_purchase_at": BOUGHT,
        "source": "telefono",
    }
    values.update(overrides)
    return CustomerStatus(**values)


# ------------------------------------------------------------- la entrada


def _routes(tmp_path: Path) -> ProductRoutes:
    settings = _manifest_settings(tmp_path)
    return ProductRoutes.build(settings.commercial_ally_config, settings.instance_manifest)


def test_without_products_the_product_is_the_hotmart_name(tmp_path: Path) -> None:
    routes = _routes(tmp_path)
    assert routes.principal.key is None
    assert routes.principal.config.product_name == PRODUCT

    entries = customer_status_entries([_status()], routes=routes, labels=())

    assert entries == [{
        "product": PRODUCT, "status": "cliente",
        "since": BOUGHT.isoformat(), "source": "telefono",
    }]


def test_the_cliente_label_is_the_fourth_source(tmp_path: Path) -> None:
    routes = _routes(tmp_path)

    assert customer_status_entries([], routes=routes, labels=("cliente",)) == [
        {"product": PRODUCT, "status": "cliente", "source": "etiqueta"}
    ]
    # Si la base ya la vio, la etiqueta no agrega un segundo item.
    assert customer_status_entries(
        [_status(source="link")], routes=routes, labels=("cliente", "otra")
    ) == [{"product": PRODUCT, "status": "cliente", "since": BOUGHT.isoformat(), "source": "link"}]
    # Otra etiqueta no es cliente.
    assert customer_status_entries([], routes=routes, labels=("automation_paused",)) == []


def test_a_funnel_the_instance_does_not_have_is_dropped(tmp_path: Path) -> None:
    assert customer_status_entries([_status("att2")], routes=_routes(tmp_path), labels=()) == []


def test_the_labels_come_from_the_conversation_of_the_webhook() -> None:
    assert conversation_labels({"conversation": {"labels": ["cliente", 3, "x"]}}) == ("cliente", "x")
    assert conversation_labels({"conversation": {"labels": None}}) == ()
    assert conversation_labels({}) == ()


def test_any_status_of_the_product_blocks_its_link(tmp_path: Path) -> None:
    routes = _routes(tmp_path)
    for status in ("cliente", "reembolsada", "contracargo"):
        context = {"customer_status": customer_status_entries(
            [_status(status=status)], routes=routes, labels=())}
        assert customer_entry_for(context, route=routes.principal) is not None
    assert customer_entry_for({}, route=routes.principal) is None


# ------------------------------------------------------------- el cliente


def _client(handler: Any) -> SupabaseClient:
    return SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="test-key",
        transport=httpx.MockTransport(handler),
    )


def _rpc(rows: object, status: int = 200, seen: list[httpx.Request] | None = None) -> SupabaseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=rows)

    return _client(handler)


def _ask(client: SupabaseClient) -> tuple[CustomerStatus, ...]:
    import asyncio

    return asyncio.run(client.get_customer_status(
        tenant_ref="lancemos", funnel_refs=("att1",), chatwoot_account_id=1,
        chatwoot_inbox_id=11, chatwoot_conversation_id=343, whatsapp_phone=WA_ID,
    ))


ROW = {
    "funnel_ref": "att1", "status": "cliente",
    "first_purchase_at": "2026-10-19T15:00:00+00:00",
    "last_purchase_at": "2026-10-20T15:00:00+00:00", "source": "mail",
}


def test_the_client_asks_by_conversation_phone_and_funnels() -> None:
    seen: list[httpx.Request] = []

    [status] = _ask(_rpc([ROW], seen=seen))

    [request] = seen
    assert request.url.path == "/rest/v1/rpc/get_customer_status_v1"
    assert json.loads(request.content) == {
        "p_tenant_ref": "lancemos", "p_funnel_refs": ["att1"],
        "p_chatwoot_account_id": 1, "p_chatwoot_inbox_id": 11,
        "p_chatwoot_conversation_id": 343, "p_whatsapp_phone": WA_ID,
    }
    assert status == CustomerStatus(
        funnel_ref="att1", status="cliente", first_purchase_at=BOUGHT,
        last_purchase_at=datetime(2026, 10, 20, 15, 0, tzinfo=UTC), source="mail",
    )
    assert _ask(_rpc([])) == ()


@pytest.mark.parametrize(
    "row",
    [
        {**ROW, "status": "prospecta"},
        {**ROW, "source": "etiqueta"},
        {**ROW, "funnel_ref": "att2"},
        {**ROW, "first_purchase_at": "2026-10-19T15:00:00"},
        {**ROW, "first_purchase_at": "2026-10-21T15:00:00+00:00"},
        {**ROW, "email": "x@example.test"},
    ],
)
def test_the_client_refuses_a_row_it_does_not_know(row: dict[str, object]) -> None:
    with pytest.raises(SupabaseError, match="customer_status_get_invalid_row"):
        _ask(_rpc([row]))


def test_the_client_says_when_the_migration_is_missing() -> None:
    with pytest.raises(SupabaseRpcMissing):
        _ask(_rpc({"code": "PGRST202", "message": "Could not find the function"}, status=404))
    with pytest.raises(SupabaseError, match="HTTP 503"):
        _ask(_rpc({}, status=503))


# ------------------------------------------------------------- el webhook


class _Customer(_CouponConversation):
    """La conversacion con la lectura de si ya es cliente."""

    def __init__(
        self,
        *,
        statuses: tuple[CustomerStatus, ...] | Exception = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.statuses = statuses
        self.customer_lookups: list[dict[str, object]] = []

    async def get_customer_status(self, **kwargs: object) -> tuple[CustomerStatus, ...]:
        self.customer_lookups.append(kwargs)
        if isinstance(self.statuses, Exception):
            raise self.statuses
        return self.statuses


def _settings(tmp_path: Path, *, enabled: bool = True) -> Settings:
    return replace(
        _manifest_settings(tmp_path),
        payment_link_enabled=True,
        agent_customer_status_enabled=enabled,
    )


def _ask_for_the_link(
    tmp_path: Path,
    supabase: _Customer,
    *,
    settings: Settings | None = None,
    labels: list[str] | None = None,
) -> tuple[Any, Any, Any]:
    webhook, history = _text_webhook("Pasame el link para pagar")
    if labels is not None:
        webhook["conversation"]["labels"] = labels
    return _process(
        tmp_path,
        supabase,
        settings=settings or _settings(tmp_path),
        webhook=webhook,
        history=history,
        proposal=PAYMENT_LINK_PROPOSAL,
    )


def test_a_customer_reaches_the_agent_as_such_and_gets_no_link(tmp_path: Path) -> None:
    supabase = _Customer(statuses=(_status(),), identities=(FORM_ID,))
    settings = _settings(tmp_path)

    chatwoot, shadow, _ = _ask_for_the_link(tmp_path, supabase, settings=settings)

    [lookup] = supabase.customer_lookups
    assert lookup == {
        "tenant_ref": settings.commercial_ally_config.tenant_ref,
        "funnel_refs": ("att1",),
        "chatwoot_account_id": 1, "chatwoot_inbox_id": 9,
        "chatwoot_conversation_id": 200, "whatsapp_phone": FORM_ID,
    }
    [(_, context)] = shadow.calls
    assert context["customer_status"] == [{
        "product": PRODUCT, "status": "cliente",
        "since": BOUGHT.isoformat(), "source": "telefono",
    }]
    # D6: el modelo pidio el link y el bridge no lo emitio: lo atiende una persona.
    assert supabase.candidate_calls == []
    assert not any("src=hermes" in str(call["content"]) for call in chatwoot.reply_calls)
    [handoff] = supabase.handoff_calls
    assert handoff["detail_reason_code"] == CUSTOMER_LINK_REQUEST_REASON


@pytest.mark.parametrize("status", ["reembolsada", "contracargo"])
def test_a_refund_or_a_chargeback_asking_to_buy_again_goes_to_a_person(
    tmp_path: Path, status: str
) -> None:
    # D4: si pide comprar otra vez, deriva.
    supabase = _Customer(statuses=(_status(status=status),), identities=(FORM_ID,))

    _, shadow, _ = _ask_for_the_link(tmp_path, supabase)

    [(_, context)] = shadow.calls
    assert context["customer_status"][0]["status"] == status  # type: ignore[index]
    assert supabase.candidate_calls == []
    assert len(supabase.handoff_calls) == 1


def test_the_cliente_label_alone_blocks_the_link(tmp_path: Path) -> None:
    # La #18 de ATT1: compro con otro telefono y otro mail; el equipo la marca.
    supabase = _Customer(identities=(FORM_ID,))

    _, shadow, _ = _ask_for_the_link(tmp_path, supabase, labels=["cliente"])

    [(_, context)] = shadow.calls
    assert context["customer_status"] == [
        {"product": PRODUCT, "status": "cliente", "source": "etiqueta"}
    ]
    assert supabase.candidate_calls == []
    assert len(supabase.handoff_calls) == 1


def test_without_purchases_the_entry_and_the_link_are_as_before(tmp_path: Path) -> None:
    supabase = _Customer(identities=(FORM_ID,))

    chatwoot, shadow, _ = _ask_for_the_link(tmp_path, supabase)

    assert len(supabase.customer_lookups) == 1
    [(_, context)] = shadow.calls
    assert "customer_status" not in context
    [reservation] = supabase.candidate_calls
    assert reservation["chatwoot_conversation_id"] == 200
    assert any("src=hermes" in str(call["content"]) for call in chatwoot.reply_calls)
    assert supabase.handoff_calls == []


def test_with_the_flag_off_nothing_is_asked(tmp_path: Path) -> None:
    supabase = _Customer(statuses=(_status(),), identities=(FORM_ID,))

    chatwoot, shadow, _ = _ask_for_the_link(
        tmp_path, supabase, settings=_settings(tmp_path, enabled=False), labels=["cliente"]
    )

    assert supabase.customer_lookups == []
    [(_, context)] = shadow.calls
    assert "customer_status" not in context
    assert len(supabase.candidate_calls) == 1
    assert supabase.handoff_calls == []


@pytest.mark.parametrize(
    "failure",
    [SupabaseRpcMissing("customer_status_get_missing: HTTP 404"),
     SupabaseError("customer_status_get_failed: HTTP 503")],
)
def test_a_failed_lookup_never_stops_the_agent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, failure: SupabaseError
) -> None:
    supabase = _Customer(statuses=failure, identities=(FORM_ID,))

    with caplog.at_level(logging.WARNING, logger="bridge.app"):
        _, shadow, _ = _ask_for_the_link(tmp_path, supabase)

    [(_, context)] = shadow.calls
    assert "customer_status" not in context
    assert len(supabase.candidate_calls) == 1
    assert f"customer_status_lookup_failed error={type(failure).__name__} conversation=200" in caplog.text


def test_the_log_carries_states_and_sources_and_never_the_phone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    supabase = _Customer(statuses=(_status(source="mail"),), identities=(FORM_ID,))

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        _ask_for_the_link(tmp_path, supabase)

    assert "customer_status_context items=1 statuses=cliente sources=mail conversation=200" in caplog.text
    assert "customer_status_link_blocked status=cliente source=mail conversation=200" in caplog.text
    assert FORM_ID not in caplog.text and WA_ID not in caplog.text


def test_a_customer_gets_no_coupon_and_no_renewal_by_reply(tmp_path: Path) -> None:
    # D4 y D5: con el cupon por respuesta prendido, a una clienta no se le
    # renueva ni se le dice que tiene uno.
    supabase = _Customer(
        statuses=(_status(),),
        coupon=RecoveryCouponForConversation(
            state="renewal_in_progress", code=None, discount=None, expires_at=None, ordinal=2
        ),
        identities=(FORM_ID,),
    )
    webhook, history = _text_webhook(ATT1_517)
    _, shadow, _ = _process(
        tmp_path,
        supabase,
        settings=replace(_coupon_settings(tmp_path), agent_customer_status_enabled=True),
        webhook=webhook,
        history=[_a2_message(history[0]), *history],
        proposal={"decision": "reply", "reply": "Te leemos."},
    )

    assert supabase.by_reply == [] and supabase.lookups == []
    [(_, context)] = shadow.calls
    assert "recovery_coupon" not in context
    assert context["customer_status"][0]["status"] == "cliente"  # type: ignore[index]


# ------------------------------------------------------------- el arranque


def test_the_flag_is_off_by_default_and_portable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings.__dataclass_fields__["agent_customer_status_enabled"].default is False
    assert "agent_customer_status_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    for name, value in {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "CHATWOOT_INBOX_ID": "9",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AGENT_CUSTOMER_STATUS_ENABLED", raising=False)
    assert Settings.from_env().agent_customer_status_enabled is False
    monkeypatch.setenv("AGENT_CUSTOMER_STATUS_ENABLED", "yes")
    assert Settings.from_env().agent_customer_status_enabled is False
    monkeypatch.setenv("AGENT_CUSTOMER_STATUS_ENABLED", " TRUE ")
    assert Settings.from_env().agent_customer_status_enabled is True


def test_without_a_manifest_it_does_not_start(tmp_path: Path) -> None:
    settings = replace(_johanna_settings(tmp_path), agent_customer_status_enabled=True)
    with pytest.raises(ValueError, match="AGENT_CUSTOMER_STATUS_ENABLED requires an instance manifest"):
        create_app(settings)
