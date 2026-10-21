"""El plan y el arranque del motor: ``plan_steps``, ``not_supported``, ``plan_start`` y ``wins``.

Diseno de la T3a, §3.1 y §8.1. Sobre las definiciones del carrito y del pago
fallido de ATT1 (``tests/fixtures/flow_simulation/att1_definitions_by_scope_version.json``,
la v3, la v4 y la v5) y sobre definiciones de prueba armadas desde la v5:

* el plan: las esperas, el vencimiento, la franja y la bolsa de cada version;
  las capacidades (``PlanNotSupported``) y la forma (``PlanInvalid``); y N
  pasos con ``PREVIEW_CAPABILITIES``, cada uno desde el primer envio y con el
  vencimiento comun (la regla del legado, ``20260813000100:337-355``);
* el arranque: el permiso, la repeticion (``persona_y_oferta``, ``0m``),
  ``no_arranca_mientras``, la repeticion del pago fallido que se suma al
  recorrido abierto (F#11, ``joins_run``) y el disparo sin contacto;
* ``wins``: por persona, con el recorrido en curso, y por intencion.

Los datos son inventados: uuid de la forma ``00000000-0000-4000-8000-<n>``.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bridge.flow_engine import (
    ENGINE_CAPABILITIES_V1,
    PREVIEW_CAPABILITIES,
    EngineFlow,
    Firing,
    PersonView,
    PlanInvalid,
    PlanNotSupported,
    RunView,
    StartDecision,
    not_supported,
    plan_start,
    plan_steps,
    wins,
)
from bridge.flow_parity import EXCEPTIONS_V1
from bridge.flow_schedule import franja_of

ROOT = Path(__file__).resolve().parents[1]
DEFINITIONS: dict[str, Any] = json.loads(
    (ROOT / "tests" / "fixtures" / "flow_simulation" / "att1_definitions_by_scope_version.json"
     ).read_text(encoding="utf-8"))
ZONE = "America/Mexico_City"
SQL_AND_DISPATCH = frozenset(item.name for item in EXCEPTIONS_V1 if item.layer in ("sql", "despacho"))
SEND = {"plantilla": "att1_carrito_abandonado_01", "idioma": "es_MX", "variables": ["nombre", "producto"]}
DAY = datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc)   # 12:00 de CDMX


def uid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def flow_of(key: str = "carrito", version: int = 5, **changes: Any) -> EngineFlow:
    definition = copy.deepcopy(DEFINITIONS["versions"][str(version)][key])
    for name, value in changes.items():
        if name == "vence":
            definition["termina"]["vence"] = value
        elif name == "si":
            definition["termina"]["si"] = value
        else:
            definition[name] = value
    return EngineFlow(
        flow_id=DEFINITIONS["flows"][key]["flow_id"], product_ref="att1", flow_key=key, version=version,
        definition=definition, legacy_kind=DEFINITIONS["flows"][key]["legacy_kind"], fixed_zone=ZONE,
        pool="att1-recuperacion", exceptions=SQL_AND_DISPATCH,
    )


def fire(number: int, when: datetime, *, event: str = "carrito", who: int = 1, offer: str = "oferta",
         intents: frozenset[str] = frozenset(), permission: str | None = "formulario",
         contact: bool = True) -> Firing:
    return Firing(
        source_ref=uid(number), event=event, at=when, received_at=when + timedelta(seconds=1), product_ref="att1",
        offer_ref=offer, contact_ref=uid(9000 + who) if contact else None, intent_refs=intents,
        permission=permission,
    )


def run(flow: EngineFlow, fired: Firing, *, sends: tuple[datetime, ...] = (),
        closed_at: datetime | None = None) -> RunView:
    return RunView(run_ref=f"r{fired.source_ref[-4:]}", flow=flow.ref, version=flow.version, firing=fired,
                   started_at=fired.at, sends=sends, closed_at=closed_at)


FLOWS = {flow.ref: flow for flow in (flow_of("carrito"), flow_of("pago_fallido"))}


# ---------------------------------------------------------------------------
# 1. plan_steps sobre las definiciones de ATT1.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("version", [3, 4, 5])
@pytest.mark.parametrize("key", ["carrito", "pago_fallido"])
def test_the_plan_of_each_att1_version(key: str, version: int) -> None:
    flow = flow_of(key, version)
    plan = plan_steps(flow, fire(1, DAY, event=key))
    caps = {3: (40, 400), 4: (40, 1000), 5: (80, 1000)}[version]
    assert (plan.per_day, plan.total) == caps
    assert plan.pool == "att1-recuperacion"
    assert plan.expires_after == timedelta(days=1)
    assert plan.franja == franja_of({"zona": ZONE, "desde": "09:00", "hasta": "21:00"})
    assert plan.exits == frozenset({"compra"})
    [step] = plan.steps
    assert (step.index, step.after, step.language, step.variables, step.button) == (
        0, timedelta(0), "es_MX", ("nombre", "producto"), None)
    assert step.template == {"carrito": "att1_carrito_abandonado_01", "pago_fallido": "att1_compra_fallida_01"}[key]
    assert step.step_key == {"carrito": "first_contact", "pago_fallido": "payment_failure_first_contact"}[key]


def test_the_waits_before_the_send_add_up_from_the_firing() -> None:
    flow = flow_of(pasos=[{"esperar": "30m"}, {"esperar": "2h"}, SEND])
    [step] = plan_steps(flow, fire(1, DAY)).steps
    assert step.after == timedelta(minutes=150)


def test_siempre_takes_the_fixed_zone_of_the_flow() -> None:
    plan = plan_steps(flow_of(horario="siempre"), fire(1, DAY))
    assert plan.franja.always and plan.franja.zone == ZONE


def test_sin_tope_is_no_cap() -> None:
    flow = flow_of(topes={"bolsa": "att1-recuperacion", "por_dia": "sin_tope", "total": "sin_tope",
                          "repeticion": {"una_por": "persona_y_oferta", "otra_despues_de": "0m"}})
    plan = plan_steps(flow, fire(1, DAY))
    assert (plan.per_day, plan.total) == (None, None)


THREE_TOUCHES = [{"esperar": "60m"}, SEND, {"esperar": "24h"}, SEND, {"esperar": "24h"}, SEND]


def test_with_the_preview_each_step_counts_from_the_first_send_and_shares_the_deadline() -> None:
    # La propuesta de los tres toques (§6): el primero a 1 h del disparo, y los
    # otros a 24 h y 48 h del primer envio, no a 24 h del anterior.
    flow = flow_of(pasos=THREE_TOUCHES, vence="4d")
    plan = plan_steps(flow, fire(1, DAY), capabilities=PREVIEW_CAPABILITIES)
    assert [step.after for step in plan.steps] == [timedelta(hours=1), timedelta(hours=24), timedelta(hours=48)]
    assert [step.index for step in plan.steps] == [0, 1, 2]
    # Desde la T2.1, el step_key de cada paso es el de la forma del carrito.
    assert [step.step_key for step in plan.steps] == ["first_contact", "cart_second_touch", "cart_last_touch"]
    assert plan.expires_after == timedelta(days=4)


def test_with_the_v1_three_touches_plan_like_the_preview() -> None:
    # La T2.1: la v1 ejecuta hasta tres envios por camino, con la espera entre uno y otro.
    flow = flow_of(pasos=THREE_TOUCHES, vence="4d")
    assert plan_steps(flow, fire(1, DAY)) == plan_steps(flow, fire(1, DAY), capabilities=PREVIEW_CAPABILITIES)


def test_with_the_v1_a_fourth_send_is_not_supported() -> None:
    flow = flow_of(pasos=[*THREE_TOUCHES, {"esperar": "24h"}, SEND], vence="5d")
    with pytest.raises(PlanNotSupported) as raised:
        plan_steps(flow, fire(1, DAY))
    assert raised.value.codes == ("engine_not_supported:demasiados_envios",)
    assert str(raised.value) == "engine_not_supported:demasiados_envios"
    assert len(plan_steps(flow, fire(1, DAY), capabilities=PREVIEW_CAPABILITIES).steps) == 4
    # Un cuarto paso no tiene step_key en la forma del carrito.
    assert plan_steps(flow, fire(1, DAY), capabilities=PREVIEW_CAPABILITIES).steps[3].step_key is None


@pytest.mark.parametrize(("changes", "codes"), [
    ({"dispara": {"evento": "ficha_impresa"}}, ["disparo_ficha_impresa"]),
    ({"dispara": {"evento": "compra"}}, ["disparo_compra"]),
    ({"dispara": {"silencio": {"lo_ultimo": "agente", "desde": "1d", "hasta": "7d"}}}, []),
    ({"pasos": [{"conversar": "Que termine la compra."}]}, ["conversar"]),
    ({"pasos": [SEND, {"derivar": "humano"}]}, ["derivar"]),
    ({"pasos": [{**SEND, "variables": ["nombre", "medio_de_pago"]}]}, ["variable_medio_de_pago"]),
    ({"pasos": [{**SEND, "boton": {"link": "ficha"}}]}, ["boton_ficha"]),
    ({"pasos": [{**SEND, "boton": {"link": "checkout", "cupon": "TIROIDES10"}}]}, []),
    ({"pasos": [SEND, {"esperar": "1h"}]}, ["espera_despues_del_envio"]),
    ({"termina": {"si": ["compra", "ficha_vencida"], "vence": "1d"}}, ["salida_ficha_vencida"]),
    ({"termina": {"si": ["compra", "contesta"], "vence": "1d"}}, ["contesta_con_disparo_de_evento"]),
])
def test_not_supported_names_what_the_v1_does_not_run(changes: dict[str, Any], codes: list[str]) -> None:
    definition = {**copy.deepcopy(DEFINITIONS["versions"]["5"]["carrito"]), **changes}
    assert not_supported(definition) == tuple(f"engine_not_supported:{code}" for code in codes)


def test_the_preview_differs_from_the_v1_only_in_the_sends_per_path() -> None:
    assert PREVIEW_CAPABILITIES.max_sends_per_path is None
    assert ENGINE_CAPABILITIES_V1.max_sends_per_path == 3
    assert ENGINE_CAPABILITIES_V1.waits_between_sends is True
    assert {**vars(PREVIEW_CAPABILITIES), "max_sends_per_path": 3} == vars(ENGINE_CAPABILITIES_V1)
    definition = {**copy.deepcopy(DEFINITIONS["versions"]["5"]["carrito"]), "pasos": THREE_TOUCHES}
    assert not_supported(definition, PREVIEW_CAPABILITIES) == ()
    assert not_supported(definition) == ()
    trailing = {**definition, "pasos": [*THREE_TOUCHES, {"esperar": "1h"}]}
    assert not_supported(trailing, PREVIEW_CAPABILITIES) == ("engine_not_supported:espera_despues_del_envio",)
    assert not_supported(trailing) == ("engine_not_supported:espera_despues_del_envio",)


def test_a_capability_without_waits_between_sends_names_the_wait() -> None:
    one_send = dataclasses.replace(ENGINE_CAPABILITIES_V1, max_sends_per_path=1, waits_between_sends=False)
    definition = {**copy.deepcopy(DEFINITIONS["versions"]["5"]["carrito"]), "pasos": THREE_TOUCHES}
    assert not_supported(definition, one_send) == ("engine_not_supported:espera_despues_del_envio",
                                                   "engine_not_supported:demasiados_envios")


@pytest.mark.parametrize(("changes", "code"), [
    ({"horario": "a_medir"}, "plan_horario_invalid"),
    ({"horario": {"zona": "Europe/Madrid", "desde": "09:00", "hasta": "21:00"}}, "plan_horario_invalid"),
    ({"pasos": [{"esperar": "a_medir"}, SEND]}, "plan_wait_invalid"),
    ({"pasos": [{"esperar": "1h"}]}, "plan_without_send"),
    ({"pasos": []}, "plan_without_send"),
    ({"vence": "a_medir"}, "plan_vence_invalid"),
    ({"vence": "0m"}, "plan_vence_invalid"),
    ({"topes": {"bolsa": "att1-recuperacion", "por_dia": "a_medir", "total": 1000}}, "plan_topes_invalid"),
])
def test_plan_steps_rejects_the_shape_it_cannot_read(changes: dict[str, Any], code: str) -> None:
    with pytest.raises(PlanInvalid) as raised:
        plan_steps(flow_of(**changes), fire(1, DAY))
    assert str(raised.value) == code


# ---------------------------------------------------------------------------
# 2. plan_start.
# ---------------------------------------------------------------------------

CART, FAILED = FLOWS[("att1", "carrito")], FLOWS[("att1", "pago_fallido")]
NOBODY = PersonView(runs=(), winners_fired=())


def test_a_firing_with_the_permission_starts() -> None:
    assert plan_start(CART, fire(1, DAY), NOBODY, flows=FLOWS) == StartDecision(True, None, None)


def test_without_the_permission_it_does_not_enter() -> None:
    decision = plan_start(CART, fire(1, DAY, permission=None), NOBODY, flows=FLOWS)
    assert decision == StartDecision(False, "no_entra:permiso", None)


def test_a_second_cart_while_the_first_waits_is_ya_en_curso() -> None:
    # G9: la repeticion persona_y_oferta, 0m; el disparo se suma al recorrido abierto.
    first = run(CART, fire(1, DAY))
    decision = plan_start(CART, fire(2, DAY + timedelta(minutes=20)), PersonView((first,), ()), flows=FLOWS)
    assert decision == StartDecision(False, "ya_en_curso", first.run_ref)


def test_a_second_cart_of_another_offer_starts() -> None:
    first = run(CART, fire(1, DAY))
    decision = plan_start(CART, fire(2, DAY + timedelta(minutes=20), offer="otra"), PersonView((first,), ()),
                          flows=FLOWS)
    assert decision.starts


def test_a_cart_after_the_first_was_sent_starts_another_run() -> None:
    # G10: con otra_despues_de 0m, despues del envio aceptado arranca otro.
    first = run(CART, fire(1, DAY), sends=(DAY,), closed_at=DAY + timedelta(seconds=3))
    decision = plan_start(CART, fire(2, DAY + timedelta(minutes=20)), PersonView((first,), ()), flows=FLOWS)
    assert decision.starts


def test_otra_despues_de_counts_from_the_last_send() -> None:
    repeat = {"bolsa": "att1-recuperacion", "por_dia": 80, "total": 1000,
              "repeticion": {"una_por": "persona_y_oferta", "otra_despues_de": "24h"}}
    flow = flow_of(topes=repeat)
    first = run(flow, fire(1, DAY), sends=(DAY,), closed_at=DAY + timedelta(seconds=3))
    person = PersonView((first,), ())
    inside = plan_start(flow, fire(2, DAY + timedelta(hours=23)), person, flows=FLOWS)
    assert inside == StartDecision(False, "ya_en_curso", None)
    assert plan_start(flow, fire(3, DAY + timedelta(hours=25)), person, flows=FLOWS).starts
    never = flow_of(topes={**repeat, "repeticion": {"una_por": "persona", "otra_despues_de": "nunca"}})
    old = run(never, fire(1, DAY - timedelta(days=30)), closed_at=DAY - timedelta(days=29))
    assert plan_start(never, fire(4, DAY), PersonView((old,), ()), flows=FLOWS) == StartDecision(
        False, "ya_en_curso", None)


def test_a_cart_with_a_payment_failure_open_does_not_start() -> None:
    # G7: no_arranca_mientras: [pago_fallido].
    failed = run(FAILED, fire(1, DAY, event="pago_fallido"))
    decision = plan_start(CART, fire(2, DAY + timedelta(minutes=11)), PersonView((failed,), ()), flows=FLOWS)
    assert decision == StartDecision(False, "no_arranca_mientras:pago_fallido", None)


def test_a_payment_failure_with_a_cart_open_opens_its_own_run() -> None:
    # G8: el pago fallido no tiene no_arranca_mientras.
    cart = run(CART, fire(1, DAY))
    decision = plan_start(FAILED, fire(2, DAY + timedelta(minutes=1), event="pago_fallido"),
                          PersonView((cart,), ()), flows=FLOWS)
    assert decision.starts


def test_a_payment_failure_repeated_before_the_send_joins_the_open_run() -> None:
    # F#11: la repeticion queda como evidencia del recorrido abierto. El replay
    # adelanta su comienzo con joins_run (diseno de la T3a, §4.4).
    first = run(FAILED, fire(1, DAY, event="pago_fallido"))
    decision = plan_start(FAILED, fire(2, DAY + timedelta(minutes=20), event="pago_fallido"),
                          PersonView((first,), ()), flows=FLOWS)
    assert decision == StartDecision(False, "ya_en_curso", first.run_ref)


def test_without_a_contact_the_repetition_is_not_looked_at() -> None:
    first = run(CART, fire(1, DAY))
    decision = plan_start(CART, fire(2, DAY + timedelta(minutes=20), contact=False), PersonView((first,), ()),
                          flows=FLOWS)
    assert decision.starts


def test_a_flow_that_wins_beats_it_when_it_fired_first() -> None:
    # El carrito le gana al primer contacto: con un carrito ya disparado para la
    # persona, un flujo del motor que el carrito tiene en le_gana_a no arranca.
    precheckout = EngineFlow(
        flow_id="1b000003-0000-4000-8000-0000000c0003", product_ref="att1", flow_key="precheckout", version=1,
        definition={**copy.deepcopy(CART.definition), "flujo": "precheckout", "dispara": {"evento": "intencion"}},
        legacy_kind="precheckout", fixed_zone=ZONE, pool="att1-primer-contacto", exceptions=SQL_AND_DISPATCH,
    )
    flows = {**FLOWS, precheckout.ref: precheckout}
    winner = fire(1, DAY)
    decision = plan_start(precheckout, fire(2, DAY + timedelta(minutes=5), event="intencion"),
                          PersonView((), (winner,)), flows=flows)
    assert decision == StartDecision(False, "le_gano:carrito", None)
    assert plan_start(precheckout, fire(3, DAY - timedelta(minutes=5), event="intencion"),
                      PersonView((), (winner,)), flows=flows).starts


def test_a_firing_of_another_product_is_an_error() -> None:
    other = Firing(uid(1), "carrito", DAY, DAY, "att2", None, None, frozenset(), "formulario")
    with pytest.raises(ValueError, match="firing_product_differs_from_flow"):
        plan_start(CART, other, NOBODY, flows=FLOWS)


# ---------------------------------------------------------------------------
# 3. wins.
# ---------------------------------------------------------------------------

def test_wins_by_person_looks_at_the_run_in_course() -> None:
    winner = fire(1, DAY, who=7)
    seen: list[tuple[str, datetime]] = []

    def open_at(ref: str, when: datetime) -> bool:
        seen.append((ref, when))
        return when < DAY + timedelta(hours=1)

    kwargs = {"route": "persona", "product_ref": "att1", "run_open_at": open_at}
    assert wins(winner, uid(9007), None, at=DAY + timedelta(minutes=5), **kwargs)
    assert not wins(winner, uid(9007), None, at=DAY + timedelta(hours=2), **kwargs)
    assert not wins(winner, uid(9008), None, at=DAY, **kwargs)
    assert not wins(winner, None, None, at=DAY, **kwargs)
    assert seen == [(winner.source_ref, DAY + timedelta(minutes=5)), (winner.source_ref, DAY + timedelta(hours=2))]
    # Con at None alcanza con que el recorrido exista, y run_open_at no se llama.
    assert wins(winner, uid(9007), None, at=None, **kwargs)
    assert len(seen) == 2
    # Sin caso (sin contacto) no gana por persona; otro producto no gana.
    assert not wins(fire(2, DAY, contact=False), uid(9001), None, at=None, **kwargs)
    assert not wins(winner, uid(9007), None, at=None, route="persona", product_ref="att2", run_open_at=open_at)


def test_wins_by_intent_with_any_of_its_two_correlations() -> None:
    # Un pago fallido con dos correlaciones (cart_correlation y
    # payment_failure_correlation) le gana por intencion a cada una.
    winner = fire(1, DAY, event="pago_fallido", contact=False, intents=frozenset({uid(501), uid(502)}))

    def never(ref: str, when: datetime) -> bool:
        raise AssertionError("por intencion no se mira el recorrido")

    kwargs = {"route": "intencion", "product_ref": "att1", "run_open_at": never, "at": DAY}
    assert wins(winner, None, uid(501), **kwargs)
    assert wins(winner, uid(9001), uid(502), **kwargs)
    assert not wins(winner, None, uid(503), **kwargs)
    assert not wins(winner, None, None, **kwargs)
