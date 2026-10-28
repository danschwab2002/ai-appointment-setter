"""El motor nativo arranca con el carrito abandonado (E-161, flow-engine-v2 2.7.0).

El PR 2 del hito 4 de la version 1: el carrito de un toque de Maribel como flujo
nativo, con ``permiso: formulario`` y la plantilla MARKETING
``maribel_retomar_proceso_01``. Migracion ``20261028000200_flow_engine_native_cart_v1.sql``.

Datos:

* La fila del contexto del paso sale de la base real (PGlite):
  ``tests/fixtures/flow_engine/native_cart_execution_context_rows.json``, que
  escribe ``validate_flow_engine_native_cart.mjs --emit`` con el formulario y el
  carrito por las admisiones portables de hoy.
* La plantilla es la reconstruida de Maribel
  (``chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json``):
  falta capturarla del catalogo del inbox 13 (ver E-158).
* El carrito es el capturado de ``test_resolution_pilot_plan_rejection`` (el
  panel de Hotmart, 2026-09-21), con el producto y la oferta de ATT1.

Lo que fijan:

* la regla 14 nativa deja pasar el carrito de un toque con ``formulario`` y la
  salida ``compra``, y frena las formas cruzadas y los tres toques del legado;
  el validador del ciclo lo deja validado con la plantilla de Maribel;
* el envio toma la categoria del permiso de la version fijada: con
  ``formulario`` sale la plantilla MARKETING; sin el dato (una base sin la
  migracion) o con ``compra``, no sale;
* el resolver manda el carrito primero a la admision nativa solo si el worker
  le pasa un producto con un flujo nativo del carrito; si el nativo lo toma, el
  legado (y el motor del carrito) no planifica; si no lo toma, la llamada del
  legado es byte a byte la de hoy.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import test_flow_engine_native_template_shape as shape
from test_flow_engine_native_dispatch import _dispatcher, _NativeAuthority, _step, _templates
from test_flow_engine_native_quick_replies import AS_SUBMITTED, MARIBEL, NAME, NOMBRE, PRODUCTO, _MaribelChatwoot
from test_flow_engine_native_quick_replies import maribel_catalog  # noqa: F401 - el fixture del validador
from test_flow_engine_resolution import NATIVE_ROWS, PLAN_ROW, _Base, _resolve, _routes, _runtime
from test_resolution_pilot_plan_rejection import CONFIG, CONTACT_ID, EVENT_ID

from bridge.flow_engine import ENGINE_CAPABILITIES_NATIVE, NATIVE_PERMISSION_BY_TRIGGER, native_not_supported
from bridge.flow_engine_runtime import FORCE_LEGACY_AFTER_FAILURES
from bridge.native_flows import (
    NATIVE_CART_TRIGGER,
    NATIVE_PURCHASE_TEMPLATE_CATEGORIES,
    cart_product_ref,
    native_step_categories,
)
from bridge.resolution import FLOW_ENGINE_ADMISSION_PENDING, ResolutionError
from bridge.supabase import FlowEngineNativeStepTail, SupabaseClient, SupabaseError
from test_durable_dispatcher_approved_template import NOW

FIXTURES = Path(__file__).parent / "fixtures"
ROWS = json.loads((FIXTURES / "flow_engine" / "native_cart_execution_context_rows.json").read_text(encoding="utf-8"))
STEP_ROW = ROWS["step_context"]
FLOW_ID = "00000000-0000-4000-8000-0000000c4a71"
NATIVE_CASE = "00000000-0000-4000-8000-0000000c4a72"

# El carrito de un toque de Maribel, como lo arma el AE (lo demas lo completa el esquema).
CART = {
    "esquema": "setter-flujo/v1", "flujo": "carrito_nativo", "producto": "att1", "estado": "borrador",
    "nombre": "Carrito nativo", "dispara": {"evento": "carrito"}, "entra": {"permiso": "formulario", "contesto_a": []},
    "pasos": [{"plantilla": NAME, "idioma": "es_MX", "variables": ["nombre", "producto"]}],
    "termina": {"si": ["compra"], "vence": "1d"}, "horario": "siempre",
    "topes": {"bolsa": "att1-carrito-nativo", "por_dia": 150, "total": 3000,
              "repeticion": {"una_por": "persona_y_oferta", "otra_despues_de": "0m"}},
    "prioridad": {"le_gana_a": [], "no_arranca_mientras": []},
}


def test_the_fixture_is_the_real_chain_with_the_maribel_template() -> None:
    assert ROWS["_capture"]["encargo"] == "E-161"
    assert ROWS["flow_event"]["tipo"] == NATIVE_CART_TRIGGER == "carrito"
    assert ROWS["admission"]["outcome"] == "motor_arranco"
    assert (STEP_ROW["template_name"], STEP_ROW["template_language"], STEP_ROW["entry_permission"]) == (
        NAME, MARIBEL["language"], "formulario")
    assert STEP_ROW["template_variables"] == ["nombre", "producto"]
    assert MARIBEL["category"] == "MARKETING"


# -- La regla 14 nativa ----------------------------------------------------------


def test_the_native_engine_runs_the_one_touch_cart() -> None:
    assert native_not_supported(CART) == ()
    assert "carrito" in ENGINE_CAPABILITIES_NATIVE.triggers and ENGINE_CAPABILITIES_NATIVE.exits == {"compra"}
    assert dict(NATIVE_PERMISSION_BY_TRIGGER) == {"compra": "compra", "carrito": "formulario"}


@pytest.mark.parametrize(("change", "code"), [
    ({"entra": {"permiso": "compra"}}, "motor_nativo:permiso"),
    ({"dispara": {"evento": "compra"}}, "motor_nativo:permiso"),
    ({"dispara": {"evento": "pago_fallido"}}, "motor_nativo:disparador"),
    ({"dispara": {"evento": "carrito", "otra": 1}}, "motor_nativo:disparador"),
    ({"pasos": [{"plantilla": NAME}, {"esperar": "1h"}, {"plantilla": NAME}]}, "engine_not_supported:demasiados_envios"),
    ({"termina": {"si": ["compra", "contesta"], "vence": "1d"}}, "engine_not_supported:salida_contesta"),
])
def test_what_the_native_engine_does_not_run(change: dict, code: str) -> None:
    assert code in native_not_supported({**copy.deepcopy(CART), **change})


def test_the_cycle_validates_the_maribel_cart(maribel_catalog: None, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    def item(pasos: list) -> dict:
        definition = {key: copy.deepcopy(CART[key]) for key in ("nombre", "dispara", "entra", "termina", "horario")}
        return {"producto": "att1", "flujo": "carrito_nativo", "tipo": "nuevo", "prender": "cohorte",
                "definicion": {**definition, "pasos": pasos}}

    monkeypatch.setattr(shape, "_item", item)
    _, result = shape._propose_and_validate(copy.deepcopy(CART["pasos"]))

    assert result["blocks"] == [], result["blocks"]
    assert result["status"] == "validada"


def test_without_the_purchase_exit_the_cart_is_still_blocked(maribel_catalog: None) -> None:  # noqa: F811
    def item(pasos: list) -> dict:
        definition = {key: copy.deepcopy(CART[key]) for key in ("nombre", "dispara", "entra", "horario")}
        return {"producto": "att1", "flujo": "carrito_nativo", "tipo": "nuevo", "prender": "cohorte",
                "definicion": {**definition, "termina": {"si": [], "vence": "1d"}, "pasos": pasos}}

    emulator = shape.PostgrestEmulator()
    emulator.new_flow_counts = dict(shape.COUNTS)
    client, _ = shape._client(emulator)
    saved = client.post(f"{shape.PREFIX}/flow-changes", headers=shape.AUTH, json={
        "items": [item(copy.deepcopy(CART["pasos"]))], "base": {}, "purpose": "el carrito nativo sin la compra",
        "requested_by": shape.SLACK})

    # La regla 4 lo frena al guardar: sin la salida compra le escribiria a quien ya compro.
    assert saved.status_code == 422
    assert "purchase_exit_missing" in {block["code"] for block in saved.json()["data"]["blocks"]}


# -- El permiso en el envio ------------------------------------------------------


@pytest.mark.parametrize(("permission", "categories"), [
    ("formulario", {"MARKETING", "UTILITY"}),
    ("compra", {"UTILITY"}),
    (None, {"UTILITY"}),
    ("", {"UTILITY"}),
])
def test_the_send_takes_the_category_from_the_pinned_permission(permission: str | None, categories: set[str]) -> None:
    assert native_step_categories(permission) == categories
    assert native_step_categories(None) == NATIVE_PURCHASE_TEMPLATE_CATEGORIES


def _step_response(row: dict[str, Any]) -> SupabaseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/rpc/flow_engine_execution_context_v1"
        return httpx.Response(200, json=[{key: FLOW_ID if value == "<FLOW>" else value for key, value in row.items()}])

    return SupabaseClient(base_url="https://supabase.example.test", service_role_key="clave-de-prueba",
                          transport=httpx.MockTransport(handler))


def test_the_parser_reads_the_permission_of_the_database_row() -> None:
    step = asyncio.run(_step_response(STEP_ROW).get_flow_engine_step_context("action-1"))
    assert step is not None
    assert (step.template_name, step.template_variables, step.entry_permission) == (
        NAME, ("nombre", "producto"), "formulario")
    # Una base sin la migracion: sin la columna, None.
    before = {key: value for key, value in STEP_ROW.items() if key != "entry_permission"}
    older = asyncio.run(_step_response(before).get_flow_engine_step_context("action-1"))
    assert older is not None and older.entry_permission is None
    with pytest.raises(SupabaseError, match="invalid_row"):
        asyncio.run(_step_response({**STEP_ROW, "entry_permission": 5}).get_flow_engine_step_context("action-1"))


class _CartAuthority(_NativeAuthority):
    """El paso del carrito nativo: el contexto de la base (PGlite) y sin etiquetar."""

    def __init__(self, permission: str | None) -> None:
        super().__init__(step=_step(template_name=STEP_ROW["template_name"], flow_key=STEP_ROW["flow_key"],
                                    template_language=STEP_ROW["template_language"],
                                    template_variables=tuple(STEP_ROW["template_variables"]),
                                    entry_permission=permission))

    async def get_flow_engine_native_step_tail(self, action_id: str) -> FlowEngineNativeStepTail | None:
        self.engine_calls.append("tail")
        return None


def test_the_cart_step_sends_the_marketing_template_of_maribel(tmp_path: Path) -> None:
    authority = _CartAuthority("formulario")
    chatwoot = _MaribelChatwoot(MARIBEL)

    asyncio.run(_dispatcher(authority, chatwoot, tmp_path).dispatch_due(now=NOW))

    [message] = _templates(chatwoot)
    params = message["template_params"]
    assert (params["name"], params["category"], params["language"]) == (NAME, "MARKETING", "es_MX")
    # Las dos respuestas rapidas no llevan parametro: solo el nombre y el producto del cuerpo.
    assert set(params["processed_params"]) == {"body"} and list(params["processed_params"]["body"]) == ["1", "2"]
    assert authority.acceptances
    assert chatwoot.labels == [] and chatwoot.notes == []


@pytest.mark.parametrize("permission", [None, "compra"], ids=["base sin la migracion", "permiso compra"])
def test_without_the_cart_permission_the_marketing_template_never_goes(tmp_path: Path, permission: str | None) -> None:
    authority = _CartAuthority(permission)
    chatwoot = _MaribelChatwoot(MARIBEL)

    asyncio.run(_dispatcher(authority, chatwoot, tmp_path).dispatch_due(now=NOW))

    assert _templates(chatwoot) == []
    assert authority.finalizations[0]["reason_code"] == "approved_template_mismatch"


def test_a_utility_template_still_goes_without_the_permission(tmp_path: Path) -> None:
    # La misma plantilla en UTILITY (como se subio): la bienvenida de antes sigue igual.
    authority = _CartAuthority(None)
    chatwoot = _MaribelChatwoot(AS_SUBMITTED)

    asyncio.run(_dispatcher(authority, chatwoot, tmp_path).dispatch_due(now=NOW))

    [message] = _templates(chatwoot)
    assert message["template_params"]["category"] == "UTILITY"
    assert NOMBRE and PRODUCTO


# -- El resolver: primero la admision nativa --------------------------------------


def _row(outcome: str, reason: str | None = None) -> dict[str, Any]:
    started = outcome == "motor_arranco"
    return {"flow_id": FLOW_ID, "outcome": outcome, "reason": reason,
            "recovery_case_id": NATIVE_CASE if started else None,
            "followup_sequence_id": "00000000-0000-4000-8000-0000000c4a73" if started else None,
            "scheduled_action_id": "00000000-0000-4000-8000-0000000c4a74" if started else None,
            "created": True if started else None}


class _NativeBase(_Base):
    """La base del resolver, con la admision nativa del carrito."""

    def __init__(self, native: list[httpx.Response], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.native = list(native)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/rest/v1/rpc/admit_flow_engine_event_v1":
            self.calls.append(("admit_flow_engine_event_v1", json.loads(request.content)))
            return self.native.pop(0)
        return super().handler(request)


def _resolve_native(base: _NativeBase, *, client: SupabaseClient | None = None, runtime: Any = None) -> Any:
    client = client or base.client()
    runtime = runtime or _runtime(base, client)
    from bridge.hotmart import EVENT_CART_ABANDONMENT
    from bridge.resolution import resolve_event
    from test_flow_engine_resolution import POLICY_KEY
    from test_resolution_pilot_plan_rejection import BOUNDARY, _cart

    return asyncio.run(resolve_event(
        webhook_event_id=EVENT_ID, payload=_cart(CONFIG.offer_code), supabase=client, policy_key=POLICY_KEY,
        policy_version=3, allowed_jid=None, chatwoot_account_id=CONFIG.chatwoot_account_id,
        chatwoot_inbox_id=CONFIG.chatwoot_inbox_id, pilot_boundary=BOUNDARY, commercial_ally_config=CONFIG,
        event_type=EVENT_CART_ABANDONMENT, flow_engine=runtime, native_cart_product_ref=CONFIG.funnel_ref,
    ))


def _answer(*rows: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=list(rows))


def test_a_started_native_cart_owns_the_event_and_the_legacy_does_not_plan() -> None:
    base = _NativeBase([_answer(_row("motor_arranco"))], routes=_routes("motor"))
    report = _resolve_native(base)

    names = base.names()
    assert "admit_flow_engine_event_v1" in names
    assert not any(name.startswith("plan_") for name in names)
    assert "admit_flow_engine_hotmart_v1" not in names
    assert report.contact_id == CONTACT_ID
    assert [patch["processing_status"] for patch in base.status_patches] == ["processed"]
    event = base.body_of("admit_flow_engine_event_v1")["p_flow_event"]
    # La forma que la base admitio (PGlite), con los valores de este carrito.
    assert set(event) == set(ROWS["flow_event"])
    assert (event["tipo"], event["fuente"], event["producto_ref"], event["oferta"], event["contacto_id"]) == (
        "carrito", "hotmart", CONFIG.funnel_ref, CONFIG.offer_code, CONTACT_ID)
    assert event["chatwoot"] == {"account_id": CONFIG.chatwoot_account_id, "inbox_id": CONFIG.chatwoot_inbox_id}


@pytest.mark.parametrize("answer", [
    _answer({**_row("sin_flujo"), "flow_id": None}),
    _answer(_row("motor_no_arranca", "fuera_de_cohorte")),
    _answer(_row("motor_no_arranca", "flujo_pausado")),
    httpx.Response(400, json={"code": "22023", "message": "invalid_flow_engine_event"}),
], ids=["sin flujo", "fuera de la cohorte", "pausado", "rechazo de la forma"])
def test_what_no_native_flow_takes_goes_to_the_legacy_exactly_as_today(answer: httpx.Response) -> None:
    today = _Base()
    _resolve(today, flow_engine=True)
    base = _NativeBase([answer])
    _resolve_native(base)

    assert base.body_of("plan_lancemos_pilot_cart_recovery") == today.body_of("plan_lancemos_pilot_cart_recovery")
    assert [name for name in base.names() if name != "admit_flow_engine_event_v1"] == today.names()
    assert base.status_patches == today.status_patches


def test_with_the_route_in_motor_what_no_native_flow_takes_goes_to_the_engine_of_the_cart() -> None:
    from test_flow_engine_resolution import _admission

    base = _NativeBase([_answer(_row("motor_no_arranca", "fuera_de_cohorte"))], routes=_routes("motor"),
                       admission=[_admission("motor_arranco")])
    _resolve_native(base)
    assert "admit_flow_engine_hotmart_v1" in base.names()
    assert [patch["processing_status"] for patch in base.status_patches] == ["processed"]


def test_a_person_the_native_flow_takes_and_does_not_start_ends_without_the_legacy() -> None:
    base = _NativeBase([_answer(_row("motor_no_arranca", "no_entra:permiso"))])
    with pytest.raises(ResolutionError, match="^flow_engine_no_start:no_entra:permiso$"):
        _resolve_native(base)
    assert not any(name.startswith("plan_") for name in base.names())
    assert base.status_patches == [
        {"processing_status": "failed", "processed_at": None, "processing_error": "flow_engine_no_start:no_entra:permiso"}
    ]


def test_an_admission_without_answer_leaves_the_event_received_and_the_fourth_goes_to_the_legacy() -> None:
    timeout = httpx.Response(504, text="gateway timeout")
    base = _NativeBase([timeout] * (FORCE_LEGACY_AFTER_FAILURES + 1))
    client = base.client()
    runtime = _runtime(base, client)
    for _ in range(FORCE_LEGACY_AFTER_FAILURES):
        with pytest.raises(ResolutionError, match=f"^{FLOW_ENGINE_ADMISSION_PENDING}$"):
            _resolve_native(base, client=client, runtime=runtime)
    assert base.status_patches == []
    assert not any(name.startswith("plan_") for name in base.names())

    _resolve_native(base, client=client, runtime=runtime)
    # El cuarto intento le pregunta igual al nativo; como tampoco contesta, planifica el legado.
    assert [name for name, _ in base.calls].count("admit_flow_engine_event_v1") == FORCE_LEGACY_AFTER_FAILURES + 1
    assert "plan_lancemos_pilot_cart_recovery" in base.names()
    assert [patch["processing_status"] for patch in base.status_patches] == ["processed"]
    # El contador del motor del carrito no se toca: la admision del motor del mismo evento va sin forzar.
    assert len(runtime.admission_failures) == 0 and len(runtime.native_admission_failures) == 0


def test_the_fourth_attempt_still_finds_a_case_the_native_admission_created() -> None:
    timeout = httpx.Response(504, text="gateway timeout")
    base = _NativeBase([timeout] * FORCE_LEGACY_AFTER_FAILURES + [_answer({**_row("motor_arranco"), "created": False})])
    client = base.client()
    runtime = _runtime(base, client)
    for _ in range(FORCE_LEGACY_AFTER_FAILURES):
        with pytest.raises(ResolutionError, match=f"^{FLOW_ENGINE_ADMISSION_PENDING}$"):
            _resolve_native(base, client=client, runtime=runtime)
    _resolve_native(base, client=client, runtime=runtime)
    assert not any(name.startswith("plan_") for name in base.names())
    assert [patch["processing_status"] for patch in base.status_patches] == ["processed"]


def test_without_a_native_cart_product_there_is_no_new_call() -> None:
    today = _Base()
    _resolve(today, flow_engine=True)
    assert "admit_flow_engine_event_v1" not in today.names()
    assert today.body_of("plan_lancemos_pilot_cart_recovery")["p_webhook_event_id"] == EVENT_ID
    assert PLAN_ROW["created"] is True


# -- El worker: el producto, solo con un flujo nativo del carrito ----------------


def _worker_with(flows: dict[str, Any]) -> Any:
    from bridge.flow_engine_runtime import FlowEngineRuntime
    from bridge.worker import ResolutionWorker

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/rpc/read_flow_engine_native_flows_v1"
        return httpx.Response(200, json=flows)

    client = SupabaseClient(base_url="https://supabase.example.test", service_role_key="clave-de-prueba",
                            transport=httpx.MockTransport(handler))
    runtime = FlowEngineRuntime(supabase=client, instance={"tenant_ref": CONFIG.tenant_ref,
                                                           "ally_ref": CONFIG.ally_ref})
    asyncio.run(runtime.native_flows.refresh_if_due())
    return ResolutionWorker(supabase=client, commercial_ally_config=CONFIG, flow_engine=runtime)


def _flows(trigger: str, state: str = "cohorte") -> dict[str, Any]:
    flows = copy.deepcopy(NATIVE_ROWS["native_flows_cohort"])
    flows["flows"][0].update(trigger=trigger, state=state, product_ref=CONFIG.funnel_ref)
    return flows


def test_the_worker_passes_the_product_only_with_a_native_cart_flow() -> None:
    from test_resolution_pilot_plan_rejection import _cart

    cart = _cart(CONFIG.offer_code)
    assert _worker_with(_flows("carrito"))._native_cart_product(cart) == CONFIG.funnel_ref
    assert _worker_with(_flows("carrito", "prendido"))._native_cart_product(cart) == CONFIG.funnel_ref
    # La bienvenida (compra) prendida no cambia nada del carrito; pausado no cuenta.
    assert _worker_with(_flows("compra"))._native_cart_product(cart) is None
    assert _worker_with(_flows("carrito", "pausado"))._native_cart_product(cart) is None
    assert _worker_with({"flows": []})._native_cart_product(cart) is None


def test_the_product_of_a_cart_is_by_hotmart_id() -> None:
    from test_resolution_pilot_plan_rejection import _cart

    cart = _cart(CONFIG.offer_code)
    assert cart_product_ref(cart, commercial_ally_config=CONFIG, product_routes=None) == CONFIG.funnel_ref
    other = copy.deepcopy(cart)
    other["data"]["product"]["id"] = CONFIG.hotmart_product_id + 1
    assert cart_product_ref(other, commercial_ally_config=CONFIG, product_routes=None) is None
    assert cart_product_ref(cart, commercial_ally_config=None, product_routes=None) is None
    assert replace is not None
