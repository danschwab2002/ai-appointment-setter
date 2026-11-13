"""E-483: los toques siguientes nativos por el despachador durable entero (``DurableDispatcher.dispatch_due``).

Hasta aca native_touch_2 y native_touch_3 (el de la rama de C3, el del cupon propio de C2) se probaban
armando la composicion (``_compose_approved_template_proposal``). Aca corre ``dispatch_due`` de punta a punta:
el claim, la evidencia de Chatwoot del toque anterior, la reevaluacion, la reserva, el arranque del motor, el
envio por Chatwoot y la aceptacion que lo asienta.

Datos:

* El contexto de cada paso es el que da la base (PGlite): el toque 2 del carrito nativo de tres toques de ATT1
  y el toque 2 de la rama del banco del pago rechazado completo
  (``tests/fixtures/flow_engine/native_legacy_double_case_touches_rows.json``, de
  ``validate_e483_native_legacy_double_case_touches.mjs --emit``) y Z con el cupon 2
  (``native_personal_coupon_rows.json``, de ``validate_e483_native_personal_coupon.mjs --emit``).
* Las plantillas: A2 y Z del carrito de ATT1 aprobadas el 18/10
  (``chatwoot_message_templates_inbox_11_carrito_20261018.json``) y la de Maribel
  (``chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json``, reconstruida de lo que se
  subio a Meta), servidas bajo el inbox 11 del doble como en ``test_flow_engine_native_dispatch.py``.
* Los dobles de la base y de Chatwoot son los de ``test_flow_engine_native_dispatch.py`` y los del cupon propio
  de ``test_e483_native_personal_coupon.py``. La evidencia de Chatwoot del toque anterior (la conversacion 200,
  el mensaje 888 aceptado, sin respuesta ni humano despues) es la de ``test_cart_followups.py``, el molde de A2 y
  Z del legado: la lectura de la conversacion es la misma para el legado y el nativo y la prueba
  ``test_chatwoot`` / ``test_cart_followups``. Aca se cuenta que el despachador la pidio.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
from test_cart_followups import EVIDENCE
from test_e483_native_personal_coupon import (
    BARE,
    CODE_2,
    ISSUANCE_ID,
    NAME,
    PRODUCT,
    _CouponAuthority,
    _native,
)
from test_e483_native_personal_coupon import _body as _carrito_body
from test_flow_engine_native_dispatch import _dispatcher, _NativeAuthority, _NativeChatwoot, _run, _templates

from bridge.supabase import FlowEngineStepContext
from bridge.worker import DurableDispatcher
from bridge.worker import NATIVE_MULTITOUCH_DISABLED

FIXTURES = Path(__file__).parent / "fixtures"
ROWS = json.loads((FIXTURES / "flow_engine" / "native_legacy_double_case_touches_rows.json").read_text(encoding="utf-8"))
CARRITO = json.loads((FIXTURES / "chatwoot_message_templates_inbox_11_carrito_20261018.json").read_text(
    encoding="utf-8"))["message_templates"]
MARIBEL = json.loads((FIXTURES / "chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json").read_text(
    encoding="utf-8"))["message_templates"]
FLOW_ID = "0b3c5f00-0e30-4000-8000-0000000e4830"
A2 = "att1_carrito_dudas_01"
Z = "att1_ultima_renovacion_01"
FOLLOW_BANCO = "maribel_retomar_proceso_01"


def _step_of(row: dict[str, Any]) -> FlowEngineStepContext:
    """La fila de flow_engine_execution_context_v1 como la lee el cliente de Supabase."""
    return FlowEngineStepContext(
        flow_id=FLOW_ID, version=row["version"], product_ref=row["product_ref"], flow_key=row["flow_key"],
        step_key=row["step_key"], send_ordinal=row["send_ordinal"], template_name=row["template_name"],
        template_language=row["template_language"], template_variables=tuple(row["template_variables"]),
        template_button=row["template_button"], legacy_dispatch_ok=row["legacy_dispatch_ok"],
        entry_permission=row["entry_permission"], template_branches=row["template_branches"],
    )


class _FollowingTouch(_NativeAuthority):
    """Un toque siguiente nativo vencido: el no_reply_review anclado al mensaje aceptado del toque anterior."""

    def __init__(self, row: dict[str, Any]) -> None:
        super().__init__(step=_step_of(row))
        step_key = row["step_key"]
        self.action = replace(self.action, action_type="no_reply_review", step_key=step_key,
                              anchor_type="message", anchor_subject_internal_id="message-touch-1")
        self.context = replace(self.context, action_type="no_reply_review", step_key=step_key)


class _FollowingChatwoot(_NativeChatwoot):
    """El inbox 11 del despachador nativo, con el seguimiento como lo devuelve Chatwoot: del AgentBot.

    Es el molde de ``test_payment_slip_reminders._ReminderChatwoot`` (medido el 2026-10-19: los salientes del
    inbox 11 vuelven con sender_type AgentBot); sin eso el envio de un no_reply_review queda inconcluso.
    """

    def handler(self, request: httpx.Request) -> httpx.Response:
        response = super().handler(request)
        body = json.loads(request.content) if request.content else None
        if (request.method == "POST" and request.url.path.endswith("/conversations/200/messages")
                and body is not None and "recovery_followup_hash" in body.get("content_attributes", {})):
            payload = response.json()
            payload["sender"] = {"type": "agent_bot", "id": 99}
            return httpx.Response(200, json=payload)
        return response


def _catalog_chatwoot(*templates: str) -> _NativeChatwoot:
    chatwoot = _FollowingChatwoot()
    chatwoot.catalog.extend(copy.deepcopy([t for t in (*CARRITO, *MARIBEL) if t["name"] in templates]))
    return chatwoot


def _filled(name: str, values: list[str]) -> str:
    [template] = [t for t in (*CARRITO, *MARIBEL) if t["name"] == name]
    [body] = [c["text"] for c in template["components"] if c["type"] == "BODY"]
    for position, value in enumerate(values, start=1):
        body = body.replace("{{%d}}" % position, value)
    return body


def _with_evidence(dispatcher: DurableDispatcher) -> list[str]:
    """La evidencia del toque anterior (la de test_cart_followups), y la lista de las acciones que la pidieron."""
    reads: list[str] = []

    async def evidence(*, action: Any, **_: object) -> dict[str, object]:
        reads.append(action.action_id)
        return dict(EVIDENCE)

    dispatcher._load_chatwoot_evidence = evidence  # type: ignore[method-assign]
    return reads


def _dispatch(authority: _NativeAuthority, chatwoot: _NativeChatwoot, tmp_path: Path, *, multitouch: bool) -> list[str]:
    dispatcher = _dispatcher(authority, chatwoot, tmp_path)
    dispatcher._native_multitouch_enabled = multitouch
    reads = _with_evidence(dispatcher)
    _run(dispatcher)
    return reads


def test_the_rows_are_the_following_touches_of_the_base() -> None:
    assert (ROWS["carrito_toque_2_context"]["step_key"], ROWS["carrito_toque_2_context"]["template_name"]) == (
        "native_touch_2", A2)
    assert ROWS["pago_toque_2_context"]["rama"] == 3
    assert (ROWS["pago_toque_2_context"]["step_key"], ROWS["pago_toque_2_context"]["template_name"]) == (
        "native_touch_2", FOLLOW_BANCO)


def test_a2_goes_out_by_chatwoot_with_its_template_and_variables_and_is_recorded(tmp_path: Path) -> None:
    authority = _FollowingTouch(ROWS["carrito_toque_2_context"])
    chatwoot = _catalog_chatwoot(A2)

    reads = _dispatch(authority, chatwoot, tmp_path, multitouch=True)

    values = [NAME, PRODUCT]
    [message] = _templates(chatwoot)
    assert message["content"] == _filled(A2, values)
    assert message["template_params"] == {
        "name": A2, "category": "MARKETING", "language": "es_MX",
        "processed_params": {"body": {"1": NAME, "2": PRODUCT}},
    }
    # La evidencia del toque anterior se leyo y el envio arranco por el motor, no por el legado.
    assert reads and set(reads) == {authority.action.action_id}
    assert "request_started" in authority.engine_calls and "request_started" not in authority.events
    # Asentado: la aceptacion de Chatwoot, sin ninguna finalizacion de falla.
    [accepted] = authority.acceptances
    assert accepted["action_id"] == authority.action.action_id
    assert authority.finalizations == []


def test_without_the_flag_a2_closes_with_its_reason_and_chatwoot_gets_nothing(tmp_path: Path) -> None:
    authority = _FollowingTouch(ROWS["carrito_toque_2_context"])
    chatwoot = _catalog_chatwoot(A2)

    _dispatch(authority, chatwoot, tmp_path, multitouch=False)

    assert [request for request in chatwoot.requests if request[0] == "POST"] == []
    assert "request_started" not in authority.engine_calls
    assert authority.acceptances == []
    [closed] = authority.finalizations
    assert closed["reason_code"] == NATIVE_MULTITOUCH_DISABLED
    assert closed["next_attempt_at"] is None


def test_z_with_its_own_coupon_goes_out_with_the_button_and_the_code(tmp_path: Path) -> None:
    authority = _CouponAuthority(step_key="native_touch_3")
    chatwoot = _catalog_chatwoot(*(template["name"] for template in CARRITO))
    dispatcher = _native(authority, chatwoot, tmp_path)
    reads = _with_evidence(dispatcher)

    _run(dispatcher)

    assert reads

    values = [NAME, PRODUCT, "mañana a las 11:00 a. m."]
    [message] = _templates(chatwoot)
    assert message["content"] == _carrito_body(Z, values)
    assert message["template_params"] == {
        "name": Z, "category": "MARKETING", "language": "es_MX",
        "processed_params": {"body": {str(i): v for i, v in enumerate(values, start=1)},
                             "buttons": [{"type": "url", "parameter": f"{BARE}&offDiscount={CODE_2}"}]},
    }
    assert [event for event in authority.events if event in ("step_coupon", "reserve_link", "authorize_link")] == [
        "step_coupon", "reserve_link", "authorize_link"]
    assert authority.issuance_finalizations[0]["issuance_id"] == ISSUANCE_ID
    assert authority.issuance_finalizations[0]["status"] == "accepted_by_chatwoot"
    [accepted] = authority.acceptances
    assert accepted["action_id"] == authority.action.action_id


def test_the_second_touch_of_the_c3_branch_goes_out_with_the_template_of_its_branch(tmp_path: Path) -> None:
    authority = _FollowingTouch(ROWS["pago_toque_2_context"])
    # El catalogo tiene tambien el seguimiento de la otra rama (A2 de ATT1, el de las ramas con cupon).
    chatwoot = _catalog_chatwoot(FOLLOW_BANCO, A2)

    _dispatch(authority, chatwoot, tmp_path, multitouch=True)

    [message] = _templates(chatwoot)
    assert message["template_params"]["name"] == FOLLOW_BANCO
    assert message["template_params"]["processed_params"] == {"body": {"1": NAME, "2": PRODUCT}}
    assert message["content"] == _filled(FOLLOW_BANCO, [NAME, PRODUCT])
    # El toque siguiente no elige la rama otra vez: no pide los datos del rechazo ni registra una rama.
    assert not hasattr(authority, "get_flow_engine_native_payment_failure_context")
    [accepted] = authority.acceptances
    assert accepted["action_id"] == authority.action.action_id
