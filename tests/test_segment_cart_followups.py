"""El cupon de la campana frena lo que el producto manda solo (E-339, D6 de E-338).

Decision de Dan del 2026-11-06: quien viene por una campana con cupon no recibe
las plantillas del carrito ni el seguimiento con cupon: tendria dos cupones
distintos y Hotmart toma uno solo. A1 le llega igual, sin el 10 %, con la plantilla
de siempre, y A2 o Z frenados cierran el caso para que un carrito nuevo arranque
(``close_recovery_case_segment_coupon_v1``, probada contra la base en
``validate_recovery_case_segment_coupon_close.mjs``). Contrato:
``segment-knowledge-v1``.

Lo medido en el codigo: ``TIROIDES10`` no va en A2 ni en Z. A2 promete el 10 % sin
link, Z lleva el cupon 2 personal y ``TIROIDES10`` es del seguimiento a 24 h
(``ConversationFollowupSweeper``). Las tres puertas se frenan, mas el boton «Quiero
mi 10 %», que sin esto adelantaria un Z que no sale y dejaria el boton sin respuesta.

Datos: A2 y Z son los de ``test_cart_followups`` (catalogo capturado del inbox 11);
el seguimiento es la 21 de ATT1 capturada (``test_followup_stops``); el boton es el
webhook de la conversacion 200 del inbox 9 (``test_cart_followups_wiring``), con la
etiqueta en ``conversation.labels``. El segmento es el FICTICIO de
``tests/fixtures/segments`` (cupon ``PRUEBA11``), con la vigencia reescrita para no
depender del reloj. **Deuda:** no hay captura de un webhook ni de una conversacion
con la etiqueta de una campana real.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from datetime import UTC, datetime, timedelta
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_cart_discount_first_touch import _app as _cart_app
from test_cart_discount_first_touch import _dispatcher as _first_touch_dispatcher
from test_cart_discount_first_touch import CONFIG, _CartAuthority, _the_old_template_went_out
from test_cart_followups import (
    A2,
    Z,
    _dispatcher,
    _nothing_went_out,
    _ReminderChatwoot,
    _StepAuthority,
)
from test_cart_discount_first_touch import CARRITO_CATALOG, _Chatwoot
from test_cart_followups_wiring import _with_followups
from test_durable_dispatcher_approved_template import (
    ACCOUNT_ID,
    CATALOG,
    FINAL_NOW,
    INBOX_ID,
    NOW,
)
from test_followup_discount import (
    ATT1_AGENT_LINK,
    ATT1_LEAD_REPLIED_AT,
    HOUR,
    _att1_chatwoot,
    _att1_sweeper,
    _FakeChatwoot,
    _FakeSupabase,
)
from test_flow_engine_native_silence_sweep import _changed, _silence
from test_flow_engine_ready import _app as _ready_app
from test_first_touch_active_conversation import (
    CONTACT,
    INBOX,
    _listing_at_sent_time,
    _MaribelInbox,
)
from test_flow_engine_ready import _ReadyAuthority
from test_followup_discount_wiring import _chatwoot_client, _johanna_followup_settings
from test_followup_stops import _run_c3_with
from test_payment_slip_reminders import _ReminderAuthority
from test_recovery_coupon_reply import _rpc
from test_segment_bridge import FUTURE, PAST
from test_segment_link_coupon import LABEL, _process, _settings
from test_whatsapp_inbound_equivalence import (
    FORM_ID,
    PAYMENT_LINK_PROPOSAL,
    _PaymentLinkSupabase,
    _text_webhook,
)

from bridge import app as app_module
from bridge.app import create_app
from bridge.cart_followups import CART_LAST_TOUCH_STEP, CART_SECOND_TOUCH_STEP
from bridge import automation_expert_read as reader
from bridge.chatwoot import CanonicalConversationSnapshot, ChatwootProtocolError, ContactConversation
from bridge.event_log import LOGGED_INFO_EVENTS
from bridge.payment_link_coupon import (
    SEGMENT_COUPON_ACTIVE,
    SegmentCoupon,
    segment_coupon_for_labels,
)
from bridge.messaging import FirstTouchRecipient
from bridge.payment_slip_reminders import PAYMENT_SLIP_DUE_DAY_STEP
from bridge.supabase import ChatwootAuthorityContext, SupabaseError
from bridge.worker import (
    SEGMENT_COUPON_CLOSE_UNAVAILABLE,
    SEGMENT_LABELS_RETRIES,
    SEGMENT_LABELS_UNAVAILABLE,
)

BUTTON = "Quiero mi 10 %"
STEPS = [CART_SECOND_TOUCH_STEP, CART_LAST_TOUCH_STEP]
TEMPLATE_OF = {CART_SECOND_TOUCH_STEP: A2, CART_LAST_TOUCH_STEP: Z}


# ------------------------------------------------------------- la funcion pura


def test_the_coupon_comes_from_the_labels_with_the_selection_of_a_turn(tmp_path: Path) -> None:
    catalog = _settings(tmp_path).agent_segment_catalog
    now = _now()

    assert segment_coupon_for_labels(catalog, [LABEL, "otra"], now=now) == SegmentCoupon(
        LABEL, "PRUEBA11", None
    )
    assert segment_coupon_for_labels(catalog, ["otra"], now=now) is None
    assert segment_coupon_for_labels(catalog, [], now=now) is None


def test_without_a_catalog_the_labels_are_not_even_looked_at() -> None:
    assert segment_coupon_for_labels(None, object(), now=_now()) is None


@pytest.mark.parametrize(
    "labels", [None, 7, "campana-de-prueba", {"a": 1}, [7, None, ""], [{"a": 1}]]
)
def test_labels_with_another_shape_count_as_none_and_do_not_raise(
    tmp_path: Path, labels: object
) -> None:
    catalog = _settings(tmp_path).agent_segment_catalog

    assert segment_coupon_for_labels(catalog, labels, now=_now()) is None


def _now() -> datetime:
    return datetime.fromisoformat(NOW)


# ------------------------------------------------------- A2 y Z por el despachador


class _ClosingAuthority(_StepAuthority):
    """A2 o Z, con la RPC del cierre del caso."""

    def __init__(
        self, step_key: str, *, close: str | Exception = "closed", attempt_number: int = 1
    ) -> None:
        super().__init__(step_key, attempt_number=attempt_number)
        self.close = close
        self.reads: list[str] = []
        self.closes: list[dict[str, object]] = []

    async def close_recovery_case_segment_coupon(self, **kwargs: object) -> str:
        self.closes.append(kwargs)
        self.events.append("close_case")
        if isinstance(self.close, Exception):
            raise self.close
        return self.close


def _contact_reads(
    dispatcher: Any,
    contact: list[str] | Exception,
    *,
    second: list[str] | None = None,
    no_contact: bool = False,
    truncated: bool = False,
) -> list[str]:
    """El contacto y sus conversaciones, con la forma de ``ContactConversation``.

    ``contact``: las etiquetas de la mas activa; ``second``, las de otra menos
    activa; ``no_contact``: la persona no tiene contacto en Chatwoot;
    ``truncated``: la lista vino cortada.
    """

    calls: list[str] = []

    async def recipient(*, phone: str) -> FirstTouchRecipient:
        calls.append("contacto")
        if no_contact:
            return FirstTouchRecipient(wa_id="15550000000")
        return FirstTouchRecipient(wa_id="15550000000", contact_id=7, source_id="15550000000")

    async def conversations(**kwargs: Any) -> list[ContactConversation]:
        calls.append("conversaciones")
        if isinstance(contact, Exception):
            raise contact
        if truncated:
            kwargs["on_truncated"]()
        return [
            ContactConversation(
                conversation_id=150 - index,
                status="resolved",
                last_activity_at=1_790_000_000 - index,
                labels=tuple(labels),
                assignee_present=False,
                team_present=False,
                last_inbound_at=None,
            )
            for index, labels in enumerate([contact, *([second] if second is not None else [])])
        ]

    dispatcher._whatsapp_equivalence_enabled = True
    dispatcher._sender.resolve_first_touch_recipient = recipient
    dispatcher._chatwoot.list_contact_conversations = conversations
    return calls


def _dispatch(
    step_key: str,
    tmp_path: Path,
    *,
    labels: tuple[str, ...],
    catalog: object | None,
    close: str | Exception = "closed",
    contact: list[str] | Exception | None = None,
    attempt_number: int = 1,
    declared_payment_at: int | None = None,
    **reads: Any,
) -> tuple[_ReminderChatwoot, _ClosingAuthority]:
    """A2 o Z con la lectura canonica real: las etiquetas salen del snapshot.

    ``contact``: las etiquetas de las conversaciones del contacto.
    """

    authority = _ClosingAuthority(step_key, close=close, attempt_number=attempt_number)
    chatwoot = _ReminderChatwoot([*CATALOG, *CARRITO_CATALOG])
    dispatcher = _dispatcher(authority, chatwoot, tmp_path)
    dispatcher._segment_catalog = catalog
    del dispatcher._load_chatwoot_evidence  # type: ignore[method-assign]

    async def context(**_: object) -> ChatwootAuthorityContext:
        return ChatwootAuthorityContext(
            action_id=authority.action.action_id,
            action_type="no_reply_review",
            chatwoot_account_id=ACCOUNT_ID,
            external_conversation_id=200,
            expected_inbox_id=INBOX_ID,
            anchor_external_message_id=40,
        )

    async def jids(**_: object) -> list[str | None]:
        return [None]

    async def snapshot(**_: object) -> CanonicalConversationSnapshot:
        return CanonicalConversationSnapshot(
            conversation_id=200,
            inbox_id=INBOX_ID,
            status="open",
            can_reply=False,
            labels=labels,
            anchor_found=True,
            inbound_after_anchor=False,
            human_activity_after_anchor=False,
            checkpoint_message_id=888,
            checkpoint_created_at=1_790_000_000,
            declared_payment_at=declared_payment_at,
        )

    authority.get_followup_chatwoot_context = context  # type: ignore[method-assign]
    dispatcher._case_conversation_jids = jids  # type: ignore[method-assign]
    dispatcher._chatwoot.get_canonical_conversation_snapshot = snapshot  # type: ignore[union-attr,method-assign]
    authority.reads = (
        _contact_reads(dispatcher, contact, **reads) if contact is not None else []
    )
    asyncio.run(dispatcher.dispatch_due(now=NOW))
    return chatwoot, authority


@pytest.mark.parametrize("step_key", STEPS)
def test_a2_and_z_do_not_go_out_to_a_person_of_a_campaign_with_a_coupon(
    tmp_path: Path, step_key: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="bridge")

    chatwoot, authority = _dispatch(
        step_key,
        tmp_path,
        labels=(LABEL,),
        catalog=_settings(tmp_path).agent_segment_catalog,
    )

    assert chatwoot.posts("/messages") == [] and chatwoot.posts("/conversations") == []
    # La RPC cierra intento, secuencia y caso; no queda una finalizacion suelta.
    [close] = authority.closes
    assert close["action_id"] == authority.action.action_id
    assert close["attempt_id"] == authority.attempt.attempt_id
    assert close["lease_generation"] == authority.action.lease_generation
    assert authority.finalizations == []
    # Z no lee el cupon 2 ni emite un link.
    assert authority.coupon_ordinals == []
    assert "reserve_link" not in authority.events
    assert (
        f"segment_cart_followup_skipped key={LABEL} step={step_key} "
        f"action_id={authority.action.action_id}" in caplog.text
    )
    assert "PRUEBA11" not in caplog.text


def test_a_case_something_else_closed_first_is_not_an_error(tmp_path: Path) -> None:
    chatwoot, authority = _dispatch(
        CART_SECOND_TOUCH_STEP,
        tmp_path,
        labels=(LABEL,),
        catalog=_settings(tmp_path).agent_segment_catalog,
        close="case_not_open",
    )

    assert chatwoot.posts("/messages") == []
    assert len(authority.closes) == 1 and authority.finalizations == []


def test_without_the_close_rpc_nothing_goes_out_and_the_attempt_retries(tmp_path: Path) -> None:
    """Con el flag prendido y sin la migracion: A2 no sale a ciegas."""

    chatwoot, authority = _dispatch(
        CART_SECOND_TOUCH_STEP,
        tmp_path,
        labels=(LABEL,),
        catalog=_settings(tmp_path).agent_segment_catalog,
        close=SupabaseError("close_recovery_case_segment_coupon_v1_failed: HTTP 404"),
    )

    closed = _nothing_went_out(chatwoot, authority)
    assert closed["reason_code"] == SEGMENT_COUPON_CLOSE_UNAVAILABLE
    assert closed["next_attempt_at"] is not None


@pytest.mark.parametrize("step_key", STEPS)
def test_a_label_in_another_conversation_of_the_contact_also_stops_a2_and_z(
    tmp_path: Path, step_key: str
) -> None:
    """A1 mira todo el contacto: A2 y Z no pueden mirar menos."""

    chatwoot, authority = _dispatch(
        step_key,
        tmp_path,
        labels=(),
        catalog=_settings(tmp_path).agent_segment_catalog,
        contact=[LABEL],
    )

    assert chatwoot.posts("/messages") == [] and len(authority.closes) == 1


@pytest.mark.parametrize("failure", [ChatwootProtocolError("x"), httpx.ConnectError("x")])
def test_a2_does_not_go_out_blind_if_the_contact_cannot_be_read(
    tmp_path: Path, failure: Exception
) -> None:
    chatwoot, authority = _dispatch(
        CART_SECOND_TOUCH_STEP,
        tmp_path,
        labels=(),
        catalog=_settings(tmp_path).agent_segment_catalog,
        contact=failure,
    )

    closed = _nothing_went_out(chatwoot, authority)
    assert closed["reason_code"] == SEGMENT_LABELS_UNAVAILABLE
    assert closed["next_attempt_at"] is not None and authority.closes == []


@pytest.mark.parametrize(
    "labels, contact",
    [((LABEL,), None), ((), [LABEL])],
    ids=["en-la-conversacion-del-caso", "en-otra-del-contacto"],
)
def test_the_validity_uses_the_clock_of_the_dispatcher(
    tmp_path: Path, labels: tuple[str, ...], contact: list[str] | None
) -> None:
    """Vigente para el reloj del despachador y vencida para el de la maquina."""

    at = datetime.fromisoformat(FINAL_NOW)
    window = ((at - timedelta(days=1)).isoformat(), (at + timedelta(days=1)).isoformat())
    assert datetime.fromisoformat(window[1]) < datetime.now(UTC)

    chatwoot, authority = _dispatch(
        CART_SECOND_TOUCH_STEP,
        tmp_path,
        labels=labels,
        catalog=_settings(tmp_path, window=window).agent_segment_catalog,
        contact=contact,
    )

    assert chatwoot.posts("/messages") == [] and len(authority.closes) == 1


def test_a_mark_of_another_batch_does_not_stop_a2(tmp_path: Path) -> None:
    authority = _ClosingAuthority(CART_SECOND_TOUCH_STEP)
    chatwoot = _ReminderChatwoot([*CATALOG, *CARRITO_CATALOG])
    dispatcher = _dispatcher(authority, chatwoot, tmp_path)
    dispatcher._segment_coupon_after_anchor[authority.action.action_id] = LABEL

    asyncio.run(dispatcher.dispatch_due(now=NOW))

    assert len(chatwoot.posts("/conversations/200/messages")) == 1
    assert authority.closes == []


def test_a2_reads_every_conversation_of_the_contact(tmp_path: Path) -> None:
    chatwoot, authority = _dispatch(
        CART_SECOND_TOUCH_STEP,
        tmp_path,
        labels=(),
        catalog=_settings(tmp_path).agent_segment_catalog,
        contact=["otra-etiqueta"],
        second=[LABEL],
    )

    assert chatwoot.posts("/messages") == [] and len(authority.closes) == 1


def test_a2_goes_out_for_a_person_without_a_contact_and_reads_no_conversations(
    tmp_path: Path,
) -> None:
    chatwoot, authority = _dispatch(
        CART_SECOND_TOUCH_STEP,
        tmp_path,
        labels=(),
        catalog=_settings(tmp_path).agent_segment_catalog,
        contact=[LABEL],
        no_contact=True,
    )

    assert len(chatwoot.posts("/conversations/200/messages")) == 1
    assert authority.reads == ["contacto"] and authority.closes == []


def test_the_labels_wait_fifteen_minutes_on_the_last_retry(tmp_path: Path) -> None:
    chatwoot, authority = _dispatch(
        CART_LAST_TOUCH_STEP,
        tmp_path,
        labels=(),
        catalog=_settings(tmp_path).agent_segment_catalog,
        contact=httpx.ConnectError("x"),
        attempt_number=SEGMENT_LABELS_RETRIES,
    )

    closed = _nothing_went_out(chatwoot, authority)
    assert closed["reason_code"] == SEGMENT_LABELS_UNAVAILABLE
    assert datetime.fromisoformat(str(closed["next_attempt_at"])) == (
        datetime.fromisoformat(FINAL_NOW) + timedelta(minutes=15)
    )


@pytest.mark.parametrize("step_key", STEPS)
@pytest.mark.parametrize(
    "failure",
    [ChatwootProtocolError("invalid_contact_conversation"), httpx.ConnectError("x")],
)
def test_after_the_retries_a_step_without_evidence_goes_out(
    tmp_path: Path, step_key: str, failure: Exception
) -> None:
    """Sin evidencia de la etiqueta nadie pierde el paso ni queda el caso trabado."""

    chatwoot, authority = _dispatch(
        step_key,
        tmp_path,
        labels=(),
        catalog=_settings(tmp_path).agent_segment_catalog,
        contact=failure,
        attempt_number=SEGMENT_LABELS_RETRIES + 1,
    )

    assert len(chatwoot.posts("/conversations/200/messages")) == 1
    assert authority.closes == []


@pytest.mark.parametrize(
    "contact, stopped, outcome",
    [(["otra-etiqueta"], False, "sin_evidencia"), ([LABEL], True, "frenado")],
    ids=["sin-evidencia", "con-evidencia"],
)
def test_a_truncated_list_decides_with_what_was_read(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    contact: list[str],
    stopped: bool,
    outcome: str,
) -> None:
    caplog.set_level(logging.INFO, logger="bridge")

    chatwoot, authority = _dispatch(
        CART_SECOND_TOUCH_STEP,
        tmp_path,
        labels=(),
        catalog=_settings(tmp_path).agent_segment_catalog,
        contact=contact,
        truncated=True,
    )

    assert len(authority.closes) == (1 if stopped else 0)
    assert len(chatwoot.posts("/conversations/200/messages")) == (0 if stopped else 1)
    assert f"segment_contact_labels_truncated contact=7 read=1 outcome={outcome}" in caplog.text


def test_the_client_only_accepts_a_truncated_list_if_asked() -> None:
    listing = _listing_at_sent_time()
    template = listing["payload"][0]
    listing["payload"] = [{**copy.deepcopy(template), "id": 1000 + index} for index in range(20)]
    cut: list[bool] = []

    read = asyncio.run(
        _MaribelInbox(listing).client().list_contact_conversations(
            contact_id=CONTACT, inbox_id=INBOX, on_truncated=lambda: cut.append(True)
        )
    )

    assert cut == [True] and len(read) == 20


def test_a_purchase_by_another_offer_closes_the_case_before_the_brake(tmp_path: Path) -> None:
    """El orden inverso: la reevaluacion cancela y el bridge ni arma A2."""

    authority = _ClosingAuthority(CART_SECOND_TOUCH_STEP)
    reevaluate = authority.reevaluate_followup_action

    async def cancelled(**kwargs: object) -> Any:
        return replace(
            await reevaluate(**kwargs),
            decision="cancel",
            reason_code="purchase_detected_any_offer",
        )

    authority.reevaluate_followup_action = cancelled  # type: ignore[method-assign]
    chatwoot = _ReminderChatwoot([*CATALOG, *CARRITO_CATALOG])
    dispatcher = _dispatcher(authority, chatwoot, tmp_path)
    dispatcher._segment_catalog = _settings(tmp_path).agent_segment_catalog

    asyncio.run(dispatcher.dispatch_due(now=NOW))

    assert chatwoot.posts("/messages") == []
    assert authority.closes == [] and authority.finalizations == []


@pytest.mark.parametrize("step_key", STEPS)
def test_with_the_flag_off_a2_and_z_go_out_as_today_whatever_the_labels(
    tmp_path: Path, step_key: str
) -> None:
    chatwoot, authority = _dispatch(step_key, tmp_path, labels=(LABEL,), catalog=None)

    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["template_params"]["name"] == TEMPLATE_OF[step_key]  # type: ignore[index]
    assert authority.events[-1] == "accepted"


@pytest.mark.parametrize("step_key", STEPS)
@pytest.mark.parametrize(
    "labels, overrides",
    [
        (("otra-etiqueta",), {}),
        ((LABEL,), {"coupon": False}),
        ((LABEL,), {"window": PAST}),
        ((LABEL,), {"window": FUTURE}),
    ],
    ids=["sin-la-etiqueta", "campana-sin-cupon", "campana-vencida", "campana-por-empezar"],
)
def test_without_a_coupon_in_force_a2_and_z_go_out_as_today(
    tmp_path: Path, step_key: str, labels: tuple[str, ...], overrides: dict[str, Any]
) -> None:
    catalog = _settings(tmp_path, **overrides).agent_segment_catalog

    chatwoot, _ = _dispatch(step_key, tmp_path, labels=labels, catalog=catalog)

    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["template_params"]["name"] == TEMPLATE_OF[step_key]  # type: ignore[index]


def test_the_brake_is_only_for_the_steps_of_the_cart(tmp_path: Path) -> None:
    """C3 de la ficha, con la marca de la campana puesta, sale igual."""

    chatwoot, authority = _run_c3_with(
        tmp_path,
        mark=lambda dispatcher, action: dispatcher._segment_coupon_after_anchor.__setitem__(
            action.action_id, LABEL
        ),
    )

    assert authority.action.step_key == PAYMENT_SLIP_DUE_DAY_STEP
    assert isinstance(authority, _ReminderAuthority)
    assert len(chatwoot.posts("/conversations/200/messages")) == 1


# --------------------------------------------------------------------- A1


def _first_touch(
    tmp_path: Path,
    *,
    catalog: object | None,
    contact: list[str] | Exception | None,
    attempt_number: int = 1,
    **reads_options: Any,
) -> tuple[_Chatwoot, _CartAuthority, list[str]]:
    authority = _CartAuthority(attempt_number=attempt_number)
    chatwoot = _Chatwoot([*CATALOG, *CARRITO_CATALOG])
    dispatcher = _first_touch_dispatcher(authority, chatwoot, tmp_path)
    dispatcher._segment_catalog = catalog
    reads = _contact_reads(dispatcher, contact if contact is not None else [], **reads_options)
    # El arnes de A1 manda por el telefono exacto: la lectura del contacto es la
    # real, y el envio sigue como en test_cart_discount_first_touch.
    dispatcher._whatsapp_equivalence_enabled = False
    read = dispatcher._contact_segment_coupon

    async def reading(phone: str | None) -> Any:
        dispatcher._whatsapp_equivalence_enabled = True
        try:
            return await read(phone)
        finally:
            dispatcher._whatsapp_equivalence_enabled = False

    dispatcher._contact_segment_coupon = reading  # type: ignore[method-assign]
    asyncio.run(dispatcher.dispatch_due(now=NOW))
    return chatwoot, authority, reads


def test_a1_reaches_a_person_of_a_campaign_without_the_coupon(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="bridge")

    chatwoot, authority, _ = _first_touch(
        tmp_path, catalog=_settings(tmp_path).agent_segment_catalog, contact=[LABEL]
    )

    # La plantilla de siempre, sin el 10 %: ni cupon ni link emitido.
    _the_old_template_went_out(chatwoot)
    assert "coupon" not in authority.events and "reserve_link" not in authority.events
    assert f"segment_first_touch_without_coupon key={LABEL}" in caplog.text
    assert "PRUEBA11" not in caplog.text


def test_a1_keeps_the_coupon_without_a_campaign_coupon_in_force(tmp_path: Path) -> None:
    chatwoot, authority, reads = _first_touch(
        tmp_path, catalog=_settings(tmp_path).agent_segment_catalog, contact=["otra-etiqueta"]
    )

    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["template_params"]["name"] == "att1_carrito_descuento_01"  # type: ignore[index]
    assert "reserve_link" in authority.events
    assert reads == ["contacto", "conversaciones"]


def test_with_the_flag_off_a1_reads_nothing_new(tmp_path: Path) -> None:
    chatwoot, authority, reads = _first_touch(tmp_path, catalog=None, contact=[LABEL])

    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["template_params"]["name"] == "att1_carrito_descuento_01"  # type: ignore[index]
    assert "reserve_link" in authority.events
    assert reads == []


@pytest.mark.parametrize("failure", [ChatwootProtocolError("x"), httpx.ConnectError("x")])
@pytest.mark.parametrize("attempt_number", [1, 2])
def test_a1_waits_five_minutes_if_the_labels_cannot_be_read(
    tmp_path: Path, failure: Exception, attempt_number: int
) -> None:
    chatwoot, authority, _ = _first_touch(
        tmp_path,
        catalog=_settings(tmp_path).agent_segment_catalog,
        contact=failure,
        attempt_number=attempt_number,
    )

    closed = _nothing_went_out(chatwoot, authority)  # type: ignore[arg-type]
    assert closed["reason_code"] == SEGMENT_LABELS_UNAVAILABLE
    assert datetime.fromisoformat(str(closed["next_attempt_at"])) == (
        datetime.fromisoformat(FINAL_NOW) + CONFIG.defer_delay
    )


@pytest.mark.parametrize("failure", [ChatwootProtocolError("x"), httpx.ConnectError("x")])
def test_a1_is_not_lost_if_the_labels_never_can_be_read(
    tmp_path: Path, failure: Exception
) -> None:
    chatwoot, authority, _ = _first_touch(
        tmp_path,
        catalog=_settings(tmp_path).agent_segment_catalog,
        contact=failure,
        attempt_number=CONFIG.defer_attempts + 1,
    )

    _the_old_template_went_out(chatwoot)
    assert "reserve_link" not in authority.events


@pytest.mark.parametrize("window", [PAST, FUTURE], ids=["vencida", "por-empezar"])
def test_a1_keeps_the_coupon_outside_the_validity_of_the_campaign(
    tmp_path: Path, window: tuple[str, str]
) -> None:
    chatwoot, authority, _ = _first_touch(
        tmp_path, catalog=_settings(tmp_path, window=window).agent_segment_catalog, contact=[LABEL]
    )

    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["template_params"]["name"] == "att1_carrito_descuento_01"  # type: ignore[index]
    assert "reserve_link" in authority.events


def test_a1_with_a_truncated_list_stops_only_with_evidence(tmp_path: Path) -> None:
    chatwoot, _, _ = _first_touch(
        tmp_path,
        catalog=_settings(tmp_path).agent_segment_catalog,
        contact=[LABEL],
        truncated=True,
    )

    _the_old_template_went_out(chatwoot)


# ----------------------------------------------------------------- el seguimiento


def _sweep(
    tmp_path: Path, *, labels: list[str] | None, enabled: bool = True, **overrides: Any
) -> tuple[Any, _FakeChatwoot, _FakeSupabase]:
    chatwoot = _att1_chatwoot()
    if labels is None:
        del chatwoot.entries[0]["conversation"]["labels"]
    else:
        chatwoot.entries[0]["conversation"]["labels"] = labels
    supabase = _FakeSupabase(issued_url=ATT1_AGENT_LINK)
    sweeper = _att1_sweeper(
        chatwoot,
        supabase,
        now=ATT1_LEAD_REPLIED_AT + 30 * HOUR,
        segment_catalog=(
            _settings(tmp_path, **overrides).agent_segment_catalog if enabled else None
        ),
    )
    return sweeper, chatwoot, supabase


def test_the_fixed_coupon_followup_is_skipped_for_a_person_of_a_campaign(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sweeper, chatwoot, supabase = _sweep(tmp_path, labels=[LABEL])

    with caplog.at_level(logging.INFO, logger="bridge.followup_discount"):
        assert asyncio.run(sweeper.run_once()) == 0
        assert asyncio.run(sweeper.run_once()) == 0

    assert supabase.claims == []
    assert chatwoot.sent == []
    assert sweeper.last_scan_summary == f"scanned=1 sent=0 {SEGMENT_COUPON_ACTIVE}=1"
    # Una linea por conversacion aunque el barrido se repita cada 5 minutos.
    assert [
        record.getMessage()
        for record in caplog.records
        if "segment_followup_coupon_skipped" in record.getMessage()
    ] == [f"segment_followup_coupon_skipped key={LABEL} conversation=21"]


@pytest.mark.parametrize(
    "labels, enabled, overrides",
    [
        ([LABEL], False, {}),
        (["otra-etiqueta"], True, {}),
        ([LABEL], True, {"coupon": False}),
        ([LABEL], True, {"window": PAST}),
        (None, True, {}),
    ],
    ids=["flag-apagado", "sin-la-etiqueta", "campana-sin-cupon", "campana-vencida", "sin-labels"],
)
def test_without_a_campaign_coupon_the_followup_goes_out_as_today(
    tmp_path: Path, labels: list[str] | None, enabled: bool, overrides: dict[str, Any]
) -> None:
    sweeper, chatwoot, supabase = _sweep(tmp_path, labels=labels, enabled=enabled, **overrides)

    assert asyncio.run(sweeper.run_once()) == 1

    assert len(supabase.claims) == 1
    assert len(chatwoot.sent) == 1


def test_a_followup_with_the_coupon_of_the_campaign_itself_goes_out(tmp_path: Path) -> None:
    """Un silencio nativo de la campana con su propio cupon no es un segundo cupon."""

    sweeper, chatwoot, supabase = _sweep(tmp_path, labels=[LABEL])
    sweeper._coupon_code = "PRUEBA11"

    assert asyncio.run(sweeper.run_once()) == 1

    assert len(supabase.claims) == 1


def test_the_validity_of_the_campaign_uses_the_clock_of_the_sweeper(tmp_path: Path) -> None:
    """Vigente para el reloj del barredor y vencida para el de la maquina: frena."""

    replied = datetime.fromtimestamp(ATT1_LEAD_REPLIED_AT, UTC)
    window = (
        (replied - timedelta(days=1)).isoformat(),
        (replied + timedelta(days=3)).isoformat(),
    )
    assert datetime.fromisoformat(window[1]) < datetime.now(UTC)

    sweeper, _, supabase = _sweep(tmp_path, labels=[LABEL], window=window)

    assert asyncio.run(sweeper.run_once()) == 0
    assert supabase.claims == []
    assert sweeper.last_scan_summary == f"scanned=1 sent=0 {SEGMENT_COUPON_ACTIVE}=1"


# ---------------------------------------------------------------------- el boton


class _Renewal(_PaymentLinkSupabase):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.renewals: list[dict[str, object]] = []

    async def request_cart_renewal(self, **kwargs: object) -> str:
        self.renewals.append(kwargs)
        return "advanced"


def _press(settings: Any, labels: list[str]) -> tuple[_Renewal, Any]:
    webhook, history = _text_webhook(BUTTON)
    webhook["conversation"]["labels"] = labels
    supabase = _Renewal(identities=(FORM_ID,))
    _, shadow = _process(
        supabase,
        settings=settings,
        webhook=webhook,
        history=history,
        proposal=PAYMENT_LINK_PROPOSAL,
    )
    return supabase, shadow


def test_the_button_does_not_bring_z_forward_and_the_agent_answers(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="bridge")

    supabase, shadow = _press(
        _settings(tmp_path, recovery_cart_followups_enabled=True), [LABEL]
    )

    assert supabase.renewals == []
    # Sin Z en camino el boton tiene que tener respuesta.
    assert len(shadow.calls) == 1
    assert f"segment_recovery_coupon_skipped key={LABEL} conversation=200" in caplog.text


def test_without_the_label_the_button_brings_z_forward_as_today(tmp_path: Path) -> None:
    supabase, shadow = _press(_settings(tmp_path, recovery_cart_followups_enabled=True), [])

    assert len(supabase.renewals) == 1
    assert shadow.calls == []


@pytest.mark.parametrize("window", [PAST, FUTURE], ids=["vencida", "por-empezar"])
def test_outside_the_validity_of_the_campaign_the_button_brings_z_forward(
    tmp_path: Path, window: tuple[str, str]
) -> None:
    supabase, shadow = _press(
        _settings(tmp_path, window=window, recovery_cart_followups_enabled=True), [LABEL]
    )

    assert len(supabase.renewals) == 1
    assert shadow.calls == []


def test_with_the_flag_off_the_button_brings_z_forward_as_today(tmp_path: Path) -> None:
    supabase, shadow = _press(
        _settings(tmp_path, enabled=False, recovery_cart_followups_enabled=True), [LABEL]
    )

    assert len(supabase.renewals) == 1
    assert shadow.calls == []


# ---------------------------------------------------------------- el cliente y el AE


@pytest.mark.parametrize("outcome", ["closed", "case_not_open"])
def test_the_client_calls_the_close_rpc(outcome: str) -> None:
    seen: list[httpx.Request] = []

    answer = asyncio.run(
        _rpc([{"outcome": outcome}], seen=seen).close_recovery_case_segment_coupon(
            action_id="a", attempt_id="b", worker_id="w", lease_generation=3, now=NOW
        )
    )

    assert answer == outcome
    assert seen[0].url.path == "/rest/v1/rpc/close_recovery_case_segment_coupon_v1"
    assert json.loads(seen[0].content) == {
        "p_action_id": "a",
        "p_attempt_id": "b",
        "p_worker_id": "w",
        "p_lease_generation": 3,
        "p_now": NOW,
    }


@pytest.mark.parametrize(
    "rows, status",
    [([], 200), ([{"outcome": "otro"}], 200), ([{"message": "no existe"}], 404)],
)
def test_the_client_refuses_any_other_answer(rows: object, status: int) -> None:
    with pytest.raises(SupabaseError):
        asyncio.run(
            _rpc(rows, status).close_recovery_case_segment_coupon(
                action_id="a", attempt_id="b", worker_id="w", lease_generation=3, now=NOW
            )
        )


def test_the_automation_expert_can_explain_the_brake() -> None:
    assert reader.EXPLANATIONS[SEGMENT_COUPON_ACTIVE]
    assert reader._REASON_BY_CODE[SEGMENT_COUPON_ACTIVE] == "ya_no_entra"


# ------------------------------------------------------------- el armado y los logs


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> object:
    """Un catalogo cualquiera: aca solo importa quien lo recibe, no lo que trae."""

    monkeypatch.setattr(app_module, "_validate_agent_segments", lambda settings: None)
    return object()


def test_the_app_gives_the_catalog_to_the_dispatcher_only_with_the_flag(
    tmp_path: Path, catalog: object
) -> None:
    on = _cart_app(
        _with_followups(tmp_path, agent_segments_enabled=True, agent_segment_catalog=catalog)
    )
    off = _cart_app(_with_followups(tmp_path))

    assert on.state.durable_dispatcher._segment_catalog is catalog
    assert off.state.durable_dispatcher._segment_catalog is None


def test_the_app_gives_the_catalog_to_the_legacy_sweeper_only_with_the_flag(catalog: object) -> None:
    def sweeper(**overrides: Any) -> Any:
        return create_app(
            _johanna_followup_settings(**overrides),
            chatwoot_client=_chatwoot_client(1),
            supabase_client=_FakeSupabase(),  # type: ignore[arg-type]
        ).state.conversation_followup_sweeper

    assert sweeper(agent_segments_enabled=True, agent_segment_catalog=catalog)._segment_catalog is catalog
    assert sweeper()._segment_catalog is None


def test_the_app_gives_the_catalog_to_the_native_sweeper_only_with_the_flag(
    tmp_path: Path, catalog: object
) -> None:
    class _WithSilence(_ReadyAuthority):
        async def read_flow_engine_native_silences(self, instance: object) -> tuple[Any, ...]:
            return (_silence(_changed(("dispara", "silencio", "desde"), "30h")),)

    def sweeper(**overrides: Any) -> Any:
        sweepers = _ready_app(tmp_path, _WithSilence(), **overrides).state.native_silence_sweepers

        async def refresh_and_stop() -> Any:
            await sweepers.refresh_once()
            [built] = sweepers.sweepers
            await sweepers.stop()
            return built

        return asyncio.run(refresh_and_stop())

    assert sweeper(agent_segments_enabled=True, agent_segment_catalog=catalog)._segment_catalog is catalog
    assert sweeper()._segment_catalog is None


@pytest.mark.parametrize(
    "event",
    [
        "segment_link_coupon",
        "segment_link_coupon_other_product",
        "segment_recovery_coupon_skipped",
        "segment_contact_labels_truncated",
        "segment_first_touch_without_coupon",
        "segment_cart_followup_skipped",
        "segment_coupon_case_closed",
        "segment_followup_coupon_skipped",
    ],
)
def test_the_coupon_logs_reach_the_container(event: str) -> None:
    assert event in LOGGED_INFO_EVENTS


# --- E-486 (item 5.2): «ya pague» / «voy a pagar» antes de A2 y Z -------------


@pytest.mark.parametrize("step_key", STEPS)
def test_a2_and_z_do_not_go_out_after_a_declared_payment(
    tmp_path: Path, step_key: str
) -> None:
    from bridge.lead_decline import LEAD_DECLARED_PAYMENT

    # Maribel #9: «voy a hacer el pago en OXXO» una hora antes.
    chatwoot, authority = _dispatch(
        step_key,
        tmp_path,
        labels=(),
        catalog=None,
        declared_payment_at=int(_now().timestamp()) - 3600,
    )

    closed = _nothing_went_out(chatwoot, authority)
    assert closed["reason_code"] == LEAD_DECLARED_PAYMENT
    assert closed["next_attempt_at"] is None
    assert authority.closes == []


def test_a_declared_payment_older_than_72_hours_does_not_hold_a2(tmp_path: Path) -> None:
    from bridge.lead_decline import LEAD_DECLARED_PAYMENT

    _, authority = _dispatch(
        CART_SECOND_TOUCH_STEP,
        tmp_path,
        labels=(),
        catalog=None,
        declared_payment_at=int(_now().timestamp()) - 73 * 3600,
    )

    assert all(
        final.get("reason_code") != LEAD_DECLARED_PAYMENT
        for final in authority.finalizations
    )
