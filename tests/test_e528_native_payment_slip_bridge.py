"""E-528 (C4 del mapa «el AE maneja todos los flujos», P25 de M16): la ficha de OXXO nativa en el bridge.

C1 y C3 de un recorrido nativo de la ficha salen con la plantilla de su version fijada y la ficha mas reciente
del caso (``get_flow_engine_native_payment_slip_context_v1``, 20261113200000), solo con
``FLOW_ENGINE_NATIVE_PAYMENT_SLIP_ENABLED``; C3 sale el dia del vencimiento desde las 09:00 de la persona y no
despues (``ficha_vencida``). La ficha impresa va primero a la admision nativa si hay un flujo nativo de la ficha
(la precedencia nativa, D-P25-6); sin el, como hoy.

Bordes capturados: la ficha 3 de ``tests/fixtures/hotmart_payment_slip_printed_att1_20261018.json`` (ATT1,
2026-10-18, anonimizada en la base) y las plantillas de la bandeja 11 de
``tests/fixtures/chatwoot_message_templates_inbox_11_ficha_recordatorios_20261019.json`` (2026-10-19).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from test_e528_native_payment_slip import C1, C3, SLIP_TEMPLATES, _runtime


def test_the_settings_flag_is_off_by_default_and_reaches_the_runtime() -> None:
    from bridge.app import Settings, instance_runtime_from

    assert Settings.__dataclass_fields__["flow_engine_native_payment_slip_enabled"].default is False
    assert "flow_engine_native_payment_slip_enabled" in instance_runtime_from.__code__.co_names
    assert _runtime(True).native_payment_slip is True


# -- El bridge: C1 y C3 de la ficha nativa -----------------------------------------------------------
#
# El molde es el de la ficha del legado (test_payment_slip_first_touch.py: la ficha 3 capturada de ATT1,
# CASHPAYMENT de Mexico generada el 12/10 a las 21:56 de CDMX, vence el 15/10) y el del paso nativo
# (test_flow_engine_native_dispatch.py). La ficha la lee get_flow_engine_native_payment_slip_context_v1
# (20261113200000), nunca la lectura del legado.


def _native_slip_world(tmp_path: Path, *, following: bool, clock: str, slip_flag: bool = True,
                       multitouch: bool = False):
    import asyncio
    from dataclasses import replace

    import test_payment_slip_first_touch as tps
    from test_flow_engine_native_dispatch import _NativeAuthority, _NativeChatwoot, _step

    from bridge.messaging import ChatwootMessageSender, FinalMetaEffectGate
    from bridge.worker import DurableDispatcher

    step_key = "native_touch_2" if following else "first_contact"
    template = C3 if following else C1
    step = _step(step_key=step_key, send_ordinal=2 if following else 1, flow_key="ficha_oxxo",
                 template_name=template["plantilla"], template_variables=tuple(template["variables"]),
                 template_button={"link": "ficha"}, entry_permission="formulario")

    class _SlipNativeAuthority(_NativeAuthority):
        def __init__(self) -> None:
            super().__init__(step=step, offer_code="2uafw5bg", product_name="Alimenta tu Tiroides")
            self.action = replace(self.action, step_key=step_key,
                                  action_type="no_reply_review" if following else "first_contact_review",
                                  anchor_type="message" if following else self.action.anchor_type,
                                  due_at=clock, anchor_observed_at="2026-10-13T03:56:18+00:00")
            self.context = replace(self.context, step_key=step_key,
                                   action_type="no_reply_review" if following else "first_contact_review")
            self.native_slip_calls: list[dict[str, object]] = []
            self.legacy_slip_calls = 0

        async def get_flow_engine_native_payment_slip_context_v1(self, **kwargs: object) -> object:
            self.native_slip_calls.append(kwargs)
            return tps._context(tps.MX_SLIP)

        async def get_portable_payment_slip_send_context(self, **kwargs: object) -> object:
            self.legacy_slip_calls += 1
            raise AssertionError("la ficha nativa no lee la ficha del legado")

    authority = _SlipNativeAuthority()
    chatwoot = _NativeChatwoot()
    slips = json.loads(SLIP_TEMPLATES.read_text(encoding="utf-8"))["message_templates"]
    chatwoot.catalog.extend(copy.deepcopy(slips))
    client = chatwoot.client()
    dispatcher = DurableDispatcher(
        supabase=authority,  # type: ignore[arg-type]
        worker_id="att1-dispatcher",
        chatwoot=client,
        chatwoot_account_id=tps.ACCOUNT_ID,
        chatwoot_inbox_id=tps.INBOX_ID,
        sender=ChatwootMessageSender(chatwoot=client, inbox_id=tps.INBOX_ID, allowed_jid=None,
                                     dynamic_recipient_enabled=True, template=tps.TEMPLATE),
        allowed_jid=None,
        commercial_ally_config=tps.ALLY,
        portable_recipient_enabled=True,
        pilot_boundary=tps.BOUNDARY,
        clock=lambda: clock,
        final_meta_effect_gate=FinalMetaEffectGate(enabled=True, evidence_dir=tmp_path / "meta-effects"),
        waba_template=tps.TEMPLATE,
        approved_template_direct=True,
        flow_engine_enabled=True,
        native_multitouch_enabled=multitouch,
        native_payment_slip=tps.SLIP_CONFIG if slip_flag else None,
    )
    if following:
        # Como test_e483_native_multitouch: la evidencia de Chatwoot de un no_reply_review es el camino
        # generico de A2 y Z, que no cambia; aca se mira la composicion de C3.
        composition = asyncio.run(dispatcher._compose_approved_template_proposal(
            action=authority.action, execution_context=authority.context, attempt=None, step_template=step))
        return authority, composition
    asyncio.run(dispatcher.dispatch_due(now=clock))
    sent = [post for post in chatwoot.posts("/conversations/200/messages") if "template_params" in post]
    return authority, sent


def test_c1_of_the_native_slip_goes_out_with_its_pinned_template_and_the_slip_of_the_case(tmp_path: Path) -> None:
    authority, sent = _native_slip_world(tmp_path, following=False, clock="2026-10-13T04:56:30+00:00")
    [message] = sent
    params = message["template_params"]
    assert params["name"] == "att1_ficha_efectivo_01" and params["category"] == "UTILITY"
    # medio_de_pago es la tienda (OXXO) y vencimiento el dia que vence, en la zona de Mexico.
    assert list(params["processed_params"]["body"].values())[1:] == ["Alimenta tu Tiroides", "OXXO", "jueves 15 de octubre"]
    assert params["processed_params"]["buttons"] == [{"type": "url", "parameter": "f" * 48}]
    assert len(authority.native_slip_calls) == 1 and authority.legacy_slip_calls == 0
    assert "request_started" in authority.engine_calls


def test_without_the_flag_the_native_slip_closes_and_reads_nothing(tmp_path: Path) -> None:
    from bridge.worker import FLOW_ENGINE_STEP_UNSUPPORTED

    # Sin FLOW_ENGINE_NATIVE_PAYMENT_SLIP_ENABLED, el camino de antes: el paso nativo generico no arma el link a
    # la ficha y cierra sin salir; nunca con la plantilla del manifiesto ni la lectura del legado.
    authority, sent = _native_slip_world(tmp_path, following=False, clock="2026-10-13T04:56:30+00:00",
                                         slip_flag=False)
    assert sent == [] and authority.native_slip_calls == [] and authority.legacy_slip_calls == 0
    assert authority.finalizations[-1]["reason_code"] == FLOW_ENGINE_STEP_UNSUPPORTED


def test_c3_waits_for_the_due_day_at_nine_and_does_not_need_the_multitouch_flag(tmp_path: Path) -> None:
    from datetime import timedelta

    # La vispera a la noche (14/10 22:00 de CDMX): espera hasta el 15/10 09:00, once horas.
    _, early = _native_slip_world(tmp_path, following=True, clock="2026-10-15T04:00:00+00:00")
    assert early.proposal is None and early.failure_reason == "payment_slip_step_not_due"
    assert early.retry_after == timedelta(hours=11)
    # El 15/10 a las 09:30 de CDMX sale, con FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED apagado.
    authority, due = _native_slip_world(tmp_path, following=True, clock="2026-10-15T15:30:00+00:00")
    assert due.failure_reason is None and due.template_name == "att1_ficha_vence_hoy_01"
    assert list(due.template_params["processed_params"]["body"].values())[1:] == ["OXXO", "Alimenta tu Tiroides"]
    assert due.template_params["processed_params"]["buttons"] == [{"type": "url", "parameter": "f" * 48}]
    assert len(authority.native_slip_calls) == 1


def test_c3_does_not_go_out_after_the_due_day(tmp_path: Path) -> None:
    # ficha_vencida: el corte del codigo, hoy despues del vencimiento (D-P25-3).
    _, late = _native_slip_world(tmp_path, following=True, clock="2026-10-16T15:30:00+00:00")
    assert late.proposal is None and late.retryable is False
    assert late.failure_reason in ("payment_slip_expired", "payment_slip_step_late")


# -- El resolver: la ficha va primero a la admision nativa (precedencia nativa, D-P25-6) ---------------
#
# La base del resolver es la de test_flow_engine_native_payment_failure.py; la ficha, la 3 capturada de ATT1
# con el producto y la oferta del runtime de prueba. Las filas de la admision son las que da la base
# (validate_e528_native_payment_slip.mjs): motor_arranco, y ya_en_curso para la segunda ficha.


def _resolve_slip(native_rows: list[Any] | None) -> Any:
    import asyncio

    import test_flow_engine_native_payment_failure as tnp
    import test_payment_slip_first_touch as tps
    from test_flow_engine_resolution import _runtime

    from bridge.hotmart import EVENT_PURCHASE_BILLET_PRINTED
    from bridge.resolution import resolve_event

    payload = copy.deepcopy(tps.MX_SLIP)
    payload["data"]["product"] = {**payload["data"]["product"], "id": tnp.CONFIG.hotmart_product_id,
                                  "name": tnp.CONFIG.product_name}
    payload["data"]["purchase"]["offer"] = {"code": tnp.CONFIG.offer_code}
    base = tnp._NativeBase(list(native_rows or []))
    client = base.client()
    runtime = _runtime(base, client)
    error = None
    try:
        asyncio.run(resolve_event(
            webhook_event_id=tnp.EVENT_ID, payload=payload, supabase=client, policy_key="att1-ficha",
            policy_version=1, allowed_jid=None, chatwoot_account_id=tnp.CONFIG.chatwoot_account_id,
            chatwoot_inbox_id=tnp.CONFIG.chatwoot_inbox_id, pilot_boundary=tnp.BOUNDARY,
            commercial_ally_config=tnp.CONFIG, event_type=EVENT_PURCHASE_BILLET_PRINTED,
            flow_engine=runtime if native_rows is not None else None,
            native_payment_slip_product_ref=tnp.CONFIG.funnel_ref if native_rows is not None else None,
        ))
    except Exception as caught:  # noqa: BLE001 - la prueba mira el motivo
        error = caught
    return base, error


def test_a_started_native_slip_owns_the_event_and_the_legacy_does_not_plan() -> None:
    from test_flow_engine_native_cart import _answer, _row

    base, error = _resolve_slip([_answer(_row("motor_arranco"))])
    assert error is None
    names = base.names()
    assert "admit_flow_engine_event_v1" in names
    assert not any(name.startswith("plan_") for name in names)
    event = base.body_of("admit_flow_engine_event_v1")["p_flow_event"]
    assert (event["tipo"], event["fuente"]) == ("ficha_impresa", "hotmart")
    assert [patch["processing_status"] for patch in base.status_patches] == ["processed"]


def test_a_second_slip_is_added_to_the_native_run_and_the_event_ends_processed() -> None:
    from test_flow_engine_native_cart import _answer, _row

    base, error = _resolve_slip([_answer(_row("motor_no_arranca", "ya_en_curso"))])
    assert str(error) == "flow_engine_payment_failure_absorbed"
    assert not any(name.startswith("plan_") for name in base.names())
    assert [patch["processing_status"] for patch in base.status_patches] == ["processed"]


def test_without_a_native_slip_flow_the_slip_goes_as_today() -> None:
    base, _ = _resolve_slip(None)
    assert "admit_flow_engine_event_v1" not in base.names()
