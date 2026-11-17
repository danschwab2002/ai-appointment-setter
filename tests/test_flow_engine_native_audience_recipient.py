"""El envio de un flujo nativo de audiencia pasa el chequeo del destinatario (E-661).

Lo que vio produccion. En Maribel, el 2026-11-17 16:01-16:10Z, los flujos de
prueba ``aviso_fecha_prueba`` y ``aviso_diario_prueba`` (cohorte) liberaron su
ocurrencia y el motor arranco (casos ``083dc040...`` y ``602c59d3...``). Cada
envio termino en 4 intentos ``failed_before_request`` / ``pre_request_failed``
y la accion en ``permanent_failed``, con ``followup_recipient_not_allowlisted``
en el log del bridge. En 30 dias no salio ningun envio de audiencia en ninguna
instancia: el evento de audiencia no lleva oferta (por diseno) y el chequeo
pedia una oferta aceptada.

Esta prueba no tiene un doble de la base en el medio del chequeo:

1. El evento es el que arma el bridge (``flow_audience_opener.audience_event``)
   y es, campo por campo, el que la base admite en
   ``tests/sql/followup_engine/validate_flow_engine_native_audience_recipient.mjs``.
2. Lo que la base contesta en ese validador (la cadena entera de migraciones,
   PGlite, con y sin 20261117000500) esta en
   ``tests/fixtures/flow_engine/native_audience_execution_context_rows.json``,
   comparado en cada ``npm test`` (``--check``). Entra al dispatcher por el parser
   real de ``SupabaseClient.get_followup_execution_context`` y el chequeo del
   destinatario es el real, con el binding real de ATT1 (el manifiesto).

Casos: (a) audiencia de un producto declarado: sale; rojo, la fila de antes de
la migracion: se frena como en produccion; (b) audiencia de un producto que la
instancia no declara: se frena con ``recipient_product_not_declared`` y sin
reintentos; (c) una compra de Hotmart con una oferta no aceptada: igual que hoy.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from test_durable_dispatcher_approved_template import ALLY, NOW
from test_flow_engine_native_dispatch import _dispatcher, _templates
from test_flow_engine_native_purchase_e2e import ROWS as PURCHASE_ROWS
from test_flow_engine_native_purchase_e2e import _DatabaseContextAuthority
from test_flow_engine_native_template_shape import _RealWelcomeChatwoot
from test_product_routing import _manifest, _routes

from bridge import worker as worker_module
from bridge.flow_audience_opener import audience_event
from bridge.flow_audience_sweeper import AudienceFlow, OccurrenceRow, ReleasedMember
from bridge.supabase import FollowupExecutionContext, SupabaseError

FIXTURES = Path(__file__).parent / "fixtures"
ROWS = json.loads(
    (FIXTURES / "flow_engine" / "native_audience_execution_context_rows.json").read_text(encoding="utf-8"))


def test_the_fixture_is_the_audience_case_of_production() -> None:
    # El caso de audiencia como 083dc040... de Maribel: sin oferta, ni en la columna ni en el context.
    assert ROWS["case_context"] == {"flow_engine_trigger": "audiencia", "flow_engine_offer": None, "offer_code": None}
    assert ROWS["admission"]["outcome"] == "motor_arranco"
    # La instancia del validador es ATT1, la misma del binding de esta prueba.
    assert ROWS["instance"] == {"tenant_ref": ALLY.tenant_ref, "funnel_ref": ALLY.funnel_ref,
                                "product_name": ALLY.product_name}
    # Con la migracion, el producto del evento; antes, ni oferta ni producto.
    assert ROWS["execution_context"]["flow_engine_audience_product_ref"] == ALLY.funnel_ref
    assert ROWS["execution_context"]["offer_code"] is None
    assert "flow_engine_audience_product_ref" not in ROWS["execution_context_before_migration"]


def test_the_bridge_builds_the_audience_event_the_database_admits() -> None:
    event = ROWS["flow_event"]
    built = audience_event(
        AudienceFlow(flow_id="<FLOW>", version=event["version"], flow_key="aviso_a_mano", definition={},
                     product_ref=event["producto_ref"]),
        OccurrenceRow(occurrence_id="<OCCURRENCE>", flow_id="<FLOW>", state="liberando", form=event["tipo"]),
        ReleasedMember(contact_id="<CONTACT>", phone=event["destino"]),
        now=datetime(2026, 11, 17, 16, 1, tzinfo=timezone.utc),
        chatwoot_account_id=event["chatwoot"]["account_id"],
        chatwoot_inbox_id=event["chatwoot"]["inbox_id"],
    )
    # El evento de audiencia no tiene oferta: es la razon del bug, no un descuido del fixture.
    assert "oferta" not in built
    assert {**built, "momento": "<MOMENTO>"} == event


# -- (a) y el rojo: el despacho con el contexto de la base y el chequeo real ------------


def test_an_audience_of_a_declared_product_is_sent_through_the_real_recipient_check(tmp_path: Path) -> None:
    # La fila de la base tal cual; solo el telefono es el del contacto que tiene el doble de Chatwoot de la
    # bienvenida (el de la compra capturada), para que el envio llegue a aceptarse. El chequeo no mira el numero.
    row = {**copy.deepcopy(ROWS["execution_context"]),
           "buyer_phone": PURCHASE_ROWS["execution_context"]["buyer_phone"]}
    authority = _DatabaseContextAuthority(row)
    chatwoot = _RealWelcomeChatwoot()

    asyncio.run(_dispatcher(authority, chatwoot, tmp_path).dispatch_due(now=NOW))

    assert len(authority.context_reads) == 1
    [message] = _templates(chatwoot)
    assert message["template_params"]["processed_params"] == {"body": {"1": ALLY.product_name}}
    assert authority.acceptances
    assert authority.finalizations == []


def test_without_the_migration_the_audience_stops_like_in_production(tmp_path: Path) -> None:
    # El rojo de Maribel: la fila sin el producto del evento se frena con el motivo y los reintentos de hoy.
    authority = _DatabaseContextAuthority(ROWS["execution_context_before_migration"])
    chatwoot = _RealWelcomeChatwoot()

    with pytest.raises(SupabaseError, match="followup_recipient_not_allowlisted"):
        asyncio.run(_dispatcher(authority, chatwoot, tmp_path).dispatch_due(now=NOW))

    assert _templates(chatwoot) == []
    [finalization] = authority.finalizations
    assert finalization["reason_code"] == "pre_request_failed"
    assert finalization["next_attempt_at"] is not None


# -- (b) audiencia de un producto que la instancia no declara -----------------------------


@pytest.mark.parametrize(
    ("product_ref", "product_name"),
    [("otro-producto", ALLY.product_name), (ALLY.funnel_ref, "Otro producto")],
    ids=["otro producto_ref", "otro nombre"],
)
def test_an_audience_of_an_undeclared_product_closes_without_retry(
    tmp_path: Path, product_ref: str, product_name: str
) -> None:
    row = {**copy.deepcopy(ROWS["execution_context"]), "flow_engine_audience_product_ref": product_ref,
           "product_name": product_name}
    authority = _DatabaseContextAuthority(row)
    chatwoot = _RealWelcomeChatwoot()

    with pytest.raises(SupabaseError, match="followup_recipient_not_allowlisted"):
        asyncio.run(_dispatcher(authority, chatwoot, tmp_path).dispatch_due(now=NOW))

    assert _templates(chatwoot) == []
    [finalization] = authority.finalizations
    assert finalization["reason_code"] == "recipient_product_not_declared"
    assert finalization["next_attempt_at"] is None


# -- (c) los disparos por evento, igual que hoy ---------------------------------------------


@pytest.mark.parametrize(
    ("offer", "reason", "retries"),
    [("otra0001", "recipient_offer_not_declared", False), (None, "pre_request_failed", True)],
    ids=["oferta no aceptada", "sin oferta"],
)
def test_a_hotmart_purchase_keeps_the_offer_check(
    tmp_path: Path, offer: str | None, reason: str, retries: bool
) -> None:
    # La compra capturada de ATT1 (E-122): la columna nueva llega nula y manda la oferta.
    row = {**copy.deepcopy(PURCHASE_ROWS["execution_context"]), "offer_code": offer,
           "flow_engine_audience_product_ref": None}
    authority = _DatabaseContextAuthority(row)
    chatwoot = _RealWelcomeChatwoot()

    with pytest.raises(SupabaseError, match="followup_recipient_not_allowlisted"):
        asyncio.run(_dispatcher(authority, chatwoot, tmp_path).dispatch_due(now=NOW))

    assert _templates(chatwoot) == []
    [finalization] = authority.finalizations
    assert finalization["reason_code"] == reason
    assert (finalization["next_attempt_at"] is not None) is retries


def test_a_hotmart_purchase_with_its_accepted_offer_still_goes_out(tmp_path: Path) -> None:
    row = {**copy.deepcopy(PURCHASE_ROWS["execution_context"]), "flow_engine_audience_product_ref": None}
    authority = _DatabaseContextAuthority(row)
    chatwoot = _RealWelcomeChatwoot()

    asyncio.run(_dispatcher(authority, chatwoot, tmp_path).dispatch_due(now=NOW))

    assert len(_templates(chatwoot)) == 1
    assert authority.finalizations == []


# -- El chequeo, de a una regla ---------------------------------------------------------------


def _context(**overrides: Any) -> FollowupExecutionContext:
    base: dict[str, Any] = dict(
        action_id="a", action_type="first_contact_review", step_key="first_contact", recovery_case_id="r",
        contact_id="c", source_event_id="s", buyer_name="Ana", buyer_email=None, buyer_phone="12025550401",
        product_name=ALLY.product_name, offer_code=None, current_goal=None, lead_stage="new",
        audience_product_ref=ALLY.funnel_ref,
    )
    return FollowupExecutionContext(**{**base, **overrides})


def _authorized(context: FollowupExecutionContext, **kwargs: Any) -> bool:
    return worker_module._is_authorized_followup_recipient(
        execution_context=context, allowed_jid=None, commercial_ally_config=kwargs.pop("config", ALLY),
        portable_recipient_enabled=kwargs.pop("portable", True), **kwargs)


def test_the_audience_rule_needs_the_portable_boundary_and_a_phone() -> None:
    assert _authorized(_context()) is True
    assert _authorized(_context(), portable=False) is False
    assert _authorized(_context(buyer_phone=None)) is False
    assert _authorized(_context(), config=None) is False
    # Sin la columna (una base sin la migracion) no es una audiencia: sin oferta, se frena.
    assert _authorized(_context(audience_product_ref=None)) is False


def test_a_case_with_an_offer_is_never_an_audience() -> None:
    # Con oferta manda la oferta, aunque la fila trajera un producto.
    assert _authorized(_context(offer_code="otra0001")) is False
    assert _authorized(_context(offer_code=ALLY.offer_code)) is True


def test_an_audience_of_the_second_product_of_the_instance_is_authorized() -> None:
    manifest = _manifest()
    routes = _routes(manifest)
    principal = manifest.to_commercial_ally_config()
    second = routes.by_key("att2")
    assert second is not None
    context = _context(product_name=second.config.product_name, audience_product_ref=second.config.funnel_ref)

    assert _authorized(context, config=principal, product_routes=routes) is True
    # Sin las rutas, el segundo producto no es de la instancia.
    assert _authorized(context, config=principal) is False


def test_the_new_reason_is_only_for_an_audience() -> None:
    reason = worker_module._recipient_audience_product_not_declared
    assert reason(execution_context=_context(), commercial_ally_config=ALLY, portable_recipient_enabled=True)
    assert not reason(execution_context=_context(audience_product_ref=None), commercial_ally_config=ALLY,
                      portable_recipient_enabled=True)
    assert not reason(execution_context=_context(offer_code="otra0001"), commercial_ally_config=ALLY,
                      portable_recipient_enabled=True)
    assert not reason(execution_context=_context(), commercial_ally_config=ALLY, portable_recipient_enabled=False)
    assert worker_module.RECIPIENT_PRODUCT_NOT_DECLARED == "recipient_product_not_declared"
