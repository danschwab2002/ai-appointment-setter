"""E-483 (C1 y C5 del mapa «el AE maneja todos los flujos», 2026-11-12): el nativo de varios toques.

La base (20261112210000) agenda los toques siguientes de un flujo nativo como ``no_reply_review`` con
``native_touch_2`` y ``native_touch_3`` (lo mide ``validate_e483_native_multitouch.mjs`` en PGlite). Aca:
la regla 14 nativa (hasta tres envios, con contesta, boton solo en el primero, sin condicion), el cambio de
salidas que el AE ya puede publicar, y el bridge, que manda un toque siguiente solo con
``FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED`` y con su plantilla fijada.

Los bordes externos (Chatwoot, la plantilla) son los capturados de ``test_flow_engine_native_dispatch.py``.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from bridge.flow_engine import (
    ENGINE_CAPABILITIES_NATIVE,
    NATIVE_CAPABILITIES_BY_TRIGGER,
    NATIVE_MULTITOUCH_TRIGGERS,
    native_not_supported,
)
from bridge.worker import FLOW_ENGINE_STEP_UNSUPPORTED, NATIVE_MULTITOUCH_DISABLED
from test_flow_engine_native_dispatch import (
    WELCOME_BODY,
    _dispatcher,
    _NativeAuthority,
    _NativeChatwoot,
    _run,
    _step,
    _templates,
)

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20261112210000_e483_native_multitouch_v1.sql"

SEND = {"plantilla": "maribel_retomar_proceso_01", "idioma": "es_MX", "variables": ["nombre", "producto"]}
CART = {
    "esquema": "setter-flujo/v1", "flujo": "carrito", "producto": "maribel", "estado": "borrador",
    "nombre": "Carrito de tres toques", "dispara": {"evento": "carrito"},
    "entra": {"permiso": "formulario", "contesto_a": []},
    "pasos": [SEND, {"esperar": "24h"}, SEND, {"esperar": "48h"}, SEND],
    "termina": {"si": ["compra", "contesta"], "vence": "4d"}, "horario": "siempre",
    "topes": {"bolsa": "maribel-carrito", "por_dia": 150, "total": 3000,
              "repeticion": {"una_por": "persona_y_oferta", "otra_despues_de": "0m"}},
    "prioridad": {"le_gana_a": [], "no_arranca_mientras": []},
}


def _cart(**changes: Any) -> dict[str, Any]:
    return {**copy.deepcopy(CART), **changes}


# -- La regla 14 nativa ----------------------------------------------------------


@pytest.mark.parametrize("event", ["carrito", "intencion", "pago_fallido"])
def test_three_touches_with_waits_and_reply_exit_run_natively(event: str) -> None:
    assert native_not_supported(_cart(dispara={"evento": event})) == ()
    assert NATIVE_CAPABILITIES_BY_TRIGGER[event].max_sends_per_path == 3
    assert event in NATIVE_MULTITOUCH_TRIGGERS


def test_the_welcome_stays_one_send() -> None:
    assert NATIVE_CAPABILITIES_BY_TRIGGER["compra"] is ENGINE_CAPABILITIES_NATIVE
    welcome = _cart(dispara={"evento": "compra"}, entra={"permiso": "compra", "contesto_a": []})
    assert "engine_not_supported:demasiados_envios" in native_not_supported(welcome)


@pytest.mark.parametrize(("change", "code"), [
    ({"termina": {"si": ["compra"], "vence": "4d"}}, "engine_not_supported:varios_envios_sin_contesta"),
    ({"pasos": [*CART["pasos"], {"esperar": "1h"}, SEND]}, "engine_not_supported:demasiados_envios"),
    ({"pasos": [SEND, SEND]}, "engine_not_supported:espera_entre_envios"),
    ({"pasos": [SEND, {"esperar": "1h"}, SEND, {"esperar": "1h"}]}, "engine_not_supported:espera_despues_del_envio"),
    ({"dispara": {"evento": "pago_fallido"},
      "pasos": [SEND, {"esperar": "1h"}, {**SEND, "boton": {"link": "checkout"}}]},
     "engine_not_supported:boton_despues_del_primer_envio"),
    ({"dispara": {"evento": "pago_fallido"},
      "pasos": [SEND, {"esperar": "1h"}, {"condicion": {"dato": "rechazos", "es": ["uno"]},
                                          "entonces": [SEND], "si_no": [SEND]}]},
     "engine_not_supported:condicion_despues_de_un_envio"),
])
def test_what_a_multitouch_native_flow_still_cannot_do(change: dict[str, Any], code: str) -> None:
    assert code in native_not_supported(_cart(**change))


def test_the_base_mirrors_the_python_rule() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert sql.count("\nbegin;\n") == 1 and sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s';" in sql
    assert "message = 'e483_native_multitouch_already_applied'" in sql
    for needed in ("native_touch_2", "native_touch_3", "not between 1 and 3", '["contesta"]',
                   "(''carrito'', ''intencion'', ''pago_fallido'')"):
        assert needed in sql, needed


# -- El bridge -------------------------------------------------------------------


def _following(authority: _NativeAuthority, step_key: str = "native_touch_2") -> _NativeAuthority:
    authority.action = replace(authority.action, action_type="no_reply_review", step_key=step_key,
                               anchor_type="message")
    authority.context = replace(authority.context, action_type="no_reply_review", step_key=step_key)
    authority.step = _step(step_key=step_key, send_ordinal=2)
    return authority


def test_without_the_flag_a_following_native_touch_closes_and_sends_nothing(tmp_path: Path) -> None:
    authority = _following(_NativeAuthority())
    chatwoot = _NativeChatwoot()

    _run(_dispatcher(authority, chatwoot, tmp_path))

    assert _templates(chatwoot) == []
    [closed] = authority.finalizations
    assert closed["reason_code"] == NATIVE_MULTITOUCH_DISABLED
    assert closed["next_attempt_at"] is None


def test_with_the_flag_a_following_native_touch_is_built_with_its_pinned_template(tmp_path: Path) -> None:
    # La composicion del envio: la evidencia de Chatwoot de un no_reply_review (el ancla del toque anterior)
    # es el camino generico de A2 y Z, que no cambia; aca se mira que el toque siguiente nativo se arma con
    # la plantilla de su version y no cae al carrito del legado ni a FLOW_ENGINE_STEP_UNSUPPORTED.
    import asyncio

    authority = _following(_NativeAuthority())
    chatwoot = _NativeChatwoot()
    dispatcher = _dispatcher(authority, chatwoot, tmp_path)
    dispatcher._native_multitouch_enabled = True

    composition = asyncio.run(dispatcher._compose_approved_template_proposal(
        action=authority.action, execution_context=authority.context, attempt=None, step_template=authority.step))

    assert composition.failure_reason is None
    assert composition.template_name == "att1_bienvenida_01"
    assert composition.proposal is not None
    assert composition.proposal.message == WELCOME_BODY.replace("{{1}}", "Edith García Pérez").replace(
        "{{2}}", "Alimenta tu Tiroides")


@pytest.mark.parametrize("changes", [{"template_button": {"link": "checkout"}}, {"step_key": "native_touch_3"}])
def test_a_following_touch_the_base_would_not_open_closes_for_good(tmp_path: Path, changes: dict[str, Any]) -> None:
    authority = _following(_NativeAuthority())
    authority.step = replace(authority.step, **changes)
    chatwoot = _NativeChatwoot()
    dispatcher = _dispatcher(authority, chatwoot, tmp_path)
    dispatcher._native_multitouch_enabled = True

    _run(dispatcher)

    assert _templates(chatwoot) == []
    [closed] = authority.finalizations
    assert closed["reason_code"] == FLOW_ENGINE_STEP_UNSUPPORTED


# -- Con los toques siguientes apagados, el validador no publica en silencio ------------------------------


def _multitouch_runtime(on: bool) -> Any:
    from bridge.flow_validator import NativeRuntime

    return NativeRuntime(engine_enabled=True, native_intent_enabled=True, native_silence_sweeper=True,
                           cart_touches=True, main_product="att1", native_multitouch=on)


def _new_cart_findings(runtime: Any, *, actor: str, pasos: list[Any] | None = None) -> set[str]:
    """El primer contacto nuevo de tres toques (el molde del validador, sobre el mundo de ATT1)."""
    import test_flow_validator as tfv
    from bridge.flow_definition import InstanceView

    definition = tfv._three_touch_form()
    if pasos is not None:
        definition["pasos"] = pasos
    instance = None if runtime is None else InstanceView(
        "lancemos", "att1", frozenset({"att1"}), "America/Mexico_City", runtime=runtime)
    findings = tfv.run_new(definition, "primer_contacto", actor=actor,
                           **({"instance": instance} if instance is not None else {}))
    return {finding.code for finding in findings}


def test_the_ae_cannot_publish_three_touches_while_the_bridge_sends_only_the_first() -> None:
    # Antes de este cambio el validador lo aceptaba y salia solo el primero (el segundo cerraba con
    # flow_engine_native_multitouch_disabled).
    assert "native_multitouch_not_running" in _new_cart_findings(_multitouch_runtime(False),
                                                                  actor="automation_expert")
    assert "native_multitouch_not_running" not in _new_cart_findings(_multitouch_runtime(True),
                                                                      actor="automation_expert")


def tfv_step() -> dict[str, Any]:
    import test_flow_validator as tfv

    return copy.deepcopy(tfv.CONFIRMED_STEP)


def test_one_touch_does_not_need_the_flag() -> None:
    codes = _new_cart_findings(_multitouch_runtime(False), actor="automation_expert", pasos=[tfv_step()])
    assert not {"native_multitouch_not_running", "native_multitouch_not_running_warning"} & codes


def test_the_operator_is_blocked_too() -> None:
    # Correccion de la revision (2026-11-13): mejor no publicar que mandar solo el primero. Antes era una
    # advertencia (native_multitouch_not_running_warning), que ya no existe.
    codes = _new_cart_findings(_multitouch_runtime(False), actor="operador")
    assert "native_multitouch_not_running" in codes
    assert "native_multitouch_not_running_warning" not in codes
    assert "native_multitouch_not_running" not in _new_cart_findings(_multitouch_runtime(True), actor="operador")


def test_an_unknown_runtime_does_not_block() -> None:
    assert not {"native_multitouch_not_running", "native_multitouch_not_running_warning"} & _new_cart_findings(
        None, actor="automation_expert")


def test_the_runtime_carries_the_flag_of_the_bridge_and_the_operator_input_defaults_it_off() -> None:
    from bridge.app import Settings, instance_runtime_from
    from bridge.flow_change_operator import instance_from_input

    on = _multitouch_runtime(True)
    assert on.to_json()["native_multitouch_enabled"] is True
    base = {"tenant_ref": "lancemos", "ally_ref": "maribel", "products": ["maribel"],
            "time_zone": "America/Mexico_City"}
    assert instance_from_input({**base, "runtime": on.to_json()}).runtime == on
    legacy_input = {key: value for key, value in on.to_json().items() if key != "native_multitouch_enabled"}
    from bridge.flow_validator import runtime_runs_multitouch

    assert runtime_runs_multitouch(instance_from_input({**base, "runtime": legacy_input}).runtime) is False
    with pytest.raises(Exception):
        instance_from_input({**base, "runtime": {**on.to_json(), "native_multitouch_enabled": "si"}})
    assert Settings.__dataclass_fields__["flow_engine_native_multitouch_enabled"].default is False
    assert "native_multitouch" in instance_runtime_from.__code__.co_names \
        or "flow_engine_native_multitouch_enabled" in instance_runtime_from.__code__.co_names


def test_with_the_flag_on_the_three_touch_form_publishes_clean_for_the_ae() -> None:
    assert _new_cart_findings(_multitouch_runtime(True), actor="automation_expert") == set()


def test_a_plain_instance_runtime_counts_as_multitouch_off() -> None:
    from bridge.flow_definition import InstanceRuntime

    plain = InstanceRuntime(engine_enabled=True, native_intent_enabled=True, native_silence_sweeper=True,
                            cart_touches=True, main_product="att1")
    assert "native_multitouch_not_running" in _new_cart_findings(plain, actor="automation_expert")
