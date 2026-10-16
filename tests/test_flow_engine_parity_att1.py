"""La paridad del motor sobre la captura real de ATT1 (T1): P3, P6 y las diferencias con nombre.

Diseno del motor de flujos, §3.1, §3.2 y §5.3. Fixtures
(``tests/fixtures/flow_engine/``):

* ``att1_recovery_capture_20261016.json``: la captura v2 de att1-db, leida en
  solo lectura el 2026-10-16 a las 21:03:26Z con
  ``att1_recovery_capture_query_v2.sql`` y recortada por
  ``scripts/flow_engine_capture_fixture.py`` (§6.4). La completa esta en los
  anexos del vault; el script de la paridad da lo mismo sobre las dos;
* ``att1_parity_named_differences.json``: las diferencias con nombre, cada una
  con su caso generado o su cita;
* ``legacy_recovery_cases.json``: los 25 casos que genera la cadena real en
  PGlite (``tests/sql/followup_engine/validate_flow_engine_legacy.mjs``); de
  ahi sale que el caso que cita una diferencia existe.

Los numeros que fija esta prueba son los del documento de ``docs/operations/``
(§5.5). Si una captura nueva los cambia, se cambian aca y alla juntos.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bridge.flow_capture import count_sections, restore_fixture
from bridge.flow_parity import (
    Capture,
    control_state_at,
    eligible_at,
    parity_report,
    send_timings,
    supersessions,
)
from bridge.flow_schedule import franja_of_policy, is_open, next_open

ROOT = Path(__file__).resolve().parents[1]
FLOW_ENGINE = ROOT / "tests" / "fixtures" / "flow_engine"
FIXTURE = FLOW_ENGINE / "att1_recovery_capture_20261016.json"
NAMED_FILE = FLOW_ENGINE / "att1_parity_named_differences.json"
LEGACY_CASES = FLOW_ENGINE / "legacy_recovery_cases.json"

RAW: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
CAPTURE = Capture.from_json(RAW)
NAMED: list[dict[str, Any]] = json.loads(NAMED_FILE.read_text(encoding="utf-8"))["diferencias"]
REPORT = parity_report(CAPTURE, NAMED)
ACTIONS = {action["id"]: action for action in CAPTURE.actions}
CASES = {case["id"]: case for case in CAPTURE.cases}
EVENTS = {event["id"]: event for event in CAPTURE.events}
# Las 15:00Z son las 09:00 de America/Mexico_City (UTC-6 todo el anio).
NINE_IN_MEXICO_CITY = "15:00:00"


def _instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_the_fixture_counts_are_the_ones_the_query_counted() -> None:
    counted = count_sections(restore_fixture(RAW))
    expected = RAW["_captured"]["conteos"]
    for name in ("disparos_desde", "envios", "casos", "acciones", "intentos"):
        assert counted[name] == expected[name], name
    assert {name: expected[name] for name in ("disparos_desde", "envios", "casos", "acciones", "intentos")} == {
        "disparos_desde": 252, "envios": 210, "casos": 215, "acciones": 215, "intentos": 930}


def test_the_firings_since_desde_by_kind_and_result() -> None:
    assert dict(REPORT.firings) == {
        "carrito/create_recovery_case_failed": 4,
        "carrito/pilot_scope_rejected:pilot_audience_consented_intent_prior_opt_out": 1,
        "carrito/pilot_scope_rejected:pilot_audience_intent_unresolved": 26,
        "carrito/processed": 208,
        "pago_fallido/payment_failure_correlation_unresolved": 5,
        "pago_fallido/pilot_scope_rejected:pilot_audience_consented_intent_prior_opt_out": 2,
        "pago_fallido/processed": 5,
        "pago_fallido/processed:evidencia": 1,
    }
    by_kind = {kind: sum(n for key, n in REPORT.firings.items() if key.startswith(f"{kind}/"))
               for kind in ("carrito", "pago_fallido")}
    assert by_kind == {"carrito": 239, "pago_fallido": 13}
    # Llegaron al flujo todos menos los 4 create_recovery_case_failed, que son del adaptador:
    # no tienen clave de persona, asi que no es un caso de la persona en curso (la 5b, G14).
    assert REPORT.reached_flow == 248
    # 203 carritos y 5 pagos fallidos; los otros 2 envios de la captura son de casos de la v2.
    assert REPORT.sends == 208


def test_p3_never_before_and_at_most_five_minutes_later() -> None:
    assert REPORT.never_before_violations == ()
    assert REPORT.over_tolerance == ()
    timings = send_timings(CAPTURE)
    assert len(timings) == 208 and all(timing.eligible_at is not None for timing in timings)
    assert all(timing.explained_by is None for timing in timings)
    assert dict(REPORT.delay) == pytest.approx(
        {"min": 4.171384, "mediana": 12.926061, "p95": 106.936716, "max": 144.607011}, abs=1e-6)


def test_p3_e_is_the_due_at_that_the_legacy_wrote_and_the_night_waits_for_nine() -> None:
    # La reevaluacion de la base difiere el due_at al proximo inicio: en los
    # 208 envios, E coincide con el due_at observado. Los 89 que llegaron con
    # la franja cerrada salen de las 09:00 de CDMX.
    deferred = 0
    for timing in send_timings(CAPTURE):
        action = ACTIONS[timing.action_id]
        assert timing.eligible_at == _instant(action["due_at"]), timing.action_id
        if timing.eligible_at > _instant(action["anchor_observed_at"]):
            deferred += 1
            assert timing.eligible_at.strftime("%H:%M:%S") == NINE_IN_MEXICO_CITY
    assert deferred == 89


def test_p3_on_the_real_data_only_the_window_moves_e() -> None:
    # Sobre el dato de ATT1, el control y la bolsa no corren el E de ningun envio: E es el ancla
    # o la proxima apertura de la franja (la gracia es 0). Que el control y la bolsa esten bien
    # lo fijan las lineas de tiempo de test_flow_parity.py y los 5 sin envio.
    [policy] = [row for row in restore_fixture(RAW)["config"]["followup_policy_versions"]
                if row["policy_key"] == "att1-recuperacion-un-toque"]
    assert policy["grace_period"] == "00:00:00"
    franja = franja_of_policy(policy)
    for timing in send_timings(CAPTURE):
        anchor = _instant(ACTIONS[timing.action_id]["anchor_observed_at"])
        assert timing.eligible_at == (anchor if is_open(franja, anchor) else next_open(franja, anchor))


def test_p3_the_tolerance_has_teeth() -> None:
    # Con 2 minutos quedan afuera los 7 envios mas lentos, todos de la tanda de las 09:00 de
    # CDMX del 12/10.
    report = parity_report(CAPTURE, NAMED, tolerance=timedelta(minutes=2))
    assert len(report.over_tolerance) == 7
    slow = {timing.firing_ref: timing for timing in send_timings(CAPTURE)}
    assert {slow[ref].eligible_at for ref in report.over_tolerance} == {_instant("2026-10-12T15:00:00Z")}


def test_p6_every_first_contact_that_ceded_has_its_winner() -> None:
    assert REPORT.supersessions == 192
    # Con MATCH_ORDER (primero la persona): 79 y 113. Sin depender del orden: 78 por las dos
    # rutas, 1 solo por persona y 113 solo por intencion.
    assert dict(REPORT.supersessions_by) == {"persona": 79, "intencion": 113}
    assert dict(REPORT.supersessions_by_routes) == {"las_dos": 78, "solo_persona": 1, "solo_intencion": 113}
    assert REPORT.supersessions_without_winner == ()
    assert REPORT.winners_without_cancel == ()
    # 5 cedieron ante un disparo sin caso, que no mando nada: 4 carritos sin telefono (sin
    # clave de persona) y 1 de la siembra rechazado con el control sin armar.
    assert dict(REPORT.ceded_to_a_firing_without_case) == {
        "create_recovery_case_failed": 4, "pilot_scope_rejected:pilot_runtime_not_armed": 1}
    no_phone = [item for item in supersessions(CAPTURE)
                if item.matched_by == "intencion" and EVENTS[item.winner_ref].get("person_key") is None]
    assert len(no_phone) == 4
    assert {EVENTS[item.winner_ref]["processing_error"] for item in no_phone} == {"create_recovery_case_failed"}


def test_the_five_carts_of_the_13th_are_lo_en_curso_sigue() -> None:
    assert dict(REPORT.named) == {"lo_en_curso_sigue": 5}
    assert REPORT.named_total == len(NAMED) == 5
    for entry in NAMED:
        case = CAPTURE.case_of_event(entry["disparo"])
        action = next(action for action in CAPTURE.actions if action["recovery_case_id"] == case["id"])
        # Con la v4, vencidos sin salir, con los 144 intentos reservados.
        assert case["binding"]["scope_version"] == 4
        assert (action["status"], action["terminal_reason"]) == ("expired", "expired_after_worker_crash")
        assert [(tramo["n"], tramo["primero"]["phase"]) for tramo in action["intentos"]] == [(144, "reserved")]
        # El motor los habria mandado el 13/10 a las 09:00 de CDMX, con el cupo del dia nuevo.
        assert eligible_at(CAPTURE, action) == _instant("2026-10-13T15:00:00Z")
        # A la hora del carrito, la v4 estaba armada y ya tenia el tope del 12/10 (40) gastado.
        anchor = _instant(action["anchor_observed_at"])
        assert control_state_at(CAPTURE, "att1-recuperacion", anchor) == (4, "armed")
        spent = [row for row in CAPTURE.authorizations
                 if row["scope_key"] == "att1-recuperacion" and row["local_budget_date"] == "2026-10-12"
                 and _instant(row["authorized_at"]) < anchor]
        assert len(spent) == 40


def _generated_case_ids() -> set[str]:
    assert LEGACY_CASES.exists(), (
        "falta legacy_recovery_cases.json: lo genera validate_flow_engine_legacy.mjs --write (commit 3)")
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("id"), str) and re.fullmatch(r"G[0-9]{1,2}", value["id"]):
                found.add(value["id"])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(json.loads(LEGACY_CASES.read_text(encoding="utf-8")))
    return found


def test_each_named_difference_has_its_generated_case_or_its_citation() -> None:
    generated = None
    for entry in NAMED:
        assert ("caso" in entry) != ("cita" in entry), entry["disparo"]
        assert entry["clase"] in {"E", "D", "C", "M"}
        if "caso" in entry:
            generated = generated if generated is not None else _generated_case_ids()
            assert entry["caso"] in generated, entry["caso"]
        else:
            path, _, lines = entry["cita"].partition(":")
            start, _, end = lines.partition("-")
            source = ROOT / path
            assert source.is_file(), entry["cita"]
            length = len(source.read_text(encoding="utf-8").splitlines())
            assert 1 <= int(start) <= int(end or start) <= length, entry["cita"]


def test_the_report_is_green() -> None:
    assert [dict(item) for item in REPORT.unexplained] == []
    assert REPORT.ok()


def test_the_control_ends_where_the_runtime_controls_are() -> None:
    captured_at = CAPTURE.captured_at
    assert captured_at is not None
    for row in CAPTURE.config["pilot_runtime_controls"]:
        assert control_state_at(CAPTURE, row["scope_key"], captured_at) == (
            row["scope_version"], row["runtime_state"])


def test_each_case_is_its_contacts_person() -> None:
    contacts = {contact["id"]: set(contact["person_keys"]) for contact in CAPTURE.contacts}
    for case in CAPTURE.cases:
        assert EVENTS[case["abandonment_event_id"]]["person_key"] in contacts[case["contact_id"]], case["id"]


def test_the_script_prints_the_report_and_exits_zero_in_green_and_one_in_red(
        capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("flow_engine_parity", ROOT / "scripts" / "flow_engine_parity.py")
    assert spec and spec.loader
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    assert script.main(["--capture", str(FIXTURE), "--named", str(NAMED_FILE)]) == 0
    out = capsys.readouterr().out
    assert "Sin explicar: 0" in out and "Resultado: verde" in out
    assert "sin depender del orden: 78 por las dos rutas, 1 solo por persona, 113 solo por intencion" in out
    assert script.main(["--capture", str(FIXTURE), "--named", str(NAMED_FILE), "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is True and printed["named_total"] == 5 and printed["sends"] == 208
    # Sin las diferencias con nombre, los 5 de lo_en_curso_sigue quedan sin explicar: sale con 1.
    # La paridad continua de T3 (§3.4) decide por este codigo de salida.
    empty = tmp_path / "sin_nombre.json"
    empty.write_text('{"diferencias": []}', encoding="utf-8")
    assert script.main(["--capture", str(FIXTURE), "--named", str(empty)]) == 1
    out = capsys.readouterr().out
    assert "Sin explicar: 5" in out and "Resultado: rojo" in out


def test_the_sql_suite_checks_the_generated_legacy_cases() -> None:
    # Sin --check en npm test, los 25 casos de hoy podrian quedar viejos sin que la CI lo vea.
    package = json.loads((ROOT / "tests" / "sql" / "followup_engine" / "package.json").read_text(encoding="utf-8"))
    assert "node validate_flow_engine_legacy.mjs --check" in package["scripts"]["test"].split(" && ")
