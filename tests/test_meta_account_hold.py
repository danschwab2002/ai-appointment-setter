"""Un rechazo de la cuenta no gasta el toque (E-348, P-33).

Decisiones de Dan del 2026-11-07: D1 solo el 131042 (lista en
``META_ACCOUNT_HOLD_CODES``), D2 vence a las 72 h, D3 solo los flujos de un
mensaje (la lista de plantillas del 131049), D4 nada retroactivo, D5 la cuenta
volvio cuando se entrega o se lee una plantilla nuestra despues del ultimo
rechazo (la sonda sin trafico no esta: mandaria el mensaje real a alguien), D6
el aviso cuando se libera la cola.

Los bordes de Chatwoot salen de capturas del repo:

* ``chatwoot_db_messages_inbox_13_maribel_131042_20261107.json``: los dos
  primeros 131042 de Maribel del 04/11 (5781 y 5782, conversaciones 122 y 123)
  y la primera plantilla entregada despues (5884, conversacion 143, leida el
  05/11 17:08Z), leidos de la base de Chatwoot en solo lectura y pasados a la
  forma de la API de mensajes (la adaptacion esta escrita en el fixture).
* ``chatwoot_daily_feedback_att1_conversations_20261015.json``: el envoltorio
  del GET show (contacto y ``template_params`` del ultimo mensaje), como en
  ``tests/test_meta_delivery.py``. La captura de Maribel no copio los
  ``template_params`` a proposito; el contenido es sintetico.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bridge.chatwoot import FIRST_TOUCH_RETRY_MARKER
from bridge.meta_delivery import (
    DEFAULT_ACCOUNT_HOLD_CODES,
    AccountHoldConfig,
    MetaDeliveryRejectionSweeper,
    RetryConfig,
    account_hold_release_notice,
    evaluate_retry,
    find_rejections,
    latest_template_delivered,
    meta_error_code,
    parse_account_hold_codes,
    touch_kind,
)
from bridge.messaging import FinalMetaEffectGate
from bridge.supabase import (
    DueMetaAccountHoldRetry,
    MetaAccountHoldClaim,
    MetaAccountHoldRelease,
    MetaDeliveryRejectionRecord,
    SupabaseError,
)
from slack_correlation.catalog import EVENT_TEMPLATES, render_message

FIXTURES = Path(__file__).parent / "fixtures"
CAPTURE = "chatwoot_db_messages_inbox_13_maribel_131042_20261107.json"
INBOX = 13
ACCOUNT = 3
TEMPLATE = "maribel_carrito_abandonado_01"
MESSAGE = 5781
CONVERSATION = 122
DELIVERED_CONVERSATION = 143
ID = "22222222-2222-4222-8222-222222222222"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _capture() -> dict:
    return copy.deepcopy(_load(CAPTURE))


def _failed(message_id: int = MESSAGE) -> dict:
    return next(m for m in _capture()["rechazados_131042"] if m["id"] == message_id)


def _show(conversation_id: int, last: dict, *, template: str = TEMPLATE) -> dict:
    """El envoltorio del show de ATT1 con el ultimo mensaje de Maribel."""
    show = copy.deepcopy(
        _load("chatwoot_daily_feedback_att1_conversations_20261015.json")["conversations"]["120"]["conversation"]
    )
    shown = copy.deepcopy(show["messages"][0])
    params = copy.deepcopy(shown["additional_attributes"]["template_params"])
    params["name"] = template
    shown.update({"id": last["id"], "conversation_id": conversation_id, "content": last.get("content"),
                  "status": last["status"], "content_attributes": last["content_attributes"]})
    shown["additional_attributes"] = {"template_params": params}
    show.update({"id": conversation_id, "inbox_id": INBOX, "messages": [shown], "status": "open"})
    return show


def _entry(message_id: int = MESSAGE, conversation_id: int = CONVERSATION) -> dict:
    """Una conversacion de Maribel con su primer contacto rechazado por la cuenta."""
    failed = _failed(message_id)
    failed["content"] = "Hola NOMBRE, vimos que tu inscripcion quedo a mitad de camino."
    return {"conversation": _show(conversation_id, failed), "messages": [failed]}


def _delivered_entry() -> dict:
    delivered = _capture()["primera_plantilla_entregada_despues"]
    delivered["content"] = "Hola NOMBRE, te escribimos por tu inscripcion."
    return {"conversation": _show(DELIVERED_CONVERSATION, delivered), "messages": [delivered]}


SENT_AT = _failed()["created_at"]
DELIVERED_AT = _capture()["primera_plantilla_entregada_despues"]["created_at"]
# El 05/11 17:20Z: la cuenta ya volvio y el rechazo tiene 26 h.
NOW = DELIVERED_AT + 12 * 60
HOLD_AGE = 259_200


# ------------------------------------------------------------- la captura


def test_the_fixture_is_the_capture_in_the_api_shape() -> None:
    capture = _capture()
    statuses = {0: "sent", 1: "delivered", 2: "read", 3: "failed"}
    pairs = list(zip(capture["rechazados_131042"], capture["captura_original"]["rechazados_131042"], strict=True))
    pairs.append((capture["primera_plantilla_entregada_despues"],
                  capture["captura_original"]["primera_plantilla_entregada_despues"]))
    for adapted, row in pairs:
        created = datetime.fromisoformat(row["created_at"]).replace(tzinfo=UTC)
        assert adapted["id"] == row["message_id"]
        assert adapted["conversation_id"] == row["conversation_display_id"]
        assert adapted["status"] == statuses[row["status"]]
        assert adapted["created_at"] == int(created.timestamp())
        assert adapted["content_attributes"] == row["content_attributes"]
    # Lo medido: dos 131042 seguidos el 04/11 15:00Z y la primera plantilla
    # entregada despues, 26 h mas tarde, leida.
    assert [m["status"] for m in capture["rechazados_131042"]] == ["failed", "failed"]
    assert capture["primera_plantilla_entregada_despues"]["status"] == "read"
    assert DELIVERED_AT - SENT_AT > 24 * 3600


def test_the_captured_131042_is_a_first_touch_of_the_account() -> None:
    failed = _failed()
    assert touch_kind(failed) == "first_touch"
    assert meta_error_code(failed) == "131042"
    assert "131042" in DEFAULT_ACCOUNT_HOLD_CODES
    [rejection] = find_rejections(_entry(), inbox_id=INBOX)
    assert rejection.code == "131042" and rejection.template_name == TEMPLATE
    assert rejection.message_id == MESSAGE and rejection.conversation_id == CONVERSATION


def test_the_signal_is_a_template_of_ours_delivered_or_read() -> None:
    assert latest_template_delivered(_delivered_entry()) == DELIVERED_AT
    # Un rechazo no es la senal.
    assert latest_template_delivered(_entry()) is None
    # Un mensaje entregado que no es una plantilla nuestra tampoco: con el
    # problema de pago, las respuestas en la ventana de 24 h salen igual.
    reply = _delivered_entry()
    reply["messages"][0]["content_attributes"] = {}
    assert latest_template_delivered(reply) is None
    sent_only = _delivered_entry()
    sent_only["messages"][0]["status"] = "sent"
    assert latest_template_delivered(sent_only) is None


def test_the_signal_needs_template_params_not_only_the_mark() -> None:
    """H5: un mensaje con la marca del bridge pero sin plantilla no es la senal."""
    freeform = _delivered_entry()
    # El show trae otro ultimo mensaje: no hay template_params de la 5884.
    freeform["conversation"]["messages"][0]["id"] = 5884 + 1
    assert latest_template_delivered(freeform) is None
    # Con los template_params en el propio mensaje (la base de Chatwoot los tiene) si.
    own = copy.deepcopy(freeform)
    own["messages"][0]["additional_attributes"] = {
        "template_params": copy.deepcopy(_delivered_entry()["conversation"]["messages"][0]
                                         ["additional_attributes"]["template_params"])}
    assert latest_template_delivered(own) == DELIVERED_AT


# ------------------------------------------------------------- el criterio


def _hold_evaluate(entry: dict, now: int = NOW, *, allowed: frozenset[str] = frozenset({TEMPLATE})):
    return evaluate_retry(entry, message_id=MESSAGE, inbox_id=INBOX, now_epoch=now,
                          allowed_templates=allowed, max_age_seconds=HOLD_AGE,
                          retryable_codes=frozenset({"131042"}), min_age_seconds=0,
                          ignore_own_failed_retries=True)


def test_the_hold_plan_resends_the_same_template_without_the_24_hour_wait() -> None:
    plan = _hold_evaluate(_entry(), now=SENT_AT + 600)
    assert not isinstance(plan, str), plan
    assert plan.template_name == TEMPLATE and plan.message_id == MESSAGE
    assert plan.content.startswith("Hola NOMBRE")
    # El criterio del 131049 no toca un 131042.
    assert evaluate_retry(_entry(), message_id=MESSAGE, inbox_id=INBOX, now_epoch=NOW,
                          allowed_templates=frozenset({TEMPLATE}), max_age_seconds=HOLD_AGE) == "not_retryable"


def test_the_hold_refuses_what_the_policy_excludes() -> None:
    assert _hold_evaluate(_entry(), now=SENT_AT + 73 * 3600) == "too_late"
    assert _hold_evaluate(_entry(), allowed=frozenset({"otra_plantilla"})) == "template_not_retryable"
    wrote = _entry()
    wrote["messages"].append({"id": MESSAGE + 500, "conversation_id": CONVERSATION, "message_type": 0,
                              "private": False, "status": "sent", "created_at": SENT_AT + 60,
                              "content": "hola", "content_attributes": {}})
    assert _hold_evaluate(wrote) == "lead_wrote"
    moved = _entry()
    moved["messages"].append({"id": MESSAGE + 600, "conversation_id": CONVERSATION, "message_type": 1,
                              "private": False, "status": "delivered", "created_at": SENT_AT + 60,
                              "content": "equipo", "content_attributes": {}})
    assert _hold_evaluate(moved) == "conversation_moved_on"


def test_an_own_retry_rejected_again_does_not_count_as_moving_on() -> None:
    entry = _entry()
    retry = copy.deepcopy(entry["messages"][0])
    retry["id"] = 990_001
    retry["created_at"] = SENT_AT + 3600
    retry["content_attributes"] = {**retry["content_attributes"], FIRST_TOUCH_RETRY_MARKER: MESSAGE}
    entry["messages"].append(retry)
    # El show trae como ultimo al reintento: los parametros salen de ahi.
    entry["conversation"] = _show(CONVERSATION, retry)
    plan = _hold_evaluate(entry)
    assert not isinstance(plan, str), plan
    assert plan.message_id == MESSAGE and plan.template_name == TEMPLATE
    # Para el 131049 el mismo reintento si es «la conversacion siguio».
    assert evaluate_retry(entry, message_id=MESSAGE, inbox_id=INBOX, now_epoch=NOW,
                          allowed_templates=frozenset({TEMPLATE}), max_age_seconds=HOLD_AGE,
                          retryable_codes=frozenset({"131042"}), min_age_seconds=0) == "conversation_moved_on"


def test_the_codes_and_the_config() -> None:
    assert parse_account_hold_codes("") == frozenset({"131042"})
    assert parse_account_hold_codes("131042, 131031") == frozenset({"131042", "131031"})
    for raw in ("131049", "131042,131026", "abc", "131042,,1", ",".join(str(100 + i) for i in range(11))):
        with pytest.raises(ValueError):
            parse_account_hold_codes(raw)
    gate = FinalMetaEffectGate(enabled=True, evidence_dir=Path("/tmp/unused-e348"))
    base: dict[str, object] = {"codes": frozenset({"131042"}), "allowed_templates": frozenset({TEMPLATE}),
                               "send_hours": (9, 21), "time_zone": ZoneInfo("UTC"), "gate": gate}
    AccountHoldConfig(**base)  # type: ignore[arg-type]
    for bad in ({"codes": frozenset()}, {"codes": frozenset({"131049"})}, {"allowed_templates": frozenset()},
                {"send_hours": (10, 9)}, {"max_age_seconds": 60}, {"max_age_seconds": 700_000},
                {"max_per_scan": 50}):
        with pytest.raises(ValueError):
            AccountHoldConfig(**{**base, **bad})  # type: ignore[arg-type]


def test_the_release_notice_counts_without_anyone_inside() -> None:
    command = account_hold_release_notice(inbox_id=INBOX, released=2, account_ok_epoch=DELIVERED_AT)
    assert command.event_code == "SYS-008" and EVENT_TEMPLATES["SYS-008"].severity == "p4"
    assert command.count == 2 and command.reason_code == "meta_account_hold_released"
    assert command.component == f"chatwoot.inbox.{INBOX}"
    assert account_hold_release_notice(inbox_id=INBOX, released=2, account_ok_epoch=DELIVERED_AT).event_id \
        == command.event_id
    rendered = json.dumps(render_message(command, tenant_label="Maribel"), ensure_ascii=False)
    sender = _entry()["conversation"]["meta"]["sender"]
    assert sender["phone_number"] not in rendered and sender["email"] not in rendered
    assert "Recuperación confirmada" in rendered and "`2`" in rendered


# ------------------------------------------------------------- el barredor


class FakeChatwoot:
    def __init__(self, *entries: dict) -> None:
        self.entries = {e["conversation"]["id"]: e for e in entries}
        self.sent: list[dict] = []
        self.listed: list[dict] = []

    async def list_recent_conversations_with_messages(self, **kwargs: object) -> list[dict]:
        self.listed.append(kwargs)
        return [copy.deepcopy(e) for e in self.entries.values()]

    async def get_conversation_with_messages(self, **kwargs: object) -> dict:
        assert kwargs["expected_inbox_id"] == INBOX
        return copy.deepcopy(self.entries[kwargs["conversation_id"]])

    async def send_first_message(self, **kwargs: object) -> dict:
        self.sent.append(kwargs)
        return {"status": "sent", "message_id": 990_001}


class FakeSupabase:
    def __init__(self, *, record: str = "recorded", released: int = 0, due: bool = False,
                 claim: str = "claimed") -> None:
        self.calls: list[tuple[str, dict]] = []
        self._record = record
        self._released = released
        self._due = due
        self._claim = claim
        self.fail_mark = 0
        self.fail_hold = 0
        self.hold_outcome = "held"

    async def record_meta_delivery_rejection(self, **kwargs: object) -> MetaDeliveryRejectionRecord:
        self.calls.append(("record", kwargs))
        return MetaDeliveryRejectionRecord(self._record, ID, "none", None, False)

    async def mark_meta_account_hold_enabled(self, **kwargs: object) -> str:
        self.calls.append(("mark", kwargs))
        if self.fail_mark:
            self.fail_mark -= 1
            raise SupabaseError("meta_account_hold_mark_failed: HTTP 503")
        return "marked"

    async def hold_meta_account_rejection(self, **kwargs: object) -> str:
        self.calls.append(("hold", kwargs))
        if self.fail_hold:
            self.fail_hold -= 1
            raise SupabaseError("meta_account_hold_failed: HTTP 503")
        return self.hold_outcome

    async def requeue_meta_account_hold(self, **kwargs: object) -> str:
        self.calls.append(("requeue", kwargs))
        return "requeued"

    async def release_meta_account_holds(self, **kwargs: object) -> MetaAccountHoldRelease:
        self.calls.append(("release", kwargs))
        return MetaAccountHoldRelease(self._released, 0, 0 if self._released else 1)

    async def list_due_meta_account_hold_retries(self, **kwargs: object) -> list[DueMetaAccountHoldRetry]:
        self.calls.append(("due", kwargs))
        return [DueMetaAccountHoldRetry(ID, CONVERSATION, MESSAGE, 0)] if self._due else []

    async def claim_meta_account_hold_retry(self, **kwargs: object) -> MetaAccountHoldClaim:
        self.calls.append(("claim", kwargs))
        return MetaAccountHoldClaim(self._claim, ID, 1 if self._claim == "claimed" else 0)

    async def settle_meta_account_hold_retry(self, **kwargs: object) -> str:
        self.calls.append(("settle", kwargs))
        return "settled"

    async def block_meta_account_hold_retry(self, **kwargs: object) -> str:
        self.calls.append(("block", kwargs))
        return "blocked"

    def names(self) -> list[str]:
        return [name for name, _ in self.calls if name != "mark"]

    def call(self, name: str) -> dict:
        return next(kwargs for n, kwargs in self.calls if n == name)


class FakeProducer:
    def __init__(self) -> None:
        self.commands: list[object] = []

    async def admit(self, command: object) -> object:
        self.commands.append(command)
        return object()


def _hold(tmp_path: Path, *, gate_open: bool = True, hours: tuple[int, int] = (0, 24)) -> AccountHoldConfig:
    return AccountHoldConfig(
        codes=frozenset({"131042"}),
        allowed_templates=frozenset({TEMPLATE}),
        send_hours=hours,
        time_zone=ZoneInfo("America/Mexico_City"),
        gate=FinalMetaEffectGate(enabled=gate_open, evidence_dir=tmp_path / "gate"),
    )


def _sweeper(chatwoot: FakeChatwoot, supabase: FakeSupabase, **kwargs: object) -> MetaDeliveryRejectionSweeper:
    defaults: dict[str, object] = {"clock": lambda: float(NOW)}
    defaults.update(kwargs)
    return MetaDeliveryRejectionSweeper(chatwoot=chatwoot, supabase=supabase, account_id=ACCOUNT,
                                        inbox_id=INBOX, **defaults)  # type: ignore[arg-type]


def test_without_the_flag_a_131042_is_only_recorded() -> None:
    chatwoot, supabase = FakeChatwoot(_entry(), _delivered_entry()), FakeSupabase()
    counts = asyncio.run(_sweeper(chatwoot, supabase).run_once())
    assert supabase.names() == ["record"] and chatwoot.sent == []
    assert counts == {"recorded": 1}


def test_a_new_131042_waits_and_nothing_goes_out_without_the_signal(tmp_path: Path) -> None:
    chatwoot, supabase = FakeChatwoot(_entry()), FakeSupabase()
    sweeper = _sweeper(chatwoot, supabase, account_hold=_hold(tmp_path), clock=lambda: float(SENT_AT + 600))
    counts = asyncio.run(sweeper.run_once())
    assert supabase.names() == ["record", "hold", "release", "due"]
    assert supabase.call("hold")["rejection_id"] == ID and supabase.call("hold")["max_age_seconds"] == HOLD_AGE
    release = supabase.call("release")
    assert release["codes"] == ["131042"] and release["account_ok_at"] is None
    assert chatwoot.sent == [] and counts["hold_held"] == 1 and counts["hold_waiting"] == 1
    # El primer barrido mira el vencimiento entero.
    assert chatwoot.listed[0]["active_since_epoch"] == SENT_AT + 600 - HOLD_AGE


def test_a_replayed_rejection_asks_the_base_once_and_the_base_cuts_by_the_mark(tmp_path: Path) -> None:
    """H2 y H4: un replayed tambien se pregunta (una llamada fallida se repite), y
    lo anterior a la marca de prendido lo corta la base (D4)."""
    chatwoot, supabase = FakeChatwoot(_entry()), FakeSupabase(record="replayed")
    supabase.hold_outcome = "before_enabled"
    sweeper = _sweeper(chatwoot, supabase, account_hold=_hold(tmp_path))
    counts = asyncio.run(sweeper.run_once())
    asyncio.run(sweeper.run_once())
    assert supabase.names().count("hold") == 1 and counts["hold_before_enabled"] == 1
    # La marca se pide una vez por proceso, al primer barrido.
    assert [name for name, _ in supabase.calls].count("mark") == 1
    assert supabase.call("mark")["chatwoot_inbox_id"] == INBOX


def test_a_failed_hold_does_not_stop_the_scan_and_is_retried(tmp_path: Path) -> None:
    """H2: un error de la base al poner en espera no corta el barrido."""
    retry = RetryConfig(allowed_templates=frozenset({TEMPLATE}), send_hours=(0, 24),
                        time_zone=ZoneInfo("UTC"),
                        gate=FinalMetaEffectGate(enabled=True, evidence_dir=tmp_path / "r"))

    class Both(FakeSupabase):
        async def list_due_meta_first_touch_retries(self, **kwargs: object) -> list:
            self.calls.append(("due_131049", kwargs))
            return []

    supabase = Both()
    supabase.fail_hold = 1
    sweeper = _sweeper(FakeChatwoot(_entry()), supabase, retry=retry, account_hold=_hold(tmp_path))
    counts = asyncio.run(sweeper.run_once())
    assert counts["hold_error"] == 1 and sweeper.last_scan_state == "healthy"
    assert supabase.names() == ["record", "hold", "due_131049", "release", "due"]
    # El barrido siguiente: el registro da replayed y la espera se pide otra vez.
    supabase._record = "replayed"
    counts = asyncio.run(sweeper.run_once())
    assert counts["hold_held"] == 1


def test_without_the_mark_nothing_is_held_and_the_mark_is_retried(tmp_path: Path) -> None:
    supabase = FakeSupabase()
    supabase.fail_mark = 1
    sweeper = _sweeper(FakeChatwoot(_entry()), supabase, account_hold=_hold(tmp_path))
    counts = asyncio.run(sweeper.run_once())
    assert "hold" not in supabase.names() and counts["hold_error"] == 1
    supabase._record = "replayed"
    asyncio.run(sweeper.run_once())
    assert [name for name, _ in supabase.calls].count("mark") == 2 and "hold" in supabase.names()


def test_other_codes_are_not_held(tmp_path: Path) -> None:
    for code in ("131049: This message was not delivered", "131026: Message undeliverable", "131048: spam"):
        entry = _entry()
        entry["messages"][0]["content_attributes"]["external_error"] = code
        supabase = FakeSupabase()
        asyncio.run(_sweeper(FakeChatwoot(entry), supabase, account_hold=_hold(tmp_path)).run_once())
        assert "hold" not in supabase.names(), code


def test_the_delivered_template_releases_and_the_hold_goes_out_with_a_notice(tmp_path: Path) -> None:
    chatwoot = FakeChatwoot(_entry(), _delivered_entry())
    supabase, producer = FakeSupabase(record="replayed", released=2, due=True), FakeProducer()
    counts = asyncio.run(_sweeper(chatwoot, supabase, account_hold=_hold(tmp_path), producer=producer).run_once())
    assert supabase.names() == ["record", "hold", "release", "due", "claim", "settle"]
    release = supabase.call("release")
    assert release["account_ok_at"] == datetime.fromtimestamp(DELIVERED_AT, UTC).isoformat()
    [command] = producer.commands
    assert command.event_code == "SYS-008" and command.count == 2  # type: ignore[attr-defined]
    claim = supabase.call("claim")
    assert claim["chatwoot_message_id"] == MESSAGE and claim["external_user_id"].isdigit()
    assert "max_age_seconds" not in claim
    [sent] = chatwoot.sent
    assert sent["retry_of_message_id"] == MESSAGE and sent["conversation_id"] == CONVERSATION
    assert sent["delivery_id"] == f"meta-hold:{INBOX}:{MESSAGE}:1"
    assert sent["template_params"]["name"] == TEMPLATE  # type: ignore[index]
    settle = supabase.call("settle")
    assert settle["status"] == "sent" and settle["retry_message_id"] == 990_001
    assert counts["hold_released"] == 2 and counts["hold_notified"] == 1 and counts["hold_retried"] == 1


def test_the_signal_survives_the_conversation_leaving_the_window(tmp_path: Path) -> None:
    chatwoot, supabase = FakeChatwoot(_delivered_entry()), FakeSupabase()
    sweeper = _sweeper(chatwoot, supabase, account_hold=_hold(tmp_path))
    asyncio.run(sweeper.run_once())
    chatwoot.entries = {}
    asyncio.run(sweeper.run_once())
    releases = [kwargs for name, kwargs in supabase.calls if name == "release"]
    assert releases[-1]["account_ok_at"] == datetime.fromtimestamp(DELIVERED_AT, UTC).isoformat()


def test_the_release_without_slack_is_counted(tmp_path: Path) -> None:
    supabase = FakeSupabase(released=1)
    counts = asyncio.run(_sweeper(FakeChatwoot(_delivered_entry()), supabase,
                                  account_hold=_hold(tmp_path)).run_once())
    assert counts["hold_notice_without_slack"] == 1


def test_a_retry_rejected_again_by_the_account_goes_back_to_wait(tmp_path: Path) -> None:
    entry = _entry()
    retry = copy.deepcopy(entry["messages"][0])
    retry["id"] = 990_001
    retry["created_at"] = SENT_AT + 3600
    retry["content_attributes"] = {**retry["content_attributes"], FIRST_TOUCH_RETRY_MARKER: MESSAGE}
    entry["messages"] = [retry]
    entry["conversation"] = _show(CONVERSATION, retry)
    supabase = FakeSupabase()
    counts = asyncio.run(_sweeper(FakeChatwoot(entry), supabase, account_hold=_hold(tmp_path)).run_once())
    assert supabase.names()[:2] == ["record", "requeue"]
    assert supabase.call("record")["touch_kind"] == "first_touch_retry"
    requeue = supabase.call("requeue")
    assert requeue["original_message_id"] == MESSAGE and requeue["retry_message_id"] == 990_001
    assert counts["hold_requeue_requeued"] == 1


def test_the_base_barriers_and_the_bridge_blocks(tmp_path: Path) -> None:
    chatwoot, supabase = FakeChatwoot(_entry()), FakeSupabase(record="replayed", due=True,
                                                              claim="purchase_already_approved")
    counts = asyncio.run(_sweeper(chatwoot, supabase, account_hold=_hold(tmp_path)).run_once())
    assert chatwoot.sent == [] and "settle" not in supabase.names()
    assert counts["hold_purchase_already_approved"] == 1
    # La persona escribio: lo frena el bridge, para siempre.
    wrote = _entry()
    wrote["messages"].append({"id": MESSAGE + 500, "conversation_id": CONVERSATION, "message_type": 0,
                              "private": False, "status": "sent", "created_at": SENT_AT + 60,
                              "content": "hola", "content_attributes": {}})
    chatwoot, supabase = FakeChatwoot(wrote), FakeSupabase(record="replayed", due=True)
    asyncio.run(_sweeper(chatwoot, supabase, account_hold=_hold(tmp_path)).run_once())
    assert supabase.names() == ["record", "hold", "release", "due", "block"]
    assert supabase.call("block")["reason"] == "bridge_lead_wrote" and chatwoot.sent == []


def test_the_send_hours_and_the_gate(tmp_path: Path) -> None:
    hour = NOW // 86_400 * 86_400 + 12 * 3600
    chatwoot, supabase = FakeChatwoot(_entry()), FakeSupabase(record="replayed", due=True)
    closed = AccountHoldConfig(codes=frozenset({"131042"}), allowed_templates=frozenset({TEMPLATE}),
                               send_hours=(9, 10), time_zone=ZoneInfo("UTC"),
                               gate=FinalMetaEffectGate(enabled=True, evidence_dir=tmp_path / "g"))
    counts = asyncio.run(_sweeper(chatwoot, supabase, account_hold=closed, clock=lambda: float(hour)).run_once())
    assert "due" not in supabase.names() and counts["hold_outside_send_hours"] == 1
    chatwoot, supabase = FakeChatwoot(_entry()), FakeSupabase(record="replayed", due=True)
    asyncio.run(_sweeper(chatwoot, supabase, account_hold=_hold(tmp_path, gate_open=False)).run_once())
    assert "claim" not in supabase.names() and chatwoot.sent == []


def test_the_131049_retry_and_the_hold_live_together(tmp_path: Path) -> None:
    retry = RetryConfig(allowed_templates=frozenset({TEMPLATE}), send_hours=(0, 24),
                        time_zone=ZoneInfo("UTC"),
                        gate=FinalMetaEffectGate(enabled=True, evidence_dir=tmp_path / "r"))

    class Both(FakeSupabase):
        async def list_due_meta_first_touch_retries(self, **kwargs: object) -> list:
            self.calls.append(("due_131049", kwargs))
            return []

    supabase = Both()
    asyncio.run(_sweeper(FakeChatwoot(_entry()), supabase, retry=retry, account_hold=_hold(tmp_path)).run_once())
    assert supabase.names() == ["record", "hold", "due_131049", "release", "due"]
