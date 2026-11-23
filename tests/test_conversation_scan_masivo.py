"""Los barridos de Chatwoot despues de un envio masivo (Maribel, 2026-11-22).

El sabado 22/11 salieron ~3.600 plantillas en la bandeja 13 de Maribel y desde
entonces el seguimiento con cupon (y su barrido nativo de silencio), el
registro de rechazos de Meta y la reactivacion fallaban en cada vuelta con
``conversation_scan_incomplete``: su tope de paginas (25 conversaciones cada
una) era de 20 como maximo y nunca llegaban al corte de su ventana. El
2026-11-29 sale otro masivo del mismo tamano.

Las paginas se arman repitiendo items capturados de Chatwoot con ids y
actividad corridos; la forma de cada item es la capturada:

* el item del listado: ``chatwoot_followup_candidate_inbox_11_20261010.json``
  (``/conversations?status=open&inbox_id=11&sort_by=last_activity_at_desc``
  de ATT1), una conversacion con ``can_reply`` en ``false`` y sin etiquetas,
  la forma de una plantilla sin respuesta;
* el show: la 120 de ATT1 en
  ``chatwoot_daily_feedback_att1_conversations_20261015.json``, abierta;
* los mensajes: la plantilla del primer contacto de esa misma conversacion
  (2962, ``recovery_first_touch_hash``, ``delivered``) y nada mas; para un
  rechazo, el 2831 de la 52 de ATT1 en
  ``automation_expert/chatwoot_messages_inbox_11_convs_50_17_52_20261015.json``
  (``failed``, con el ``external_error`` 131049 repuesto como en
  ``tests/test_meta_delivery.py``).

Cada test cuenta las llamadas HTTP de un barrido: el primero lee todo una vez;
los siguientes, sin actividad nueva, solo el listado.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from bridge.chatwoot import (
    DEFAULT_CONVERSATION_SCAN_PAGES,
    MAX_CONVERSATION_SCAN_PAGES,
    ChatwootClient,
    ChatwootProtocolError,
    SettledConversationMemo,
)
from bridge.followup_discount import ConversationFollowupSweeper, followup_listing_skip_reason
from bridge.messaging import FinalMetaEffectGate
from bridge.meta_delivery import (
    MetaDeliveryRejectionSweeper,
    RetryConfig,
    conversation_settled,
)
from bridge.reactivation import (
    ConversationReactivationSweeper,
    reactivation_listing_skip_reason,
)
from bridge.supabase import MetaDeliveryRejectionRecord


FIXTURES = Path(__file__).parent / "fixtures"
ACCOUNT = 2
INBOX = 11
MASSIVE = 3_600


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


LISTING = _load("chatwoot_followup_candidate_inbox_11_20261010.json")["pages"]["open"]["payload"]
SHOW_120 = _load("chatwoot_daily_feedback_att1_conversations_20261015.json")["conversations"]["120"]
REJECTED_52 = _load(
    "automation_expert/chatwoot_messages_inbox_11_convs_50_17_52_20261015.json"
)["conversations"]["52"]["payload"]
ATT1_CATALOG = _load("chatwoot_inbox_11_message_templates_20261010.json")
# El catalogo de Johanna (inbox 9) con la plantilla de reactivacion; se sirve
# como el de la bandeja 11, que no tiene una capturada.
JOHANNA_REACTIVATION_CATALOG = {
    **_load("chatwoot_inbox_9_message_templates_20260923.json"),
    "id": INBOX,
}

# La plantilla 2962 de la 120 salio a las 1791817342; el masivo, dos horas
# despues de la ultima actividad de esa conversacion.
NOW = 1_791_904_251 + 2 * 3_600


def _silent_item() -> dict:
    item = next(
        entry for entry in LISTING if entry["can_reply"] is False and not entry["labels"]
    )
    return copy.deepcopy(item)


def _template_message() -> dict:
    template = next(m for m in SHOW_120["messages"] if m["id"] == 2962)
    assert template["status"] == "delivered"
    assert "recovery_first_touch_hash" in template["content_attributes"]
    return copy.deepcopy(template)


def _rejected_message() -> dict:
    failed = copy.deepcopy(next(m for m in REJECTED_52 if m["id"] == 2831))
    assert failed["status"] == "failed"
    failed["content_attributes"]["external_error"] = (
        "131049: This message was not delivered to maintain healthy ecosystem engagement."
    )
    return failed


class MassiveInbox:
    """Un Chatwoot falso con la forma capturada: listado, show y mensajes."""

    def __init__(self, items: list[dict], messages: dict[int, list[dict]]) -> None:
        self.items = items
        self.messages = messages
        self.inbox_payload: dict = ATT1_CATALOG
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        base = f"/api/v1/accounts/{ACCOUNT}"
        if path == f"{base}/inboxes/{INBOX}":
            return httpx.Response(200, json=self.inbox_payload)
        if path == f"{base}/conversations":
            params = request.url.params
            assert params["inbox_id"] == str(INBOX)
            # list_open_conversations_with_messages no pide orden.
            assert params.get("sort_by") in (None, "last_activity_at_desc")
            status = params["status"]
            listed = sorted(
                (item for item in self.items if item["status"] == status),
                key=lambda item: item["last_activity_at"],
                reverse=True,
            )
            page = int(params["page"])
            payload = listed[(page - 1) * 25 : page * 25]
            return httpx.Response(
                200,
                json={"data": {"meta": {"all_count": len(listed)}, "payload": payload}},
            )
        prefix = f"{base}/conversations/"
        assert path.startswith(prefix)
        rest = path.removeprefix(prefix)
        if rest.endswith("/messages"):
            conversation_id = int(rest.removesuffix("/messages"))
            return httpx.Response(200, json={"payload": self.messages[conversation_id]})
        conversation_id = int(rest)
        item = next(item for item in self.items if item["id"] == conversation_id)
        show = copy.deepcopy(SHOW_120["conversation"])
        show.update(
            {
                "id": conversation_id,
                "inbox_id": INBOX,
                "status": item["status"],
                "can_reply": item["can_reply"],
                "labels": item["labels"],
                "last_activity_at": item["last_activity_at"],
            }
        )
        show["messages"] = copy.deepcopy(self.messages[conversation_id][-1:])
        return httpx.Response(200, json=show)

    def calls(self) -> dict[str, int]:
        counts = {"inbox": 0, "listing": 0, "show": 0, "messages": 0}
        base = f"/api/v1/accounts/{ACCOUNT}"
        for request in self.requests:
            path = request.url.path
            if path == f"{base}/inboxes/{INBOX}":
                counts["inbox"] += 1
            elif path == f"{base}/conversations":
                counts["listing"] += 1
            elif path.endswith("/messages"):
                counts["messages"] += 1
            else:
                counts["show"] += 1
        self.requests.clear()
        return counts

    def new_activity(self, conversation_id: int, message: dict, at: int) -> None:
        item = next(item for item in self.items if item["id"] == conversation_id)
        item["last_activity_at"] = at
        self.messages[conversation_id] = [*self.messages[conversation_id], message]

    def client(self) -> ChatwootClient:
        return ChatwootClient(
            base_url="https://chatwoot.example.test",
            account_id=ACCOUNT,
            access_token="control-token",
            transport=httpx.MockTransport(self.handler),
        )


def _massive_inbox(
    total: int = MASSIVE, *, older: int = 0, now: int = NOW
) -> MassiveInbox:
    """``total`` plantillas sin respuesta en la ultima hora, y ``older`` mas viejas.

    Las ``older`` tienen actividad entre 7 y 70 h antes de ``now``: las
    conversaciones que se crean de antemano y las del masivo anterior.
    """
    items: list[dict] = []
    messages: dict[int, list[dict]] = {}
    template = _template_message()
    for index in range(total + older):
        conversation_id = 20_000 + index
        if index < total:
            # Mas de 2 minutos atras: lo reciente no se asienta (ver el test).
            activity = now - 300 - index
        else:
            activity = now - 7 * 3_600 - (index - total) * 150
        item = _silent_item()
        item.update({"id": conversation_id, "last_activity_at": activity, "timestamp": activity})
        items.append(item)
        message = dict(template, conversation_id=conversation_id, created_at=activity)
        messages[conversation_id] = [message]
    return MassiveInbox(items, messages)


def _inbound_reply(conversation_id: int, at: int) -> dict:
    reply = copy.deepcopy(next(m for m in SHOW_120["messages"] if m["id"] == 2968))
    assert reply["message_type"] == 0 and reply["sender"]["type"] == "contact"
    reply.update({"id": 900_000 + conversation_id, "conversation_id": conversation_id, "created_at": at})
    return reply


class _Clock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> float:
        return float(self.now)


# --- el listado ------------------------------------------------------------


def test_the_listing_skips_items_before_reading_them() -> None:
    inbox = _massive_inbox(100)
    skipped: list[int] = []

    def skip(item: dict) -> bool:
        if item["id"] % 10 == 0:
            return False
        skipped.append(item["id"])
        return True

    entries = asyncio.run(
        inbox.client().list_recent_conversations_with_messages(
            expected_inbox_id=INBOX,
            statuses=("open",),
            active_since_epoch=NOW - 3_600,
            max_pages=DEFAULT_CONVERSATION_SCAN_PAGES,
            skip_listing_item=skip,
        )
    )
    assert [entry["conversation"]["id"] for entry in entries] == [
        20_000 + index for index in range(0, 100, 10)
    ]
    assert len(skipped) == 90
    # Las salteadas igual cuentan como listadas: el barrido cierra en 4 paginas.
    assert inbox.calls() == {"inbox": 0, "listing": 4, "show": 10, "messages": 10}


def test_every_massive_scan_accepts_the_new_page_cap() -> None:
    client = _massive_inbox(1).client()
    assert MAX_CONVERSATION_SCAN_PAGES * 25 >= 10_000
    assert DEFAULT_CONVERSATION_SCAN_PAGES * 25 >= 4_000
    with pytest.raises(ValueError):
        asyncio.run(
            client.list_open_conversations_with_messages(
                expected_inbox_id=INBOX, max_pages=MAX_CONVERSATION_SCAN_PAGES + 1
            )
        )
    supabase = object()
    for max_pages in (1, 20, DEFAULT_CONVERSATION_SCAN_PAGES, MAX_CONVERSATION_SCAN_PAGES):
        ConversationFollowupSweeper(
            chatwoot=client, supabase=supabase, account_id=ACCOUNT, inbox_id=INBOX,
            template_name="t", coupon_code="C10", product_name="p", max_pages=max_pages,
        )
        ConversationReactivationSweeper(
            chatwoot=client, supabase=supabase, inbox_id=INBOX, template_name="t",
            max_pages=max_pages,
        )
        MetaDeliveryRejectionSweeper(
            chatwoot=client, supabase=supabase, account_id=ACCOUNT, inbox_id=INBOX,
            max_pages=max_pages,
        )
    for max_pages in (0, MAX_CONVERSATION_SCAN_PAGES + 1):
        with pytest.raises(ValueError):
            ConversationFollowupSweeper(
                chatwoot=client, supabase=supabase, account_id=ACCOUNT, inbox_id=INBOX,
                template_name="t", coupon_code="C10", product_name="p", max_pages=max_pages,
            )
        with pytest.raises(ValueError):
            ConversationReactivationSweeper(
                chatwoot=client, supabase=supabase, inbox_id=INBOX, template_name="t",
                max_pages=max_pages,
            )
        with pytest.raises(ValueError):
            MetaDeliveryRejectionSweeper(
                chatwoot=client, supabase=supabase, account_id=ACCOUNT, inbox_id=INBOX,
                max_pages=max_pages,
            )


def test_the_open_listing_accepts_the_new_page_cap() -> None:
    inbox = _massive_inbox(30)
    entries = asyncio.run(
        inbox.client().list_open_conversations_with_messages(
            expected_inbox_id=INBOX, max_pages=MAX_CONVERSATION_SCAN_PAGES
        )
    )
    assert len(entries) == 30


def test_the_default_settings_reach_a_massive_send() -> None:
    from bridge.app import Settings

    fields = Settings.__dataclass_fields__
    for name in (
        "conversation_followup_max_pages",
        "conversation_reactivation_max_pages",
        "meta_delivery_max_pages",
    ):
        assert fields[name].default * 25 >= 4_000, name


# --- lo que el listado ya descarta -------------------------------------------


def test_listing_skip_reasons_use_only_captured_listing_fields() -> None:
    by_id = {item["id"]: item for item in LISTING}
    # 30 y 20: dadas de baja y pausadas; 27: pausada; 21: respondio hace menos de 24 h.
    assert followup_listing_skip_reason(by_id[30]) == "contact_opted_out"
    assert followup_listing_skip_reason(by_id[27]) == "conversation_paused"
    assert followup_listing_skip_reason(by_id[21]) is None
    assert followup_listing_skip_reason(by_id[32]) is None
    assert reactivation_listing_skip_reason(by_id[30]) == "inside_service_window"
    assert reactivation_listing_skip_reason(by_id[21]) == "inside_service_window"
    assert reactivation_listing_skip_reason(by_id[32]) is None
    # Sin el campo, o con otra forma, no se descarta: el show decide.
    bare = {"id": 1, "inbox_id": INBOX, "last_activity_at": NOW}
    assert followup_listing_skip_reason(bare) is None
    assert reactivation_listing_skip_reason(bare) is None
    assert reactivation_listing_skip_reason({**bare, "can_reply": None}) is None
    assert followup_listing_skip_reason({**bare, "labels": "automation_paused"}) is None
    # El listado del inbox 9 (2026-09-26) trae meta.sender.blocked.
    feedback = _load("chatwoot_daily_feedback_conversations_20260926.json")["data"]["payload"]
    blocked = copy.deepcopy(next(item for item in feedback if not item["labels"]))
    assert followup_listing_skip_reason(blocked) is None
    blocked["meta"]["sender"]["blocked"] = True
    assert followup_listing_skip_reason(blocked) == "contact_blocked_or_unknown"
    blocked["can_reply"] = False
    assert reactivation_listing_skip_reason(blocked) == "contact_blocked_or_unknown"
    # Muteada, pospuesta, otro producto.
    assert followup_listing_skip_reason({**by_id[32], "muted": True}) == "conversation_muted"
    assert (
        followup_listing_skip_reason({**by_id[32], "snoozed_until": 1_791_999_999})
        == "conversation_snoozed"
    )
    assert (
        followup_listing_skip_reason(
            {**by_id[32], "labels": ["producto_b"]},
            other_product_labels=frozenset({"producto_b"}),
            own_product_label="producto_a",
        )
        == "other_product_conversation"
    )


def test_the_memo_keeps_a_reason_only_while_the_activity_mark_holds() -> None:
    memo = SettledConversationMemo()
    item = {"id": 7, "last_activity_at": 100}
    memo.begin()
    assert memo.lookup(item) is None
    memo.settle({"id": 7, "last_activity_at": 100}, "never_replied", now_epoch=1_000)
    memo.settle({"id": 8, "last_activity_at": "100"}, "never_replied", now_epoch=1_000)
    # Con actividad de hace menos de 2 minutos, no se guarda.
    memo.settle({"id": 9, "last_activity_at": 900}, "never_replied", now_epoch=1_000)
    memo.commit()
    assert len(memo) == 1
    memo.begin()
    assert memo.lookup(item) == "never_replied"
    assert memo.lookup({"id": 7, "last_activity_at": 101}) is None
    memo.commit()
    assert len(memo) == 1
    # Lo que el listado no vuelve a traer se olvida.
    memo.begin()
    memo.commit()
    assert len(memo) == 0


# --- el seguimiento con cupon -------------------------------------------------


def _followup(inbox: MassiveInbox, clock: _Clock) -> ConversationFollowupSweeper:
    return ConversationFollowupSweeper(
        chatwoot=inbox.client(),
        supabase=object(),
        account_id=ACCOUNT,
        inbox_id=INBOX,
        template_name="att1_seguimiento_descuento_01",
        coupon_code="TIROIDES10",
        product_name="Alimenta tu Tiroides",
        expected_template_language="es_MX",
        clock=clock,
    )


def test_followup_finishes_after_a_massive_send_and_reads_each_conversation_once() -> None:
    inbox = _massive_inbox()
    clock = _Clock(NOW)
    sweeper = _followup(inbox, clock)

    assert asyncio.run(sweeper.run_once()) == 0
    # 144 paginas de abiertas y 1 de resueltas (vacia); cada plantilla, una vez.
    assert inbox.calls() == {"inbox": 1, "listing": 145, "show": MASSIVE, "messages": MASSIVE}
    assert sweeper.last_scan_summary == f"scanned={MASSIVE} sent=0 never_replied={MASSIVE}"

    # Cinco minutos despues, sin actividad nueva: solo el listado.
    clock.now += 300
    assert asyncio.run(sweeper.run_once()) == 0
    assert inbox.calls() == {"inbox": 1, "listing": 145, "show": 0, "messages": 0}
    assert sweeper.last_scan_summary == f"scanned={MASSIVE} sent=0 never_replied={MASSIVE}"

    # Una persona contesta la plantilla: esa conversacion se lee de nuevo.
    clock.now += 300
    inbox.new_activity(20_005, _inbound_reply(20_005, clock.now - 10), clock.now - 10)
    assert asyncio.run(sweeper.run_once()) == 0
    assert inbox.calls() == {"inbox": 1, "listing": 145, "show": 1, "messages": 1}
    assert sweeper.last_scan_summary == (
        f"scanned={MASSIVE} sent=0 never_replied={MASSIVE - 1} last_message_inbound=1"
    )


def test_followup_without_the_page_cap_fails_closed_like_before() -> None:
    inbox = _massive_inbox()
    sweeper = ConversationFollowupSweeper(
        chatwoot=inbox.client(),
        supabase=object(),
        account_id=ACCOUNT,
        inbox_id=INBOX,
        template_name="att1_seguimiento_descuento_01",
        coupon_code="TIROIDES10",
        product_name="Alimenta tu Tiroides",
        expected_template_language="es_MX",
        max_pages=20,
        clock=_Clock(NOW),
    )
    with pytest.raises(ChatwootProtocolError, match="conversation_scan_incomplete"):
        asyncio.run(sweeper.run_once())


# --- la reactivacion ----------------------------------------------------------


def test_reactivation_finishes_after_a_massive_send_and_reads_each_conversation_once() -> None:
    inbox = _massive_inbox()
    inbox.inbox_payload = JOHANNA_REACTIVATION_CATALOG
    clock = _Clock(NOW)
    sweeper = ConversationReactivationSweeper(
        chatwoot=inbox.client(),
        supabase=object(),
        inbox_id=INBOX,
        template_name="johanna_reactivacion_01",
        clock=clock,
    )
    assert asyncio.run(sweeper.run_once()) == 0
    # Solo las abiertas, por actividad: 144 paginas, sin el conteo total que
    # cambiaba entre paginas durante el masivo.
    assert inbox.calls() == {"inbox": 1, "listing": 144, "show": MASSIVE, "messages": MASSIVE}
    assert sweeper.last_scan_summary == (
        f"scanned={MASSIVE} sent=0 last_message_not_inbound={MASSIVE}"
    )

    clock.now += 900
    assert asyncio.run(sweeper.run_once()) == 0
    assert inbox.calls() == {"inbox": 1, "listing": 144, "show": 0, "messages": 0}

    # La persona que contesto tiene can_reply en true: el listado la descarta.
    clock.now += 900
    inbox.new_activity(20_009, _inbound_reply(20_009, clock.now - 10), clock.now - 10)
    next(item for item in inbox.items if item["id"] == 20_009)["can_reply"] = True
    assert asyncio.run(sweeper.run_once()) == 0
    assert inbox.calls() == {"inbox": 1, "listing": 144, "show": 0, "messages": 0}
    assert sweeper.last_scan_summary == (
        f"scanned={MASSIVE} sent=0 last_message_not_inbound={MASSIVE - 1} "
        "inside_service_window=1"
    )


def test_reactivation_default_reaches_two_massive_sends_in_its_window() -> None:
    # 30 dias de ventana: las ~1.900 abiertas del masivo anterior mas las 3.600
    # del nuevo son 5.500, 220 paginas: mas que las 200 de los otros barridos.
    inbox = _massive_inbox(older=1_900)
    inbox.inbox_payload = JOHANNA_REACTIVATION_CATALOG
    sweeper = ConversationReactivationSweeper(
        chatwoot=inbox.client(),
        supabase=object(),
        inbox_id=INBOX,
        template_name="johanna_reactivacion_01",
        clock=_Clock(NOW),
    )
    assert asyncio.run(sweeper.run_once()) == 0
    assert inbox.calls()["listing"] == 220
    from bridge.app import Settings

    default = Settings.__dataclass_fields__["conversation_reactivation_max_pages"].default
    assert default * 25 >= 5_500


def test_reactivation_stops_listing_at_its_window() -> None:
    # 30 dias de ventana: una abierta de hace 40 dias ni se lee.
    inbox = _massive_inbox(25)
    stale = _silent_item()
    stale.update({"id": 99_999, "last_activity_at": NOW - 40 * 86_400})
    inbox.items.append(stale)
    inbox.messages[99_999] = [_template_message()]
    inbox.inbox_payload = JOHANNA_REACTIVATION_CATALOG
    sweeper = ConversationReactivationSweeper(
        chatwoot=inbox.client(),
        supabase=object(),
        inbox_id=INBOX,
        template_name="johanna_reactivacion_01",
        clock=_Clock(NOW),
    )
    asyncio.run(sweeper.run_once())
    assert inbox.calls() == {"inbox": 1, "listing": 2, "show": 25, "messages": 25}


# --- el registro de rechazos de Meta -----------------------------------------


class _MetaSupabase:
    def __init__(self) -> None:
        self.recorded: list[int] = []

    async def record_meta_delivery_rejection(self, **kwargs: object) -> MetaDeliveryRejectionRecord:
        self.recorded.append(kwargs["chatwoot_message_id"])  # type: ignore[arg-type]
        return MetaDeliveryRejectionRecord(
            "replayed", "11111111-1111-4111-8111-111111111111", "none", None, False
        )

    async def list_due_meta_first_touch_retries(self, **kwargs: object) -> list:
        return []


def _retry(tmp_path: Path) -> RetryConfig:
    return RetryConfig(
        allowed_templates=frozenset({"att1_carrito_abandonado_01"}),
        send_hours=(0, 24),
        time_zone=ZoneInfo("America/Mexico_City"),
        gate=FinalMetaEffectGate(enabled=False, evidence_dir=tmp_path / "gate"),
    )


def _with_rejections(inbox: MassiveInbox, ids: list[int]) -> None:
    for conversation_id in ids:
        failed = _rejected_message()
        failed["conversation_id"] = conversation_id
        inbox.messages[conversation_id] = [failed]


def test_meta_delivery_finishes_after_a_massive_send_and_rereads_only_what_can_change(
    tmp_path: Path,
) -> None:
    inbox = _massive_inbox()
    _with_rejections(inbox, [20_001, 20_002, 20_003])
    # Dos que Meta todavia no confirmo: pueden volver rechazadas.
    for conversation_id in (20_010, 20_011):
        inbox.messages[conversation_id][0]["status"] = "sent"
    clock = _Clock(NOW)
    supabase = _MetaSupabase()
    sweeper = MetaDeliveryRejectionSweeper(
        chatwoot=inbox.client(), supabase=supabase, account_id=ACCOUNT, inbox_id=INBOX,
        retry=_retry(tmp_path), clock=clock,
    )
    counts = asyncio.run(sweeper.run_once())
    # Cuatro estados: 144 paginas de abiertas y una vacia por cada uno de los otros.
    assert inbox.calls() == {"inbox": 0, "listing": 147, "show": MASSIVE, "messages": MASSIVE}
    assert sorted(supabase.recorded) == [2831, 2831, 2831]
    assert "first_scan_wide_incomplete" not in counts

    clock.now += 600
    counts = asyncio.run(sweeper.run_once())
    # Solo los rechazos (se registran en cada vuelta, como siempre) y las dos sin confirmar.
    assert inbox.calls() == {"inbox": 0, "listing": 147, "show": 5, "messages": 5}
    assert counts["unchanged_skipped"] == MASSIVE - 5
    assert len(supabase.recorded) == 6

    # Meta confirma una de las dos: deja de leerse.
    inbox.messages[20_010][0]["status"] = "delivered"
    clock.now += 600
    asyncio.run(sweeper.run_once())
    clock.now += 600
    asyncio.run(sweeper.run_once())
    inbox.calls()
    clock.now += 600
    asyncio.run(sweeper.run_once())
    assert inbox.calls() == {"inbox": 0, "listing": 147, "show": 4, "messages": 4}


def test_a_later_failure_on_an_unconfirmed_template_is_still_recorded(tmp_path: Path) -> None:
    inbox = _massive_inbox(50)
    inbox.messages[20_007][0]["status"] = "sent"
    clock = _Clock(NOW)
    supabase = _MetaSupabase()
    sweeper = MetaDeliveryRejectionSweeper(
        chatwoot=inbox.client(), supabase=supabase, account_id=ACCOUNT, inbox_id=INBOX,
        clock=clock,
    )
    asyncio.run(sweeper.run_once())
    assert supabase.recorded == []
    # Meta la rechaza despues: el estado cambia, la actividad no.
    _with_rejections(inbox, [20_007])
    clock.now += 600
    asyncio.run(sweeper.run_once())
    assert supabase.recorded == [2831]


def test_conversation_settled_needs_every_own_template_confirmed() -> None:
    delivered = {"conversation": {}, "messages": [_template_message()]}
    assert conversation_settled(delivered)
    pending = copy.deepcopy(delivered)
    pending["messages"][0]["status"] = "sent"
    assert not conversation_settled(pending)
    rejected = {"conversation": {}, "messages": [_rejected_message()]}
    assert not conversation_settled(rejected)
    # Un mensaje de la persona en "sent" no es una plantilla nuestra.
    reply = {"conversation": {}, "messages": [_template_message(), _inbound_reply(1, NOW)]}
    assert conversation_settled(reply)


def test_meta_delivery_first_wide_scan_that_cannot_finish_falls_back_to_the_normal_window(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # El masivo de la ultima hora mas 1.500 conversaciones de las 72 h previas
    # (las que se crean de antemano): 5.100 no entran en 200 paginas, 3.600 si.
    inbox = _massive_inbox(older=1_500)
    clock = _Clock(NOW)
    supabase = _MetaSupabase()
    sweeper = MetaDeliveryRejectionSweeper(
        chatwoot=inbox.client(), supabase=supabase, account_id=ACCOUNT, inbox_id=INBOX,
        retry=_retry(tmp_path), clock=clock,
    )
    counts = asyncio.run(sweeper.run_once())
    assert counts["first_scan_wide_incomplete"] == 1
    assert sweeper.last_scan_state == "healthy"
    assert "meta_delivery_first_scan_wide_incomplete" in caplog.text
    # La segunda vuelta ya usa la ventana normal (6 h) y no reintenta las 72 h.
    clock.now += 600
    counts = asyncio.run(sweeper.run_once())
    assert "first_scan_wide_incomplete" not in counts
    assert counts["unchanged_skipped"] == MASSIVE


def test_meta_delivery_normal_window_that_cannot_finish_still_fails_closed(
    tmp_path: Path,
) -> None:
    inbox = _massive_inbox()
    sweeper = MetaDeliveryRejectionSweeper(
        chatwoot=inbox.client(), supabase=_MetaSupabase(), account_id=ACCOUNT,
        inbox_id=INBOX, max_pages=20, clock=_Clock(NOW),
    )
    with pytest.raises(ChatwootProtocolError, match="conversation_scan_incomplete"):
        asyncio.run(sweeper.run_once())


# --- la memoria no esconde lo que cambia sin actividad nueva -----------------

OTHER_PHONE = "+5215599999999"


def _answered(inbox: MassiveInbox, conversation_id: int, inbound_at: int, labels: list[str]) -> None:
    """Contesto la plantilla y el agente le respondio un minuto despues (la 120)."""
    by_id = {m["id"]: m for m in SHOW_120["messages"]}
    template = dict(copy.deepcopy(by_id[2962]), created_at=inbound_at - 600)
    reply = dict(copy.deepcopy(by_id[2968]), created_at=inbound_at)
    agent = dict(copy.deepcopy(by_id[2971]), created_at=inbound_at + 60)
    assert agent["sender"]["type"] == "agent_bot" and agent["message_type"] == 1
    for message in (template, reply, agent):
        message["conversation_id"] = conversation_id
    item = _silent_item()
    item.update({"id": conversation_id, "last_activity_at": inbound_at + 60,
                 "timestamp": inbound_at + 60, "labels": labels})
    inbox.items.append(item)
    inbox.messages[conversation_id] = [template, reply, agent]


def _followup_only_other_phone(inbox: MassiveInbox, clock: _Clock) -> ConversationFollowupSweeper:
    # Con only_phone de otro numero la candidata se cuenta como held_only_phone
    # sin reservar nada: se ve que llego a candidata.
    return ConversationFollowupSweeper(
        chatwoot=inbox.client(), supabase=object(), account_id=ACCOUNT, inbox_id=INBOX,
        template_name="att1_seguimiento_descuento_01", coupon_code="TIROIDES10",
        product_name="Alimenta tu Tiroides", expected_template_language="es_MX",
        only_phone=OTHER_PHONE, clock=clock,
    )


def test_followup_candidate_by_the_passage_of_time_with_the_memo_loaded() -> None:
    inbox = _massive_inbox(50)
    _answered(inbox, 30_000, NOW - 12 * 3_600, [])
    clock = _Clock(NOW)
    sweeper = _followup_only_other_phone(inbox, clock)
    asyncio.run(sweeper.run_once())
    assert sweeper.last_scan_summary == "scanned=51 sent=0 never_replied=50 inbound_too_recent=1"
    inbox.calls()
    clock.now += 300
    asyncio.run(sweeper.run_once())
    # La memoria tiene las 50 plantillas; la que espera el reloj se relee.
    assert inbox.calls() == {"inbox": 1, "listing": 4, "show": 1, "messages": 1}
    # A las 24 h del entrante, sin actividad nueva, es candidata.
    clock.now = NOW + 12 * 3_600 + 60
    asyncio.run(sweeper.run_once())
    assert inbox.calls() == {"inbox": 1, "listing": 4, "show": 1, "messages": 1}
    assert sweeper.last_scan_summary == "scanned=51 sent=0 never_replied=50 held_only_phone=1"


def test_followup_resumes_when_the_pause_label_is_removed_with_the_memo_loaded() -> None:
    inbox = _massive_inbox(50)
    _answered(inbox, 30_001, NOW - 30 * 3_600, ["automation_paused"])
    clock = _Clock(NOW)
    sweeper = _followup_only_other_phone(inbox, clock)
    asyncio.run(sweeper.run_once())
    assert sweeper.last_scan_summary == "scanned=51 sent=0 never_replied=50 conversation_paused=1"
    assert inbox.calls()["show"] == 50
    # El equipo saca la pausa: la etiqueta cambia, la actividad no.
    next(item for item in inbox.items if item["id"] == 30_001)["labels"] = []
    clock.now += 300
    asyncio.run(sweeper.run_once())
    assert inbox.calls() == {"inbox": 1, "listing": 4, "show": 1, "messages": 1}
    assert sweeper.last_scan_summary == "scanned=51 sent=0 never_replied=50 held_only_phone=1"


def test_the_summary_counts_the_listing_reason_before_the_memo() -> None:
    inbox = _massive_inbox(50)
    clock = _Clock(NOW)
    sweeper = _followup(inbox, clock)
    asyncio.run(sweeper.run_once())
    # Una ya asentada como never_replied se pausa: el resumen dice el motivo vigente.
    next(item for item in inbox.items if item["id"] == 20_000)["labels"] = ["automation_paused"]
    clock.now += 300
    asyncio.run(sweeper.run_once())
    assert sweeper.last_scan_summary == "scanned=50 sent=0 never_replied=49 conversation_paused=1"


def test_meta_delivery_does_not_settle_a_conversation_without_messages() -> None:
    # El primer contacto crea la conversacion y manda la plantilla en dos
    # llamadas: si el barrido lee entre medio, la ve vacia. La plantilla entra
    # en el mismo segundo (la marca no cambia) y Meta la rechaza.
    inbox = _massive_inbox(3)
    inbox.messages[20_001] = []
    clock = _Clock(NOW)
    supabase = _MetaSupabase()
    sweeper = MetaDeliveryRejectionSweeper(
        chatwoot=inbox.client(), supabase=supabase, account_id=ACCOUNT, inbox_id=INBOX, clock=clock,
    )
    asyncio.run(sweeper.run_once())
    _with_rejections(inbox, [20_001])
    clock.now += 600
    asyncio.run(sweeper.run_once())
    assert supabase.recorded == [2831]
    assert not conversation_settled({"conversation": {}, "messages": []})


def test_meta_delivery_does_not_settle_what_moved_in_the_last_two_minutes() -> None:
    # Leida con actividad de hace 10 s: un mensaje del mismo segundo no movera
    # last_activity_at, asi que se relee en la vuelta siguiente.
    inbox = _massive_inbox(3)
    item = next(item for item in inbox.items if item["id"] == 20_002)
    item["last_activity_at"] = NOW - 10
    clock = _Clock(NOW)
    supabase = _MetaSupabase()
    sweeper = MetaDeliveryRejectionSweeper(
        chatwoot=inbox.client(), supabase=supabase, account_id=ACCOUNT, inbox_id=INBOX, clock=clock,
    )
    asyncio.run(sweeper.run_once())
    failed = _rejected_message()
    failed["conversation_id"] = 20_002
    inbox.messages[20_002] = [*inbox.messages[20_002], failed]
    clock.now += 600
    asyncio.run(sweeper.run_once())
    assert supabase.recorded == [2831]
