"""E-309: el aviso por Slack de una persona derivada que sigue esperando al equipo.

Bordes capturados:

* Chatwoot: ``chatwoot_conversation_inbox_11_conv_50_opt_out_paused_20261017.json``
  (ATT1, cuenta 2, inbox 11, leida el 2026-10-17 desde el bridge con el token de
  control, anonimizada). Es una derivacion real: la persona contesto el sabado
  10/10 a las 15:48 de Mexico (2836), el bridge la derivo (2837-2839, la pausa,
  la asignacion y la nota), el equipo contesto el lunes 12/10 a las 10:21 (3037)
  y la persona volvio a escribir (3107, 3108, 3247). Los tests cortan el
  historial capturado en un momento (el prefijo real que la API habria devuelto
  entonces).
* La base: la forma de fila de ``list_unattended_human_handoffs_v1`` es la de
  ``claim_slack_handoff_notifications`` (``supabase_human_handoff_requests_20260925.json``)
  con ``created_at`` en vez de ``occurred_at``; el id del pedido de la 50 es
  sintetico.
* Slack: no hay captura de la API de Slack en el repo; la tarjeta se valida con
  ``render_message`` como HND-001 (``test_slack_handoff_card.py``). Deuda anotada
  en el reporte de E-309.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bridge.chatwoot import ChatwootProtocolError
from bridge.handoff_wait_alerts import (
    BusinessCalendar,
    HandoffWaitAlertSweeper,
    UnattendedHandoff,
    WaitAlert,
    business_days_text,
    evaluate_wait_alerts,
    parse_business_days,
    parse_business_hours,
)
from slack_correlation.catalog import EVENT_TEMPLATES, render_message
from slack_correlation.producer import (
    AdmissionReceipt,
    ConnectorAdmissionUnknown,
    ConnectorRejected,
    ConnectorSemanticConflict,
)

FIXTURES = Path(__file__).parent / "fixtures"
CONV_50 = json.loads(
    (FIXTURES / "chatwoot_conversation_inbox_11_conv_50_opt_out_paused_20261017.json").read_text(
        encoding="utf-8"
    )
)["conversation"]
MX = ZoneInfo("America/Mexico_City")
CALENDAR = BusinessCalendar(time_zone=MX)
TWO_HOURS = 7200
WEEK = 168 * 3600
HANDOFF_ID = "7c1f2e0a-4b9d-4c55-9d1e-2f6a0b3c4d5e"
# 2837, la etiqueta automation_paused de la derivacion: 10/10 15:49 de Mexico.
HANDOFF_AT = 1791668969


def _mx(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=MX).timestamp())


def _until(message_id: int, **overrides: object) -> tuple[dict, list[dict]]:
    """La 50 como la habria devuelto la API cuando el ultimo mensaje era ``message_id``."""
    details = {**CONV_50["conversation"], **overrides}
    messages = [m for m in CONV_50["messages"] if m["id"] <= message_id]
    return details, messages


def _handoff(**overrides: object) -> UnattendedHandoff:
    values: dict[str, object] = {
        "handoff_request_id": HANDOFF_ID,
        "external_conversation_id": 50,
        "primary_reason_code": "commercial_exception",
        "detail_reason_code": "policy_requires_human",
        "created_at": datetime.fromtimestamp(HANDOFF_AT, UTC),
    }
    values.update(overrides)
    return UnattendedHandoff(**values)  # type: ignore[arg-type]


def _evaluate(details: dict, messages: list[dict], *, now: int, handoff=None, **kw):
    return evaluate_wait_alerts(
        details,
        messages,
        handoff=handoff,
        expected_inbox_id=11,
        now_epoch=now,
        calendar=kw.get("calendar", CALENDAR),
        after_business_seconds=kw.get("after", TWO_HOURS),
        max_age_seconds=kw.get("max_age", WEEK),
    )


# ── el calendario ──────────────────────────────────────────────────────────


def test_a_saturday_handoff_starts_counting_on_monday_morning() -> None:
    # La 50: derivada el sabado 15:49; con 2 h habiles de lunes a viernes 09-18
    # vence el lunes a las 11:00, no el sabado a las 17:49.
    assert CALENDAR.add_business_seconds(HANDOFF_AT, TWO_HOURS) == _mx("2026-10-12 11:00")


def test_the_threshold_crosses_the_end_of_the_day() -> None:
    # Viernes 17:00 + 2 h: una hora el viernes y una el lunes.
    assert CALENDAR.add_business_seconds(_mx("2026-10-16 17:00"), TWO_HOURS) == _mx(
        "2026-10-19 10:00"
    )
    # Adentro del horario es reloj comun.
    assert CALENDAR.add_business_seconds(_mx("2026-10-13 10:30"), TWO_HOURS) == _mx(
        "2026-10-13 12:30"
    )


def test_a_seven_day_calendar_counts_the_weekend() -> None:
    every_day = BusinessCalendar(time_zone=MX, weekdays=frozenset(range(1, 8)))
    assert every_day.add_business_seconds(HANDOFF_AT, TWO_HOURS) == HANDOFF_AT + TWO_HOURS  # sabado 17:49


def test_the_calendar_rejects_an_impossible_schedule() -> None:
    with pytest.raises(ValueError):
        BusinessCalendar(time_zone=MX, start_hour=18, end_hour=9)
    with pytest.raises(ValueError):
        BusinessCalendar(time_zone=MX, weekdays=frozenset())
    with pytest.raises(ValueError):
        BusinessCalendar(time_zone=MX, weekdays=frozenset({0}))


def test_the_environment_values_parse_and_print_back() -> None:
    assert parse_business_hours("") == (9, 18)
    assert parse_business_hours("08-20") == (8, 20)
    assert parse_business_days("") == frozenset({1, 2, 3, 4, 5})
    assert parse_business_days("1-6") == frozenset(range(1, 7))
    assert parse_business_days("1,3,5") == frozenset({1, 3, 5})
    assert business_days_text(frozenset(range(1, 6))) == "1-5"
    assert business_days_text(frozenset({1, 3, 5})) == "1,3,5"
    for bad in ("9-18", "18-09", "00-25", "nine"):
        with pytest.raises(ValueError):
            parse_business_hours(bad)
    for bad in ("5-1", "0-5", "1,1", "lun-vie", "8"):
        with pytest.raises(ValueError):
            parse_business_days(bad)


# ── la derivacion sin respuesta y la ventana ───────────────────────────────


def test_before_the_threshold_nothing_is_due() -> None:
    details, messages = _until(2839)
    alerts, skip = _evaluate(details, messages, handoff=_handoff(), now=_mx("2026-10-12 10:00"))
    assert alerts == () and skip == "handoff_not_due"


def test_the_unattended_alert_carries_only_the_conversation_reason_and_hours() -> None:
    details, messages = _until(2839)
    now = _mx("2026-10-12 11:05")
    alerts, skip = _evaluate(details, messages, handoff=_handoff(), now=now)
    assert skip is None
    assert alerts == (
        WaitAlert(
            kind="unattended",
            conversation_id=50,
            cycle_key=f"unattended:{HANDOFF_ID}",
            reason_code="policy_requires_human",
            waiting_since_epoch=HANDOFF_AT,
            waited_hours=(now - HANDOFF_AT) // 3600,
        ),
    )
    command = alerts[0].command(account_id=2, inbox_id=11)
    assert command.event_code == "HND-006"
    assert command.component == "chatwoot.conversation.50"
    assert command.reason_code == "policy_requires_human"
    assert command.count == 43
    assert command.subject_ref is None
    assert command.occurred_at == datetime.fromtimestamp(HANDOFF_AT, UTC)


def test_the_window_alert_fires_two_hours_before_it_closes_even_on_sunday() -> None:
    # La ultima de la persona es la 2836 (sabado 15:48): la ventana se cierra el
    # domingo 15:48 y el aviso sale desde las 13:48, aunque el equipo no atienda
    # el domingo. El de la derivacion todavia no vence (lunes 11:00).
    details, messages = _until(2839)
    closes = 1791668934 + 86400
    alerts, skip = _evaluate(details, messages, handoff=_handoff(), now=closes - 3600)
    assert skip is None
    assert [alert.kind for alert in alerts] == ["window_closing"]
    window = alerts[0]
    assert window.cycle_key == "window:50:2836"
    assert window.deadline_epoch == closes
    command = window.command(account_id=2, inbox_id=11)
    assert command.event_code == "HND-005"
    assert command.deadline_at == datetime.fromtimestamp(closes, UTC)
    # Antes de las 2 h y despues de cerrarse, no.
    assert _evaluate(details, messages, handoff=_handoff(), now=closes - 7201)[0] == ()
    assert _evaluate(details, messages, handoff=_handoff(), now=closes)[0] == ()


def test_both_alerts_when_the_threshold_passed_and_the_window_is_closing() -> None:
    every_day = BusinessCalendar(time_zone=MX, weekdays=frozenset(range(1, 8)))
    details, messages = _until(2839)
    alerts, _ = _evaluate(
        details, messages, handoff=_handoff(), now=1791668934 + 86400 - 600, calendar=every_day
    )
    assert [alert.kind for alert in alerts] == ["unattended", "window_closing"]


def test_a_team_reply_after_the_handoff_counts_as_attended() -> None:
    # 3037: el equipo contesto el lunes 10:21. attended_at puede no haber llegado
    # todavia, pero el historial ya lo muestra.
    details, messages = _until(3037)
    alerts, skip = _evaluate(details, messages, handoff=_handoff(), now=_mx("2026-10-12 12:00"))
    assert alerts == () and skip == "team_replied"


def test_the_agent_and_private_notes_are_not_a_team_reply() -> None:
    # 2839 es la nota privada del bridge (sender user, private): no atiende.
    details, messages = _until(2839)
    assert any(m["private"] and m["sender"]["type"] == "user" for m in messages)
    alerts, _ = _evaluate(details, messages, handoff=_handoff(), now=_mx("2026-10-12 12:00"))
    assert [alert.kind for alert in alerts] == ["unattended"]


def test_an_opted_out_or_closed_conversation_never_alerts() -> None:
    details, messages = _until(2839, labels=["automation_paused", "automation_opted_out"])
    assert _evaluate(details, messages, handoff=_handoff(), now=_mx("2026-10-12 12:00")) == (
        (),
        "contact_opted_out",
    )
    details, messages = _until(2839, status="resolved")
    assert _evaluate(details, messages, handoff=_handoff(), now=_mx("2026-10-12 12:00")) == (
        (),
        "conversation_not_open",
    )


def test_the_detail_reason_falls_back_to_the_primary_one() -> None:
    assert _handoff(detail_reason_code=None).reason_code == "commercial_exception"
    assert _handoff(detail_reason_code="Not A Code").reason_code == "commercial_exception"


# ── la conversacion pausada donde la persona volvio a escribir ─────────────


def test_a_paused_conversation_where_the_person_wrote_after_the_team() -> None:
    # Sin derivacion sin atender (la de la 50 ya esta atendida): el equipo
    # escribio (3037) y la persona contesto el lunes 11:48 (3107). Vence 13:48.
    details, messages = _until(3107)
    assert _evaluate(details, messages, now=_mx("2026-10-12 13:47")) == (
        (),
        "paused_reply_not_due",
    )
    alerts, skip = _evaluate(details, messages, now=_mx("2026-10-12 14:00"))
    assert skip is None
    assert alerts == (
        WaitAlert(
            kind="paused_reply",
            conversation_id=50,
            cycle_key="paused:50:3107",
            reason_code="automation_paused",
            waiting_since_epoch=1791827336,
            waited_hours=2,
        ),
    )
    assert alerts[0].command(account_id=2, inbox_id=11).event_code == "HND-012"


def test_more_messages_from_the_person_stay_in_the_same_cycle() -> None:
    # 3108 llega un segundo despues: el ciclo sigue siendo el de la 3107.
    details, messages = _until(3108)
    alerts, _ = _evaluate(details, messages, now=_mx("2026-10-12 14:00"))
    assert [alert.cycle_key for alert in alerts] == ["paused:50:3107"]


def test_a_new_team_message_opens_a_new_cycle() -> None:
    details, messages = _until(3247)
    alerts, _ = _evaluate(details, messages, now=_mx("2026-10-13 16:10"))
    assert [alert.cycle_key for alert in alerts] == ["paused:50:3247"]


def test_the_paused_alert_needs_the_team_as_the_last_one_to_write() -> None:
    # Si el ultimo saliente es del agente (la pausa al comprar, una baja), no.
    details, messages = _until(2836, labels=["automation_paused"])
    assert _evaluate(details, messages, now=_mx("2026-10-12 12:00")) == (
        (),
        "last_outgoing_not_team",
    )
    # Si el ultimo mensaje es del equipo, nadie espera (3329 fallo, pero el
    # equipo escribio: es el criterio de attended_at).
    details, messages = _until(3329)
    assert _evaluate(details, messages, now=_mx("2026-10-15 12:00")) == (
        (),
        "last_message_not_from_contact",
    )
    details, messages = _until(3107, labels=[])
    assert _evaluate(details, messages, now=_mx("2026-10-12 14:00")) == ((), "not_paused")


def test_a_paused_reply_older_than_the_ceiling_is_left_alone() -> None:
    details, messages = _until(3107)
    assert _evaluate(details, messages, now=1791827336 + WEEK + 1) == ((), "too_old")


def test_an_unreadable_history_fails_closed() -> None:
    details, messages = _until(3107)
    broken = [*messages[:-1], {**messages[-1], "message_type": 9}]
    with pytest.raises(ChatwootProtocolError):
        _evaluate(details, broken, now=_mx("2026-10-12 14:00"))
    with pytest.raises(ChatwootProtocolError):
        _evaluate({**details, "inbox_id": 13}, messages, now=_mx("2026-10-12 14:00"))


def test_event_ids_are_stable_per_cycle_and_per_account() -> None:
    details, messages = _until(3107)
    first, _ = _evaluate(details, messages, now=_mx("2026-10-12 14:00"))
    later, _ = _evaluate(details, messages, now=_mx("2026-10-12 18:00"))
    a = first[0].command(account_id=2, inbox_id=11)
    b = later[0].command(account_id=2, inbox_id=11)
    assert a.event_id == b.event_id and a.dedupe_key == b.dedupe_key
    assert a.count != b.count
    other = first[0].command(account_id=3, inbox_id=13)
    assert other.event_id != a.event_id


# ── la tarjeta ─────────────────────────────────────────────────────────────

BASE_URL = "https://chat.example.test/app/accounts/3/conversations"


def test_the_catalog_knows_hnd_012() -> None:
    assert EVENT_TEMPLATES["HND-012"].severity == "p2"


@pytest.mark.parametrize(
    ("kind", "headline", "phrases"),
    [
        (
            "unattended",
            "[p2] Derivación sin respuesta del equipo · Maribel",
            ["*#50*", "*43 h*", "la política manda el caso a una persona"],
        ),
        (
            "window_closing",
            "[p2] Se cierra la ventana de 24 h de WhatsApp · Maribel",
            ["*#50*", "solo se le puede escribir con una plantilla", "<!date^1791755334^"],
        ),
        (
            "paused_reply",
            "[p2] La persona volvió a escribir en una conversación pausada · Maribel",
            ["*#50*", "*2 h*", "el agente no contesta"],
        ),
    ],
)
def test_the_card_says_who_waits_and_links_to_the_conversation(
    kind: str, headline: str, phrases: list[str]
) -> None:
    alert = WaitAlert(
        kind=kind,
        conversation_id=50,
        cycle_key=f"{kind}:50:1",
        reason_code="policy_requires_human" if kind != "paused_reply" else "automation_paused",
        waiting_since_epoch=HANDOFF_AT,
        waited_hours=43 if kind == "unattended" else 2,
        deadline_epoch=1791755334 if kind == "window_closing" else None,
    )
    rendered = render_message(
        alert.command(account_id=3, inbox_id=13),
        tenant_label="Maribel",
        conversation_base_url=BASE_URL,
    )
    assert rendered["text"] == headline
    body = rendered["blocks"][1]["text"]["text"]
    for phrase in phrases:
        assert phrase in body
    button = rendered["blocks"][-1]["elements"][0]
    assert button["url"] == f"{BASE_URL}/50"
    assert button["text"]["text"] == "Ir a la conversación"


def test_without_the_tenant_base_url_the_card_has_no_button() -> None:
    alert = WaitAlert(
        kind="unattended",
        conversation_id=50,
        cycle_key="unattended:x",
        reason_code="some_new_reason",
        waiting_since_epoch=HANDOFF_AT,
        waited_hours=3,
    )
    rendered = render_message(alert.command(account_id=3, inbox_id=13), tenant_label="Maribel")
    assert rendered["blocks"][-1]["type"] == "context"
    # Un motivo sin frase sale como codigo, no se pierde.
    assert "`some_new_reason`" in rendered["blocks"][1]["text"]["text"]


# ── el barredor ────────────────────────────────────────────────────────────


@dataclass
class _Chatwoot:
    entries: dict[int, tuple[dict, list[dict]]]
    reads: list[int]

    async def list_recent_conversations_with_messages(self, **kwargs):
        assert kwargs["statuses"] == ("open",)
        assert kwargs["include_messages"] is False
        return [{"conversation": details} for details, _ in self.entries.values()]

    async def get_conversation_with_messages(self, *, conversation_id, expected_inbox_id):
        self.reads.append(conversation_id)
        details, messages = self.entries[conversation_id]
        return {"conversation": details, "messages": messages}


@dataclass
class _Supabase:
    handoffs: list[UnattendedHandoff]
    calls: list[dict]

    async def list_unattended_human_handoffs(self, **kwargs):
        self.calls.append(kwargs)
        return self.handoffs


class _Producer:
    def __init__(self, *errors: Exception | None) -> None:
        self.errors = list(errors)
        self.commands = []

    async def admit(self, command):
        self.commands.append(command)
        error = self.errors.pop(0) if self.errors else None
        if error is not None:
            raise error
        return AdmissionReceipt(
            status="admitted", notification_id=command.event_id, delivery_state="pending"
        )


def _sweeper(chatwoot, supabase, producer, now: int) -> HandoffWaitAlertSweeper:
    return HandoffWaitAlertSweeper(
        chatwoot=chatwoot,
        supabase=supabase,
        producer=producer,
        account_id=2,
        inbox_id=11,
        calendar=CALENDAR,
        clock=lambda: float(now),
    )


def test_the_sweeper_alerts_once_per_cycle() -> None:
    derived = _until(2839)
    chatwoot = _Chatwoot(entries={50: derived}, reads=[])
    supabase = _Supabase(handoffs=[_handoff()], calls=[])
    producer = _Producer()
    now = _mx("2026-10-12 11:05")
    sweeper = _sweeper(chatwoot, supabase, producer, now)

    counts = asyncio.run(sweeper.run_once())
    assert counts["admitted_unattended"] == 1
    assert [c.event_code for c in producer.commands] == ["HND-006"]
    assert supabase.calls[0]["chatwoot_account_id"] == 2
    assert supabase.calls[0]["since"] == datetime.fromtimestamp(now - WEEK, UTC).isoformat()
    assert sweeper.last_scan_state == "healthy"

    # El barrido siguiente no vuelve a pedirlo.
    counts = asyncio.run(sweeper.run_once())
    assert counts["already_admitted"] == 1
    assert len(producer.commands) == 1


def test_after_a_restart_the_connector_answers_duplicate_or_conflict() -> None:
    derived = _until(2839)
    for error in (ConnectorSemanticConflict("x"),):
        chatwoot = _Chatwoot(entries={50: derived}, reads=[])
        producer = _Producer(error)
        sweeper = _sweeper(
            chatwoot, _Supabase(handoffs=[_handoff()], calls=[]), producer, _mx("2026-10-12 12:00")
        )
        counts = asyncio.run(sweeper.run_once())
        assert counts["already_admitted"] == 1 and sweeper.last_scan_state == "healthy"


def test_an_unknown_admission_is_retried_and_a_rejection_is_not() -> None:
    derived = _until(2839)
    chatwoot = _Chatwoot(entries={50: derived}, reads=[])
    producer = _Producer(ConnectorAdmissionUnknown("x"))
    sweeper = _sweeper(
        chatwoot, _Supabase(handoffs=[_handoff()], calls=[]), producer, _mx("2026-10-12 12:00")
    )
    counts = asyncio.run(sweeper.run_once())
    assert counts["admit_failed"] == 1 and sweeper.last_scan_state == "error"
    counts = asyncio.run(sweeper.run_once())
    assert counts["admitted_unattended"] == 1 and len(producer.commands) == 2

    producer = _Producer(ConnectorRejected("x"))
    sweeper = _sweeper(
        chatwoot, _Supabase(handoffs=[_handoff()], calls=[]), producer, _mx("2026-10-12 12:00")
    )
    assert asyncio.run(sweeper.run_once())["rejected"] == 1
    assert asyncio.run(sweeper.run_once())["already_admitted"] == 1
    assert len(producer.commands) == 1


def test_the_sweeper_reads_only_what_can_alert() -> None:
    # La 50 pausada (candidata), una abierta sin pausa ni derivacion (no se lee),
    # una con baja (no se lee) y una derivacion de una conversacion cerrada.
    paused = _until(3107)
    plain = _until(3107, id=51, labels=[])
    opted = _until(3107, id=52, labels=["automation_paused", "automation_opted_out"])
    chatwoot = _Chatwoot(entries={50: paused, 51: plain, 52: opted}, reads=[])
    supabase = _Supabase(handoffs=[_handoff(external_conversation_id=99)], calls=[])
    producer = _Producer()
    counts = asyncio.run(
        _sweeper(chatwoot, supabase, producer, _mx("2026-10-12 14:00")).run_once()
    )
    assert chatwoot.reads == [50]
    assert counts["admitted_paused_reply"] == 1
    assert counts["contact_opted_out"] == 1
    assert counts["handoff_not_open"] == 1
    assert [c.event_code for c in producer.commands] == ["HND-012"]


def test_the_sweeper_rejects_an_impossible_configuration() -> None:
    chatwoot = _Chatwoot(entries={}, reads=[])
    supabase = _Supabase(handoffs=[], calls=[])
    for kwargs in (
        {"after_business_hours": 0},
        {"after_business_hours": 73},
        {"max_age_hours": 12},
        {"scan_interval_seconds": 10},
    ):
        with pytest.raises(ValueError):
            HandoffWaitAlertSweeper(
                chatwoot=chatwoot,
                supabase=supabase,
                producer=_Producer(),
                account_id=3,
                inbox_id=13,
                calendar=CALENDAR,
                **kwargs,
            )
