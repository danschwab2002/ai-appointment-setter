"""El Automation Expert crea un flujo (el PR 4 del hito 4): el item nuevo del ciclo 1.7.0.

Contratos: docs/contracts/flow-change-cycle-v1.md («Crear un flujo: el item
nuevo y la cohorte (1.7.0)») y docs/contracts/flow-engine-v2.md (secciones 6, 7
y 14). Diseno: «el hito 4» (2026-10-24, en el vault), pieza C.

**La conversacion de prueba** («crea la bienvenida despues de la compra») va
contra el doble de siempre: la app real con el cliente real del almacen
(``FlowChangeStore``) y PostgREST emulado (``PostgrestEmulator`` de
test_flow_change_api_app.py, con los errores capturados el 2026-10-21). Es lo
que el agente hace con sus herramientas: leer el almacen, proponer el item
nuevo con lo minimo y validar. Lo que guarda y lo que valida (los items
normalizados, el validador, el resultado y la vista previa) va a
``tests/fixtures/flow_change_new_flow/ae_bienvenida_20261024.json``, y PGlite
lo publica con las funciones reales de la migracion 20261024000600
(``validate_flow_change_new_flow.mjs``): ahi termina en un flujo nativo en
cohorte. Con NEW_FLOW_FIXTURE_DIR se escribe; sin la variable, el del repo
tiene que ser el de hoy.

Fixtures capturados: el catalogo de la bandeja 11 de ATT1 leido de Chatwoot el
2026-10-21 (``flow_change_operator/chatwoot_db_message_templates_inbox_11_20261021.json``),
de donde sale la plantilla de utilidad ``att1_pago_efectivo_confirmado_01``
(nombre y producto, sin boton), y las definiciones de ATT1 del 2026-10-16.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest
from test_flow_change_api_app import AUTH, PREFIX, SLACK, PostgrestEmulator, _client

from bridge.flow_change_simulation import build_new_flow_simulation, build_simulation
from bridge.flow_definition import ProposalItem, parse_items, touch_times
from bridge.flow_engine import native_not_supported

FIXTURES = Path(__file__).parent / "fixtures"
BRIDGE = FIXTURES / "flow_change_new_flow" / "ae_bienvenida_20261024.json"
CONFIRMED = "att1_pago_efectivo_confirmado_01"
CLIENT_OK = {"quien": "juan", "medio": "whatsapp", "fecha": "2026-10-15"}
# Lo que contesta la lectura de la simulacion en el doble: dos compras de dos personas en la semana.
COUNTS = {"contract": "flow-engine-v2", "since": "2026-10-17T18:00:00Z", "until": "2026-10-24T18:00:00Z",
          "events": 2, "people": 2}
PURPOSE = "una bienvenida despues de la compra, con la etiqueta de clienta"


def _welcome(**changes: object) -> dict:
    """Lo que escribe el agente: que evento, que mensaje y cuando; el resto son defaults del esquema."""
    definition: dict = {
        "nombre": "Bienvenida despues de la compra", "dispara": {"evento": "compra"},
        "entra": {"permiso": "compra"},
        "pasos": [{"plantilla": CONFIRMED}, {"etiquetar": "cliente"}],
        "horario": "siempre",
    }
    definition.update(changes)
    return definition


def _new_item(definition: dict | None = None, *, flujo: str = "bienvenida", prender: str = "cohorte") -> dict:
    return {"producto": "att1", "flujo": flujo, "tipo": "nuevo", "definicion": definition or _welcome(),
            "prender": prender, "ok_del_cliente": CLIENT_OK}


def _propose(client, items: list[dict], base: dict | None = None):
    return client.post(f"{PREFIX}/flow-changes", headers=AUTH, json={
        "items": items, "base": base if base is not None else {}, "purpose": PURPOSE, "requested_by": SLACK})


def _validate(client, proposal_id: str, revision: int = 1):
    return client.post(f"{PREFIX}/flow-changes/{proposal_id}/validate", headers=AUTH,
                       json={"revision": revision, "requested_by": SLACK})


@pytest.fixture
def image(monkeypatch: pytest.MonkeyPatch) -> None:
    # La identidad del validador sale de la imagen (el Dockerfile pone SETTER_VERSION y GIT_SHA).
    monkeypatch.setenv("SETTER_VERSION", "1.17.1")
    monkeypatch.setenv("GIT_SHA", "fc6239300000000000000000000000000000abcd")


# ----------------------------------------------------------------- la conversacion de prueba


def test_create_the_welcome_after_the_purchase_ends_validated_for_the_cohort(image: None) -> None:
    emulator = PostgrestEmulator()
    emulator.new_flow_counts = dict(COUNTS)
    client, _ = _client(emulator)

    # 1. El agente lee el almacen: la bienvenida no existe, y los flujos de hoy son del legado.
    store = client.get(f"{PREFIX}/flow-store", headers=AUTH).json()["data"]
    assert [flow["flow_key"] for flow in store["flows"]] == ["carrito", "pago_fallido", "precheckout"]
    assert all(flow["native"] is False for flow in store["flows"])

    # 2. Propone el flujo nuevo con lo minimo; los defaults del esquema completan el resto.
    saved = _propose(client, [_new_item()])
    assert saved.status_code == 201, saved.text
    data = saved.json()["data"]
    assert data["status"] == "borrador" and data["revision"] == 1
    assert data["toques"] == [{"flujo": "bienvenida", "antes": [], "despues": [
        {"paso": 1, "plantilla": CONFIRMED, "horas_desde_el_primero": 0.0}]}]
    proposal = emulator.proposals[data["proposal_id"]]
    [item] = proposal["items"]
    assert "flow_id" not in item and item["prender"] == "cohorte" and proposal["draft_base"] == {}
    definition = item["definicion"]
    # Lo que llena el esquema y el agente dice: la clave, el producto, las variables, la salida, el
    # plazo, la bolsa, los topes y la repeticion de la compra.
    assert definition["flujo"] == "bienvenida" and definition["producto"] == "att1"
    assert definition["pasos"][0] == {"plantilla": CONFIRMED, "idioma": "es_MX", "variables": ["nombre", "producto"]}
    assert definition["termina"] == {"si": [], "vence": "1d"}
    assert definition["topes"] == {"bolsa": "att1-bienvenida", "por_dia": "sin_tope", "total": "sin_tope",
                                   "repeticion": {"una_por": "persona", "otra_despues_de": "nunca"}}
    assert native_not_supported(definition) == ()

    # 3. Valida: sin bloqueos, para la cohorte, con la simulacion de su disparador.
    validated = _validate(client, data["proposal_id"])
    assert validated.status_code == 200, validated.text
    out = validated.json()["data"]
    assert out["status"] == "validada" and out["blocks"] == []
    preview = out["preview"]
    [change] = preview["cambios"]
    assert (change["tipo"], change["nace_en"], change["base_version"]) == ("nuevo", "cohorte", None)
    assert preview["a_quien"] == "cohorte"
    assert preview["volver_atras"] == [{"producto": "att1", "flujo": "bienvenida", "a_version": None,
                                        "como": "pausar_o_apagar"}]
    simulation = preview["simulacion"]
    assert simulation["status"] == "built"
    assert {key: simulation["summary"][key] for key in ("disparador", "disparos", "personas", "nace_en")} == {
        "disparador": "compra", "disparos": 2, "personas": 2, "nace_en": "cohorte"}
    assert "simulation_not_built" not in [gap["code"] for gap in preview["huecos"]]
    counts = [body for function, body, _ in emulator.requests
              if function == "read_flow_engine_native_trigger_counts_v1"]
    assert [(body["p_product_ref"], body["p_trigger"]) for body in counts] == [("att1", "compra")]

    # El puente con la base: lo que guardo y valido el bridge, que PGlite publica.
    stored = emulator.proposals[data["proposal_id"]]["validation"]
    document = {
        "_generated": {
            "generator": "tests/test_flow_change_new_flow.py "
                         "(test_create_the_welcome_after_the_purchase_ends_validated_for_the_cohort)",
            "from": "la conversacion de prueba contra el doble: el item nuevo normalizado, el validador, el "
                    "resultado y la vista previa que registro la ruta de validar del agente",
            "regenerate": "NEW_FLOW_FIXTURE_DIR=tests/fixtures/flow_change_new_flow "
                          "uv run pytest tests/test_flow_change_new_flow.py",
        },
        "purpose": PURPOSE,
        "items": proposal["items"],
        "validator": stored["validator"],
        "result": stored["result"],
        "preview": stored["preview"],
    }
    target = os.environ.get("NEW_FLOW_FIXTURE_DIR")
    if target:
        Path(target).mkdir(parents=True, exist_ok=True)
        (Path(target) / BRIDGE.name).write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                                                encoding="utf-8")
    assert json.loads(BRIDGE.read_text(encoding="utf-8")) == document, (
        "ae_bienvenida_20261024.json no es el de hoy: regenerarlo con NEW_FLOW_FIXTURE_DIR")


def test_the_template_to_create_saves_the_draft_but_does_not_validate_to_publish(image: None) -> None:
    emulator = PostgrestEmulator()
    emulator.new_flow_counts = dict(COUNTS)
    client, _ = _client(emulator)
    item = _new_item(_welcome(pasos=[{"plantilla": {"a_crear": "att1_bienvenida_01"}, "idioma": "es_MX",
                                      "variables": ["nombre", "producto"]}, {"etiquetar": "cliente"}]))
    saved = _propose(client, [item])
    assert saved.status_code == 201, saved.text
    validated = _validate(client, saved.json()["data"]["proposal_id"])
    assert validated.status_code == 200, validated.text
    out = validated.json()["data"]
    assert out["status"] == "borrador"
    assert ("unresolved_value", "/0/definicion/pasos/0/plantilla") in [
        (block["code"], block["path"]) for block in out["blocks"]]


def test_publishing_to_everyone_is_another_proposal(image: None) -> None:
    emulator = PostgrestEmulator()
    client, _ = _client(emulator)
    # Un flujo nuevo no se publica prendido para todos: nace en cohorte.
    refused = _propose(client, [_new_item(prender="prendido")])
    assert refused.status_code == 422
    assert [(block["code"], block["path"]) for block in refused.json()["data"]["blocks"]] == [
        ("new_flow_must_start_in_cohort", "/0/prender")]
    # Ya publicado en cohorte, pasarlo a todos es un item prender, con su propuesta y su sí.
    definition = copy.deepcopy(_published_welcome())
    flow_id = "0000000b-0000-4000-8000-0000000a7701"
    emulator.flows.append({"flow_id": flow_id, "product_ref": "att1", "flow_key": "bienvenida", "state": "cohorte",
                           "active_version": 1, "generation": 2, "changed_at": "2026-10-24T18:00:00Z",
                           "changed_by_kind": "operador", "native": True, "versions": {"1": definition}})
    store = client.get(f"{PREFIX}/flow-store", headers=AUTH).json()["data"]
    welcome = next(flow for flow in store["flows"] if flow["flow_key"] == "bienvenida")
    assert (welcome["state"], welcome["native"]) == ("cohorte", True)
    item = {"producto": "att1", "flujo": "bienvenida", "flow_id": flow_id, "tipo": "prender"}
    saved = _propose(client, [item], {flow_id: {"active_version": 1}})
    assert saved.status_code == 201, saved.text
    validated = _validate(client, saved.json()["data"]["proposal_id"])
    out = validated.json()["data"]
    assert out["status"] == "validada", out["blocks"]
    assert out["preview"]["cambios"][0]["estado"] == {"antes": "cohorte", "despues": "prendido"}


def _published_welcome() -> dict:
    [item] = parse_items([_new_item()])
    emulator = PostgrestEmulator()
    emulator.new_flow_counts = dict(COUNTS)
    client, _ = _client(emulator)
    saved = _propose(client, [_new_item()]).json()["data"]
    definition = copy.deepcopy(emulator.proposals[saved["proposal_id"]]["items"][0]["definicion"])
    definition.update(version=1, estado="publicada")
    assert item.flujo == definition["flujo"]
    return definition


def test_the_agent_cannot_create_a_flow_on_the_legacy(image: None) -> None:
    emulator = PostgrestEmulator()
    emulator.new_flow_counts = dict(COUNTS)
    client, _ = _client(emulator)
    # Un flujo nuevo del pago rechazado no es nativo: el motor nativo de esta version no lo ejecuta (la
    # compra y, desde E-161, el carrito si).
    cart_like = _new_item(_welcome(dispara={"evento": "pago_fallido"}, entra={"permiso": "formulario"},
                                   pasos=[{"plantilla": CONFIRMED}]), flujo="pago_nuevo")
    saved = _propose(client, [cart_like])
    assert saved.status_code == 201, saved.text
    validated = _validate(client, saved.json()["data"]["proposal_id"]).json()["data"]
    assert validated["status"] == "borrador"
    assert "motor_nativo:disparador" in [block["code"] for block in validated["blocks"]]
    # Una clave que ya existe es de un flujo registrado: no se crea encima.
    taken = _propose(client, [_new_item(flujo="carrito")])
    assert taken.status_code == 422
    assert "flow_already_registered" in [block["code"] for block in taken.json()["data"]["blocks"]]
    # La primera version de un flujo del legado registrado sin version sigue siendo del operador.
    emulator.flows.append({"flow_id": "0000000c-0000-4000-8000-0000000a7701", "product_ref": "att1",
                           "flow_key": "ficha", "state": "apagado", "active_version": None, "generation": 0,
                           "changed_at": "2026-10-24T18:00:00Z", "changed_by_kind": "operador", "versions": {}})
    legacy = {"producto": "att1", "flujo": "ficha", "flow_id": "0000000c-0000-4000-8000-0000000a7701",
              "tipo": "version", "definicion": _welcome(), "en_curso": "seguir", "ok_del_cliente": CLIENT_OK}
    refused = _propose(client, [legacy], {"0000000c-0000-4000-8000-0000000a7701": {"active_version": None}})
    assert refused.status_code == 422
    assert "flow_creation_not_enabled" in [block["code"] for block in refused.json()["data"]["blocks"]]


def test_without_the_counts_reading_the_simulation_says_why(image: None) -> None:
    emulator = PostgrestEmulator()  # una base sin 20261024000600: la lectura no existe
    client, _ = _client(emulator)
    saved = _propose(client, [_new_item()]).json()["data"]
    out = _validate(client, saved["proposal_id"]).json()["data"]
    assert out["status"] == "validada"
    assert out["preview"]["simulacion"] == {"status": "failed", "reason_code": "flow_engine_unavailable"}


# ----------------------------------------------------------------- las piezas


def test_the_new_flow_simulation_counts_and_refuses_what_does_not_fit() -> None:
    [item] = parse_items([_new_item()])
    counts = {**COUNTS, "product_ref": "att1", "trigger": "compra"}
    built = build_new_flow_simulation(counts, [item])
    assert built["status"] == "built" and built["summary"]["mensajes_si_fuera_para_todos"] == 2
    assert build_new_flow_simulation({**counts, "people": 3}, [item]) == {
        "status": "failed", "reason_code": "simulation_new_flow_counts_invalid"}
    assert build_new_flow_simulation({**counts, "otra": 1}, [item])["reason_code"] == \
        "simulation_new_flow_counts_invalid"
    assert build_new_flow_simulation({**counts, "product_ref": "otro"}, [item])["status"] == "failed"
    # La simulacion del motor no simula un flujo nuevo: no tiene historia.
    assert build_simulation({}, {}, [item]) == {"status": "failed", "reason_code": "simulation_new_flow_not_built"}


def test_a_new_item_has_no_flow_id_and_only_one_per_flow() -> None:
    [item] = parse_items([_new_item()])
    assert isinstance(item, ProposalItem) and item.flow_id is None and item.is_content
    assert item.to_json() == _new_item()
    with pytest.raises(ValueError, match="keys_invalid"):
        parse_items([{**_new_item(), "flow_id": "0000000b-0000-4000-8000-0000000a7701"}])
    with pytest.raises(ValueError, match="item_class_repeated"):
        parse_items([_new_item(), _new_item()])


def test_etiquetar_does_not_stop_the_touch_times() -> None:
    assert touch_times({"pasos": [{"esperar": "1h"}, {"plantilla": CONFIRMED}, {"etiquetar": "cliente"}]}) == [
        {"paso": 2, "plantilla": CONFIRMED, "horas_desde_el_primero": 0.0}]


# ----------------------------------------------------------------- el comando del operador

from test_flow_change_operator_script import FINGERPRINT, FLOWS, PROPOSAL, FakeDocker, FakeTerminal  # noqa: E402
from test_flow_change_operator_script import run as run_script  # noqa: E402
from test_flow_change_operator_script import script  # noqa: E402

TEST_PHONE = "+1 202 555 0142"  # rango de ficcion
WELCOME_ID = "0000000b-0000-4000-8000-0000000a7701"


class CohortTerminal(FakeTerminal):
    def __init__(self, phone: str | None = TEST_PHONE) -> None:
        super().__init__()
        self.phone = phone
        self.secrets = 0

    def secret(self, prompt: str) -> str:
        self.secrets += 1
        if self.phone is None:
            raise script.Refused("sin terminal no se carga un telefono: este comando lo corre una persona")
        return self.phone


def _native_docker() -> FakeDocker:
    docker = FakeDocker()
    docker.flows = [*FLOWS, {"flow_id": WELCOME_ID, "product_ref": "att1", "flow_key": "bienvenida",
                             "state": "cohorte", "active_version": 1, "generation": 2, "native": True,
                             "changed_at": "2026-10-24T18:00:00Z", "changed_by_kind": "slack", "versions": {}}]
    return docker


def test_cohorte_agregar_reads_the_phone_without_echo_and_never_prints_it() -> None:
    docker = _native_docker()
    terminal = CohortTerminal()
    code, out = run_script(["cohorte", "att1/bienvenida", "agregar", "--quien", "dan", "--motivo",
                            "el telefono de prueba de Dan"], docker, terminal)
    assert code == script.EXIT_OK, out
    assert terminal.secrets == 1 and terminal.asked == 1
    [write] = docker.writes()
    assert "operator_flow_cohort_v1" in write
    # El telefono va en el bloque COPY de _args, nunca en el texto del SQL ni en la salida.
    sql_text = write.split("\\.\n", 1)[1]
    assert TEST_PHONE not in sql_text and "phone\t" + TEST_PHONE in write
    assert TEST_PHONE not in out and "2025550142" not in out


def test_cohorte_refuses_a_legacy_flow_and_a_phone_without_shape() -> None:
    docker = FakeDocker()
    code, out = run_script(["cohorte", "att1/carrito", "agregar", "--quien", "dan", "--motivo", "x"], docker,
                           CohortTerminal())
    assert code == script.EXIT_REFUSED and "no es un flujo nativo" in out and docker.writes() == []
    code, out = run_script(["cohorte", "att1/bienvenida", "agregar", "--quien", "dan", "--motivo", "x"],
                           _native_docker(), CohortTerminal("hola"))
    assert code == script.EXIT_USAGE and "hola" not in out
    code, out = run_script(["cohorte", "att1/bienvenida", "otra", "--quien", "dan", "--motivo", "x"],
                           _native_docker(), CohortTerminal())
    assert code == script.EXIT_USAGE


def test_publishing_passes_the_zone_of_the_instance() -> None:
    proposal = {"id": PROPOSAL, "status": "aprobada", "effective_status": "aprobada", "revision": 1,
                "items": [_new_item()], "draft_base": {}, "purpose": PURPOSE,
                "requested_by_kind": "automation_expert", "fingerprint": FINGERPRINT, "validated_at": None,
                "expires_at": "2026-10-25T15:00:00Z", "created_at": "2026-10-24T15:00:00Z",
                "approved_by_ref": "dan", "validation": None}
    docker = FakeDocker(prop=proposal)
    code, out = run_script(["publicar", PROPOSAL, "--quien", "dan", "--sin-simulacion"], docker)
    assert code == script.EXIT_OK, out
    assert "nuevo (un flujo nativo; al publicarlo queda en cohorte" in out
    publish = next(write for write in docker.writes() if "operator_publish_flow_change_v1" in write)
    assert "'time_zone', (select v from pg_temp._args where k = 'zone')" in publish
    assert "zone\tAmerica/Mexico_City" in publish


def test_validating_a_new_flow_reads_the_counts_of_its_trigger() -> None:
    docker = FakeDocker()
    ops = script.Ops(script.Config.from_env({**_ENV}), docker=docker, terminal=FakeTerminal(),
                     out=__import__("io").StringIO(), code=lambda: "abc123")
    inputs = ops.read_engine_inputs([_new_item()])
    assert "new_flow_counts" in inputs
    [counts_sql] = [stdin for argv, stdin in docker.calls if "read_flow_engine_native_trigger_counts_v1" in stdin]
    assert "trigger\tcompra" in counts_sql and "product\tatt1" in counts_sql
    assert ops.read_engine_inputs([_new_item(), _new_item(flujo="otra")]) == {
        "simulation_unavailable": "simulation_new_flow_mixed"}


_ENV = {"CONTENEDOR_DB": "att1-db", "CONTENEDOR_CHATWOOT_DB": "chatwoot-db", "BASE_CHATWOOT_DB": "infra",
        "BANDEJA_CHATWOOT": "11", "IMAGEN_BRIDGE": "setter-bridge@sha256:" + "d" * 64, "TENANT": "lancemos",
        "ALIADA": "att1", "ZONA": "America/Mexico_City", "APROBADORES": "dan"}


def test_the_soul_of_the_agent_teaches_creating_a_flow_with_the_minimum() -> None:
    soul = (Path(__file__).resolve().parents[1] / "profiles/automation-expert/SOUL.md").read_text(encoding="utf-8")
    section = soul.split("**Creás un flujo nuevo cuando te lo piden**", 1)[1].split("- **No aprobás", 1)[0]
    for phrase in ("qué evento lo dispara", "a quién le llega", "qué mensaje", "cuándo sale",
                   "cuándo deja de mandar", '"nuevo"', 'prender: "cohorte"', "Decí qué completó",
                   "{a_crear: <nombre>}", "cohorte de prueba", "otra propuesta"):
        assert phrase in section, phrase
