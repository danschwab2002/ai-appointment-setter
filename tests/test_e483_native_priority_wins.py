"""E-483 (C7 del mapa «el AE maneja todos los flujos», 2026-11-12): ``prioridad.le_gana_a`` en el nativo.

La base (20261112210200) corta, al arrancar un recorrido nativo, los recorridos en curso de la misma persona y
el mismo producto de los flujos de su ``le_gana_a`` (lo mide ``validate_e483_native_priority_wins.mjs`` en
PGlite), solo si el bridge lo pide con ``p_instance.native_priority_wins``. Aca: la forma de la migracion, el
bridge (la clave va solo con ``FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED``; apagado, el cuerpo es el de antes) y el
validador con su texto para el AE (solo nombra flujos del sistema nuevo del mismo producto).
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

import test_flow_validator as tfv
from bridge import flow_definition as fd
from bridge.flow_validator import NATIVE_PRIORITY_WINS_TRIGGERS
from bridge.supabase import SupabaseClient

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20261112210200_e483_native_priority_wins_v1.sql"
TECHNICAL = re.compile(r"\boperador\b|motor nativo|\bnativo\b|\blegado\b|\bscope\b|[a-z]+_[a-z]+|\{\{")
INSTANCE = {"tenant_ref": "lancemos", "ally_ref": "att1"}
EVENT = {"fuente": "hotmart", "tipo": "pago_fallido", "evento_id": "00000000-0000-4000-8000-000000000001"}
ROW = {"flow_id": None, "outcome": "sin_flujo", "reason": None, "recovery_case_id": None,
       "scheduled_action_id": None, "created": None}


# -- La migracion ----------------------------------------------------------------


def test_the_migration_is_one_transaction_with_its_guard_and_a_private_function() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert sql.count("\nbegin;\n") == 1 and sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s';" in sql
    assert "message = 'e483_native_priority_wins_already_applied'" in sql
    assert "message = 'unexpected_admit_flow_engine_event_definition'" in sql
    # Solo con la clave del bridge, y solo un recorrido nuevo.
    assert "p_instance -> ''native_priority_wins'' = ''true''::jsonb" in sql
    assert "v_one.created is true" in sql
    # Ningun rol de la API ejecuta la funcion nueva; el legado (legacy_kind) no se mira.
    assert "revoke all on function %s from %I" in sql and "e483_native_priority_wins_acl_unexpected" in sql
    assert "and run.legacy_kind is null" in sql
    # Lo que se esta mandando no se toca.
    assert "attempt.phase <> 'completed'" in sql and "action_row.lease_owner is not null" in sql
    assert "c_reason constant text := 'le_gana_otro_flujo'" in sql


def test_the_new_test_runs_with_npm_test() -> None:
    package = json.loads((ROOT / "tests/sql/followup_engine/package.json").read_text(encoding="utf-8"))
    # Despues van el de C3 (20261112210300) y el de la pieza PF (20261112210400).
    assert package["scripts"]["test"].endswith(
        "node validate_e483_native_priority_wins.mjs && node validate_e483_native_condition_after_send.mjs --check"
        " && node validate_e483_payment_failure_by_phone.mjs")


# -- El bridge -------------------------------------------------------------------


def _client(seen: list[dict[str, Any]]) -> SupabaseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=[ROW])

    return SupabaseClient(base_url="https://supabase.example", service_role_key="service",
                          transport=httpx.MockTransport(handler))


def test_without_the_flag_the_admission_body_is_the_one_of_before() -> None:
    seen: list[dict[str, Any]] = []
    asyncio.run(_client(seen).admit_flow_engine_event(INSTANCE, EVENT))
    assert seen == [{"p_instance": INSTANCE, "p_flow_event": EVENT}]


def test_with_the_flag_the_admission_asks_the_base_to_cut() -> None:
    seen: list[dict[str, Any]] = []
    asyncio.run(_client(seen).admit_flow_engine_event(INSTANCE, EVENT, priority_wins=True))
    assert seen == [{"p_instance": {**INSTANCE, "native_priority_wins": True}, "p_flow_event": EVENT}]
    # La instancia del runtime no cambia: la usan las otras lecturas.
    assert "native_priority_wins" not in INSTANCE


def test_the_runtime_carries_the_multitouch_flag_and_starts_off() -> None:
    from bridge import app, native_flows
    from bridge.flow_engine_runtime import FlowEngineRuntime

    runtime = FlowEngineRuntime(supabase=None, instance=INSTANCE)  # type: ignore[arg-type]
    assert runtime.native_priority_wins is False
    assert FlowEngineRuntime(supabase=None, instance=INSTANCE,  # type: ignore[arg-type]
                             native_priority_wins=True).native_priority_wins is True
    source = Path(app.__file__).read_text(encoding="utf-8")
    assert "native_priority_wins=settings.flow_engine_native_multitouch_enabled" in source
    flows = Path(native_flows.__file__).read_text(encoding="utf-8")
    assert flows.count("priority_wins=runtime.native_priority_wins") == 2


class _Admission:
    """La admision nativa, que anota con que la llamo el bridge (sin red)."""

    def __init__(self) -> None:
        self.priority_wins: list[bool] = []

    async def admit_flow_engine_event(self, instance: Any, flow_event: dict[str, Any], *,
                                      priority_wins: bool = False) -> tuple[Any, ...]:
        from bridge.supabase import FlowEngineNativeAdmissionRow

        self.priority_wins.append(priority_wins)
        return (FlowEngineNativeAdmissionRow("flow-1", "motor_arranco", None, "case-1", "action-1", True),)


@pytest.mark.parametrize("on", [False, True])
def test_the_refusal_admission_carries_the_flag_of_the_runtime(on: bool) -> None:
    from bridge.flow_engine_runtime import FlowEngineRuntime
    from bridge.native_flows import NATIVE_PAYMENT_FAILURE_TRIGGER, admit_native_cart

    admission = _Admission()
    runtime = FlowEngineRuntime(supabase=None, instance=INSTANCE, native_priority_wins=on)  # type: ignore[arg-type]
    result = asyncio.run(admit_native_cart(
        admission,  # type: ignore[arg-type]
        runtime=runtime, webhook_event_id=EVENT["evento_id"], product_ref="att1",
        contact_id="00000000-0000-4000-8000-000000000002", offer_code=None, moment="2026-11-12T12:00:00+00:00",
        chatwoot_account_id=1, chatwoot_inbox_id=11, destination="5215500000000",
        trigger=NATIVE_PAYMENT_FAILURE_TRIGGER))
    assert result.recovery_case_id == "case-1"
    assert admission.priority_wins == [on]


# -- El validador y el texto del AE ----------------------------------------------


def _cart_wins(*refs: str, runtime: Any = None, definition: dict[str, Any] | None = None) -> list[Any]:
    def change(draft: dict[str, Any]) -> None:
        draft["prioridad"]["le_gana_a"].extend(refs)
    instance = tfv._running() if runtime is None else runtime
    return tfv._native_change(definition or tfv._native_cart(), change, instance=instance)


def _codes(findings: list[Any]) -> set[str]:
    return {finding.code for finding in findings}


def test_with_the_flag_a_native_cart_can_beat_a_native_flow_of_its_product() -> None:
    assert _codes(_cart_wins("bienvenida")) == set()


def test_the_payment_failure_can_beat_a_native_cart() -> None:
    refusal = tfv.welcome(nombre="Pago rechazado", dispara={"evento": "pago_fallido"},
                          entra={"permiso": "formulario"}, pasos=[{"esperar": "15m"}, copy.deepcopy(tfv.CONFIRMED_STEP)],
                          termina={"si": ["compra"], "vence": "1d"})
    assert NATIVE_PRIORITY_WINS_TRIGGERS == {"compra", "carrito", "pago_fallido"}
    assert _codes(_cart_wins("bienvenida", definition=refusal)) == set()


def test_naming_the_legacy_cart_is_a_block_with_words_for_the_owner() -> None:
    [finding] = [f for f in _cart_wins("carrito") if f.code == "native_change_priority_wins_ref"]
    assert finding.path == "/0/definicion/prioridad/le_gana_a/0"
    assert "sistema nuevo" in finding.reason and "sistema anterior" in (finding.alternative or "")
    assert not TECHNICAL.search(finding.reason) and not TECHNICAL.search(finding.alternative or "")


def test_a_flow_cannot_beat_itself() -> None:
    assert "native_change_priority_wins_ref" in _codes(_cart_wins("nativo"))


def test_without_the_flag_it_is_still_the_block_of_before() -> None:
    off = tfv._running(multitouch=False)
    findings = [f for f in _cart_wins("bienvenida", runtime=off) if f.code == "native_change_priority_wins"]
    assert [f.reason for f in findings] == [
        "Hoy un flujo no le puede ganar a otro. Lo que sí se puede decir es que este flujo no arranque mientras "
        "la persona está en otro."]
    # Sin saber lo que corre la instancia (el comando del operador), igual que antes.
    assert "native_change_priority_wins" in _codes(tfv._native_change(
        tfv._native_cart(), lambda d: d["prioridad"]["le_gana_a"].append("bienvenida")))


def test_the_first_contact_and_the_follow_up_do_not_cut() -> None:
    for definition in (tfv._form(), tfv._silence()):
        [finding] = [f for f in _cart_wins("bienvenida", definition=definition)
                     if f.code == "native_change_priority_wins"]
        assert "pueden cortar" in finding.reason and not TECHNICAL.search(finding.reason)


def test_taking_the_list_out_is_always_allowed() -> None:
    base = tfv.native_flow({**tfv._native_cart(), "prioridad": {"le_gana_a": ["bienvenida"], "no_arranca_mientras": []}})
    draft = fd.as_draft(base.versions[1])
    draft["prioridad"]["le_gana_a"] = []
    others = tfv.world(base, tfv._native_welcome("bienvenida", tfv.WELCOME_ID, "prendido"), tfv._routed("legado"))
    result = tfv.check([tfv.version_item(draft, "nativo", flow_id=tfv.NATIVE_ID)], actor="automation_expert",
                       in_world=others, catalog=None, phase="draft", instance=tfv._running(multitouch=False))
    assert not {"native_change_priority_wins", "native_change_priority_wins_ref"} & _codes([*result.blocks])


@pytest.mark.parametrize(("multitouch", "refs", "code"), [
    (False, ["bienvenida"], "native_change_priority_wins"),
    (True, ["carrito"], "native_change_priority_wins_ref"),
])
def test_a_new_flow_of_the_ae_follows_the_same_rule(multitouch: bool, refs: list[str], code: str) -> None:
    definition = tfv._native_cart()
    definition["prioridad"] = {"le_gana_a": refs, "no_arranca_mientras": []}
    others = tfv.world(tfv._native_welcome("bienvenida", tfv.WELCOME_ID, "prendido"), tfv._routed("legado"))
    findings = tfv.run_new(definition, "carrito_nuevo", in_world=others, phase="draft", catalog=None,
                           instance=tfv._running(multitouch=multitouch))
    assert code in _codes(findings)
    definition["prioridad"]["le_gana_a"] = ["bienvenida"]
    clean = tfv.run_new(definition, "carrito_nuevo", in_world=others, phase="draft", catalog=None,
                        instance=tfv._running())
    assert not {"native_change_priority_wins", "native_change_priority_wins_ref"} & _codes(clean)


def test_the_ae_guide_says_what_cuts_and_what_does_not() -> None:
    guide = (ROOT / "profiles/automation-expert/skills/flujos/cambiar-un-flujo/SKILL.md").read_text(encoding="utf-8")
    assert "`prioridad.le_gana_a`" in guide
    assert "salvo un mensaje que ya está saliendo" in guide
    assert "un flujo del sistema anterior no se corta" in guide
    assert "que un flujo le gane a otro" not in guide
