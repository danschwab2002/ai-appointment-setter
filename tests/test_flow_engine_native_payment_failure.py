"""El motor nativo arranca con el pago rechazado (E-207, M1b, flow-engine-v2 2.11.0).

El paso P18 del mapa de M16: el pago rechazado de Maribel como flujo nativo, con
``permiso: formulario`` (la intencion con la que la admision portable
correlaciono el rechazo, como el ``consented_intent`` de su scope propio de hoy)
y los valores de hoy. Migracion ``20261030000200_flow_engine_native_payment_failure_v1.sql``.

Datos:

* Las filas de la admision y el error del planificador del legado salen de la
  base real (PGlite): ``tests/fixtures/flow_engine/native_payment_failure_rows.json``,
  que escribe ``validate_flow_engine_native_payment_failure.mjs --emit`` con el
  formulario y los rechazos por las admisiones portables de hoy.
* El rechazo es el capturado de Maribel
  (``tests/fixtures/hotmart_purchase_canceled_maribel_20261028.json``, anonimizado
  en el servidor), con el producto y la oferta de ATT1 para el runtime de prueba.
* La plantilla es la reconstruida de Maribel (``maribel_retomar_proceso_01``, la
  de ``WABA_PAYMENT_FAILURE_TEMPLATE_NAME`` en Maribel).

Lo que fijan:

* la regla 14 nativa deja pasar el pago rechazado de Maribel (espera de 15 min,
  09:00-21:00 CDMX, 20 por dia y 1.000, una por persona y 72 h, la salida
  compra) y el ciclo lo valida; el carrito nativo puede declarar
  ``no_arranca_mientras: [pago_rechazado]`` (D6);
* el resolver manda el rechazo primero a la admision nativa solo si el worker le
  pasa un producto con un flujo nativo del pago rechazado; si el nativo lo toma,
  el legado no planifica; si no lo toma (sin flujo, fuera de la cohorte,
  pausado, un caso del legado en curso, un pendiente que no es un rechazo), la
  llamada del legado es byte a byte la de hoy;
* el worker pasa el producto solo con un flujo nativo del pago rechazado
  prendido o en cohorte: sin el, el pago rechazado de ATT1 (con sus variantes)
  y el de Maribel van exactamente como hoy.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import test_flow_engine_native_template_shape as shape
from test_flow_engine_native_cart import CART, _answer, _row, _worker_with
from test_flow_engine_native_quick_replies import NAME
from test_flow_engine_native_quick_replies import maribel_catalog  # noqa: F401 - el fixture del validador
from test_flow_engine_resolution import NATIVE_ROWS, _Base, _runtime
from test_resolution_pilot_plan_rejection import BOUNDARY, CONFIG, CONTACT_ID, EVENT_ID

from bridge.flow_engine import ENGINE_CAPABILITIES_NATIVE, NATIVE_PERMISSION_BY_TRIGGER, native_not_supported
from bridge.hotmart import EVENT_PURCHASE_CANCELED
from bridge.native_flows import (
    NATIVE_CART_UNCLAIMED_REASONS,
    NATIVE_PAYMENT_FAILURE_TRIGGER,
    payment_failure_product_ref,
)
from bridge.resolution import ResolutionError, resolve_event

FIXTURES = Path(__file__).parent / "fixtures"
ROWS = json.loads((FIXTURES / "flow_engine" / "native_payment_failure_rows.json").read_text(encoding="utf-8"))
REFUSALS = json.loads((FIXTURES / "hotmart_purchase_canceled_maribel_20261028.json").read_text(encoding="utf-8"))
POLICY_KEY = "maribel-pago-rechazado-un-toque"

# El pago rechazado de Maribel con los valores de hoy (maribel_db, 2026-10-30): la politica
# maribel-pago-rechazado-un-toque v1 (gracia 15 min, 09:00-21:00 los siete dias en CDMX, vence a 1 dia)
# y el scope maribel-pago-rechazado v1 (20 por dia, 1.000 en total). La repeticion es la del legado:
# una por persona y producto, y un rechazo dentro de las 72 h de un caso que ya mando no manda otro.
PAYMENT = {
    "esquema": "setter-flujo/v1", "flujo": "pago_rechazado", "producto": "att1", "estado": "borrador",
    "nombre": "Pago rechazado", "dispara": {"evento": "pago_fallido"},
    "entra": {"permiso": "formulario", "contesto_a": []},
    "pasos": [{"esperar": "15m"}, {"plantilla": NAME, "idioma": "es_MX", "variables": ["nombre", "producto"]}],
    "termina": {"si": ["compra"], "vence": "1d"},
    "horario": {"zona": "America/Mexico_City", "desde": "09:00", "hasta": "21:00"},
    "topes": {"bolsa": "maribel-pago-rechazado-nativo", "por_dia": 20, "total": 1000,
              "repeticion": {"una_por": "persona", "otra_despues_de": "72h"}},
    "prioridad": {"le_gana_a": [], "no_arranca_mientras": []},
}


def _refusal(index: int = 0) -> dict[str, Any]:
    """Un rechazo capturado de Maribel, con el producto y la oferta del runtime de prueba."""
    payload = copy.deepcopy(REFUSALS["eventos"][index]["payload"])
    payload["data"]["product"] = {**payload["data"]["product"], "id": CONFIG.hotmart_product_id,
                                  "name": CONFIG.product_name}
    payload["data"]["purchase"]["offer"] = {"code": CONFIG.offer_code}
    return payload


def test_the_fixtures_are_the_real_chain_and_the_maribel_refusals() -> None:
    assert ROWS["_capture"]["encargo"] == "E-207"
    assert ROWS["admission"] == {"outcome": "motor_arranco", "reason": None, "created": True}
    assert [event["payload"]["event"] for event in REFUSALS["eventos"]] == ["PURCHASE_CANCELED"] * 3
    # Dos rechazos de la misma persona con 24 segundos de diferencia: el legado mando un solo mensaje.
    assert REFUSALS["eventos"][1]["persona"] == REFUSALS["eventos"][2]["persona"]
    assert ROWS["second_refusal"]["reason"] == "ya_en_curso"


# -- La regla 14 nativa ----------------------------------------------------------


def test_the_native_engine_runs_the_maribel_payment_failure() -> None:
    assert native_not_supported(PAYMENT) == ()
    assert "pago_fallido" in ENGINE_CAPABILITIES_NATIVE.triggers
    assert NATIVE_PERMISSION_BY_TRIGGER["pago_fallido"] == "formulario" == NATIVE_PERMISSION_BY_TRIGGER["carrito"]
    assert NATIVE_PAYMENT_FAILURE_TRIGGER == "pago_fallido"


@pytest.mark.parametrize(("change", "code"), [
    ({"entra": {"permiso": "compra"}}, "motor_nativo:permiso"),
    ({"pasos": [{"plantilla": NAME}, {"esperar": "1h"}, {"plantilla": NAME}]}, "engine_not_supported:demasiados_envios"),
    ({"dispara": {"evento": "ficha_impresa"}}, "motor_nativo:disparador"),
    ({"termina": {"si": ["compra", "contesta"], "vence": "1d"}}, "engine_not_supported:salida_contesta"),
])
def test_what_the_native_engine_does_not_run_with_the_payment_failure(change: dict, code: str) -> None:
    assert code in native_not_supported({**copy.deepcopy(PAYMENT), **change})


def test_the_native_cart_can_wait_for_the_native_payment_failure() -> None:
    # D6 del mapa: el carrito nativo no arranca mientras un pago rechazado nativo este abierto.
    cart = {**copy.deepcopy(CART), "prioridad": {"le_gana_a": [], "no_arranca_mientras": ["pago_rechazado"]}}
    assert native_not_supported(cart) == ()
    assert ROWS["cart_blocked"]["reason"] == "no_arranca_mientras:pago_rechazado"


def test_the_cycle_validates_the_maribel_payment_failure(maribel_catalog: None,  # noqa: F811
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    def item(pasos: list) -> dict:
        definition = {key: copy.deepcopy(PAYMENT[key]) for key in ("nombre", "dispara", "entra", "termina", "horario")}
        return {"producto": "att1", "flujo": "pago_rechazado", "tipo": "nuevo", "prender": "cohorte",
                "definicion": {**definition, "pasos": pasos}}

    monkeypatch.setattr(shape, "_item", item)
    _, result = shape._propose_and_validate(copy.deepcopy(PAYMENT["pasos"]))

    assert result["blocks"] == [], result["blocks"]
    assert result["status"] == "validada"


# -- El resolver: primero la admision nativa --------------------------------------


class _NativeBase(_Base):
    """La base del resolver, con la admision nativa del pago rechazado."""

    def __init__(self, native: list[httpx.Response], *, planner: httpx.Response | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.native = list(native)
        self.planner = planner

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/rest/v1/rpc/admit_flow_engine_event_v1":
            self.calls.append(("admit_flow_engine_event_v1", json.loads(request.content)))
            return self.native.pop(0)
        if (self.planner is not None and request.method == "POST"
                and request.url.path == "/rest/v1/rpc/plan_portable_payment_failure_recovery"):
            self.calls.append(("plan_portable_payment_failure_recovery", json.loads(request.content)))
            return self.planner
        return super().handler(request)


def _resolve(base: _Base, *, native: bool) -> Any:
    client = base.client()
    runtime = _runtime(base, client)
    return asyncio.run(resolve_event(
        webhook_event_id=EVENT_ID, payload=_refusal(), supabase=client, policy_key=POLICY_KEY,
        policy_version=1, allowed_jid=None, chatwoot_account_id=CONFIG.chatwoot_account_id,
        chatwoot_inbox_id=CONFIG.chatwoot_inbox_id, pilot_boundary=BOUNDARY, commercial_ally_config=CONFIG,
        event_type=EVENT_PURCHASE_CANCELED, flow_engine=runtime if native else None,
        native_payment_failure_product_ref=CONFIG.funnel_ref if native else None,
    ))


def test_a_started_native_payment_failure_owns_the_event_and_the_legacy_does_not_plan() -> None:
    base = _NativeBase([_answer(_row("motor_arranco"))])
    report = _resolve(base, native=True)

    names = base.names()
    assert "admit_flow_engine_event_v1" in names
    assert not any(name.startswith("plan_") for name in names)
    assert report.contact_id == CONTACT_ID
    assert [patch["processing_status"] for patch in base.status_patches] == ["processed"]
    event = base.body_of("admit_flow_engine_event_v1")["p_flow_event"]
    assert (event["tipo"], event["fuente"], event["producto_ref"], event["oferta"], event["contacto_id"]) == (
        "pago_fallido", "hotmart", CONFIG.funnel_ref, CONFIG.offer_code, CONTACT_ID)
    # El momento es la creation_date del rechazo: el mismo que exige el planificador del legado.
    today = _Base()
    _resolve(today, native=False)
    assert event["momento"] == today.body_of("plan_portable_payment_failure_recovery")["p_failed_at"]
    assert event["destino"] == today.body_of("plan_portable_payment_failure_recovery")["p_external_user_id"]


@pytest.mark.parametrize("answer", [
    _answer({**_row("sin_flujo"), "flow_id": None}),
    _answer(_row("motor_no_arranca", "fuera_de_cohorte")),
    _answer(_row("motor_no_arranca", "flujo_pausado")),
    # La base real: el caso del pago rechazado del legado en curso (PGlite).
    _answer(_row(ROWS["over_legacy"]["outcome"], ROWS["over_legacy"]["reason"])),
    _answer(_row(ROWS["not_a_refusal"]["outcome"], ROWS["not_a_refusal"]["reason"])),
    httpx.Response(400, json={"code": "22023", "message": "invalid_flow_engine_event"}),
], ids=["sin flujo", "fuera de la cohorte", "pausado", "caso del legado", "no es un rechazo", "rechazo de la forma"])
def test_what_no_native_flow_takes_goes_to_the_legacy_exactly_as_today(answer: httpx.Response) -> None:
    today = _Base()
    _resolve(today, native=False)
    base = _NativeBase([answer])
    _resolve(base, native=True)

    assert base.body_of("plan_portable_payment_failure_recovery") == today.body_of(
        "plan_portable_payment_failure_recovery")
    assert [name for name in base.names() if name != "admit_flow_engine_event_v1"] == [
        name for name in today.names()]
    assert base.status_patches == today.status_patches


@pytest.mark.parametrize("fixture", ["second_refusal", "after_sent"])
def test_a_second_refusal_is_added_to_the_native_run_and_the_event_ends_processed(fixture: str) -> None:
    # La base lo sumo al caso (PGlite): en curso, corrio el envio a la gracia de este rechazo; dentro de las
    # 72 h de un caso que ya mando, sin mensaje. Como el legado: un solo mensaje y el evento processed.
    second = ROWS[fixture]
    base = _NativeBase([_answer(_row(second["outcome"], second["reason"]))])
    with pytest.raises(ResolutionError, match="^flow_engine_payment_failure_absorbed$"):
        _resolve(base, native=True)
    assert not any(name.startswith("plan_") for name in base.names())
    assert [patch["processing_status"] for patch in base.status_patches] == ["processed"]
    assert "processing_error" not in base.status_patches[0]


@pytest.mark.parametrize("reason", [ROWS["no_permission"]["reason"], "baja"])
def test_a_person_the_native_flow_takes_and_does_not_start_ends_without_the_legacy(reason: str) -> None:
    base = _NativeBase([_answer(_row("motor_no_arranca", reason))])
    with pytest.raises(ResolutionError, match=f"^flow_engine_no_start:{reason}$"):
        _resolve(base, native=True)
    assert not any(name.startswith("plan_") for name in base.names())
    assert base.status_patches == [
        {"processing_status": "failed", "processed_at": None, "processing_error": f"flow_engine_no_start:{reason}"}
    ]


def test_the_legacy_over_an_open_native_case_ends_failed_with_its_reason() -> None:
    error = ROWS["legacy_over_native"]
    base = _NativeBase([_answer(_row("motor_no_arranca", "flujo_pausado"))], planner=httpx.Response(
        400, json={"code": error["code"], "message": error["message"], "details": error["details"], "hint": None}))
    with pytest.raises(ResolutionError, match="^pilot_scope_rejected:flujo_nativo_en_curso$"):
        _resolve(base, native=True)
    assert [patch["processing_error"] for patch in base.status_patches] == ["pilot_scope_rejected:flujo_nativo_en_curso"]


def test_the_unclaimed_reasons_are_the_ones_the_base_gives() -> None:
    assert {"caso_del_legado_en_curso", "no_es_un_rechazo", "fuera_de_cohorte", "flujo_pausado"} <= NATIVE_CART_UNCLAIMED_REASONS
    assert ROWS["not_a_refusal"]["reason"] in NATIVE_CART_UNCLAIMED_REASONS
    assert ROWS["second_refusal"]["reason"] not in NATIVE_CART_UNCLAIMED_REASONS
    assert ROWS["no_permission"]["reason"] not in NATIVE_CART_UNCLAIMED_REASONS


# -- El worker: el producto, solo con un flujo nativo del pago rechazado -----------


def _flows(trigger: str, state: str = "cohorte") -> dict[str, Any]:
    flows = copy.deepcopy(NATIVE_ROWS["native_flows_cohort"])
    flows["flows"][0].update(trigger=trigger, state=state, product_ref=CONFIG.funnel_ref)
    return flows


def test_the_worker_passes_the_product_only_with_a_native_payment_failure_flow() -> None:
    refusal = _refusal()
    assert _worker_with(_flows("pago_fallido"))._native_payment_failure_product(refusal) == CONFIG.funnel_ref
    assert _worker_with(_flows("pago_fallido", "prendido"))._native_payment_failure_product(refusal) == CONFIG.funnel_ref
    # El carrito o la bienvenida nativos no cambian nada del pago rechazado; pausado no cuenta.
    assert _worker_with(_flows("carrito"))._native_payment_failure_product(refusal) is None
    assert _worker_with(_flows("compra"))._native_payment_failure_product(refusal) is None
    assert _worker_with(_flows("pago_fallido", "pausado"))._native_payment_failure_product(refusal) is None
    assert _worker_with({"flows": []})._native_payment_failure_product(refusal) is None


def test_the_product_of_a_payment_failure_is_by_hotmart_id() -> None:
    refusal = _refusal()
    assert payment_failure_product_ref(refusal, commercial_ally_config=CONFIG, product_routes=None) == CONFIG.funnel_ref
    other = copy.deepcopy(refusal)
    other["data"]["product"]["id"] = CONFIG.hotmart_product_id + 1
    assert payment_failure_product_ref(other, commercial_ally_config=CONFIG, product_routes=None) is None


class _WorkerBase(_Base):
    """La base del worker: las lecturas del motor, la admision nativa y el planificador."""

    def __init__(self, flows: dict[str, Any], native: list[httpx.Response]) -> None:
        super().__init__()
        self.flows = flows
        self.native = list(native)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/rest/v1/rpc/read_flow_engine_native_flows_v1":
            self.calls.append(("read_flow_engine_native_flows_v1", None))
            return httpx.Response(200, json=self.flows, request=request)
        if request.method == "POST" and path == "/rest/v1/rpc/admit_flow_engine_event_v1":
            self.calls.append(("admit_flow_engine_event_v1", json.loads(request.content)))
            return self.native.pop(0)
        return super().handler(request)


def _process(base: _Base, *, engine: bool = True) -> None:
    from bridge.flow_engine_runtime import FlowEngineRuntime
    from bridge.worker import ResolutionWorker

    client = base.client()
    runtime = FlowEngineRuntime(supabase=client, instance={"tenant_ref": CONFIG.tenant_ref, "ally_ref": CONFIG.ally_ref})
    asyncio.run(runtime.native_flows.refresh_if_due())
    worker = ResolutionWorker(
        supabase=client, chatwoot_account_id=CONFIG.chatwoot_account_id, chatwoot_inbox_id=CONFIG.chatwoot_inbox_id,
        policy_key=POLICY_KEY, policy_version=1, pilot_boundary=BOUNDARY, commercial_ally_config=CONFIG,
        payment_failure_enabled=True, flow_engine=runtime if engine else None,
    )
    asyncio.run(worker._process_one({"id": EVENT_ID, "payload": _refusal(), "event_type": EVENT_PURCHASE_CANCELED}))


def test_the_worker_sends_the_refusal_to_the_native_admission_first() -> None:
    base = _WorkerBase(_flows("pago_fallido"), [_answer(_row("motor_arranco"))])
    _process(base)
    names = [name for name in base.names() if name != "read_flow_engine_native_flows_v1"]
    assert names[0] == "admit_flow_engine_event_v1"
    assert not any(name.startswith("plan_") for name in names)


def test_without_a_native_payment_failure_flow_the_worker_calls_exactly_as_today() -> None:
    # Sin un flujo nativo del pago rechazado (ATT1 con sus variantes, Maribel con su scope propio):
    # ninguna llamada nueva, el planificador del legado como hoy.
    today = _WorkerBase({"flows": []}, [])
    _process(today, engine=False)
    base = _WorkerBase(_flows("carrito"), [])
    _process(base)
    names = [name for name in base.names() if name != "read_flow_engine_native_flows_v1"]
    assert "admit_flow_engine_event_v1" not in names
    assert names == [name for name in today.names() if name != "read_flow_engine_native_flows_v1"]
    assert base.body_of("plan_portable_payment_failure_recovery") == today.body_of("plan_portable_payment_failure_recovery")
    assert base.status_patches == today.status_patches


# -- D1: un flujo nativo no usa la clave de un flujo del legado (E-211) ------------


def test_a_native_flow_with_a_legacy_key_is_blocked_even_without_the_legacy_flow(
        maribel_catalog: None) -> None:  # noqa: F811
    from bridge.automation_expert_read import FLOW_KEYS

    # Con D1, Maribel no registra los flujos del legado: la clave pago_fallido queda libre en el almacen.
    emulator = shape.PostgrestEmulator()
    emulator.new_flow_counts = dict(shape.COUNTS)
    emulator.flows = [flow for flow in emulator.flows if flow["flow_key"] not in FLOW_KEYS]
    client, _ = shape._client(emulator)

    def propose(flujo: str) -> httpx.Response:
        definition = {key: copy.deepcopy(PAYMENT[key]) for key in ("nombre", "dispara", "entra", "termina", "horario")}
        item = {"producto": "att1", "flujo": flujo, "tipo": "nuevo", "prender": "cohorte",
                "definicion": {**definition, "pasos": copy.deepcopy(PAYMENT["pasos"])}}
        return client.post(f"{shape.PREFIX}/flow-changes", headers=shape.AUTH, json={
            "items": [item], "base": {}, "purpose": "el pago rechazado nativo", "requested_by": shape.SLACK})

    reserved = propose("pago_fallido")
    assert reserved.status_code == 422, reserved.text
    codes = {block["code"] for block in reserved.json()["data"]["blocks"]}
    assert "flow_key_reserved_for_legacy" in codes and "flow_already_registered" not in codes
    assert propose("pago_rechazado").status_code == 201


def test_the_base_reserves_exactly_the_legacy_keys_of_the_reading() -> None:
    import re

    from bridge.automation_expert_read import FLOW_KEYS

    sql = (Path(__file__).resolve().parents[1] / "supabase" / "migrations"
           / "20261030000200_flow_engine_native_payment_failure_v1.sql").read_text(encoding="utf-8")
    match = re.search(r"p_item ->> ''flujo'' in \(([^)]*)\) then", sql)
    assert match is not None
    assert tuple(re.findall(r"''([a-z_]+)''", match.group(1))) == FLOW_KEYS
