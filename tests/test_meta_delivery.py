"""Lo que Meta no entrego: el registro, el reintento del 131049 y el 131026 (E-284).

Los bordes de Chatwoot salen de capturas del repo:

* ``automation_expert/chatwoot_messages_inbox_11_convs_50_17_52_20261015.json``:
  la API de mensajes de ATT1 (bandeja 11). La conversacion 52 trae la plantilla
  del motor (AgentBot, ``recovery_first_touch_hash``) en ``status = failed`` con
  ``external_error``, y despues un mensaje de actividad. La 17, una respuesta de
  la persona.
* ``chatwoot_daily_feedback_att1_conversations_20261015.json``: el GET show de
  la conversacion 120 de ATT1, con el ultimo mensaje y su
  ``additional_attributes.template_params`` (la API de mensajes no lo trae).
* ``chatwoot_message_templates_inbox_11_carrito_20261018.json``: el catalogo con
  ``att1_carrito_abandonado_01``.

DEUDA (anotada en el reporte E-284): no hay una captura de un show cuyo ultimo
mensaje sea un primer contacto rechazado con 131049. Se arma con el precedente
mas cercano del repo: el envoltorio del show de la 120, con su ultimo mensaje
reemplazado por el 2831 de la 52 y los ``template_params`` con la forma del
3159 de la 120 y el nombre del catalogo. El texto del ``external_error`` esta
tapado en la captura; se repone con la forma medida en la base de Chatwoot de
ATT1 («131049: ...», E-181 y E-282), como ya lo hace
``tests/test_automation_expert_read_app.py``. Hay que capturar la 574 de ATT1
(o cualquier 131049) en solo lectura y reemplazar este armado.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Mapping
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from bridge.chatwoot import FIRST_TOUCH_RETRY_MARKER, ChatwootClient
from bridge.meta_delivery import (
    MetaDeliveryRejectionSweeper,
    RetryConfig,
    evaluate_retry,
    find_rejections,
    meta_error_code,
    parse_retry_templates,
    touch_kind,
    unreachable_notice,
)
from bridge.messaging import FinalMetaEffectGate
from bridge.supabase import (
    DueMetaFirstTouchRetry,
    MetaDeliveryRejectionRecord,
    MetaFirstTouchRetryClaim,
)
from slack_correlation.catalog import EVENT_TEMPLATES, render_message

FIXTURES = Path(__file__).parent / "fixtures"
INBOX = 11
ACCOUNT = 2
TEMPLATE = "att1_carrito_abandonado_01"
MESSAGE = 2831
CONVERSATION = 52


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _messages(conversation: str) -> list[dict]:
    data = _load("automation_expert/chatwoot_messages_inbox_11_convs_50_17_52_20261015.json")
    return copy.deepcopy(data["conversations"][conversation]["payload"])


def _catalog_template() -> dict:
    catalog = _load("chatwoot_message_templates_inbox_11_carrito_20261018.json")
    return next(t for t in catalog["message_templates"] if t["name"] == TEMPLATE)


def _entry(code: str = "131049: This message was not delivered to maintain healthy ecosystem engagement.") -> dict:
    """La 52 rechazada, con el envoltorio del show de la 120 (ver DEUDA arriba)."""
    messages = _messages("52")
    failed = next(m for m in messages if m["id"] == MESSAGE)
    assert failed["status"] == "failed" and failed["sender"]["type"] == "agent_bot"
    assert "recovery_first_touch_hash" in failed["content_attributes"]
    failed["content_attributes"]["external_error"] = code
    failed["content"] = "Hola NOMBRE, vimos que tu inscripcion quedo a mitad de camino."
    show = copy.deepcopy(
        _load("chatwoot_daily_feedback_att1_conversations_20261015.json")["conversations"]["120"]["conversation"]
    )
    last = copy.deepcopy(show["messages"][0])
    params = copy.deepcopy(last["additional_attributes"]["template_params"])
    template = _catalog_template()
    params["name"] = template["name"]
    params["language"] = template["language"]
    params["category"] = template["category"]
    last.update({"id": MESSAGE, "conversation_id": CONVERSATION, "content": failed["content"],
                 "status": "failed", "content_attributes": failed["content_attributes"]})
    last["additional_attributes"] = {"template_params": params}
    show.update({"id": CONVERSATION, "inbox_id": INBOX, "messages": [last], "status": "open"})
    return {"conversation": show, "messages": messages}


NOW_25H = 1791661900 + 25 * 3600


# ------------------------------------------------------------- el criterio


def test_touch_kind_and_code_on_captured_messages() -> None:
    entry = _entry()
    failed = next(m for m in entry["messages"] if m["id"] == MESSAGE)
    assert touch_kind(failed) == "first_touch"
    assert meta_error_code(failed) == "131049"
    # El mensaje del equipo fallido de la 50: no es una plantilla nuestra.
    team = next(m for m in _messages("50") if m.get("status") == "failed")
    assert touch_kind(team) is None
    assert meta_error_code({"content_attributes": {"external_error": "Template not found"}}) is None
    # En la base de Chatwoot content_attributes es un JSON guardado como texto.
    as_text = dict(failed, content_attributes=json.dumps(failed["content_attributes"]))
    assert touch_kind(as_text) == "first_touch" and meta_error_code(as_text) == "131049"
    retry = dict(failed, content_attributes={**failed["content_attributes"], FIRST_TOUCH_RETRY_MARKER: MESSAGE})
    assert touch_kind(retry) == "first_touch_retry"


def test_find_rejections_reads_code_and_template_from_the_show() -> None:
    [rejection] = find_rejections(_entry(), inbox_id=INBOX)
    assert rejection.message_id == MESSAGE
    assert rejection.conversation_id == CONVERSATION
    assert rejection.code == "131049"
    assert rejection.touch_kind == "first_touch"
    assert rejection.template_name == TEMPLATE
    # Un mensaje leido no es un rechazo (la 17: la plantilla se leyo).
    entry = _entry()
    for message in entry["messages"]:
        message["status"] = "read"
    assert find_rejections(entry, inbox_id=INBOX) == []


def test_find_rejections_without_the_show_template_keeps_the_rejection() -> None:
    entry = _entry()
    entry["conversation"]["messages"][0]["id"] = MESSAGE + 1
    [rejection] = find_rejections(entry, inbox_id=INBOX)
    assert rejection.template_name is None


def _evaluate(entry: dict, now: int = NOW_25H, allowed: frozenset[str] = frozenset({TEMPLATE})):
    return evaluate_retry(entry, message_id=MESSAGE, inbox_id=INBOX, now_epoch=now,
                          allowed_templates=allowed, max_age_seconds=259_200)


def test_retry_plan_resends_the_same_template_after_24_hours() -> None:
    plan = _evaluate(_entry())
    assert not isinstance(plan, str), plan
    assert plan.template_name == TEMPLATE
    assert plan.template_params["processed_params"] == (
        _entry()["conversation"]["messages"][0]["additional_attributes"]["template_params"]["processed_params"]
    )
    assert plan.content.startswith("Hola NOMBRE")
    assert plan.target_phone.startswith("+")
    # El mensaje de actividad capturado despues (3054) no cuenta.
    assert any(m["message_type"] == 2 and m["id"] > MESSAGE for m in _entry()["messages"])


def test_retry_refuses_what_the_policy_excludes() -> None:
    assert _evaluate(_entry(), now=1791661900 + 23 * 3600) == "too_early"
    assert _evaluate(_entry(), now=1791661900 + 73 * 3600) == "too_late"
    assert _evaluate(_entry(), allowed=frozenset({"otra_plantilla"})) == "template_not_retryable"
    assert _evaluate(_entry("131026: Message undeliverable")) == "not_retryable"
    assert _evaluate(_entry("130472: experiment")) == "not_retryable"
    # La persona escribio despues: la respuesta capturada de la 17.
    wrote = _entry()
    reply = next(m for m in _messages("17") if m["message_type"] == 0)
    wrote["messages"].append(dict(reply, id=MESSAGE + 500, conversation_id=CONVERSATION))
    assert _evaluate(wrote) == "lead_wrote"
    # Otro mensaje nuestro o del equipo despues.
    moved = _entry()
    team = next(m for m in _messages("17") if m["message_type"] == 1 and m["sender"]["type"] == "user")
    moved["messages"].append(dict(team, id=MESSAGE + 600, conversation_id=CONVERSATION))
    assert _evaluate(moved) == "conversation_moved_on"
    unreadable = _entry()
    unreadable["conversation"]["messages"][0]["id"] = MESSAGE - 1
    assert _evaluate(unreadable) == "template_params_unreadable"
    blocked = _entry()
    blocked["conversation"]["meta"]["sender"]["blocked"] = True
    assert _evaluate(blocked) == "contact_blocked"
    snoozed = _entry()
    snoozed["conversation"]["status"] = "snoozed"
    assert _evaluate(snoozed) == "conversation_snoozed"


def test_unreachable_notice_is_msg_002_with_the_conversation_and_no_phone() -> None:
    [rejection] = find_rejections(_entry("131026: Message undeliverable."), inbox_id=INBOX)
    command = unreachable_notice(rejection, inbox_id=INBOX)
    assert command.event_code == "MSG-002" and EVENT_TEMPLATES["MSG-002"].severity == "p2"
    assert command.component == f"chatwoot.conversation.{CONVERSATION}"
    assert command.reason_code == "meta_131026_first_touch"
    assert unreachable_notice(rejection, inbox_id=INBOX).event_id == command.event_id
    rendered = json.dumps(render_message(command, tenant_label="ATT1"), ensure_ascii=False)
    sender = _entry()["conversation"]["meta"]["sender"]
    assert sender["phone_number"] not in rendered and sender["email"] not in rendered
    assert "Envío falló definitivamente" in rendered


def test_parse_retry_templates() -> None:
    assert parse_retry_templates("att1_carrito_abandonado_01, att1_interes_01") == frozenset(
        {"att1_carrito_abandonado_01", "att1_interes_01"})
    for raw in ("", "a,,b", "Mayuscula", "con espacio"):
        with pytest.raises(ValueError):
            parse_retry_templates(raw)


# ------------------------------------------------------------- el barredor


class FakeChatwoot:
    def __init__(self, entry: dict) -> None:
        self.entry = entry
        self.sent: list[dict] = []
        self.listed: list[dict] = []

    async def list_recent_conversations_with_messages(self, **kwargs: object) -> list[dict]:
        self.listed.append(kwargs)
        return [copy.deepcopy(self.entry)]

    async def get_conversation_with_messages(self, **kwargs: object) -> dict:
        assert kwargs["conversation_id"] == CONVERSATION and kwargs["expected_inbox_id"] == INBOX
        return copy.deepcopy(self.entry)

    async def send_first_message(self, **kwargs: object) -> dict:
        self.sent.append(kwargs)
        return {"status": "sent", "message_id": 990001}


class FakeSupabase:
    def __init__(self, *, needs_notice: bool = False, claim: str = "claimed", due: bool = True) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._needs_notice = needs_notice
        self._claim = claim
        self._due = due

    async def record_meta_delivery_rejection(self, **kwargs: object) -> MetaDeliveryRejectionRecord:
        self.calls.append(("record", kwargs))
        return MetaDeliveryRejectionRecord("recorded", "11111111-1111-4111-8111-111111111111", "none",
                                           "case_closed" if self._needs_notice else None, self._needs_notice)

    async def mark_meta_delivery_rejection_notified(self, **kwargs: object) -> str:
        self.calls.append(("mark", kwargs))
        return "marked"

    async def list_due_meta_first_touch_retries(self, **kwargs: object) -> list[DueMetaFirstTouchRetry]:
        self.calls.append(("due", kwargs))
        if not self._due:
            return []
        return [DueMetaFirstTouchRetry("11111111-1111-4111-8111-111111111111", CONVERSATION, MESSAGE)]

    async def claim_meta_first_touch_retry(self, **kwargs: object) -> MetaFirstTouchRetryClaim:
        self.calls.append(("claim", kwargs))
        return MetaFirstTouchRetryClaim(self._claim, "11111111-1111-4111-8111-111111111111")

    async def settle_meta_first_touch_retry(self, **kwargs: object) -> str:
        self.calls.append(("settle", kwargs))
        return "settled"

    async def block_meta_first_touch_retry(self, **kwargs: object) -> str:
        self.calls.append(("block", kwargs))
        return "blocked"

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakeProducer:
    def __init__(self) -> None:
        self.commands: list[object] = []

    async def admit(self, command: object) -> object:
        self.commands.append(command)
        return object()


def _clock_at(epoch: int):
    return lambda: float(epoch)


def _retry(tmp_path: Path, *, gate_open: bool = True) -> RetryConfig:
    return RetryConfig(
        allowed_templates=frozenset({TEMPLATE}),
        send_hours=(0, 24),
        time_zone=ZoneInfo("America/Mexico_City"),
        gate=FinalMetaEffectGate(enabled=gate_open, evidence_dir=tmp_path / "gate"),
    )


def _sweeper(chatwoot: FakeChatwoot, supabase: FakeSupabase, **kwargs: object) -> MetaDeliveryRejectionSweeper:
    defaults: dict[str, object] = {"clock": _clock_at(NOW_25H)}
    defaults.update(kwargs)
    return MetaDeliveryRejectionSweeper(chatwoot=chatwoot, supabase=supabase, account_id=ACCOUNT,
                                        inbox_id=INBOX, **defaults)  # type: ignore[arg-type]


def test_sweeper_records_without_retry_and_never_sends() -> None:
    chatwoot, supabase = FakeChatwoot(_entry()), FakeSupabase()
    counts = asyncio.run(_sweeper(chatwoot, supabase).run_once())
    assert supabase.names() == ["record"]
    record = supabase.calls[0][1]
    assert record["meta_error_code"] == "131049" and record["touch_kind"] == "first_touch"
    assert record["template_name"] == TEMPLATE and record["chatwoot_message_id"] == MESSAGE
    assert record["close_unreachable"] is False
    assert chatwoot.sent == [] and counts == {"recorded": 1}
    assert chatwoot.listed[0]["statuses"] == ("open", "resolved", "pending", "snoozed")


def test_sweeper_retries_once_with_the_same_template(tmp_path: Path) -> None:
    chatwoot, supabase = FakeChatwoot(_entry()), FakeSupabase()
    counts = asyncio.run(_sweeper(chatwoot, supabase, retry=_retry(tmp_path)).run_once())
    assert supabase.names() == ["record", "due", "claim", "settle"]
    claim = supabase.calls[2][1]
    assert claim["chatwoot_message_id"] == MESSAGE and claim["external_user_id"].isdigit()
    [sent] = chatwoot.sent
    show_params = _entry()["conversation"]["messages"][0]["additional_attributes"]["template_params"]
    assert sent["template_params"] == show_params
    assert sent["retry_of_message_id"] == MESSAGE and sent["conversation_id"] == CONVERSATION
    assert sent["delivery_id"] == f"meta-retry:{INBOX}:{MESSAGE}"
    settle = supabase.calls[3][1]
    assert settle["status"] == "sent" and settle["retry_message_id"] == 990001
    assert counts["retried"] == 1
    # El primer barrido mira el techo entero; el siguiente, la ventana corta.
    sweeper = _sweeper(chatwoot, FakeSupabase(due=False), retry=_retry(tmp_path))
    asyncio.run(sweeper.run_once())
    asyncio.run(sweeper.run_once())
    assert [c["active_since_epoch"] for c in chatwoot.listed[-2:]] == [NOW_25H - 259_200, NOW_25H - 21_600]


def test_sweeper_does_not_send_when_the_base_blocks(tmp_path: Path) -> None:
    chatwoot, supabase = FakeChatwoot(_entry()), FakeSupabase(claim="purchase_already_approved")
    counts = asyncio.run(_sweeper(chatwoot, supabase, retry=_retry(tmp_path)).run_once())
    assert chatwoot.sent == [] and "settle" not in supabase.names()
    assert counts["retry_purchase_already_approved"] == 1


def test_sweeper_blocks_permanent_skips_in_the_base(tmp_path: Path) -> None:
    entry = _entry()
    reply = next(m for m in _messages("17") if m["message_type"] == 0)
    entry["messages"].append(dict(reply, id=MESSAGE + 500, conversation_id=CONVERSATION))
    chatwoot, supabase = FakeChatwoot(entry), FakeSupabase()
    asyncio.run(_sweeper(chatwoot, supabase, retry=_retry(tmp_path)).run_once())
    assert supabase.names() == ["record", "due", "block"]
    assert supabase.calls[2][1]["reason"] == "bridge_lead_wrote"
    assert chatwoot.sent == []


def test_sweeper_respects_the_send_hours_and_the_gate(tmp_path: Path) -> None:
    closed_hours = RetryConfig(allowed_templates=frozenset({TEMPLATE}), send_hours=(9, 10),
                               time_zone=ZoneInfo("UTC"),
                               gate=FinalMetaEffectGate(enabled=True, evidence_dir=tmp_path / "g"))
    hour = NOW_25H // 86_400 * 86_400 + 12 * 3600  # las 12:00 UTC del mismo dia
    chatwoot, supabase = FakeChatwoot(_entry()), FakeSupabase()
    counts = asyncio.run(_sweeper(chatwoot, supabase, retry=closed_hours, clock=_clock_at(hour)).run_once())
    assert "due" not in supabase.names() and counts["retry_outside_send_hours"] == 1
    chatwoot, supabase = FakeChatwoot(_entry()), FakeSupabase()
    asyncio.run(_sweeper(chatwoot, supabase, retry=_retry(tmp_path, gate_open=False)).run_once())
    assert "claim" not in supabase.names() and chatwoot.sent == []


def test_sweeper_closes_and_notifies_131026_once() -> None:
    chatwoot = FakeChatwoot(_entry("131026: Message undeliverable."))
    supabase, producer = FakeSupabase(needs_notice=True), FakeProducer()
    counts = asyncio.run(_sweeper(chatwoot, supabase, close_unreachable=True, producer=producer).run_once())
    assert supabase.calls[0][1]["close_unreachable"] is True
    assert supabase.names() == ["record", "mark"]
    [command] = producer.commands
    assert command.event_code == "MSG-002"  # type: ignore[attr-defined]
    assert counts == {"recorded": 1, "notified": 1}
    # Sin conector de Slack: se registra y se cuenta, sin marcar el aviso.
    supabase = FakeSupabase(needs_notice=True)
    counts = asyncio.run(_sweeper(chatwoot, supabase, close_unreachable=True).run_once())
    assert supabase.names() == ["record"] and counts["notice_without_slack"] == 1


def test_retry_config_rejects_what_is_not_the_policy(tmp_path: Path) -> None:
    gate = FinalMetaEffectGate(enabled=True, evidence_dir=tmp_path)
    zone = ZoneInfo("UTC")
    for kwargs in ({"allowed_templates": frozenset()}, {"send_hours": (10, 9)},
                   {"max_age_seconds": 3600}, {"max_per_scan": 50}):
        base: dict[str, object] = {"allowed_templates": frozenset({TEMPLATE}), "send_hours": (9, 21),
                                   "time_zone": zone, "gate": gate}
        base.update(kwargs)
        with pytest.raises(ValueError):
            RetryConfig(**base)  # type: ignore[arg-type]


# ------------------------------------------------------------- el envio


def _send(retry_of: int | None) -> Mapping[str, object]:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.update(body)
        return httpx.Response(200, json={
            "id": 990001, "conversation_id": CONVERSATION, "message_type": 1, "private": False,
            "content": body["content"], "content_attributes": body["content_attributes"],
        })

    client = ChatwootClient(base_url="https://chatwoot.example.test", account_id=ACCOUNT,
                            access_token="control", agent_bot_access_token="bot", agent_bot_id=2,
                            transport=httpx.MockTransport(handler))
    kwargs: dict[str, object] = {} if retry_of is None else {"retry_of_message_id": retry_of}
    result = asyncio.run(client.send_first_message(conversation_id=CONVERSATION, content="Hola",
                                                   delivery_id="d", template_params={"name": TEMPLATE},
                                                   **kwargs))
    assert result == {"status": "sent", "message_id": 990001}
    return seen


def test_send_first_message_marks_only_the_retry() -> None:
    plain = _send(None)
    assert set(plain["content_attributes"]) == {"recovery_first_touch_hash"}  # type: ignore[arg-type]
    retried = _send(MESSAGE)
    assert retried["content_attributes"][FIRST_TOUCH_RETRY_MARKER] == MESSAGE  # type: ignore[index]
    with pytest.raises(ValueError):
        asyncio.run(ChatwootClient(base_url="https://x.test", account_id=1, access_token="t",
                                   agent_bot_access_token="b", agent_bot_id=2)
                    .send_first_message(conversation_id=1, content="x", delivery_id="d",
                                        purchase_welcome=True, retry_of_message_id=5))
