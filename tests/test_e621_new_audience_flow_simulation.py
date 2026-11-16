"""E-621: la simulacion de un flujo nuevo con disparo por audiencia (manual, fecha, cada).

El defecto, medido en Maribel (v1.40.0) el 2026-11-16: el AE armo tres flujos nuevos de audiencia
(``manual: {}``, ``fecha {el, zona}`` y ``cada {dias, hora, ...}``), los valido, y la pagina de aprobacion
dijo «La simulación no se pudo armar (simulation_new_flow_counts_invalid). La página no publica sin
simulación». ``_simulation_inputs`` (flow_change_api) pedia el evento del disparador con
``simulation_trigger``, que no tiene evento para una audiencia, y devolvia
``simulation_new_flow_counts_invalid`` sin leer nada. Sin simulacion construida, la pagina no publica.

Ahora un ``nuevo`` de audiencia arma su simulacion de la definicion: sin evento que contar, nace en la
cohorte vacia (al publicarse no le llega a nadie), a quien apunta el ``para`` y que el numero lo cuenta
la lista de cada envio.
"""

from __future__ import annotations

import copy

import pytest
from test_flow_change_api_app import AUTH, PREFIX, SLACK, PostgrestEmulator, _client
from test_flow_change_new_flow import CLIENT_OK, CONFIRMED, _ENV
from test_flow_change_operator_script import FakeDocker, FakeTerminal
from test_flow_change_operator_script import script

from bridge.flow_approval import _simulation_summary
from bridge.flow_change_simulation import (
    build_new_audience_flow_simulation,
    build_new_flow_simulation,
    new_audience_flow_form,
)
from bridge.flow_definition import parse_items

CDMX = "America/Mexico_City"
TRIGGERS = {
    "manual": {"manual": {}},
    "fecha": {"fecha": {"el": "2026-12-01T10:00", "zona": CDMX}},
    "cada": {"cada": {"dias": ["lun", "jue"], "hora": "10:00", "zona": CDMX, "desde": "2026-12-01"}},
}


def _aviso(form: str = "manual", **changes: object) -> dict:
    """Lo que escribio el AE en Maribel, con el producto y la plantilla del doble."""
    definition: dict = {
        "nombre": f"Aviso {form} de prueba", "dispara": copy.deepcopy(TRIGGERS[form]),
        "para": {"es": "clienta"}, "entra": {"permiso": "compra"},
        "pasos": [{"plantilla": CONFIRMED}],
    }
    definition.update(changes)
    return definition


def _item(form: str = "manual", **changes: object) -> dict:
    return {"producto": "att1", "flujo": f"aviso_{form}_prueba", "tipo": "nuevo",
            "definicion": _aviso(form, **changes), "prender": "cohorte", "ok_del_cliente": CLIENT_OK}


@pytest.fixture
def image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETTER_VERSION", "1.40.1")
    monkeypatch.setenv("GIT_SHA", "f6c5ce2c00000000000000000000000000000000")


@pytest.mark.parametrize("form", ["manual", "fecha", "cada"])
def test_validating_a_new_audience_flow_builds_the_simulation_without_counting_events(
        image: None, form: str) -> None:
    emulator = PostgrestEmulator()
    client, _ = _client(emulator)
    saved = client.post(f"{PREFIX}/flow-changes", headers=AUTH, json={
        "items": [_item(form)], "base": {}, "purpose": "un aviso a las clientas", "requested_by": SLACK})
    assert saved.status_code == 201, saved.text
    proposal_id = saved.json()["data"]["proposal_id"]
    validated = client.post(f"{PREFIX}/flow-changes/{proposal_id}/validate", headers=AUTH,
                            json={"revision": 1, "requested_by": SLACK})
    assert validated.status_code == 200, validated.text
    preview = validated.json()["data"]["preview"]
    simulation = preview["simulacion"]
    # Antes: {"status": "failed", "reason_code": "simulation_new_flow_counts_invalid"}.
    assert simulation["status"] == "built", simulation
    summary = simulation["summary"]
    assert (summary["disparador"], summary["disparos_por_evento"], summary["nace_en"], summary["cohorte_al_nacer"],
            summary["alcance_al_nacer"]) == (form, 0, "cohorte", 0, 0)
    assert summary["audiencia"] == {"es": "clienta", "producto": "att1"}
    assert summary["flujo_nuevo"] == f"att1/aviso_{form}_prueba"
    assert len(simulation["summary_sha256"]) == 64
    assert "simulation_not_built" not in [gap["code"] for gap in preview["huecos"]]
    # Sin evento no hay nada que contar: no se pide la lectura de conteos.
    assert not [function for function, _, _ in emulator.requests
                if function == "read_flow_engine_native_trigger_counts_v1"]


def test_the_summary_says_the_schedule_the_audience_and_who_confirms() -> None:
    [item] = parse_items([_item("cada", para={"es": "prospecta", "formulario_en": "14d", "etiquetas": ["black"],
                                               "excluye": ["vip"]},
                                entra={"permiso": "formulario"})])
    built = build_new_audience_flow_simulation([item])
    assert built["status"] == "built"
    summary = built["summary"]
    assert summary["dispara"] == TRIGGERS["cada"]["cada"]
    assert summary["audiencia"] == {"es": "prospecta", "producto": "att1", "formulario_en_dias": 14,
                                    "etiquetas": ["black"], "excluye": ["vip"]}
    assert summary["ritmo"] == {"por_hora": 200, "por_ocurrencia": 1000}
    assert summary["confirma"] == "sola_a_la_hora"
    [manual] = parse_items([_item("manual")])
    assert build_new_audience_flow_simulation([manual])["summary"]["confirma"] == \
        "una_persona_en_la_pagina_de_envios"


def test_what_is_not_a_single_new_audience_flow_is_refused() -> None:
    [manual] = parse_items([_item("manual")])
    [other] = parse_items([_item("fecha")])
    assert build_new_audience_flow_simulation([manual, other]) == {
        "status": "failed", "reason_code": "simulation_new_flow_mixed"}
    # Un permiso que no va con el estado no es una audiencia que el barredor sepa leer.
    [wrong] = parse_items([_item("manual", entra={"permiso": "formulario"})])
    assert build_new_audience_flow_simulation([wrong]) == {
        "status": "failed", "reason_code": "simulation_new_flow_audience_invalid"}
    # Un nuevo con evento sigue por los conteos, como antes.
    event = _item("manual")
    event["definicion"] = {**event["definicion"], "dispara": {"evento": "compra"}}
    event["definicion"].pop("para")
    assert new_audience_flow_form([event]) is None
    [parsed] = parse_items([event])
    counts = {"contract": "flow-engine-v2", "since": "2026-11-09T00:00:00Z", "until": "2026-11-16T00:00:00Z",
              "events": 1, "people": 1, "product_ref": "att1", "trigger": "compra"}
    assert build_new_flow_simulation(counts, [parsed])["status"] == "built"
    assert build_new_audience_flow_simulation([parsed])["status"] == "failed"


@pytest.mark.parametrize("form", ["manual", "fecha", "cada"])
def test_the_page_says_the_cohort_is_empty_and_does_not_ask_for_the_operator(form: str) -> None:
    [item] = parse_items([_item(form)])
    html = _simulation_summary(build_new_audience_flow_simulation([item]))
    assert "--sin-simulacion" not in html and "no se pudo armar" not in html
    assert "<strong>0</strong> contactos" in html
    assert "las clientas que compraron <strong>att1</strong>" in html
    assert "lista de cada envío" in html
    if form == "manual":
        assert "Armar la lista" in html
    elif form == "fecha":
        assert "2026-12-01 a las 10:00" in html
    else:
        assert "lunes, jueves a las 10:00" in html


def test_the_operator_script_does_not_read_counts_for_an_audience_and_agrees_with_the_bridge() -> None:
    docker = FakeDocker()
    ops = script.Ops(script.Config.from_env({**_ENV}), docker=docker, terminal=FakeTerminal(),
                     out=__import__("io").StringIO(), code=lambda: "abc123")
    for form in TRIGGERS:
        assert ops.read_engine_inputs([_item(form)]) == {"new_flow_audience": True}
    assert not [stdin for _, stdin in docker.calls if "read_flow_engine_native_trigger_counts_v1" in stdin]
    event = _item("manual")
    event["definicion"] = {**event["definicion"], "dispara": {"evento": "compra"}}
    two = {"manual": {}, "evento": "compra"}
    cases = [[_item(form)] for form in TRIGGERS] + [[event], [_item("manual"), _item("fecha")], [],
                                                    [{**_item("manual"), "tipo": "version"}],
                                                    [{**_item("manual"), "definicion": _aviso(dispara=two)}]]
    for items in cases:
        assert script._new_audience_flow_form(items) == new_audience_flow_form(items), items  # noqa: SLF001
