"""La paridad sobre la captura v3 (T2.2): P3 por paso, los finales y P6, con sus rojos.

Especificacion: «T2.2: la captura v3 y la paridad del carrito de tres toques»
(2026-10-21, en el vault), punto 4. El dato es el fixture sintetico
``tests/fixtures/flow_engine/att1_recovery_capture_v3_sintetico.json`` (ver
``tests/test_flow_capture_v3.py``): cinco carritos de la ventana con la
politica de tres toques (gracia de 1 h, A2 a las 24 h y Z a las 72 h de la
primera aceptacion, de 09:00 a 21:00 en America/Mexico_City, UTC-6).

Cada rojo se arma sobre una copia: se cambia una cosa, se recuentan los
conteos y se mira que la paridad lo vea.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bridge import flow_capture as fc
from bridge.flow_definition import loads_strict
from bridge.flow_parity import (
    Capture,
    CaptureError,
    CaptureV3,
    _step_start,
    eligible_at,
    parity_report_v3,
    step_finals,
    step_timings,
    supersessions,
)

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_PATH = ROOT / "tests" / "fixtures" / "flow_engine" / "att1_recovery_capture_v3_sintetico.json"
SYNTHETIC: dict[str, Any] = loads_strict(SYNTHETIC_PATH.read_bytes())
SCRIPT = ROOT / "scripts" / "flow_engine_parity.py"
SPEC = importlib.util.spec_from_file_location("flow_engine_parity_v3", SCRIPT)
assert SPEC and SPEC.loader
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


def uid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def z(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# Las acciones del sintetico, por caso: T (tres), C (contesta), K (compra), R
# (renueva) y S (sin cupon).
T_A1, T_A2, T_Z = uid(2001), uid(2002), uid(2003)
C_A2 = uid(2005)
K_A1 = uid(2007)
R_A2, R_Z = uid(2011), uid(2012)
S_Z = uid(2015)


def load(data: dict[str, Any]) -> CaptureV3:
    data["_captured"]["conteos"] = fc.count_sections(data, fc.FORM_V3)
    return CaptureV3.from_json(data)


def copy_of() -> dict[str, Any]:
    return copy.deepcopy(SYNTHETIC)


def action(data: dict[str, Any], action_id: str) -> dict[str, Any]:
    [row] = [row for row in data["acciones"] if row["id"] == action_id]
    return row


def sending_attempt(row: dict[str, Any]) -> dict[str, Any]:
    [tramo] = [tramo for tramo in row["intentos"] if tramo["primero"].get("request_started_at")]
    return tramo["primero"]


CAPTURE = CaptureV3.from_json(SYNTHETIC)
TIMINGS = {timing.action_id: timing for timing in step_timings(CAPTURE)}


# ---------------------------------------------------------------------------
# 1. El sintetico, en verde, con lo que cada paso tiene que dar.
# ---------------------------------------------------------------------------

def test_the_synthetic_capture_is_green() -> None:
    report = parity_report_v3(CAPTURE, [])
    assert report.ok(), report.as_json()["unexplained"]
    assert report.firings == {"carrito/processed": 5}
    assert report.cases == 5 and report.reached_flow == 5
    assert report.sends == {"first_contact": 5, "cart_second_touch": 3, "cart_last_touch": 1}
    assert report.never_before_violations == ()
    assert report.over_tolerance == (R_Z, K_A1)
    assert report.explained_by == {
        "a1_espera_al_cupon": 1, "renovacion_adelanta_el_ultimo_toque": 1,
        "toque_con_cupon_cierra_sin_cupon": 1,
    }
    assert report.finals == {
        "first_contact": {"enviado": 5},
        "cart_second_touch": {"compro": 1, "contesto": 1, "enviado": 3},
        "cart_last_touch": {"compro": 1, "contesto": 1, "enviado": 1, "pendiente": 1, "sin_cupon": 1},
    }
    assert report.census == {"casos": 5, "pasos_esperados": 15, "pasos_clasificados": 15,
                             "enviados": 9, "envios_de_p3": 9}
    assert report.supersessions == 1 and report.supersessions_without_winner == ()


def test_a1_counts_from_the_anchor_plus_the_grace() -> None:
    # T: el carrito de las 09:00 de CDMX (15:00Z), con una hora de gracia.
    timing = TIMINGS[T_A1]
    assert (timing.start_by, timing.start) == ("gracia", at("2026-10-20T16:00:00Z"))
    assert timing.eligible_at == at("2026-10-20T16:00:00Z") and timing.delay_seconds == 30


def test_a2_and_z_count_from_the_first_acceptance_plus_their_delay() -> None:
    # A2: la aceptacion de A1 (16:00:32Z) mas 24 h, sin gracia.
    timing = TIMINGS[T_A2]
    assert (timing.start_by, timing.start) == ("delay", at("2026-10-21T16:00:32Z"))
    assert timing.delay_seconds == 28
    # Z cuenta desde la primera aceptacion de la secuencia, no desde A2: si A2
    # se corre por el horario, Z no se corre (20260813000100:328-350).
    data = copy_of()
    z_row = action(data, T_Z)
    z_row["status"] = "accepted_by_chatwoot"
    z_row["intentos"] = [{"n": 1, "primero": {
        "attempt_number": 1, "mode": "approved_template", "phase": "completed",
        "outcome": "accepted_by_chatwoot", "reason_code": "accepted_by_chatwoot",
        "started_at": "2026-10-23T16:00:40.000000Z", "request_started_at": "2026-10-23T16:00:45.000000Z",
        "accepted_at": "2026-10-23T16:00:47.000000Z"}}]
    data["_captured"]["captured_at"] = "2026-10-24T00:00:00.000000Z"
    sent = {t.action_id: t for t in step_timings(load(data))}[T_Z]
    assert (sent.start_by, sent.start) == ("delay", at("2026-10-23T16:00:32Z"))
    assert sent.delay_seconds == 13


def test_e_of_a_following_step_does_not_read_the_due_at_the_legacy_wrote() -> None:
    data = copy_of()
    action(data, T_A2)["due_at"] = "2026-10-21T19:00:00.000000Z"
    timing = {t.action_id: t for t in step_timings(load(data))}[T_A2]
    assert timing.eligible_at == at("2026-10-21T16:00:32Z")


def test_a_following_step_out_of_hours_waits_for_nine_in_mexico_city() -> None:
    # A1 aceptado a las 03:01:30Z (21:01:30 de CDMX): A2 cae 24 h despues, con la
    # franja cerrada, y E es las 09:00 de CDMX del dia siguiente (15:00Z).
    data = copy_of()
    sending_attempt(action(data, T_A1))["accepted_at"] = "2026-10-21T03:01:30.000000Z"
    row = action(data, T_A2)
    row.update(status="pending", intentos=[], execution_attempt_count=0)
    row.pop("terminal_reason")
    data["acciones"] = [item for item in data["acciones"] if item["id"] != T_Z]
    capture = load(data)
    a2 = next(item for item in capture.actions if item["id"] == T_A2)
    case = next(item for item in capture.cases if item["id"] == a2["recovery_case_id"])
    start, by = _step_start(capture, case, a2)
    assert (by, start) == ("delay", at("2026-10-22T03:01:30Z"))
    assert eligible_at(capture, a2, start=start) == at("2026-10-22T15:00:00Z")


def test_the_renewal_moves_e_of_z_to_the_request() -> None:
    timing = TIMINGS[R_Z]
    assert (timing.start_by, timing.start) == ("renovacion", at("2026-10-20T23:30:00.5Z"))
    # Espero una vez al cupon 2: la demora la explica la espera, hasta el reintento.
    assert timing.explained_by == "toque_con_cupon_cierra_sin_cupon"
    assert timing.explained_since == at("2026-10-20T23:38:10Z")


def test_a1_waiting_for_its_coupon_is_named_by_its_exception() -> None:
    timing = TIMINGS[K_A1]
    assert timing.delay_seconds == 610
    assert timing.explained_by == "a1_espera_al_cupon"
    assert timing.explained_since == at("2026-10-20T20:10:06Z")


def test_the_finals_follow_the_steps_and_their_reasons() -> None:
    finals = {(case_id, step): final for case_id, step, final in step_finals(CAPTURE)}
    assert finals[(uid(1001), "cart_last_touch")] == "pendiente"
    # C: A2 cancelado por la respuesta, y Z hereda el motivo.
    assert finals[(uid(1002), "cart_second_touch")] == "contesto"
    assert finals[(uid(1002), "cart_last_touch")] == "contesto"
    # K: la compra con el link cancela A2; Z hereda.
    assert finals[(uid(1003), "cart_second_touch")] == "compro"
    assert finals[(uid(1003), "cart_last_touch")] == "compro"
    # S: el cupon 2 murio y Z cerro sin mensaje.
    assert finals[(uid(1005), "cart_last_touch")] == "sin_cupon"
    # El carrito de la siembra y el pago rechazado no son de la ventana.
    assert not {case_id for case_id, _ in finals} & {uid(1060), uid(1070)}


def test_p6_counts_the_payment_failure_as_a_winner_as_in_t1() -> None:
    data = copy_of()
    # Un primer contacto de la persona 6 que cede ante su pago rechazado.
    data["primer_contacto"].append({
        "contact_id": uid(9006), "purchase_intent_id": uid(106), "outcome": "planned",
        "created_at": "2026-10-20T11:00:00.000000Z", "accion_status": "cancelled",
        "accion_terminal_reason": "superseded_by_provider_event",
        "accion_cerrada_at": "2026-10-20T12:00:03.000000Z"})
    capture = load(data)
    winners = {item.plan_ref: item.winner_ref for item in supersessions(capture)}
    assert winners[uid(106)] == uid(60)
    assert parity_report_v3(capture, []).ok()


# ---------------------------------------------------------------------------
# 2. Los rojos a proposito.
# ---------------------------------------------------------------------------

def test_red_an_a2_sent_before_e_is_never_accepted() -> None:
    # A2 sale a las 15:59Z, antes de la aceptacion de A1 mas 24 h (16:00:32Z).
    data = copy_of()
    sending_attempt(action(data, T_A2))["request_started_at"] = "2026-10-21T15:59:00.000000Z"
    capture = load(data)
    named = [{"disparo": T_A2, "diferencia": "envio_antes_de_E", "clase": "E", "nombre": "a2_temprano",
              "cita": "src/bridge/cart_followups.py:1"}]
    report = parity_report_v3(capture, named)
    assert not report.ok()
    assert report.never_before_violations == (T_A2,)
    kinds = {(item["ref"], item["diferencia"]) for item in report.unexplained}
    assert (T_A2, "envio_antes_de_E") in kinds
    # Una diferencia con nombre no lo excusa.
    assert (T_A2, "nombrada_invalida:nunca_antes_no_se_acepta") in kinds


def test_red_a_z_more_than_five_minutes_late_without_a_fact() -> None:
    # Sin el intento que espero al cupon 2, nada explica los 494 s de R.
    data = copy_of()
    z_row = action(data, R_Z)
    z_row["intentos"] = [tramo for tramo in z_row["intentos"] if tramo["primero"].get("request_started_at")]
    report = parity_report_v3(load(data), [])
    assert not report.ok()
    assert [(item["ref"], item["diferencia"], item["paso"]) for item in report.unexplained] == [
        (R_Z, "envio_fuera_de_tolerancia", "cart_last_touch")]
    # La renovacion sigue explicando el comienzo, pero no la demora.
    assert report.explained_by == {"a1_espera_al_cupon": 1, "renovacion_adelanta_el_ultimo_toque": 1}


def test_red_a_coupon_wait_that_does_not_cover_the_delay() -> None:
    # La espera pidio el reintento a las 23:31:10: R a las 23:38:15 queda 7 min despues.
    data = copy_of()
    wait = next(tramo for tramo in action(data, R_Z)["intentos"] if not tramo["primero"].get("request_started_at"))
    wait["primero"]["finalized_next_attempt_at"] = "2026-10-20T23:31:10.000000Z"
    report = parity_report_v3(load(data), [])
    [item] = report.unexplained
    assert (item["ref"], item["diferencia"]) == (R_Z, "envio_fuera_de_tolerancia")
    assert "toque_con_cupon_cierra_sin_cupon explica hasta" in item["detalle"]


def test_red_without_the_renewal_z_went_out_two_days_early() -> None:
    # Si la renovacion no lo adelanto, E de Z es la aceptacion de A1 mas 72 h.
    data = copy_of()
    data["renovaciones"] = [row for row in data["renovaciones"] if row["action_id"] != R_Z]
    report = parity_report_v3(load(data), [])
    assert report.never_before_violations == (R_Z,)
    assert not report.ok()


def test_a_renewal_after_hours_waits_for_the_window_and_is_not_a_red() -> None:
    # El pedido a las 03:30Z (21:30 de CDMX): la base lo adelanta a ese momento y la
    # reevaluacion lo difiere a las 09:00 (el efecto queda «movido_despues»). E es
    # las 09:00 del dia siguiente, y Z que sale 8 s despues esta en verde.
    data = copy_of()
    renewal = next(row for row in data["renovaciones"] if row["action_id"] == R_Z)
    renewal.update(occurred_at="2026-10-21T03:30:00.000000Z", created_at="2026-10-21T03:30:00.500000Z",
                   efecto="movido_despues")
    z_row = action(data, R_Z)
    z_row["due_at"] = "2026-10-21T15:00:00.000000Z"
    z_row["intentos"] = [{"n": 1, "primero": {
        "attempt_number": 1, "mode": "approved_template", "phase": "completed",
        "outcome": "accepted_by_chatwoot", "reason_code": "accepted_by_chatwoot",
        "started_at": "2026-10-21T15:00:05.000000Z", "request_started_at": "2026-10-21T15:00:08.000000Z",
        "accepted_at": "2026-10-21T15:00:10.000000Z"}}]
    capture = load(data)
    timing = {t.action_id: t for t in step_timings(capture)}[R_Z]
    assert (timing.start_by, timing.eligible_at, timing.delay_seconds) == (
        "renovacion", at("2026-10-21T15:00:00Z"), 8)
    assert parity_report_v3(capture, []).ok()


def test_red_a_step_that_ended_without_a_reason() -> None:
    data = copy_of()
    action(data, C_A2)["terminal_reason"] = "algo_que_nadie_nombro"
    report = parity_report_v3(load(data), [])
    kinds = [(item["ref"], item["diferencia"], item["paso"]) for item in report.unexplained]
    # El paso y el que hereda su final.
    assert kinds == [(uid(1002), "final_sin_motivo", "cart_second_touch"),
                     (uid(1002), "final_sin_motivo", "cart_last_touch")]
    # Una diferencia con nombre de la v3 la puede explicar, con su cita.
    named = [{"disparo": uid(1002), "diferencia": "final_sin_motivo", "clase": "M",
              "nombre": "motivo_nuevo_del_legado", "cita": "src/bridge/worker.py:1"}]
    explained = parity_report_v3(load(data), named)
    assert explained.named == {"motivo_nuevo_del_legado": 1}
    assert explained.ok()


def test_red_a_step_that_waits_past_its_e_at_the_capture() -> None:
    # Z de T espera hasta el 23/10 16:00:32Z: con la captura del 24/10, la puerta
    # ya dijo «mandar» y no salio.
    data = copy_of()
    data["_captured"]["captured_at"] = "2026-10-24T00:00:00.000000Z"
    report = parity_report_v3(load(data), [])
    assert [(item["ref"], item["diferencia"]) for item in report.unexplained] == [(T_Z, "elegible_sin_envio")]


def test_red_a_step_the_legacy_never_created_after_its_e() -> None:
    # T: A2 salio y Z no existe. Con la captura cuatro dias despues, el E de Z (la aceptacion de A1
    # mas 72 h, el 23/10 a las 16:00:32Z) ya paso: el legado no lo creo y el motor lo habria mandado.
    data = copy_of()
    data["acciones"] = [row for row in data["acciones"] if row["id"] != T_Z]
    data["_captured"]["captured_at"] = "2026-10-25T20:00:00.000000Z"
    capture = load(data)
    finals = {(case_id, step): final for case_id, step, final in step_finals(capture)}
    assert finals[(uid(1001), "cart_last_touch")] == "paso_no_creado"
    report = parity_report_v3(capture, [])
    assert [(item["ref"], item["diferencia"], item["paso"]) for item in report.unexplained] == [
        (uid(1001), "paso_no_creado", "cart_last_touch")]
    # A la hora de la captura del sintetico (21/10) todavia no tocaba: espera.
    data["_captured"]["captured_at"] = SYNTHETIC["_captured"]["captured_at"]
    finals = {(case_id, step): final for case_id, step, final in step_finals(load(data))}
    assert finals[(uid(1001), "cart_last_touch")] == "pendiente"


def test_red_a_missing_first_touch_breaks_the_census() -> None:
    data = copy_of()
    data["acciones"] = [row for row in data["acciones"] if row["recovery_case_id"] != uid(1002)]
    report = parity_report_v3(load(data), [])
    kinds = [(item["ref"], item["diferencia"]) for item in report.unexplained]
    assert (uid(1002), "final_sin_motivo") in kinds
    assert report.census["pasos_clasificados"] == report.census["pasos_esperados"] == 15


# ---------------------------------------------------------------------------
# 3. El cargador y el script.
# ---------------------------------------------------------------------------

def test_the_loaders_do_not_mix_the_forms() -> None:
    with pytest.raises(CaptureError, match="clave fuera de la lista"):
        Capture.from_json(SYNTHETIC)
    v2 = loads_strict((ROOT / "tests" / "fixtures" / "flow_engine" / "att1_recovery_capture_20261016.json").read_bytes())
    with pytest.raises(CaptureError, match="secuencias: falta"):
        CaptureV3.from_json(v2)


def test_the_loader_rejects_two_actions_of_the_same_step() -> None:
    data = copy_of()
    twin = copy.deepcopy(action(data, T_A2))
    twin["id"] = uid(2999)
    data["acciones"].append(twin)
    with pytest.raises(CaptureError, match="ya tiene otra accion del paso cart_second_touch"):
        load(data)


def test_the_loader_checks_the_v3_counts() -> None:
    data = copy_of()
    data["_captured"]["conteos"]["renovaciones"] = 3
    with pytest.raises(CaptureError, match="renovaciones"):
        CaptureV3.from_json(data)


def test_the_script_reads_the_v3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    named = tmp_path / "diferencias.json"
    named.write_text(json.dumps({"diferencias": []}), encoding="utf-8")
    assert CLI.main(["--forma", "v3", "--capture", str(SYNTHETIC_PATH), "--named", str(named)]) == 0
    out = capsys.readouterr().out
    assert "Envios por paso: cart_last_touch 1, cart_second_touch 3, first_contact 5" in out
    assert "Censo: 5 carritos, 15 de 15 pasos clasificados, 9 enviados (9 en P3)" in out
    assert "Resultado: verde" in out
    red = tmp_path / "rojo.json"
    data = copy_of()
    sending_attempt(action(data, T_A2))["request_started_at"] = "2026-10-21T15:59:00.000000Z"
    red.write_bytes(fc.serialize(data))
    assert CLI.main(["--forma", "v3", "--capture", str(red), "--named", str(named), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["never_before_violations"] == [T_A2]


# ---------------------------------------------------------------------------
# 4. Lo que fijo el dato real (la captura del 21/10, fase 2 de la T2.2).
# ---------------------------------------------------------------------------

def test_an_a2_escalated_because_chatwoot_could_not_be_verified_has_an_identity_reason() -> None:
    # Los 17 A2 del 20/10 que el bug de la v1.15.5 (#286) cerro con chatwoot_authority_unavailable:
    # el paso escala, el caso queda escalated y Z no nace. Su final es identidad:conversacion, y Z lo
    # hereda; no son «sin motivo».
    data = copy_of()
    row = action(data, C_A2)
    row["terminal_reason"] = "chatwoot_authority_unavailable"
    report = parity_report_v3(load(data), [])
    assert report.ok(), report.as_json()["unexplained"]
    finals = {(case_id, step): final for case_id, step, final in step_finals(load(data))}
    assert finals[(uid(1002), "cart_second_touch")] == "identidad:conversacion"
    assert finals[(uid(1002), "cart_last_touch")] == "identidad:conversacion"


def _night_wait(data: dict[str, Any], *, retry_at: str = "2026-10-21T03:05:26.000000Z",
                reason: str = "recovery_coupon_not_ready") -> dict[str, Any]:
    """A1 de K como el de ATT1 del 20/10 (c62de8b6): E a las 02:55:12Z (20:55 de CDMX), dos esperas
    del cupon (02:55:24Z y 03:00:25Z, cada una pide 5 minutos), el reintento a las 03:05:26Z (21:05,
    con la franja cerrada), el diferido a las 09:00 y el envio a las 15:00:49Z."""
    row = action(data, K_A1)
    row["anchor_observed_at"] = "2026-10-21T01:55:12.000000Z"
    wait = row["intentos"][0]
    wait["primero"].update(started_at="2026-10-21T02:55:24.000000Z", reason_code=reason,
                           finalized_next_attempt_at="2026-10-21T03:00:25.000000Z")
    wait["ultimo"].update(started_at="2026-10-21T03:00:25.000000Z", reason_code=reason,
                          finalized_next_attempt_at=retry_at)
    sending_attempt(row).update(started_at="2026-10-21T15:00:35.000000Z",
                                request_started_at="2026-10-21T15:00:49.000000Z",
                                accepted_at="2026-10-21T15:00:51.000000Z")
    return row


def test_a_coupon_wait_that_runs_into_the_night_is_explained_by_the_window() -> None:
    data = copy_of()
    _night_wait(data)
    capture = load(data)
    timing = {t.action_id: t for t in step_timings(capture)}[K_A1]
    assert timing.eligible_at == at("2026-10-21T02:55:12Z")
    assert (timing.explained_by, timing.explained_since) == ("a1_espera_al_cupon", at("2026-10-21T03:05:26Z"))
    report = parity_report_v3(capture, [])
    assert report.ok(), report.as_json()["unexplained"]
    # Rojo: si sale 10 minutos despues de abrir la franja, nada lo explica.
    sending_attempt(action(data, K_A1))["request_started_at"] = "2026-10-21T15:10:00.000000Z"
    late = parity_report_v3(load(data), [])
    assert [(item["ref"], item["diferencia"]) for item in late.unexplained] == [
        (K_A1, "envio_fuera_de_tolerancia")]


def test_red_a_retry_seven_hours_later_is_not_a_coupon_wait() -> None:
    # Una espera del cupon pide 5 minutos: un reintento a las 7 h no lo pide el despachador. La cota es
    # el arranque del ultimo intento mas 5 minutos y la holgura (03:06:25Z, 21:06 de CDMX), con la
    # franja cerrada: la puerta vuelve a decir «mandar» a las 15:00Z. Un envio a las 10:00:30Z (04:00 de
    # CDMX), al reintento que la base anoto, sale con la puerta cerrada y nada lo explica.
    data = copy_of()
    row = _night_wait(data, retry_at="2026-10-21T10:00:25.000000Z")
    sending_attempt(row)["request_started_at"] = "2026-10-21T10:00:30.000000Z"
    timing = {t.action_id: t for t in step_timings(load(data))}[K_A1]
    assert timing.explained_since == at("2026-10-21T03:06:25Z")
    report = parity_report_v3(load(data), [])
    assert not report.ok()
    assert (K_A1, "envio_fuera_de_tolerancia") in {(item["ref"], item["diferencia"]) for item in report.unexplained}


def test_red_a_generic_failure_is_not_excused_beyond_its_minute() -> None:
    # Un fallo antes del pedido que no es el cupon pide 1 minuto (worker.py:1013). Con E a las 20:00Z,
    # un intento fallido a las 20:00:05Z que dice reintentar a las 20:30Z explica hasta las 20:01:05Z
    # mas la holgura (20:02:05Z): el envio a las 20:30:10Z queda sin explicar.
    data = copy_of()
    row = action(data, K_A1)
    row["intentos"] = [
        {"n": 1, "primero": {"attempt_number": 1, "mode": "approved_template", "phase": "completed",
                             "outcome": "failed_before_request", "reason_code": "chatwoot_request_failed",
                             "started_at": "2026-10-20T20:00:05.000000Z",
                             "finalized_next_attempt_at": "2026-10-20T20:30:00.000000Z"}},
        {"n": 1, "primero": {"attempt_number": 2, "mode": "approved_template", "phase": "completed",
                             "outcome": "accepted_by_chatwoot", "reason_code": "accepted_by_chatwoot",
                             "started_at": "2026-10-20T20:30:05.000000Z",
                             "request_started_at": "2026-10-20T20:30:10.000000Z",
                             "accepted_at": "2026-10-20T20:30:12.000000Z"}},
    ]
    timing = {t.action_id: t for t in step_timings(load(data))}[K_A1]
    assert (timing.explained_by, timing.explained_since) == (
        "reintento_tras_fallo_antes_del_pedido", at("2026-10-20T20:02:05Z"))
    report = parity_report_v3(load(data), [])
    assert [(item["ref"], item["diferencia"]) for item in report.unexplained] == [
        (K_A1, "envio_fuera_de_tolerancia")]


def test_red_a_third_coupon_wait_is_a_generic_failure() -> None:
    # La espera del cupon vale en los dos primeros intentos (defer_attempts = 2); en el tercero, A1 ya
    # sale sin descuento, asi que un tercer recovery_coupon_not_ready es un fallo generico.
    data = copy_of()
    row = action(data, K_A1)
    third = copy.deepcopy(row["intentos"][0]["ultimo"])
    third.update(attempt_number=3, started_at="2026-10-20T20:10:06.000000Z",
                 finalized_next_attempt_at="2026-10-20T20:15:06.000000Z")
    row["intentos"].insert(1, {"n": 1, "primero": third})
    # El tercero explica un minuto mas la holgura (20:12:06Z); el envio de las 20:20Z queda afuera.
    sending_attempt(row).update(attempt_number=4, request_started_at="2026-10-20T20:20:00.000000Z")
    timing = {t.action_id: t for t in step_timings(load(data))}[K_A1]
    assert timing.explained_by == "reintento_tras_fallo_antes_del_pedido"
    assert not parity_report_v3(load(data), []).ok()


def _ficha_world(data: dict[str, Any]) -> None:
    """Una persona con un caso de la ficha en curso (su evento, PURCHASE_BILLET_PRINTED, no viene en la
    captura) y un primer contacto que cede ante ese caso."""
    data["contactos"].append({"id": uid(9008), "person_keys": [f"{8:016x}"], "digits_keys": [],
                              "email_keys": []})
    data["casos"].append({
        "id": uid(1080), "contact_id": uid(9008), "abandonment_event_id": uid(80),
        "external_product_id": "5071808", "offer_code": "gopi6lh7", "status": "active",
        "grace_expires_at": "2026-10-21T02:33:33.000000Z", "created_at": "2026-10-21T02:18:33.000000Z",
        "policy_key": "att1-ficha", "policy_version": 1, "has_conversation": True,
        "binding": {"scope_key": "att1-ficha", "scope_version": 3, "source_event_id": uid(80),
                    "audience_mode": "consented_intent", "bound_at": "2026-10-21T02:18:33.000000Z"},
        "eventos": [{"webhook_event_id": uid(80), "event_role": "payment_slip",
                     "observed_at": "2026-10-21T02:18:30.000000Z"}],
    })
    data["primer_contacto"].append({
        "contact_id": uid(9008), "purchase_intent_id": uid(108), "outcome": "planned",
        "created_at": "2026-10-21T02:15:45.000000Z", "accion_status": "cancelled",
        "accion_terminal_reason": "superseded_by_provider_event",
        "accion_cerrada_at": "2026-10-21T03:15:46.000000Z"})


def test_p6_counts_an_open_case_whose_trigger_is_not_captured() -> None:
    # El legado cede ante cualquier caso de Hotmart abierto del producto (20261001000200:300-311): la
    # ficha le gana al primer contacto aunque su evento no venga en la captura.
    data = copy_of()
    _ficha_world(data)
    capture = load(data)
    ceded = {item.plan_ref: item for item in supersessions(capture)}
    assert ceded[uid(108)].winner_ref == uid(80)
    assert ceded[uid(108)].matched_by == "caso_en_curso"
    report = parity_report_v3(capture, [])
    # Y el caso de la ficha, sin su evento, no es una persona que no coincide.
    assert report.ok(), report.as_json()["unexplained"]
    assert report.supersessions_by["caso_en_curso"] == 1
    assert report.persons_not_comparable == (uid(1080),)


def test_red_a_cart_case_without_its_event_is_still_a_person_mismatch() -> None:
    data = copy_of()
    _ficha_world(data)
    data["casos"][-1]["binding"]["scope_key"] = "att1-recuperacion"
    data["casos"][-1]["eventos"][0]["event_role"] = "cart_abandonment"
    report = parity_report_v3(load(data), [])
    assert (uid(1080), "persona_no_coincide") in {(item["ref"], item["diferencia"]) for item in report.unexplained}


def test_p6_reads_the_plans_since_desde_as_the_fixture_does() -> None:
    # Un plan de la siembra que cedio sin un ganador en la captura: su ganador pudo llegar antes de la
    # siembra. El fixture lo recorta (§6.4, paso 2), asi que la paridad tampoco lo mira en la cruda.
    data = copy_of()
    data["primer_contacto"].append({
        "contact_id": uid(9007), "purchase_intent_id": uid(107), "outcome": "not_planned",
        "reason_code": "superseded_by_provider_event", "created_at": "2026-10-17T03:16:34.000000Z"})
    capture = load(data)
    assert uid(107) not in {item.plan_ref for item in supersessions(capture)}
    assert parity_report_v3(capture, []).ok()


def test_p6_cedes_to_an_intent_classified_before_the_capture_as_a_bound() -> None:
    # Un reenvio del formulario sobre una intencion que ya estaba clasificada confirmed_abandonment por
    # un carrito que la captura no trae: la ruta intencion_clasificada lo explica, como cota.
    data = copy_of()
    data["intenciones"].append({"id": uid(109), "funnel_ref": "att1", "product_ref": "B12345678C",
                                "offer_ref": "gopi6lh7", "submitted_at": "2026-10-11T22:52:54.000000Z",
                                "lifecycle_state": "cancelled", "current_classification": "confirmed_abandonment",
                                "person_key": f"{9:016x}"})
    data["primer_contacto"].append({
        "contact_id": uid(9007), "purchase_intent_id": uid(109), "outcome": "not_planned",
        "reason_code": "superseded_by_provider_event", "created_at": "2026-10-20T00:30:16.000000Z"})
    capture = load(data)
    ceded = {item.plan_ref: item for item in supersessions(capture)}
    assert (ceded[uid(109)].winner_ref, ceded[uid(109)].matched_by) == (uid(109), "intencion_clasificada")
    assert parity_report_v3(capture, []).ok()
    # Rojo: sin la clasificacion, cede sin ganador.
    data["intenciones"][-1]["current_classification"] = "lead_submitted"
    report = parity_report_v3(load(data), [])
    assert [(item["ref"], item["diferencia"]) for item in report.unexplained] == [(uid(109), "cesion_sin_ganador")]


REAL_PATH = ROOT / "tests" / "fixtures" / "flow_engine" / "att1_recovery_capture_v3_20261021.json"
REAL_NAMED_PATH = ROOT / "tests" / "fixtures" / "flow_engine" / "att1_parity_v3_named_differences.json"


def test_the_real_capture_of_the_21st_is_green() -> None:
    capture = CaptureV3.from_json(loads_strict(REAL_PATH.read_bytes()))
    named = loads_strict(REAL_NAMED_PATH.read_bytes())["diferencias"]
    report = parity_report_v3(capture, named)
    assert report.ok(), report.as_json()["unexplained"]
    assert report.firings == {
        "carrito/create_recovery_case_failed": 1, "carrito/pilot_case_binding_conflict": 1,
        "carrito/pilot_scope_rejected:pilot_audience_consented_intent_not_authorized": 5,
        "carrito/pilot_scope_rejected:pilot_audience_intent_unresolved": 7, "carrito/processed": 58,
    }
    assert (report.reached_flow, report.cases) == (71, 58)
    assert report.sends == {"cart_last_touch": 3, "cart_second_touch": 22, "first_contact": 58}
    assert report.never_before_violations == ()
    assert len(report.over_tolerance) == 43
    assert report.explained_by == {"a1_espera_al_cupon": 40, "renovacion_adelanta_el_ultimo_toque": 3,
                                   "toque_con_cupon_cierra_sin_cupon": 3}
    assert report.finals == {
        "cart_last_touch": {"baja": 1, "compro": 1, "contesto": 1, "enviado": 3,
                            "identidad:conversacion": 16, "pendiente": 36},
        "cart_second_touch": {"baja": 1, "compro": 1, "contesto": 1, "enviado": 22,
                              "identidad:conversacion": 16, "pendiente": 17},
        "first_contact": {"enviado": 58},
    }
    assert report.census == {"casos": 58, "pasos_esperados": 174, "pasos_clasificados": 174,
                             "enviados": 83, "envios_de_p3": 83}
    assert report.supersessions == 62 and report.supersessions_without_winner == ()
    # intencion_clasificada es una cota: los dos reenvios del formulario de la intencion f6431dad, que
    # ya estaba clasificada antes de la siembra.
    assert report.supersessions_by == {"persona": 41, "intencion": 18, "caso_en_curso": 1,
                                       "intencion_clasificada": 2}
    assert report.persons_not_comparable == ("1b5b9026-b096-453f-b65c-583135a5e7fc",
                                             "b50829ee-401d-44ee-bf5e-1105354b1719")
    assert named == [] and report.named_total == 0
