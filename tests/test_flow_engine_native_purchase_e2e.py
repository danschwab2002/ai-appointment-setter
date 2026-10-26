"""La compra real de la bienvenida de ATT1, de punta a punta hasta el envio (E-122).

El 2026-10-26 16:47:37Z la compra de prueba de Dan abrio el primer recorrido
nativo de ATT1 (``att1/bienvenida``) y ``att1_bienvenida_01`` no salio: el
despachador frenaba en ``_is_authorized_followup_recipient`` con
``followup_recipient_not_allowlisted``, porque el caso nativo nace con
``offer_code`` nulo. Las pruebas del despacho nativo pasaban porque su doble de
la base contestaba la oferta por defecto (``gopi6lh7``) en el contexto.

Esta prueba no tiene ese doble en el medio:

1. La compra es la capturada (``tests/fixtures/hotmart_purchase_approved_att1_bienvenida_20261026.json``,
   att1-db ``webhook_events`` ``c1dd277a…``, anonimizada). El bridge la
   normaliza con ``admit_native_purchase`` y el evento que arma es,
   campo por campo, el que la base admite en
   ``validate_flow_engine_native_offer_in_context.mjs``.
2. Lo que la base contesta en ese validador (la cadena entera de migraciones,
   PGlite) esta en ``tests/fixtures/flow_engine/native_purchase_execution_context_rows.json``,
   comparado en cada ``npm test`` (``--check``). El contexto del despacho entra
   al dispatcher por el parser real de ``SupabaseClient.get_followup_execution_context``
   (una respuesta HTTP de PostgREST con esa fila), y el chequeo del
   destinatario es el real, con el binding real de ATT1 (el manifiesto).
3. El envio es a la bandeja 11 con la plantilla capturada; se afirma el
   ``processed_params`` que Meta espera.

Con la fila de antes de la migracion (oferta nula) el mismo camino termina en
``followup_recipient_not_allowlisted``: el rojo que vio produccion.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_durable_dispatcher_approved_template import ALLY, NOW
from test_flow_engine_native_dispatch import TAIL, _dispatcher, _NativeAuthority, _step, _templates
from test_flow_engine_native_template_shape import PRODUCT, WELCOME_BODY, _RealWelcomeChatwoot

from bridge.flow_engine_runtime import FlowEngineRuntime
from bridge.native_flows import admit_native_purchase
from bridge.supabase import (
    FlowEngineNativeAdmissionRow,
    FlowEngineNativeStepTail,
    FollowupExecutionContext,
    SupabaseClient,
    SupabaseError,
)

FIXTURES = Path(__file__).parent / "fixtures"
CAPTURED = json.loads(
    (FIXTURES / "hotmart_purchase_approved_att1_bienvenida_20261026.json").read_text(encoding="utf-8"))
ROWS = json.loads(
    (FIXTURES / "flow_engine" / "native_purchase_execution_context_rows.json").read_text(encoding="utf-8"))
INSTANCE = {"tenant_ref": ALLY.tenant_ref, "ally_ref": ALLY.ally_ref, "funnel_ref": ALLY.funnel_ref}
EVENT_ID = "c1dd277a-5362-473e-a08c-eb00fbe48cf6"
CONTACT_ID = "00000000-0000-4000-8000-0000000e1221"


def test_the_fixtures_are_the_captured_purchase_and_the_real_chain() -> None:
    purchase = CAPTURED["payload"]["data"]["purchase"]
    assert CAPTURED["payload"]["event"] == "PURCHASE_APPROVED"
    assert (purchase["offer"]["code"], purchase["offer"]["coupon_code"]) == ("gopi6lh7", "TESTDAN2")
    assert CAPTURED["payload"]["data"]["product"]["id"] == ALLY.hotmart_product_id == 5071808
    assert "gopi6lh7" in ALLY.accepted_offer_codes
    # Lo que contesta la base: con la migracion, la oferta del evento; antes, nula.
    assert ROWS["execution_context"]["offer_code"] == "gopi6lh7"
    assert ROWS["execution_context_before_migration"]["offer_code"] is None
    assert ROWS["admission"]["outcome"] == "motor_arranco"
    assert ROWS["step_tail"] == {"etiquetar": "cliente", "producto": ALLY.product_name}


# -- 1. El bridge arma el evento que la base admite ---------------------------


class _AdmissionBase:
    """Lo que admit_native_purchase le pide a la base, para una persona nueva."""

    def __init__(self) -> None:
        self.flow_events: list[dict[str, Any]] = []

    async def find_contact_by_email(self, email: str) -> None:
        return None

    async def find_contact_by_phones(self, phones: Any) -> None:
        return None

    async def create_contact(self, **_: Any) -> str:
        return CONTACT_ID

    async def create_contact_point(self, **_: Any) -> None:
        return None

    async def find_active_whatsapp_identities(self, **_: Any) -> list[Any]:
        return []

    async def admit_flow_engine_event(self, instance: Any, flow_event: dict[str, Any]) -> tuple[Any, ...]:
        self.flow_events.append(flow_event)
        return (FlowEngineNativeAdmissionRow("flow-1", "motor_arranco", None, "case-1", "action-1", True),)

    async def finish_flow_engine_native_event(self, **_: Any) -> bool:
        return True


def test_the_bridge_builds_the_flow_event_the_database_admits() -> None:
    base = _AdmissionBase()
    outcome = asyncio.run(admit_native_purchase(
        base,  # type: ignore[arg-type]
        runtime=FlowEngineRuntime(supabase=base, instance=INSTANCE),  # type: ignore[arg-type]
        webhook_event_id=EVENT_ID,
        payload=CAPTURED["payload"],
        product_ref=ALLY.funnel_ref,
        commercial_ally_config=ALLY,
        now=NOW,
    ))
    assert outcome == "admitida"
    [event] = base.flow_events
    moment = datetime.fromisoformat(event["momento"])
    assert moment.timestamp() * 1000 == CAPTURED["payload"]["creation_date"]
    normalized = {**event, "evento_id": "<EVENT>", "contacto_id": "<CONTACT>", "momento": "<MOMENTO>"}
    assert normalized == ROWS["flow_event"]


# -- 2 y 3. El despacho, con el contexto de la base y el chequeo real ---------


def _context_response(row: dict[str, Any], action: Any) -> httpx.Response:
    # La fila de la base con los ids del doble del despachador (la accion, el caso).
    ids = {"<ACTION>": action.action_id, "<CASE>": action.recovery_case_id,
           "<CONTACT>": CONTACT_ID, "<EVENT>": EVENT_ID}
    return httpx.Response(200, json=[{key: ids.get(value, value) if isinstance(value, str) else value
                                      for key, value in row.items()}])


class _DatabaseContextAuthority(_NativeAuthority):
    """El despacho nativo con el contexto que contesta la base, por el parser real."""

    def __init__(self, row: dict[str, Any]) -> None:
        step = ROWS["step_context"]
        super().__init__(step=_step(template_name=step["template_name"], template_language=step["template_language"],
                                    template_variables=tuple(step["template_variables"]),
                                    send_ordinal=step["send_ordinal"]))
        self.row = row
        self.context_reads: list[dict[str, Any]] = []

    async def get_followup_execution_context(self, **kwargs: Any) -> FollowupExecutionContext:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/rest/v1/rpc/get_followup_execution_context"
            self.context_reads.append(json.loads(request.content))
            return _context_response(self.row, self.action)

        client = SupabaseClient(base_url="https://supabase.example.test", service_role_key="clave-de-prueba",
                                transport=httpx.MockTransport(handler))
        return await client.get_followup_execution_context(**kwargs)

    async def get_flow_engine_native_step_tail(self, action_id: str) -> FlowEngineNativeStepTail | None:
        self.engine_calls.append("tail")
        return FlowEngineNativeStepTail(flow_id=TAIL["flow_id"], version=TAIL["version"],
                                        etiquetar=ROWS["step_tail"]["etiquetar"],
                                        producto=ROWS["step_tail"]["producto"], momento=TAIL["momento"])


def test_the_captured_purchase_sends_the_welcome_through_the_real_recipient_check(tmp_path: Path) -> None:
    authority = _DatabaseContextAuthority(ROWS["execution_context"])
    chatwoot = _RealWelcomeChatwoot()

    asyncio.run(_dispatcher(authority, chatwoot, tmp_path).dispatch_due(now=NOW))

    assert len(authority.context_reads) == 1
    [message] = _templates(chatwoot)
    params = message["template_params"]
    assert (params["name"], params["category"], params["language"]) == ("att1_bienvenida_01", "UTILITY", "es_MX")
    assert params["processed_params"] == {"body": {"1": ALLY.product_name}}
    assert message["content"] == WELCOME_BODY.replace("{{1}}", ALLY.product_name)
    assert ALLY.product_name == PRODUCT
    assert authority.acceptances
    # Despues del envio: etiquetar cliente y la nota, que no miran la oferta.
    assert chatwoot.labels == ["cliente"]
    [note] = chatwoot.notes
    assert ALLY.product_name in note["content"]


def test_without_the_migration_the_same_purchase_stops_at_the_recipient_check(tmp_path: Path) -> None:
    authority = _DatabaseContextAuthority(ROWS["execution_context_before_migration"])
    chatwoot = _RealWelcomeChatwoot()

    with pytest.raises(SupabaseError, match="followup_recipient_not_allowlisted"):
        asyncio.run(_dispatcher(authority, chatwoot, tmp_path).dispatch_due(now=NOW))

    assert _templates(chatwoot) == []
    assert [f["reason_code"] for f in authority.finalizations] == ["pre_request_failed"]
    assert chatwoot.labels == [] and chatwoot.notes == []


@pytest.mark.parametrize("offer", ["otra0001", None], ids=["oferta no declarada", "sin oferta"])
def test_an_offer_the_instance_does_not_accept_still_stops(tmp_path: Path, offer: str | None) -> None:
    # La guarda sigue igual: la oferta del evento tiene que ser una de las del binding.
    row = {**copy.deepcopy(ROWS["execution_context"]), "offer_code": offer}
    authority = _DatabaseContextAuthority(row)
    chatwoot = _RealWelcomeChatwoot()

    with pytest.raises(SupabaseError, match="followup_recipient_not_allowlisted"):
        asyncio.run(_dispatcher(authority, chatwoot, tmp_path).dispatch_due(now=NOW))
    assert _templates(chatwoot) == []


def test_the_context_parser_reads_the_database_row_unchanged() -> None:
    action = replace(_NativeAuthority().action)
    authority = _DatabaseContextAuthority(ROWS["execution_context"])
    context = asyncio.run(authority.get_followup_execution_context(
        action_id=action.action_id, worker_id="att1-dispatcher", lease_generation=1, now=NOW))
    assert (context.offer_code, context.product_name, context.buyer_phone, context.lead_stage) == (
        "gopi6lh7", ALLY.product_name, "12025550124", "new")
