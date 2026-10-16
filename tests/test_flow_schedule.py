"""El horario de un flujo, gemelo de la ventana de reevaluate_followup_action.

Diseno del motor de flujos, §3.2 («La gemela del horario») y §5.3. Fixture:

* ``flow_engine/window_cases.json``: los bordes 08:59, 09:00, 21:00:30,
  21:00:59 y 21:01; de noche, con el proximo inicio en el dia y al dia
  siguiente; ``siempre``; y en Europe/Madrid, la hora que no existe y la que
  pasa dos veces. Los mismos casos corren en PGlite contra
  ``reevaluate_followup_action`` real (``validate_flow_engine_legacy.mjs``);
  ``_formato`` dice como se leen.

Lo que esta prueba mira de la decision es su coherencia con la ventana: que
``reevaluation`` diga lo que la base decidiria con ese ``is_open``, ese
``next_open`` y ese ``expires_at`` (20260805000300:346-352). Que la base lo
decida de verdad lo mira PGlite.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from bridge.flow_schedule import (
    LAST_MINUTE,
    Franja,
    franja_of,
    franja_of_policy,
    hour_text,
    is_open,
    local_day,
    next_open,
)

ROOT = Path(__file__).resolve().parents[1]
WINDOW_CASES: dict[str, Any] = json.loads(
    (ROOT / "tests" / "fixtures" / "flow_engine" / "window_cases.json").read_text(encoding="utf-8"))
CASES: list[dict[str, Any]] = WINDOW_CASES["cases"]
CDMX = {"zona": "America/Mexico_City", "desde": "09:00", "hasta": "21:00"}
ATT1_POLICY = {
    "timezone": "America/Mexico_City",
    "business_windows": [{"end": "21:00", "days": [1, 2, 3, 4, 5, 6, 7], "start": "09:00"}],
}


def _instant(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


def _franjas(case: dict[str, Any]) -> list[Franja]:
    return [franja_of(case["horario"], fixed_zone=case["fixed_zone"]), franja_of_policy(case["policy"])]


# ---------------------------------------------------------------------------
# 1. Los casos compartidos con PGlite.
# ---------------------------------------------------------------------------

def test_the_shared_cases_carry_the_borders_of_the_design() -> None:
    ids = {case["id"] for case in CASES}
    assert len(ids) == len(CASES)
    assert {
        "cdmx_0859_59", "cdmx_0900_00", "cdmx_2100_30", "cdmx_2100_59", "cdmx_2101_00",
        "cdmx_noche_0300", "cdmx_noche_2330", "siempre_zona_fija_cdmx", "siempre_sin_zona_utc",
        "madrid_hora_que_no_existe", "madrid_hora_que_pasa_dos_veces",
    } <= ids
    assert set(WINDOW_CASES["_formato"]) >= {"lado_python", "lado_pglite", "esperado_pglite", "policy_base"}
    for case in CASES:
        assert set(case) == {
            "id", "que", "horario", "fixed_zone", "policy", "at", "local", "expires_at", "is_open",
            "next_open", "reevaluation",
        }, case["id"]
        assert set(case["policy"]) == {"timezone", "business_windows"}, case["id"]
        assert set(case["reevaluation"]) == {"decision", "reason_code", "due_at"}, case["id"]
        # Las horas, en UTC y con Z: el JSON se lee igual en Python y en JavaScript.
        for key in ("at", "expires_at", "next_open"):
            assert case[key].endswith("Z"), (case["id"], key)


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_is_open_matches_the_case(case: dict[str, Any]) -> None:
    at = _instant(case["at"])
    assert at is not None
    for franja in _franjas(case):
        assert is_open(franja, at) is case["is_open"], franja


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_next_open_matches_the_case(case: dict[str, Any]) -> None:
    at = _instant(case["at"])
    assert at is not None
    for franja in _franjas(case):
        found = next_open(franja, at)
        assert found == _instant(case["next_open"]), franja
        assert found is not None and found.utcoffset() == timedelta(0) and found > at


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_the_expected_reevaluation_is_the_window_decision_of_the_base(case: dict[str, Any]) -> None:
    franja = franja_of_policy(case["policy"])
    at, expires_at = _instant(case["at"]), _instant(case["expires_at"])
    assert at is not None and expires_at is not None and expires_at > at
    upcoming = next_open(franja, at)
    if is_open(franja, at):
        expected = {"decision": "execute", "reason_code": "eligible_for_execution", "due_at": None}
    elif upcoming is None or upcoming >= expires_at:
        expected = {"decision": "escalate", "reason_code": "no_business_window_before_expiry", "due_at": None}
    else:
        expected = {"decision": "defer", "reason_code": "business_window_closed",
                    "due_at": upcoming.strftime("%Y-%m-%dT%H:%M:%SZ")}
    assert case["reevaluation"] == expected


def test_the_local_label_is_the_local_time_of_the_row() -> None:
    # La etiqueta es para leer, pero no puede mentir: la hora local de la fila.
    for case in CASES:
        at = _instant(case["at"])
        assert at is not None
        local = at.astimezone(ZoneInfo(case["policy"]["timezone"]))
        assert case["local"].startswith(local.strftime("%Y-%m-%d %H:%M:%S")), case["id"]
        assert case["local"].endswith(f"isodow {local.isoweekday()})"), case["id"]


# ---------------------------------------------------------------------------
# 2. La hora que no existe y la que pasa dos veces.
# ---------------------------------------------------------------------------

MADRID = {"zona": "Europe/Madrid", "desde": "02:30", "hasta": "05:00"}


def test_the_hour_that_does_not_exist_is_read_with_the_offset_before_the_jump() -> None:
    # 2026-03-29, 02:30 en Madrid no existe: la base da 01:30Z (las 03:30 CEST), como fold=0.
    franja = franja_of(MADRID)
    assert next_open(franja, datetime(2026, 3, 29, 0, 0, tzinfo=timezone.utc)) == datetime(
        2026, 3, 29, 1, 30, tzinfo=timezone.utc)
    # Lo que se difiere cae adentro de la franja.
    assert is_open(franja, datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc))


def test_the_hour_that_happens_twice_is_the_second_one_as_in_the_base() -> None:
    # 2026-10-25, 02:30 en Madrid pasa dos veces: la base toma la segunda (01:30Z), no la de fold=0 (00:30Z).
    franja = franja_of(MADRID)
    found = next_open(franja, datetime(2026, 10, 24, 23, 0, tzinfo=timezone.utc))
    assert found == datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
    assert found != datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)


def test_an_at_in_another_zone_is_the_same_instant() -> None:
    franja = franja_of(CDMX)
    utc = datetime(2026, 10, 17, 3, 0, 30, tzinfo=timezone.utc)
    other = utc.astimezone(timezone(timedelta(hours=9)))
    assert is_open(franja, other) is is_open(franja, utc) is True
    assert next_open(franja, other) == next_open(franja, utc)


# ---------------------------------------------------------------------------
# 3. Sin zona, ValueError.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda at: is_open(franja_of(CDMX), at),
    lambda at: next_open(franja_of(CDMX), at),
    lambda at: is_open(franja_of("siempre"), at),
    lambda at: local_day("America/Mexico_City", at),
], ids=["is_open", "next_open", "is_open_siempre", "local_day"])
def test_an_instant_without_zone_is_a_value_error(call: Any) -> None:
    with pytest.raises(ValueError, match="instant_without_zone"):
        call(datetime(2026, 10, 16, 15, 0))


# ---------------------------------------------------------------------------
# 4. El dia local.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("at", "expected"), [
    (datetime(2026, 10, 17, 5, 59, 59, 999999, tzinfo=timezone.utc), date(2026, 10, 16)),
    (datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc), date(2026, 10, 17)),
    (datetime(2026, 10, 16, 23, 0, tzinfo=timezone.utc), date(2026, 10, 16)),
])
def test_local_day_is_the_date_in_the_zone(at: datetime, expected: date) -> None:
    assert local_day("America/Mexico_City", at) == expected


def test_local_day_rejects_a_zone_that_is_not_iana() -> None:
    with pytest.raises(ValueError, match="franja_zone_invalid"):
        local_day("UTC+3", datetime(2026, 10, 16, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# 5. La franja: desde el horario y desde la fila.
# ---------------------------------------------------------------------------

def test_the_franja_of_att1_is_the_same_from_the_definition_and_from_the_row() -> None:
    expected = Franja(always=False, zone="America/Mexico_City", start_minute=9 * 60, end_minute=21 * 60)
    assert franja_of(CDMX) == expected
    assert franja_of(CDMX, fixed_zone="America/Mexico_City") == expected
    assert franja_of_policy(ATT1_POLICY) == expected


def test_siempre_is_the_whole_day_in_the_fixed_zone_or_none() -> None:
    assert franja_of("siempre") == Franja(always=True, zone=None, start_minute=0, end_minute=LAST_MINUTE)
    assert franja_of("siempre", fixed_zone="Europe/Madrid") == Franja(
        always=True, zone="Europe/Madrid", start_minute=0, end_minute=LAST_MINUTE)


@pytest.mark.parametrize(("horario", "fixed_zone", "code"), [
    ("a_medir", None, "franja_horario_invalid"),
    ({"zona": "America/Mexico_City", "desde": "09:00"}, None, "franja_horario_invalid"),
    ({**CDMX, "dias": [1, 2]}, None, "franja_horario_invalid"),
    ({**CDMX, "desde": "9:00"}, None, "franja_hour_invalid"),
    ({**CDMX, "hasta": "24:00"}, None, "franja_hour_invalid"),
    ({**CDMX, "zona": "UTC+3"}, None, "franja_zone_invalid"),
    ({**CDMX, "zona": "a_medir"}, None, "franja_zone_invalid"),
    (CDMX, "Europe/Madrid", "franja_zone_differs_from_fixed_zone"),
    ("siempre", "GMT-3 local", "franja_fixed_zone_invalid"),
])
def test_franja_of_rejects_what_it_cannot_read(horario: object, fixed_zone: str | None, code: str) -> None:
    with pytest.raises(ValueError, match=code):
        franja_of(horario, fixed_zone=fixed_zone)


def _policy(**window: Any) -> dict[str, Any]:
    return {"timezone": "America/Mexico_City",
            "business_windows": [{"days": [1, 2, 3, 4, 5, 6, 7], "start": "09:00", "end": "21:00", **window}]}


@pytest.mark.parametrize(("policy", "code"), [
    # Mas de una ventana: la base abre en la union, y una franja es una sola.
    ({**ATT1_POLICY, "business_windows": ATT1_POLICY["business_windows"] * 2}, "franja_policy_not_one_window"),
    ({**ATT1_POLICY, "business_windows": []}, "franja_policy_not_one_window"),
    ({**ATT1_POLICY, "business_windows": {"days": [1]}}, "franja_policy_not_one_window"),
    # El domingo como 0 (el rojo de T2 en la compilacion): la base no abre los domingos.
    (_policy(days=[0, 1, 2, 3, 4, 5, 6]), "franja_policy_not_every_day"),
    (_policy(days=[1, 2, 3, 4, 5]), "franja_policy_not_every_day"),
    (_policy(days=["1", "2", "3", "4", "5", "6", "7"]), "franja_policy_not_every_day"),
    (_policy(days=[True, 2, 3, 4, 5, 6, 7]), "franja_policy_not_every_day"),
    (_policy(start="9:00"), "franja_hour_invalid"),
    (_policy(end=None), "franja_hour_invalid"),
    ({**ATT1_POLICY, "timezone": "GMT-6"}, "franja_zone_invalid"),
    ("no es una fila", "franja_policy_invalid"),
])
def test_franja_of_policy_rejects_what_is_not_one_window_every_day(policy: object, code: str) -> None:
    with pytest.raises(ValueError, match=code):
        franja_of_policy(policy)  # type: ignore[arg-type]


def test_franja_of_policy_reads_what_the_base_reads() -> None:
    # Los dias desordenados o de mas, y las claves de mas, no cambian nada en la base.
    assert franja_of_policy(_policy(days=[7, 6, 5, 4, 3, 2, 1, 1])) == franja_of(CDMX)
    assert franja_of_policy(_policy(nota="x")) == franja_of(CDMX)


@pytest.mark.parametrize(("kwargs", "code"), [
    ({"always": False, "zone": None, "start_minute": 0, "end_minute": 10}, "franja_zone_missing"),
    ({"always": False, "zone": "Mars/Olympus", "start_minute": 0, "end_minute": 10}, "franja_zone_invalid"),
    ({"always": False, "zone": "UTC", "start_minute": -1, "end_minute": 10}, "franja_minute_invalid"),
    ({"always": False, "zone": "UTC", "start_minute": 0, "end_minute": LAST_MINUTE + 1}, "franja_minute_invalid"),
    ({"always": False, "zone": "UTC", "start_minute": True, "end_minute": 10}, "franja_minute_invalid"),
    ({"always": True, "zone": None, "start_minute": 9 * 60, "end_minute": LAST_MINUTE}, "franja_always_not_whole_day"),
    ({"always": 1, "zone": "UTC", "start_minute": 0, "end_minute": 10}, "franja_always_invalid"),
])
def test_a_franja_is_checked_when_built(kwargs: dict[str, Any], code: str) -> None:
    with pytest.raises(ValueError, match=code):
        Franja(**kwargs)


def test_a_franja_that_starts_after_it_ends_never_opens_as_in_the_base() -> None:
    franja = Franja(always=False, zone="UTC", start_minute=22 * 60, end_minute=6 * 60)
    start = datetime(2026, 10, 16, tzinfo=timezone.utc)
    assert not any(is_open(franja, start + timedelta(minutes=minute)) for minute in range(0, 24 * 60, 7))
    # La base igual difiere al proximo inicio: que no abra lo decide la ventana, no la proxima apertura.
    assert next_open(franja, start) == datetime(2026, 10, 16, 22, 0, tzinfo=timezone.utc)


def test_hour_text_is_the_shape_of_the_base() -> None:
    assert [hour_text(minute) for minute in (0, 9 * 60, 21 * 60, LAST_MINUTE)] == ["00:00", "09:00", "21:00", "23:59"]
