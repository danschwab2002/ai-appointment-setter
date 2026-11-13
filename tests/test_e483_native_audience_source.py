"""E-483, para E-484: la admision nativa acepta el evento de un disparo por audiencia (20261112210600).

La base (``validate_e483_native_audience_source.mjs`` en PGlite) admite, solo con
``p_instance.native_audience``, la persona de la lista congelada de una ocurrencia atada a su flujo y version.
Aca: la forma de la migracion, el bridge (la clave va solo si se pide; sin ella, el cuerpo de antes), y el
validador: ``fecha``, ``cada`` y ``manual`` dejan de ser ``motor_nativo:disparador`` solo con
FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED (``NativeRuntime.native_multitouch``), y nada cambia para los
disparadores de siempre.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import test_flow_validator as tfv
from bridge.flow_engine import (
    NATIVE_AUDIENCE_TRIGGER,
    NATIVE_UNSUPPORTED_PERMISSION,
    NATIVE_UNSUPPORTED_TRIGGER,
    native_audience_form,
    native_not_supported,
    native_trigger_key,
)
from bridge.supabase import SupabaseClient

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20261112210600_e483_native_audience_source_v1.sql"
INSTANCE = {"tenant_ref": "lancemos", "ally_ref": "att1"}
# El evento tal como lo arma ``flow_audience_opener.audience_event`` en la rama de E-484 (sin nombre ni mail).
EVENT = {
    "fuente": "audiencia", "tipo": "fecha",
    "evento_id": "audiencia:00000000-0000-4000-8000-00000000000a:00000000-0000-4000-8000-00000000000b",
    "flujo_id": "00000000-0000-4000-8000-00000000000c", "version": 1,
    "ocurrencia_id": "00000000-0000-4000-8000-00000000000a", "producto_ref": "att1",
    "contacto_id": "00000000-0000-4000-8000-00000000000b", "momento": "2026-12-01T16:00:00+00:00",
    "chatwoot": {"account_id": 1, "inbox_id": 11}, "destino": "5215500000000",
}
ROW = {"flow_id": None, "outcome": "sin_flujo", "reason": None, "recovery_case_id": None,
       "scheduled_action_id": None, "created": None}


def _audience(**changes: Any) -> dict[str, Any]:
    """Un envio por fecha a prospectas con permiso, de dos mensajes con contesta (lo que escribe el AE en E-484)."""
    send = copy.deepcopy(tfv.CONFIRMED_STEP)
    definition = tfv.welcome(
        nombre="Recordatorio de Black",
        dispara={"fecha": {"el": "2026-12-01T10:00", "zona": "America/Mexico_City"}},
        entra={"permiso": "formulario"},
        pasos=[copy.deepcopy(send), {"esperar": "24h"}, copy.deepcopy(send)],
        termina={"si": ["compra", "contesta"], "vence": "3d"},
    )
    # La repeticion por persona de E-484 (topes.repeticion, 7 dias por defecto en su lista).
    definition.setdefault("topes", {})["repeticion"] = {"una_por": "persona", "otra_despues_de": "7d"}
    definition["para"] = {"es": "prospecta"}
    definition.update(changes)
    return definition


# -- La migracion ----------------------------------------------------------------


def test_the_migration_is_one_transaction_with_its_guards() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert sql.count("\nbegin;\n") == 1 and sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s';" in sql
    assert "message = 'e483_native_audience_already_applied'" in sql
    assert "message = 'unexpected_e483_native_audience_definition'" in sql
    # Solo con la clave del bridge, y solo con fuente audiencia.
    assert "p_instance -> ''native_audience'' = ''true''::jsonb" in sql
    assert "p_flow_event -> ''fuente'' = ''\"audiencia\"''::jsonb" in sql
    # Las funciones nuevas no son de la API, y la idempotencia es por flujo, version, ocurrencia y contacto.
    assert "e483_native_audience_acl_unexpected" in sql
    assert "'flow_audience:' || v_flow_id::text || ':' || v_version::text || ':'" in sql
    assert "on conflict (source, external_event_id) do nothing" in sql
    assert "check (trigger_event in ('compra', 'carrito', 'pago_fallido', 'intencion', 'audiencia'))" in sql


def test_the_new_test_runs_last_with_npm_test() -> None:
    package = json.loads((ROOT / "tests/sql/followup_engine/package.json").read_text(encoding="utf-8"))
    assert package["scripts"]["test"].endswith(
        "node validate_e483_native_personal_coupon.mjs --check && node validate_e483_native_audience_source.mjs && node validate_flow_audience_triggers.mjs && node validate_flow_audience_native_e2e.mjs"
        " && node validate_e483_native_legacy_double_case_touches.mjs --check && node validate_e483_native_person_cap.mjs"
        " && node validate_e528_native_payment_slip.mjs")


# -- El bridge -------------------------------------------------------------------


def _client(seen: list[dict[str, Any]]) -> SupabaseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=[ROW])

    return SupabaseClient(base_url="https://supabase.example", service_role_key="service",
                          transport=httpx.MockTransport(handler))


def test_without_the_key_the_admission_body_is_the_one_of_before() -> None:
    seen: list[dict[str, Any]] = []
    asyncio.run(_client(seen).admit_flow_engine_event(INSTANCE, EVENT))
    assert seen == [{"p_instance": INSTANCE, "p_flow_event": EVENT}]


def test_with_the_key_the_base_admits_the_audience() -> None:
    seen: list[dict[str, Any]] = []
    asyncio.run(_client(seen).admit_flow_engine_event(INSTANCE, EVENT, priority_wins=True, audience=True))
    assert seen == [{"p_instance": {**INSTANCE, "native_priority_wins": True, "native_audience": True},
                     "p_flow_event": EVENT}]
    assert "native_audience" not in INSTANCE


# -- Lo que el motor nativo ejecuta ----------------------------------------------


@pytest.mark.parametrize("form", [
    {"fecha": {"el": "2026-12-01T10:00", "zona": "America/Mexico_City"}},
    {"cada": {"dias": ["lun"], "hora": "10:00", "zona": "America/Mexico_City"}},
    {"manual": {}},
])
def test_the_three_forms_are_the_audience_only_with_the_flag(form: dict[str, Any]) -> None:
    definition = _audience(dispara=form)
    assert native_audience_form(definition) == next(iter(form))
    assert native_trigger_key(definition) is None
    assert native_trigger_key(definition, audience=True) == NATIVE_AUDIENCE_TRIGGER
    assert NATIVE_UNSUPPORTED_TRIGGER in native_not_supported(definition)
    assert native_not_supported(definition, audience=True) == ()


def test_two_forms_or_a_form_that_is_not_an_object_are_not_the_audience() -> None:
    assert native_trigger_key(_audience(dispara={"fecha": {}, "manual": {}}), audience=True) is None
    assert native_trigger_key(_audience(dispara={"manual": True}), audience=True) is None


def test_the_audience_enters_with_form_or_purchase_permission_and_never_with_conversation() -> None:
    assert native_not_supported(_audience(entra={"permiso": "compra"}, para={"es": "clienta"}), audience=True) == ()
    assert NATIVE_UNSUPPORTED_PERMISSION in native_not_supported(
        _audience(entra={"permiso": "conversacion"}), audience=True)


def test_the_audience_has_the_rules_of_a_native_of_several_touches() -> None:
    send = copy.deepcopy(tfv.CONFIRMED_STEP)
    four = _audience(pasos=[copy.deepcopy(send), {"esperar": "1d"}, copy.deepcopy(send), {"esperar": "1d"},
                            copy.deepcopy(send), {"esperar": "1d"}, copy.deepcopy(send)])
    assert "engine_not_supported:demasiados_envios" in native_not_supported(four, audience=True)
    no_reply = _audience(termina={"si": ["compra"], "vence": "3d"})
    assert "engine_not_supported:varios_envios_sin_contesta" in native_not_supported(no_reply, audience=True)
    personal = _audience(pasos=[{**copy.deepcopy(send), "boton": {"link": "checkout", "cupon": "personal"}}])
    assert "engine_not_supported:cupon_personal" in native_not_supported(personal, audience=True)


@pytest.mark.parametrize("definition", [
    tfv.welcome(), tfv._native_cart(), tfv._form(), tfv._three_touch_form(), tfv._cart_personal(),
], ids=["compra", "carrito", "formulario", "formulario_de_tres", "carrito_con_cupon"])
def test_the_triggers_of_always_do_not_change_with_the_flag(definition: dict[str, Any]) -> None:
    assert native_not_supported(definition, audience=True) == native_not_supported(definition)
    assert native_trigger_key(definition, audience=True) == native_trigger_key(definition)


# -- El validador ----------------------------------------------------------------


def _codes(*, runtime: Any) -> set[str]:
    return tfv.found(tfv.run_new(_audience(), "difusion", instance=runtime))


def test_the_validator_opens_the_audience_only_with_the_multitouch_flag() -> None:
    assert "motor_nativo:disparador" in _codes(runtime=tfv.INSTANCE)
    assert "motor_nativo:disparador" in _codes(runtime=tfv._running(multitouch=False))
    on = _codes(runtime=tfv._running(multitouch=True))
    assert "motor_nativo:disparador" not in on
    assert "motor_nativo:permiso" not in on and "motor_nativo:forma" not in on
