"""El comando de la simulacion del motor (diseno de la T3a, §3.3, §5.3 y §8.1, commit 4).

Fixtures: la captura de ATT1 (``tests/fixtures/flow_engine/att1_recovery_capture_20261016.json``), las
definiciones de la v3, la v4 y la v5 (``tests/fixtures/flow_simulation/att1_definitions_by_scope_version.json``),
las diferencias con nombre de la T1 (``tests/fixtures/flow_engine/att1_parity_named_differences.json``) y las
propuestas medidas (``tests/fixtures/flow_simulation/simulation_proposals.json``), de donde salen los items de
las propuestas que se le pasan al comando.

* **Los codigos de salida:** 0 en verde, 1 si la consistencia no cierra o un archivo no se lee, 2 si la
  propuesta se rechaza. Una propuesta de mas de un envio sin ``--varios-toques`` sale con 2 (el rojo 21).
* **La salida del lienzo:** coincide con ``flow_summary`` de ``simulate``, con el indice de cada envio dentro
  de ``pasos``; pasa la segunda red de la prueba de privacidad de la captura
  (``tests/test_flow_engine_capture_privacy.py``, importada, no copiada) y su lista cerrada de claves.
* **Sin red ni entorno.**

Las pruebas comparan conteos y caminos, nunca un valor de una persona: la captura no lo trae.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import socket
import sys
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from bridge.flow_definition import parse_items
from bridge.flow_engine import PREVIEW_CAPABILITIES
from bridge.flow_parity import Capture
from bridge.flow_replay import (
    Simulation,
    current_versions,
    engine_flows_from_definitions,
    fixed,
    flow_summary,
    history_from_capture,
    proposed_versions,
    simulate,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "flow_engine" / "att1_recovery_capture_20261016.json"
NAMED_FILE = ROOT / "tests" / "fixtures" / "flow_engine" / "att1_parity_named_differences.json"
SIMULATION = ROOT / "tests" / "fixtures" / "flow_simulation"
DEFINITIONS: dict[str, Any] = json.loads(
    (SIMULATION / "att1_definitions_by_scope_version.json").read_text(encoding="utf-8"))
PROPOSALS: dict[str, Any] = json.loads((SIMULATION / "simulation_proposals.json").read_text(encoding="utf-8"))
HISTORY = history_from_capture(Capture.from_json(json.loads(FIXTURE.read_text(encoding="utf-8"))))
VERSIONS = engine_flows_from_definitions(DEFINITIONS)
CART = ("att1", "carrito")
FAILED = ("att1", "pago_fallido")
MORE_THAN_ONE_SEND = "engine_not_supported:mas_de_un_envio"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SCRIPT = _load("flow_engine_simulate_script", ROOT / "scripts" / "flow_engine_simulate.py")
# La prueba de privacidad de la captura: su segunda red y sus formas fijas.
PRIVACY = _load("flow_engine_capture_privacy_for_simulate", ROOT / "tests" / "test_flow_engine_capture_privacy.py")


def items_of(name: str) -> list[dict[str, Any]]:
    [proposal] = [item for item in PROPOSALS["propuestas"] if item["nombre"] == name]
    return copy.deepcopy(proposal["items"])


def write_proposal(tmp_path: Path, items: list[dict[str, Any]], *, wrapped: bool = True) -> Path:
    path = tmp_path / "propuesta.json"
    path.write_text(json.dumps({"items": items} if wrapped else items, ensure_ascii=False), encoding="utf-8")
    return path


@cache
def simulation(name: str) -> Simulation:
    current = current_versions(HISTORY, VERSIONS)
    versions, chosen = proposed_versions(VERSIONS, current, parse_items(items_of(name)))
    return simulate(HISTORY, versions, fixed(current), fixed(chosen), capabilities=PREVIEW_CAPABILITIES)


def lienzo_steps(summary: Mapping[str, Any], positions: list[int]) -> list[dict[str, Any]]:
    """Lo que el lienzo tiene que traer para los pasos de ``flow_summary``, con el indice dentro de ``pasos``."""
    return [{"paso": positions[row["paso"]], "envio": row["paso"], "enviados": row["enviados"],
             "terminan": row["terminan"], "movidos_por_el_tope": row["movidos_por_el_tope"], "cota": row["cota"]}
            for row in summary["pasos"]]


def two_templates() -> list[dict[str, Any]]:
    items = items_of("carrito_a_1h")
    pasos = items[0]["definicion"]["pasos"]
    items[0]["definicion"]["pasos"] = [pasos[1], {"esperar": "24h"}, copy.deepcopy(pasos[1])]
    return items


# ---------------------------------------------------------------------------
# --consistencia.
# ---------------------------------------------------------------------------

def test_the_consistency_on_the_fixture_exits_zero_with_its_numbers(capsys: pytest.CaptureFixture[str]) -> None:
    assert SCRIPT.main(["--consistencia"]) == 0
    out = capsys.readouterr().out
    for line in ("Vigente contra si misma: 0 cambios", "  disparos: 252 / 252", "  llegaron: 248 / 248",
                 "  arranques: 213 / 213", "  envios: 213 / -", "  envios_del_legado: 208 / 208",
                 "E distinta de la T1: 0", "Envios de mas en el replay: 5 (explicados por una diferencia con nombre: 5)",
                 "Con nombre y sin envio en el replay: 0",
                 ("P6 (replay / T1): cedieron 192/192, las_dos 78/78, solo_persona 1/1, solo_intencion 113/113,"
                  " sin_ganador 0/0, ganadores_sin_cancelacion 0/0"),
                 "Resultado: verde"):
        assert line in out.splitlines(), line

    assert SCRIPT.main(["--consistencia", "--capture", str(FIXTURE), "--named", str(NAMED_FILE), "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)["consistencia"]
    assert printed["ok"] is True
    assert printed["replay_counts"] == {"disparos": 252, "llegaron": 248, "arranques": 213, "envios": 213,
                                        "envios_del_legado": 208}
    assert printed["p6"] == printed["t1_p6"]
    assert len(printed["extra_sends"]) == len(printed["extra_explained_by_named"]) == 5


def test_the_consistency_without_the_named_differences_exits_one(capsys: pytest.CaptureFixture[str],
                                                                tmp_path: Path) -> None:
    empty = tmp_path / "sin_nombre.json"
    empty.write_text('{"diferencias": []}', encoding="utf-8")
    assert SCRIPT.main(["--consistencia", "--named", str(empty)]) == 1
    out = capsys.readouterr().out
    assert "Envios de mas en el replay: 5 (explicados por una diferencia con nombre: 0)" in out
    assert "Resultado: rojo" in out


def test_an_unreadable_file_exits_one(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    broken = tmp_path / "rota.json"
    broken.write_text('{"a": 1, "a": 2}', encoding="utf-8")
    assert SCRIPT.main(["--consistencia", "--named", str(broken)]) == 1
    assert capsys.readouterr().out.startswith("error: rota.json")


# ---------------------------------------------------------------------------
# --propuesta y --varios-toques.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["tres_toques_vence_4d", "dos_plantillas"])
def test_more_than_one_send_without_the_flag_is_rejected(name: str, capsys: pytest.CaptureFixture[str],
                                                         tmp_path: Path) -> None:
    # El rojo 21: las capacidades v1 son de un envio por camino, y sin --varios-toques el comando no simula.
    items = two_templates() if name == "dos_plantillas" else items_of(name)
    proposal = write_proposal(tmp_path, items)
    lienzo = tmp_path / "lienzo.json"
    assert SCRIPT.main(["--propuesta", str(proposal), "--lienzo", str(lienzo)]) == 2
    out = capsys.readouterr().out
    assert out.startswith("propuesta rechazada: ")
    assert MORE_THAN_ONE_SEND in out.splitlines()[0]
    assert "--varios-toques" in out
    assert not lienzo.exists()


def test_three_touches_with_the_flag_give_207_183_and_157(capsys: pytest.CaptureFixture[str],
                                                          tmp_path: Path) -> None:
    proposal = write_proposal(tmp_path, items_of("tres_toques_vence_4d"), wrapped=False)
    assert SCRIPT.main(["--propuesta", str(proposal), "--varios-toques"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "capacidades: vista previa de varios toques (PREVIEW_CAPABILITIES, --varios-toques)" in out
    assert "versiones: carrito v5 -> v6, pago_fallido v5 -> v5" in out
    propuesta = out[out.index("Propuesta: de 252 disparos, 212 habrian recibido el mensaje"):]
    for line in ("    envio 0: 207 enviados (como_maximo), terminan: en_curso 1, movidos por el tope: 49",
                 "    envio 1: 183 enviados (como_maximo), terminan: compro 2, en_curso 22, movidos por el tope: 24",
                 "    envio 2: 157 enviados (como_maximo), terminan: compro 1, en_curso 25, movidos por el tope: 40"):
        assert line in propuesta, line
    assert "Cambios: 208 de 252 disparos" in out

    assert SCRIPT.main(["--propuesta", str(proposal), "--varios-toques", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)["simulacion"]
    assert printed["varios_toques"] is True
    assert [row["enviados"] for row in printed["resumen"]["propuesta"]["flujos"]["carrito"]["pasos"]] == [207, 183, 157]
    assert len(printed["cambios"]) == printed["resumen"]["cambios"] == 208


def test_a_single_send_proposal_runs_with_the_v1(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    proposal = write_proposal(tmp_path, items_of("carrito_a_1h"))
    assert SCRIPT.main(["--propuesta", str(proposal)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "capacidades: v1, un envio por camino" in out
    assert "Propuesta: de 252 disparos, 212 habrian recibido el mensaje" in out
    assert "Cambios: 141 de 252 disparos" in out


@pytest.mark.parametrize(("change", "code"), [
    (lambda items: items[0].update(en_curso="cancelar"), "en_curso_not_supported"),
    (lambda items: items[0].update(flujo="otro"), "proposal_flow_unknown"),
    (lambda items: items.clear(), "items_count_invalid"),
])
def test_a_proposal_the_engine_does_not_take_exits_two(change: Any, code: str, capsys: pytest.CaptureFixture[str],
                                                       tmp_path: Path) -> None:
    items = items_of("carrito_a_1h")
    change(items)
    assert SCRIPT.main(["--propuesta", str(write_proposal(tmp_path, items)), "--varios-toques"]) == 2
    assert code in capsys.readouterr().out.splitlines()[0]


# ---------------------------------------------------------------------------
# --lienzo.
# ---------------------------------------------------------------------------

def test_the_lienzo_of_the_current_matches_simulate(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    lienzo = tmp_path / "lienzo.json"
    assert SCRIPT.main(["--lienzo", str(lienzo)]) == 0
    document = json.loads(lienzo.read_text(encoding="utf-8"))
    current = current_versions(HISTORY, VERSIONS)
    same = simulate(HISTORY, VERSIONS, fixed(current), fixed(current))
    summary = flow_summary(same.current)
    # Las dos son la v5, de un solo envio en pasos[0]: 208 carritos y 5 pagos fallidos, los dos exactos.
    assert document["flujos"] == {
        "carrito": {"hoy": {"version": 5, "recorridos": 208, "en_curso": 0, "pasos": [
            {"paso": 0, "envio": 0, "enviados": 208, "terminan": {}, "movidos_por_el_tope": 0, "cota": "exacta"}]}},
        "pago_fallido": {"hoy": {"version": 5, "recorridos": 5, "en_curso": 0, "pasos": [
            {"paso": 0, "envio": 0, "enviados": 5, "terminan": {}, "movidos_por_el_tope": 0, "cota": "exacta"}]}},
    }
    for flow, data in summary.items():
        assert document["flujos"][flow]["hoy"]["pasos"] == lienzo_steps(data, [0])
        assert document["flujos"][flow]["hoy"]["recorridos"] == data["recorridos"]
    assert (document["captura"], document["ventana"], document["varios_toques"]) == (
        "2026-10-16T21:03:26.141035Z",
        {"desde": "2026-10-08T22:42:19.893671Z", "hasta": "2026-10-16T21:03:26.141035Z"}, False)
    assert SCRIPT.lienzo_problems(document) == []
    assert PRIVACY._independent_findings(document) == []
    assert "lienzo: lienzo.json (2 flujos)" in capsys.readouterr().out


def test_the_lienzo_of_three_touches_indexes_each_send_by_its_place_in_pasos(tmp_path: Path) -> None:
    proposal = write_proposal(tmp_path, items_of("tres_toques_vence_4d"))
    lienzo = tmp_path / "lienzo.json"
    assert SCRIPT.main(["--propuesta", str(proposal), "--varios-toques", "--lienzo", str(lienzo),
                        "--etiqueta", "propuesto"]) == 0
    document = json.loads(lienzo.read_text(encoding="utf-8"))
    assert document["varios_toques"] is True
    result = simulation("tres_toques_vence_4d")
    # pasos: esperar 1h, plantilla, esperar 24h, plantilla, esperar 24h, plantilla. Los envios son p1, p3 y p5.
    cart = document["flujos"]["carrito"]
    assert cart["propuesto"]["pasos"] == lienzo_steps(flow_summary(result.proposed)["carrito"], [1, 3, 5])
    assert [(row["paso"], row["enviados"], row["cota"]) for row in cart["propuesto"]["pasos"]] == [
        (1, 207, "como_maximo"), (3, 183, "como_maximo"), (5, 157, "como_maximo")]
    assert (cart["propuesto"]["version"], cart["propuesto"]["recorridos"], cart["propuesto"]["en_curso"]) == (6, 208, 48)
    assert cart["hoy"]["pasos"] == lienzo_steps(flow_summary(result.current)["carrito"], [0])
    assert document["flujos"]["pago_fallido"]["propuesto"] == document["flujos"]["pago_fallido"]["hoy"]
    assert SCRIPT.lienzo_problems(document) == []
    assert PRIVACY._independent_findings(document) == []


def test_the_lienzo_label_of_the_proposal_is_chosen_and_a_flow_turned_off_has_no_steps(tmp_path: Path) -> None:
    off = [{"producto": "att1", "flujo": "pago_fallido", "tipo": "apagar",
            "flow_id": DEFINITIONS["flows"]["pago_fallido"]["flow_id"]}]
    lienzo = tmp_path / "lienzo.json"
    assert SCRIPT.main(["--propuesta", str(write_proposal(tmp_path, off)), "--lienzo", str(lienzo),
                        "--etiqueta", "atajo"]) == 0
    document = json.loads(lienzo.read_text(encoding="utf-8"))
    assert set(document["flujos"]["carrito"]) == {"hoy", "atajo"}
    assert document["flujos"]["pago_fallido"]["atajo"] == {"version": None, "recorridos": 0, "en_curso": 0,
                                                           "pasos": []}
    assert SCRIPT.lienzo_problems(document) == []


@pytest.mark.parametrize("label", ["hoy", "Propuesto", "con espacio", ""])
def test_a_label_out_of_shape_exits_two(label: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert SCRIPT.main(["--lienzo", "no-se-escribe.json", "--etiqueta", label]) == 2
    assert capsys.readouterr().out.startswith("error: --etiqueta")


def _valid_document() -> dict[str, Any]:
    current = current_versions(HISTORY, VERSIONS)
    result = simulation("tres_toques_vence_4d")
    versions, chosen = proposed_versions(VERSIONS, current, parse_items(items_of("tres_toques_vence_4d")))
    return SCRIPT.build_lienzo(HISTORY, versions, result.current, current, proposed_result=result.proposed,
                               chosen=chosen, several_touches=True)


def _set(path: tuple[Any, ...], value: Any) -> Any:
    def apply(document: dict[str, Any]) -> None:
        target: Any = document
        for step in path[:-1]:
            target = target[step]
        target[path[-1]] = value
    return apply


@pytest.mark.parametrize(("name", "change", "expected"), [
    ("una clave nueva arriba", _set(("telefono",), 1), "lienzo: clave fuera de la lista (telefono)"),
    ("una clave nueva en la ventana", _set(("ventana", "dias"), 7), "lienzo.ventana: clave fuera de la lista (dias)"),
    ("una clave nueva en una version", _set(("flujos", "carrito", "propuesto", "personas"), []),
     "lienzo.flujos.carrito.propuesto: clave fuera de la lista (personas)"),
    ("una clave nueva en un paso", _set(("flujos", "carrito", "propuesto", "pasos", 0, "disparos"), ["x"]),
     "lienzo.flujos.carrito.propuesto.pasos[0]: clave fuera de la lista (disparos)"),
    ("un fin que no existe", _set(("flujos", "carrito", "propuesto", "pasos", 1, "terminan", "contesto"), 1),
     "lienzo.flujos.carrito.propuesto.pasos[1].terminan: clave fuera de la lista (contesto)"),
    ("una clave con forma rara", _set(("flujos", "carrito", "propuesto", "+5215512345678"), 1),
     "lienzo.flujos.carrito.propuesto: clave fuera de la lista (<rara>)"),
    ("un telefono como conteo", _set(("flujos", "carrito", "hoy", "recorridos"), 5215512345678),
     "lienzo.flujos.carrito.hoy.recorridos: no es un conteo"),
    ("un uuid como hora", _set(("captura",), "2462eaf0-b397-45b0-9aea-2ad8c77e42ec"), "lienzo.captura: no es una hora"),
    ("una cota inventada", _set(("flujos", "pago_fallido", "hoy", "pasos", 0, "cota"), "aprox"),
     "lienzo.flujos.pago_fallido.hoy.pasos[0].cota: no es exacta ni como_maximo"),
    ("una clave que falta", lambda document: document["flujos"]["carrito"]["hoy"].pop("en_curso"),
     "lienzo.flujos.carrito.hoy: falta la clave en_curso"),
])
def test_a_key_or_value_out_of_the_closed_list_is_red(name: str, change: Any, expected: str) -> None:
    document = _valid_document()
    assert SCRIPT.lienzo_problems(document) == []
    change(document)
    problems = SCRIPT.lienzo_problems(document)
    assert expected in problems, problems
    # El mensaje dice el camino, nunca el valor.
    assert not any("5215512345678" in problem or "2462eaf0" in problem for problem in problems)


def test_the_command_does_not_write_a_lienzo_out_of_shape(monkeypatch: pytest.MonkeyPatch,
                                                          capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    real = SCRIPT.build_lienzo

    def with_a_person(*args: Any, **kwargs: Any) -> dict[str, Any]:
        document = real(*args, **kwargs)
        document["flujos"]["carrito"]["hoy"]["pasos"][0]["disparos"] = ["2462eaf0-b397-45b0-9aea-2ad8c77e42ec"]
        return document

    monkeypatch.setattr(SCRIPT, "build_lienzo", with_a_person)
    lienzo = tmp_path / "lienzo.json"
    assert SCRIPT.main(["--lienzo", str(lienzo)]) == 1
    out = capsys.readouterr().out
    assert "la salida del lienzo no tiene la forma cerrada; no se escribio" in out
    assert "2462eaf0" not in out
    assert not lienzo.exists()


# ---------------------------------------------------------------------------
# Sin red, sin entorno, y nada fuera de las formas de la captura.
# ---------------------------------------------------------------------------

def test_the_command_does_no_network_and_reads_no_environment(monkeypatch: pytest.MonkeyPatch,
                                                              capsys: pytest.CaptureFixture[str],
                                                              tmp_path: Path) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("el comando abrio la red")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    # Con el entorno vacio da lo mismo: no depende de ninguna variable. Y el script no importa ni os ni nada
    # que abra la red (argparse y gettext leen el idioma del entorno; el script, nada).
    monkeypatch.setattr(os, "environ", {})
    source = (ROOT / "scripts" / "flow_engine_simulate.py").read_text(encoding="utf-8")
    imported = set(re.findall(r"^(?:import|from) ([a-z_.]+)", source, flags=re.MULTILINE))
    assert imported.isdisjoint({"os", "socket", "urllib", "urllib.request", "http", "http.client", "subprocess"})
    proposal = write_proposal(tmp_path, items_of("tres_toques_vence_4d"))
    assert SCRIPT.main(["--consistencia", "--propuesta", str(proposal), "--varios-toques",
                        "--lienzo", str(tmp_path / "lienzo.json")]) == 0
    assert "Resultado: verde" in capsys.readouterr().out


def test_what_the_command_prints_has_only_the_shapes_of_the_capture(capsys: pytest.CaptureFixture[str],
                                                                    tmp_path: Path) -> None:
    proposal = write_proposal(tmp_path, items_of("tres_toques_vence_4d"))
    assert SCRIPT.main(["--consistencia", "--propuesta", str(proposal), "--varios-toques"]) == 0
    out = capsys.readouterr().out
    assert "@" not in out
    # Fuera de las formas fijas (uuid, hex, sha256, hora y fecha), ninguna corrida de nueve cifras.
    leftover = [line for line in (PRIVACY._FIXED.sub("", line) for line in out.splitlines())
                if re.search(r"[0-9]{9,}", line)]
    assert leftover == []
