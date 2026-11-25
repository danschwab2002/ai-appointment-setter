"""El reporte de un envio masivo, la logica pura (encargo 10 del masivo por el AE).

Bordes capturados (regla 2):

* ``chatwoot_conversation_inbox_11_conv_50_opt_out_paused_20261017.json``: ATT1,
  cuenta 2, bandeja 11, show y mensajes leidos el 2026-10-17 desde el bridge con
  el token de control, anonimizada. La plantilla 2820 sale leida; la persona
  contesta (2836), la derivan (etiqueta ``automation_paused``, el equipo
  asignado y la nota privada), toca «No más mensajes» (3108, ``in_reply_to``
  2820) y una plantilla posterior (3329) vuelve ``failed`` con un
  ``external_error`` sin codigo («Template not found…»).
* ``chatwoot_db_messages_inbox_13_maribel_131042_20261107.json``: Maribel,
  bandeja 13, filas de ``messages`` de la base de Chatwoot del 2026-11-07 ya
  pasadas a la forma de la API (``rechazados_131042`` y la primera plantilla
  leida despues).
* ``chatwoot_conversation_messages_inbox_13_conv_6_20261029.json``: Maribel,
  ``GET .../conversations/6/messages`` del 2026-10-29; el ultimo mensaje de la
  persona es una imagen sin texto (``content`` null).

Los ``external_error`` de 131049 y 131026 tienen la forma «codigo: texto» de
ATT1 (E-181 y E-282), la misma que usa ``test_meta_delivery``. Los de los
codigos de corte (131048, 130429) no estan capturados en el repo: se arman con
esa misma forma y se dice aca.
"""

from __future__ import annotations

import asyncio
import copy
import csv
import io
import json
from pathlib import Path

import pytest

from bridge.automation_expert_read import META_ERROR_EXPLANATIONS
from bridge.bulk_send_report import (
    BULK_SEND_CUT_CODE_EXPLANATIONS,
    NO_CODE_EXPLANATION,
    UNDELIVERED_CSV_COLUMNS,
    BulkSendPurchase,
    BulkSendRecipient,
    ConversationReading,
    ae_view,
    build_report,
    mask_phone,
    meta_code_explanation,
    page_view,
    read_conversations,
    undelivered_csv,
    undelivered_rows,
)
from bridge.chatwoot import ChatwootProtocolError

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


CONV_50 = _load("chatwoot_conversation_inbox_11_conv_50_opt_out_paused_20261017.json")["conversation"]
MARIBEL_131042 = _load("chatwoot_db_messages_inbox_13_maribel_131042_20261107.json")
CONV_6 = _load("chatwoot_conversation_messages_inbox_13_conv_6_20261029.json")

# Telefonos sinteticos con la forma E.164 del registro.
PHONE_A = "+5215500000050"
PHONE_B = "+5215500000122"
PHONE_C = "+5215500000123"


def _reading_50(**overrides: object) -> ConversationReading:
    details = CONV_50["conversation"]
    meta = details.get("meta") or {}
    values: dict[str, object] = {
        "conversation_id": 50,
        "messages": tuple(CONV_50["messages"]),
        "labels": tuple(details["labels"]),
        "assignee_present": meta.get("assignee") is not None,
        "team_present": meta.get("team") is not None,
    }
    values.update(overrides)
    return ConversationReading(**values)  # type: ignore[arg-type]


def _message_50(message_id: int) -> dict:
    return next(m for m in CONV_50["messages"] if m["id"] == message_id)


def _sent(recipient_id: str, phone: str, conversation_id: int, message: dict) -> BulkSendRecipient:
    return BulkSendRecipient(
        recipient_id=recipient_id,
        phone=phone,
        state="sent",
        conversation_id=conversation_id,
        message_id=message["id"],
        sent_at=message["created_at"],
    )


def _failed_message(code_text: str, *, message_id: int, conversation_id: int) -> dict:
    """Una fila rechazada de Maribel con otro ``external_error`` (misma forma)."""
    message = copy.deepcopy(MARIBEL_131042["rechazados_131042"][0])
    message["id"] = message_id
    message["conversation_id"] = conversation_id
    message["content_attributes"]["external_error"] = code_text
    return message


def test_the_derived_and_opted_out_conversation_counts_everything() -> None:
    template = _message_50(2820)
    recipient = _sent("r-50", PHONE_A, 50, template)
    purchase = BulkSendPurchase("r-50", purchased_at=template["created_at"] + 3600)

    report = build_report([recipient], {50: _reading_50()}, [purchase])

    assert (report.sent, report.delivered, report.read, report.failed) == (1, 1, 1, 0)
    assert (report.replies, report.opt_outs, report.handoffs, report.purchases) == (1, 1, 1, 1)
    assert report.failed_by_code == ()
    assert report.not_sent == {}


def test_failed_by_code_uses_the_ae_explanations() -> None:
    rejected = MARIBEL_131042["rechazados_131042"]
    later = MARIBEL_131042["primera_plantilla_entregada_despues"]
    recipients = [
        _sent("r-122", PHONE_B, rejected[0]["conversation_id"], rejected[0]),
        _sent("r-123", PHONE_C, rejected[1]["conversation_id"], rejected[1]),
        _sent("r-143", "+5215500000143", later["conversation_id"], later),
    ]
    readings = {m["conversation_id"]: ConversationReading(m["conversation_id"], (m,)) for m in [*rejected, later]}

    report = build_report(recipients, readings)

    assert (report.sent, report.delivered, report.read, report.failed) == (3, 1, 1, 2)
    [failure] = report.failed_by_code
    assert (failure.code, failure.count) == ("131042", 2)
    assert failure.explanation == META_ERROR_EXPLANATIONS["131042"]


def test_131049_and_131026_reuse_the_ae_phrases_and_cut_codes_have_their_own() -> None:
    messages = [
        _failed_message("131049: This message was not delivered to maintain healthy ecosystem engagement.",
                        message_id=9001, conversation_id=901),
        _failed_message("131049: This message was not delivered to maintain healthy ecosystem engagement.",
                        message_id=9002, conversation_id=902),
        _failed_message("131026: Message undeliverable.", message_id=9003, conversation_id=903),
        _failed_message("131048: Spam rate limit hit", message_id=9004, conversation_id=904),
        _failed_message('{"code": 130429, "title": "Rate limit hit"}', message_id=9005, conversation_id=905),
    ]
    recipients = [_sent(f"r-{m['id']}", f"+52155000{m['id']}", m["conversation_id"], m) for m in messages]
    readings = {m["conversation_id"]: ConversationReading(m["conversation_id"], (m,)) for m in messages}

    report = build_report(recipients, readings)

    by_code = {f.code: (f.count, f.explanation) for f in report.failed_by_code}
    assert by_code == {
        "131049": (2, META_ERROR_EXPLANATIONS["131049"]),
        "131026": (1, META_ERROR_EXPLANATIONS["131026"]),
        "131048": (1, BULK_SEND_CUT_CODE_EXPLANATIONS["131048"]),
        "130429": (1, BULK_SEND_CUT_CODE_EXPLANATIONS["130429"]),
    }
    assert report.failed_by_code[0].code == "131049"  # el mas frecuente primero


def test_every_cut_code_of_the_brakes_has_a_spanish_explanation() -> None:
    # Los de FRENO_CODIGOS del prototipo (masivo.py:121-124), los mismos del encargo 8.
    cut_codes = {"368", "131031", "131042", "131048", "130429", "130472", "131056",
                 "132000", "132001", "132005", "132007", "132012", "132015", "132016"}
    for code in cut_codes:
        explanation = meta_code_explanation(code)
        assert explanation.startswith("Meta"), code
        assert code not in explanation, code  # tiene frase propia, no la generica
    assert "999999" in meta_code_explanation("999999")
    assert meta_code_explanation(None) == NO_CODE_EXPLANATION


def test_a_failure_without_a_code_counts_as_failed_without_code() -> None:
    template = _message_50(3329)
    report = build_report([_sent("r-50", PHONE_A, 50, template)], {50: _reading_50()})

    assert report.failed == 1
    [failure] = report.failed_by_code
    assert failure.code is None and failure.explanation == NO_CODE_EXPLANATION
    assert (report.replies, report.opt_outs, report.purchases) == (0, 0, 0)
    assert report.handoffs == 1  # la conversacion sigue con el equipo


def test_only_inbound_after_the_template_is_a_reply() -> None:
    # En la 6 de Maribel, despues de 4600 (leido) la persona manda una imagen sin texto.
    payload = CONV_6["payload"]
    reading = ConversationReading(6, tuple(payload))
    template = next(m for m in payload if m["id"] == 4600)
    last = payload[-1]
    report = build_report([_sent("r-6", PHONE_A, 6, template)], {6: reading})
    assert (report.read, report.replies, report.opt_outs, report.handoffs) == (1, 1, 0, 0)

    # La ultima de la conversacion: nadie contesto despues.
    last_agent = next(m for m in reversed(payload) if m["message_type"] == 1)
    cut = ConversationReading(6, tuple(m for m in payload if m["id"] <= last_agent["id"]))
    report = build_report([_sent("r-6", PHONE_A, 6, last_agent)], {6: cut})
    assert report.replies == 0
    assert last["content"] is None  # la imagen sin texto no rompe la deteccion de baja


def test_the_opted_out_label_counts_as_opt_out() -> None:
    template = _message_50(2820)
    messages = tuple(m for m in CONV_50["messages"] if m["id"] <= 2820)
    reading = _reading_50(messages=messages, labels=("automation_opted_out",), team_present=False)
    report = build_report([_sent("r-50", PHONE_A, 50, template)], {50: reading})
    assert (report.opt_outs, report.handoffs, report.replies) == (1, 0, 0)


def test_unreadable_conversations_and_missing_messages() -> None:
    template = _message_50(2820)
    other = dict(template, id=999_999)
    report = build_report(
        [
            _sent("r-1", PHONE_A, 50, template),
            _sent("r-2", PHONE_B, 51, template),
            _sent("r-3", PHONE_C, 50, other),
        ],
        {50: _reading_50(), 51: None},
    )
    assert (report.sent, report.delivered, report.unreadable) == (3, 1, 2)


def test_awaiting_delivery_is_not_received() -> None:
    message = dict(_message_50(2820), status="sent")
    reading = _reading_50(messages=(message,))
    report = build_report([_sent("r-50", PHONE_A, 50, message)], {50: reading},
                          [BulkSendPurchase("r-50", message["created_at"] + 10)])
    assert (report.awaiting_delivery, report.delivered, report.purchases) == (1, 0, 0)


def test_purchases_only_after_the_send_and_only_from_who_received() -> None:
    template = _message_50(2820)
    before = BulkSendPurchase("r-50", purchased_at=template["created_at"] - 1)
    report = build_report([_sent("r-50", PHONE_A, 50, template)], {50: _reading_50()}, [before])
    assert report.purchases == 0

    failed = _message_50(3329)
    report = build_report(
        [_sent("r-50", PHONE_A, 50, failed)], {50: _reading_50()},
        [BulkSendPurchase("r-50", failed["created_at"] + 60)],
    )
    assert report.purchases == 0


def test_not_sent_recipients_are_counted_by_state() -> None:
    recipients = [
        _sent("r-50", PHONE_A, 50, _message_50(2820)),
        BulkSendRecipient("r-2", PHONE_B, "vetoed"),
        BulkSendRecipient("r-3", PHONE_C, "dubious", conversation_id=77),
        BulkSendRecipient("r-4", "+5215500000004", "send_failed"),
        BulkSendRecipient("r-5", "+5215500000005", "pending"),
        BulkSendRecipient("r-6", "+5215500000006", "pending"),
    ]
    report = build_report(recipients, {50: _reading_50()})
    assert report.sent == 1
    assert report.not_sent == {"dubious": 1, "pending": 2, "send_failed": 1, "vetoed": 1}


def test_bad_records_fail_loud() -> None:
    with pytest.raises(ValueError):
        BulkSendRecipient("r-1", PHONE_A, "sent", conversation_id=50)
    with pytest.raises(ValueError):
        BulkSendRecipient("r-1", PHONE_A, "enviado")
    with pytest.raises(ValueError):
        BulkSendRecipient("", PHONE_A, "vetoed")
    recipient = BulkSendRecipient("r-1", PHONE_A, "vetoed")
    with pytest.raises(ValueError):
        build_report([recipient, recipient], {})


def _mixed_report():
    rejected = MARIBEL_131042["rechazados_131042"][0]
    readings = {
        50: _reading_50(),
        rejected["conversation_id"]: ConversationReading(rejected["conversation_id"], (rejected,)),
    }
    return build_report(
        [
            _sent("r-50", PHONE_A, 50, _message_50(2820)),
            _sent("r-122", PHONE_B, rejected["conversation_id"], rejected),
            BulkSendRecipient("r-3", PHONE_C, "vetoed"),
        ],
        readings,
    )


def test_the_ae_view_has_only_numbers() -> None:
    view = ae_view(_mixed_report())
    dumped = json.dumps(view, ensure_ascii=False)
    for phone in (PHONE_A, PHONE_B, PHONE_C):
        assert phone[1:] not in dumped and phone[-4:] not in dumped
    assert "conversation" not in dumped and "undelivered_csv" not in view
    assert view["sent"] == 2 and view["failed"] == 1 and view["not_sent"] == {"vetoed": 1}
    assert view["failed_by_code"] == [
        {"code": "131042", "count": 1, "explanation": META_ERROR_EXPLANATIONS["131042"]}
    ]


def test_the_page_csv_masks_phones_unless_the_viewer_can_see_them() -> None:
    report = _mixed_report()
    masked = list(csv.DictReader(io.StringIO(undelivered_csv(report, viewer_can_see_phones=False))))
    assert tuple(masked[0]) == UNDELIVERED_CSV_COLUMNS
    assert [row["estado"] for row in masked] == ["fallido", "vetado"]
    assert masked[0] == {
        "telefono": "52*******0122",
        "conversacion": "122",
        "estado": "fallido",
        "codigo_meta": "131042",
        "explicacion": META_ERROR_EXPLANATIONS["131042"],
    }
    assert masked[1]["conversacion"] == "" and masked[1]["codigo_meta"] == ""

    shown = undelivered_rows(report, viewer_can_see_phones=True)
    assert [row["telefono"] for row in shown] == ["5215500000122", "5215500000123"]
    assert all(not row["telefono"].startswith("+") for row in shown)

    view = page_view(report, viewer_can_see_phones=False)
    assert view["undelivered_count"] == 2 and view["phones_visible"] is False
    assert "5215500000122" not in view["undelivered_csv"]


def test_mask_phone() -> None:
    assert mask_phone("+573001234567") == "57******4567"
    assert mask_phone("+1234") == "****"


class _FakeReader:
    def __init__(self, readings: dict[int, ConversationReading], broken: set[int]) -> None:
        self.readings = readings
        self.broken = broken
        self.calls: list[tuple[int, int]] = []

    async def read_conversation(self, *, conversation_id: int, since_message_id: int) -> ConversationReading:
        self.calls.append((conversation_id, since_message_id))
        if conversation_id in self.broken:
            raise ChatwootProtocolError("invalid_conversation_scope")
        return self.readings[conversation_id]


def test_read_conversations_reads_each_once_and_survives_a_broken_one() -> None:
    template = _message_50(2820)
    later = _message_50(3242)
    reader = _FakeReader({50: _reading_50()}, broken={51})
    recipients = [
        _sent("r-1", PHONE_A, 50, later),
        _sent("r-2", PHONE_B, 50, template),
        _sent("r-3", PHONE_C, 51, template),
        BulkSendRecipient("r-4", "+5215500000004", "vetoed", conversation_id=52),
    ]

    readings = asyncio.run(read_conversations(reader, recipients, max_concurrency=2))

    assert sorted(reader.calls) == [(50, 2820), (51, 2820)]
    assert readings[51] is None and readings[50] is not None
    report = build_report(recipients, readings)
    assert (report.sent, report.read, report.unreadable) == (3, 2, 1)
