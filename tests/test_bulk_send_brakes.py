"""Los frenos del masivo (encargo 8): las pruebas de masivo.py portadas, mas las de las decisiones.

Fuente de los casos portados: ``coordinacion/etapa-1/scripts-2026-11-25/vivo-29-11/masivo.py`` del
vault, ``selftest`` (:2114-2120 el freno, :2249-2258 el ritmo) y ``selftest_2511`` (la salud y la
salida). Las lecturas de salud tienen la forma de ``normalizar`` de
``despliegue/salud-meta/salud_meta.py`` del repo de la instancia (``estado_numero``, no el
``estado`` del prototipo). No hay borde externo: es logica pura, sin fixtures capturados.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from bridge import bulk_send_brakes as b

NOW = datetime(2026, 11, 29, 23, 50, tzinfo=timezone.utc)
YELLOW = {"calidad": "YELLOW", "limite": "TIER_100K", "estado_numero": "CONNECTED", "puede_enviar": "LIMITED"}
GREEN = dict(YELLOW, calidad="GREEN", puede_enviar="AVAILABLE")


def reason(brake: b.Brake | None) -> str | None:
    return brake.reason if brake else None


# --- (a)(b)(c) el freno de un lote: masivo.py:2114-2120 ----------------------------------------------

def test_prototype_batch_cases() -> None:
    assert b.evaluate_batch({1: 50}, {}, [False] * 50) is None
    assert reason(b.evaluate_batch({1: 30, 3: 12}, {"131049": 5}, [False] * 50)).startswith("entrega:fallidos_12_de_42")
    assert reason(b.evaluate_batch({1: 1}, {"131048": 1}, [False] * 5)) == "meta_codigo:131048_x1"
    assert b.evaluate_batch({1: 10}, {"131056": 2}, [False] * 5) is None
    assert b.evaluate_batch({1: 25, 3: 5}, {"131049": 15}, [False] * 30) is None  # 30 finales: bajo la muestra
    assert reason(b.evaluate_batch({1: 40, 3: 5}, {"131049": 0}, [True] * 12 + [False] * 28)) == "chatwoot_api:12_de_40"


@pytest.mark.parametrize("code", ["368", "131031", "131042", "131048", "130429"])
def test_codes_that_cut_at_the_first(code: str) -> None:
    assert reason(b.meta_code_brake({code: 1})) == "meta_codigo:%s_x1" % code


@pytest.mark.parametrize("code", ["130472", "131056"] + ["1320%02d" % n for n in (0, 1, 5, 7, 12, 15, 16)])
def test_codes_that_cut_at_the_third(code: str) -> None:
    assert b.meta_code_brake({code: 2}) is None
    assert reason(b.meta_code_brake({code: 3})) == "meta_codigo:%s_x3" % code


def test_template_codes_cover_the_whole_range_listed() -> None:
    template = {c for c in b.META_CODE_THRESHOLDS if c.startswith("132")}
    assert template == {"132000", "132001", "132005", "132007", "132012", "132015", "132016"}
    assert all(b.META_CODE_THRESHOLDS[c] == 3 for c in template)


def test_codes_accept_int_keys_and_unknown_codes_do_not_cut() -> None:
    assert reason(b.meta_code_brake({131048: 1})) == "meta_codigo:131048_x1"
    assert b.meta_code_brake({"131026": 500, "131049": 500}) is None
    assert b.meta_code_brake(None) is None


def test_delivery_brake_thresholds() -> None:
    # 40 finales exactos: entra a la muestra; 6/40 = 15 % no pasa, 7/40 si.
    assert b.delivery_brake({1: 34, 3: 6}, {}) is None
    assert reason(b.delivery_brake({1: 33, 3: 7}, {})) == "entrega:fallidos_7_de_40"
    # 131049: 14/40 = 35 % no pasa, 15/40 si (los 131049 son fallidos; el umbral de fallidos se sube
    # para probar solo este).
    t = b.BrakeThresholds(failed_ratio=0.9)
    assert b.delivery_brake({1: 26, 3: 14}, {"131049": 14}, t) is None
    assert reason(b.delivery_brake({1: 25, 3: 15}, {"131049": 15}, t)) == "entrega:131049_15_de_40"
    # Lo que sigue en «sent» no es resultado: no entra en la muestra.
    assert b.delivery_brake({0: 500, 1: 30, 3: 9}, {}) is None
    # Status por nombre, como los devuelve una lectura ya agrupada.
    assert reason(b.delivery_brake({"delivered": 20, "read": 13, "failed": 7}, {})) == "entrega:fallidos_7_de_40"


def test_chatwoot_api_brake_looks_at_the_last_50_with_20_minimum() -> None:
    assert b.chatwoot_api_brake([True] * 19) is None  # menos de 20 llamadas: no decide
    assert reason(b.chatwoot_api_brake([True] * 20)) == "chatwoot_api:20_de_20"
    assert b.chatwoot_api_brake([False] * 40 + [True] * 10) is None  # 10/50 = 20 %: no pasa
    assert reason(b.chatwoot_api_brake([False] * 39 + [True] * 11)) == "chatwoot_api:11_de_50"
    # Las fallas viejas salen de la ventana.
    assert b.chatwoot_api_brake([True] * 100 + [False] * 50) is None


def test_meta_code_wins_over_percentages() -> None:
    assert reason(b.evaluate_batch({1: 20, 3: 40}, {"368": 1}, [True] * 50)) == "meta_codigo:368_x1"


# --- (d) la salud: selftest_2511 parte 2, mas la decision 5 -------------------------------------------

def test_messaging_limit_level() -> None:
    assert b.messaging_limit_level("TIER_100K") == 100000
    assert b.messaging_limit_level("TIER_250") == 250
    assert b.messaging_limit_level("TIER_1K") == 1000
    assert b.messaging_limit_level("TIER_1K") < b.messaging_limit_level("TIER_2K")
    assert b.messaging_limit_level("TIER_UNLIMITED") == math.inf
    assert b.messaging_limit_level("UNLIMITED") == math.inf
    assert b.messaging_limit_level(None) is None and b.messaging_limit_level("?") is None


def test_prototype_compare_cases() -> None:
    y = YELLOW
    assert b.compare_health(y, dict(y)) == (None, None)
    assert reason(b.compare_health(y, dict(y, calidad="RED"))[0]) == "salud:calidad_YELLOW_a_RED"
    assert reason(b.compare_health(dict(y, calidad="GREEN"), y)[0]) == "salud:calidad_GREEN_a_YELLOW"
    brake, notice = b.compare_health(dict(y, calidad="RED"), y)  # mejora: aviso y sigue
    assert brake is None and notice.startswith("la calidad mejoro de RED a YELLOW")
    brake, notice = b.compare_health(y, dict(y, calidad=None))  # lectura rota: aviso, no freno
    assert brake is None and "no pude leer" in notice
    assert reason(b.compare_health(y, dict(y, limite="TIER_10K"))[0]) == "salud:limite_TIER_100K_a_TIER_10K"
    brake, notice = b.compare_health(y, dict(y, limite="TIER_UNLIMITED"))
    assert brake is None and notice.startswith("el limite subio")
    assert reason(b.compare_health(y, dict(y, puede_enviar="BLOCKED"))[0]) == "salud:cuenta_blocked"
    assert reason(b.compare_health(y, dict(y, estado_numero="FLAGGED"))[0]) == "salud:numero_flagged"
    red_flagged = dict(y, calidad="RED", estado_numero="FLAGGED")
    assert b.compare_health(red_flagged, dict(red_flagged)) == (None, None)  # arranco FLAGGED: no frena por eso


@pytest.mark.parametrize("state", ["FLAGGED", "RESTRICTED", "BANNED"])
def test_number_getting_worse_brakes(state: str) -> None:
    assert reason(b.compare_health(GREEN, dict(GREEN, estado_numero=state))[0]) == "salud:numero_%s" % state.lower()


def test_compare_accepts_the_prototype_estado_key() -> None:
    old = {"calidad": "GREEN", "limite": "TIER_1K", "estado": "CONNECTED", "puede_enviar": "AVAILABLE"}
    assert reason(b.compare_health(old, dict(old, estado="FLAGGED"))[0]) == "salud:numero_flagged"


def test_compare_with_an_error_reading_is_a_notice() -> None:
    brake, notice = b.compare_health(GREEN, {"error": "Meta respondio 500 al leer el numero"})
    assert brake is None and "Meta respondio 500" in notice
    brake, notice = b.compare_health(GREEN, None)
    assert brake is None and "sin lectura" in notice
    brake, notice = b.compare_health(GREEN, dict(GREEN, calidad="UNKNOWN"))
    assert brake is None and "UNKNOWN" in notice


def test_limited_is_not_a_brake() -> None:
    assert b.compare_health(GREEN, dict(GREEN, puede_enviar="LIMITED")) == (None, None)


def test_start_decision_5() -> None:
    assert b.start_health_verdict(GREEN, yellow_confirmed=False) == b.Verdict(True)
    assert b.start_health_verdict(YELLOW, yellow_confirmed=False).reason == "salud:amarillo_sin_confirmar"
    ok = b.start_health_verdict(YELLOW, yellow_confirmed=True)
    assert ok.allows and ok.reason == "salud:amarillo_confirmado"
    # Rojo no arranca ni con la segunda marca (el prototipo lo dejaba con --permitir-calidad).
    assert b.start_health_verdict(dict(YELLOW, calidad="RED"), yellow_confirmed=True).reason == "salud:rojo_al_arrancar"
    assert b.start_health_verdict(dict(GREEN, puede_enviar="BLOCKED"), yellow_confirmed=True).reason == (
        "salud:cuenta_blocked_al_arrancar")
    assert b.start_health_verdict(dict(GREEN, estado_numero="FLAGGED"), yellow_confirmed=True).reason == (
        "salud:numero_flagged_al_arrancar")


@pytest.mark.parametrize("reading", [None, {}, {"error": "x"}, {"sin_numero": "sin phone_number_id"},
                                     dict(GREEN, calidad="UNKNOWN"), dict(GREEN, calidad="?")])
def test_start_without_a_legible_reading_does_not_start(reading) -> None:
    verdict = b.start_health_verdict(reading, yellow_confirmed=True)
    assert not verdict.allows and verdict.reason == "salud:ilegible_al_arrancar"


# --- decision 6: el portfolio --------------------------------------------------------------------------

def test_portfolio_start() -> None:
    readings = {"maribel": GREEN, "att1": YELLOW, "johanna": dict(GREEN, calidad="RED")}
    assert b.portfolio_start_verdict("maribel", readings, yellow_confirmed=False).allows
    assert b.portfolio_start_verdict("att1", readings, yellow_confirmed=False).reason == (
        "salud:att1:amarillo_sin_confirmar")
    assert b.portfolio_start_verdict("johanna", readings, yellow_confirmed=True).reason == "salud:johanna:rojo_al_arrancar"
    assert b.portfolio_start_verdict("otro", readings, yellow_confirmed=True).reason == "salud:otro:sin_lectura"
    broken = dict(readings, att1={"error": "timeout"})
    assert b.portfolio_start_verdict("maribel", broken, yellow_confirmed=False).reason == "salud:att1:ilegible_al_arrancar"
    blocked = dict(readings, att1=dict(YELLOW, puede_enviar="BLOCKED"))
    assert b.portfolio_start_verdict("maribel", blocked, yellow_confirmed=False).reason == (
        "salud:att1:cuenta_blocked_al_arrancar")


def test_portfolio_any_number_getting_worse_brakes() -> None:
    base = {"maribel": GREEN, "att1": YELLOW, "johanna": GREEN}
    assert b.compare_portfolio(base, dict(base)) == (None, None)
    brake, _ = b.compare_portfolio(base, dict(base, johanna=dict(GREEN, calidad="YELLOW")))
    assert reason(brake) == "salud:johanna:calidad_GREEN_a_YELLOW" and brake.detail.startswith("johanna:")
    brake, notice = b.compare_portfolio(base, {"maribel": GREEN, "att1": dict(YELLOW, calidad="GREEN")})
    assert brake is None and "att1: la calidad mejoro" in notice and "johanna: no pude leer" in notice


# --- (e) el ritmo adaptativo: masivo.py:2249-2258 ------------------------------------------------------

def test_prototype_rate_cases() -> None:
    p = b.RatePolicy()
    rate, event = b.adjust_rate(2.5, 2.5, 40, 100, p)
    assert rate == 1.0 and event.startswith("ritmo_bajado de 2.5/s a 1.0/s")
    assert b.adjust_rate(1.0, 2.5, 20, 100, p) == (1.0, None)  # 20 %: ni baja ni sube
    assert b.adjust_rate(2.5, 2.5, 10, 15, p) == (2.5, None)  # muestra chica: no decide
    ups, rate = [], 1.0
    for _ in range(5):
        rate, event = b.adjust_rate(rate, 2.5, 5, 100, p)
        ups.append((rate, bool(event and event.startswith("ritmo_subido"))))
    assert ups == [(1.5, True), (2.0, True), (2.5, True), (2.5, False), (2.5, False)]
    assert b.adjust_rate(0.8, 0.8, 90, 100, p) == (0.8, None)  # pedido por debajo del lento: no baja


def test_rate_thresholds_exactly() -> None:
    assert b.adjust_rate(2.5, 2.5, 30, 100)[0] == 2.5  # 30 %: no pasa el umbral
    assert b.adjust_rate(2.5, 2.5, 31, 100)[0] == 1.0
    assert b.adjust_rate(1.0, 2.5, 15, 100)[0] == 1.5  # la mitad del umbral: sube
    assert b.adjust_rate(1.0, 2.5, 16, 100)[0] == 1.0


def test_rate_ceilings() -> None:
    assert b.requested_rate(None) == 2.5
    assert b.requested_rate(9) == 5.0  # el techo duro
    with pytest.raises(ValueError):
        b.requested_rate(0)
    assert b.adjust_rate(5.0, 9, 0, 100) == (5.0, None)
    assert b.adjust_rate(7.0, 9, 0, 100)[0] == 5.0  # un actual por encima del techo se acota
    assert b.adjust_rate(4.5, 5, 0, 100)[0] == 5.0


def test_retained_in_window() -> None:
    sent = [
        (NOW - timedelta(seconds=100), 0),  # muy reciente: fuera
        (NOW - timedelta(seconds=180), 0),  # borde: dentro, retenido
        (NOW - timedelta(seconds=300), "sent"),  # retenido
        (NOW - timedelta(seconds=300), 1),
        (NOW - timedelta(seconds=400), 3),  # fallido tiene resultado: no retenido
        (NOW - timedelta(seconds=480), 2),  # borde: dentro
        (NOW - timedelta(seconds=600), 0),  # viejo: fuera
    ]
    assert b.retained_in_window(sent, now=NOW) == (2, 5)
    with pytest.raises(ValueError):
        b.retained_in_window([(datetime(2026, 11, 29, 23, 45), 0)], now=NOW)


# --- (f) la tanda piloto (decision 12) -----------------------------------------------------------------

def pilot(**kw) -> b.PilotState:
    base = dict(recipients=3000, sent=20, now=NOW, finished_at=NOW - timedelta(minutes=6), finals=20, failed=0)
    base.update(kw)
    return b.PilotState(**base)


def test_pilot_flow() -> None:
    assert b.pilot_verdict(pilot(sent=12, finished_at=None))[0] == b.PILOT_SEND
    outcome, why = b.pilot_verdict(pilot(finished_at=NOW - timedelta(minutes=2)))
    assert outcome == b.PILOT_WAIT and why == "piloto:esperando_180s"
    assert b.pilot_verdict(pilot()) == (b.PILOT_CONTINUE, None)
    assert b.pilot_verdict(pilot(finished_at=None)) == (b.PILOT_WAIT, "piloto:sin_hora_de_fin")


def test_pilot_brake_never_continues() -> None:
    brake = b.meta_code_brake({"131048": 1})
    assert b.pilot_verdict(pilot(brake=brake)) == (b.PILOT_STOP, "piloto:meta_codigo:131048_x1")
    assert b.pilot_verdict(pilot(sent=5, brake=brake))[0] == b.PILOT_STOP
    assert b.pilot_verdict(pilot(failed=4)) == (b.PILOT_STOP, "piloto:entrega:fallidos_4_de_20")
    assert b.pilot_verdict(pilot(failed=4, finished_at=NOW - timedelta(minutes=1)))[0] == b.PILOT_STOP
    assert b.pilot_verdict(pilot(failed=3))[0] == b.PILOT_CONTINUE  # 3/20 = 15 %: no pasa


def test_pilot_without_delivery_waits_then_stops() -> None:
    assert b.pilot_verdict(pilot(finals=14)) == (b.PILOT_CONTINUE, None)  # 6/20 = 30 % sin resultado: sigue
    assert b.pilot_verdict(pilot(finals=13)) == (b.PILOT_WAIT, "piloto:sin_resultado_7_de_20")
    late = pilot(finals=13, finished_at=NOW - timedelta(minutes=15))
    assert b.pilot_verdict(late) == (b.PILOT_STOP, "piloto:entrega:sin_entrega_7_de_20")


def test_small_list_is_all_pilot() -> None:
    assert b.pilot_verdict(pilot(recipients=8, sent=5))[0] == b.PILOT_SEND
    assert b.pilot_verdict(pilot(recipients=8, sent=8, finished_at=NOW)) == (b.PILOT_DONE, None)
    assert b.pilot_verdict(pilot(recipients=8, sent=8, failed=2))[0] == b.PILOT_STOP
    assert b.pilot_verdict(pilot(recipients=0, sent=0)) == (b.PILOT_DONE, None)


# --- la salida del prototipo, como «freno de Meta» para la regla de las 24 h ---------------------------

def test_prototype_exit_codes_map_to_meta_brakes() -> None:
    # salida_de daba 4 (freno de Meta) a los FRENO_META_/FRENO_meta_ y 3 al resto; aca la clase la da
    # el prefijo del motivo. Difiere a proposito: la entrega (fallidos, 131049, piloto sin entregar)
    # cuenta como de Meta, y un corte manual o por hora no es un freno.
    assert b.is_meta_brake("salud:calidad_YELLOW_a_RED")
    assert b.is_meta_brake("meta_codigo:131048_x1")
    assert b.is_meta_brake("salud:maribel:cuenta_blocked_al_arrancar")
    assert b.is_meta_brake("entrega:fallidos_9_de_40")
    assert b.is_meta_brake("entrega:131049_15_de_40")
    assert b.is_meta_brake("piloto:meta_codigo:368_x1")
    assert b.is_meta_brake("piloto:entrega:fallidos_4_de_20")
    assert not b.is_meta_brake("chatwoot_api:12_de_40")
    assert not b.is_meta_brake("piloto:chatwoot_api:12_de_40")
    assert b.is_meta_brake("piloto:entrega:sin_entrega_7_de_20")  # Meta retuvo: cuenta
    assert not b.is_meta_brake(None) and not b.is_meta_brake("")


# --- (f) la regla de las 24 h (decision 13) ------------------------------------------------------------

def test_24h_rule() -> None:
    braked = b.FinishedSend("recordatorio-2911", NOW - timedelta(hours=2), "meta_codigo:131048_x1")
    assert b.start_after_meta_brake_verdict([], now=NOW) == b.Verdict(True)
    verdict = b.start_after_meta_brake_verdict([braked], now=NOW)
    assert not verdict.allows and verdict.reason == "regla_24h:freno_meta_recordatorio-2911"
    assert "hace 2.0 h" in verdict.detail
    # Confirmacion nueva, posterior al freno: arranca.
    ok = b.start_after_meta_brake_verdict([braked], now=NOW, confirmed_at=NOW - timedelta(minutes=5))
    assert ok.allows and ok.reason == "regla_24h:confirmado_tras_recordatorio-2911"
    # Una confirmacion anterior al freno no sirve.
    old = b.start_after_meta_brake_verdict([braked], now=NOW, confirmed_at=NOW - timedelta(hours=3))
    assert not old.allows
    # Una confirmacion en el futuro tampoco.
    assert not b.start_after_meta_brake_verdict([braked], now=NOW, confirmed_at=NOW + timedelta(minutes=1)).allows


def test_24h_rule_window_and_kinds() -> None:
    day_old = b.FinishedSend("a", NOW - timedelta(hours=24), "meta_codigo:368_x1")
    assert b.start_after_meta_brake_verdict([day_old], now=NOW).allows  # 24 h justas: ya paso
    almost = b.FinishedSend("a", NOW - timedelta(hours=23, minutes=59), "meta_codigo:368_x1")
    assert not b.start_after_meta_brake_verdict([almost], now=NOW).allows
    chatwoot = b.FinishedSend("b", NOW - timedelta(hours=1), "chatwoot_api:20_de_20")
    assert b.start_after_meta_brake_verdict([chatwoot], now=NOW).allows
    future = b.FinishedSend("c", NOW + timedelta(hours=1), "salud:calidad_GREEN_a_YELLOW")
    assert not b.start_after_meta_brake_verdict([future], now=NOW).allows  # fail-closed


def test_24h_rule_needs_confirmation_after_the_last_meta_brake() -> None:
    first = b.FinishedSend("a", NOW - timedelta(hours=5), "meta_codigo:368_x1")
    second = b.FinishedSend("b", NOW - timedelta(hours=1), "salud:calidad_GREEN_a_YELLOW")
    between = NOW - timedelta(hours=3)
    verdict = b.start_after_meta_brake_verdict([first, second], now=NOW, confirmed_at=between)
    assert not verdict.allows and verdict.reason == "regla_24h:freno_meta_b"


def test_naive_datetimes_are_rejected() -> None:
    naive = datetime(2026, 11, 29, 23, 0)
    with pytest.raises(ValueError):
        b.start_after_meta_brake_verdict([], now=naive)
    with pytest.raises(ValueError):
        b.pilot_verdict(pilot(now=naive))
