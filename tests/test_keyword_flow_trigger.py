"""El flujo que arranca con la palabra clave de una regla (Z2 3.9, ``keyword-rules-v1`` §5).

Contrato: ``docs/contracts/keyword-rules-v1.md``. Migracion ``20261127400000_flow_engine_keyword_trigger_v1``
(la prueba de la base es ``tests/sql/followup_engine/validate_flow_engine_keyword_trigger.mjs``).

Datos: el webhook del turno es la captura de la conversacion 200 del inbox 9 de ``test_keyword_rules_bridge``
(``_text_webhook`` con el texto del link); la plantilla del flujo es la del catalogo capturado que usa
``test_flow_validator`` (``CONFIRMED_STEP``). La lectura de la salud de Meta tiene la forma del ``estado.json``
de ``meta_salud.py`` que leen la audiencia y el masivo. Ningun dato personal.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_flow_validator import CONFIRMED_STEP, _keyword, _keyword_running, found, run_new
from test_keyword_rules_bridge import LINK_TEXT, RULE, _settings, _turn

from bridge import flow_definition as fd
from bridge import flow_engine as fe
from bridge import flow_validator as fv
from bridge import keyword_flow_trigger as kft
from bridge import keyword_rule_approval, keyword_rules
from bridge.bulk_send_brakes import FinishedSend
from bridge.flow_change_operator import instance_from_input
from bridge.supabase import FlowEngineAdmissionUnavailable, FlowEngineNativeAdmissionRow, SupabasePermanentError

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/20261127400000_flow_engine_keyword_trigger_v1.sql"
NOW = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
RULE_ID = "7a2b3c4d-2222-4b9f-8d2e-000000000001"
FLOW_ID = "0bf45aa4-4dfe-4ab7-88ed-49d2f25af3a2"
GREEN = {"calidad": "GREEN", "leido_en": (NOW - timedelta(hours=1)).isoformat(), "puede_enviar": "CAN_SEND"}
SERVED = keyword_rules.KeywordRule(rule_id=RULE_ID, key="soporte", name="Link de soporte",
                                   keyword="hola necesito soporte", label="soporte", macro_id=77)


# ---------------------------------------------------------------------------
# El esquema y el validador.
# ---------------------------------------------------------------------------

def test_the_trigger_is_the_key_of_a_rule() -> None:
    assert fd.trigger_of({"dispara": {"palabra_clave": "soporte"}}) == fd.Trigger("palabra_clave", "soporte")
    assert fd.trigger_of({"dispara": {"palabra_clave": "Soporte!"}}) == fd.Trigger("palabra_clave", None)
    assert fd.trigger_of({"dispara": {"palabra_clave": "soporte", "evento": "compra"}}) is None
    assert fd.is_keyword_rule_key("check-in") and not fd.is_keyword_rule_key("-x") and not fd.is_keyword_rule_key(7)


def test_a_keyword_flow_is_clean_only_with_the_three_flags() -> None:
    assert found(run_new(_keyword(), "soporte", instance=_keyword_running())) == set()
    off = found(run_new(_keyword(), "soporte", instance=_keyword_running(keyword=False)))
    assert "motor_nativo:disparador" in off
    # Sin la lista de reglas a mano no se mira la regla, pero el disparador sigue atado al flag.
    assert found(run_new(_keyword(), "soporte", instance=_keyword_running(None))) == set()


def test_the_normalized_keyword_flow_enters_with_conversation_and_ends_in_a_day() -> None:
    result = fv.validate([fd.ProposalItem(producto="att1", flujo="soporte", flow_id=None, tipo="nuevo",
                                          definicion=_keyword(), prender="cohorte")],
                         actor="automation_expert", world=fv.FlowWorld(flows=()), instance=_keyword_running(),
                         catalog=None, phase="draft", draft_base={})
    [item] = result.normalized_items
    definition = item.definicion
    assert definition["entra"] == {"permiso": "conversacion", "contesto_a": []}
    assert definition["termina"] == {"si": ["compra"], "vence": "1d"}
    assert definition["topes"]["repeticion"] == {"una_por": "persona", "otra_despues_de": "7d"}


def test_a_keyword_flow_with_another_permission_or_a_bad_key_is_refused() -> None:
    assert "permission_trigger_mismatch" in found(run_new(_keyword(entra={"permiso": "formulario"}), "soporte",
                                                          instance=_keyword_running()))
    assert "invalid_value" in found(run_new(_keyword(dispara={"palabra_clave": "Soporte!"}), "soporte",
                                            instance=_keyword_running()))


def test_the_native_engine_runs_the_keyword_only_with_its_flag() -> None:
    definition = _keyword(entra={"permiso": "conversacion", "contesto_a": []}, termina={"si": ["compra"], "vence": "1d"},
                          topes={"repeticion": {"una_por": "persona", "otra_despues_de": "7d"}})
    assert fe.native_trigger_key(definition) is None
    assert fe.native_trigger_key(definition, keyword=True) == fe.NATIVE_KEYWORD_TRIGGER
    assert fe.NATIVE_UNSUPPORTED_TRIGGER in fe.native_not_supported(definition)
    assert fe.native_not_supported(definition, keyword=True) == ()
    replied = {**definition, "entra": {"permiso": "conversacion", "contesto_a": ["otro"]}}
    assert fe.NATIVE_UNSUPPORTED_PERMISSION in fe.native_not_supported(replied, keyword=True)
    # Dos toques sin la salida contesta: lo mismo que la base (motor_nativo:forma).
    two = {**definition, "pasos": [CONFIRMED_STEP, {"esperar": "1h"}, CONFIRMED_STEP]}
    assert "engine_not_supported:varios_envios_sin_contesta" in fe.native_not_supported(two, keyword=True)
    assert fe.native_not_supported({**two, "termina": {"si": ["compra", "contesta"], "vence": "1d"}}, keyword=True) == ()


def test_a_keyword_flow_takes_a_literal_text_like_the_other_native_forms() -> None:
    # F4 (20261127300000) con Z2 3.9: la forma nativo:palabra_clave lleva texto_fijo en la base, y el motor y el
    # validador lo aceptan con FLOW_LITERAL_VARIABLES_ENABLED; sin el flag, invalid_value como en todo flujo.
    step = {**CONFIRMED_STEP, "variables": ["nombre", {"texto": "Te escribimos por el link de soporte."}]}
    definition = _keyword(pasos=[step], entra={"permiso": "conversacion", "contesto_a": []},
                          termina={"si": ["compra"], "vence": "1d"},
                          topes={"repeticion": {"una_por": "persona", "otra_despues_de": "7d"}})
    assert fe.ENGINE_CAPABILITIES_NATIVE_KEYWORD.literal_variables is True
    assert fe.native_not_supported(definition, keyword=True) == ()
    on = replace(_keyword_running(), literal_variables=True)
    assert found(run_new(_keyword(pasos=[step]), "soporte", instance=on)) == set()
    assert "invalid_value" in found(run_new(_keyword(pasos=[step]), "soporte", instance=_keyword_running()))


def test_the_runtime_json_says_the_keyword_only_when_it_is_on() -> None:
    off = fv.NativeRuntime(engine_enabled=True, native_intent_enabled=True, native_silence_sweeper=True,
                           cart_touches=True, main_product="att1")
    assert "native_keyword_enabled" not in off.to_json()
    on = replace(off, native_keyword=True)
    assert on.to_json()["native_keyword_enabled"] is True
    view = instance_from_input({"tenant_ref": "lancemos", "ally_ref": "att1", "products": ["att1"],
                                "runtime": on.to_json(), "keyword_rules": ["soporte"]})
    assert fv.runtime_runs_keyword(view.runtime) and view.keyword_rules == frozenset({"soporte"})
    assert instance_from_input({"tenant_ref": "lancemos", "ally_ref": "att1", "products": ["att1"]}).keyword_rules is None


# ---------------------------------------------------------------------------
# El disparador del bridge.
# ---------------------------------------------------------------------------

class _Lock:
    def __init__(self, *, busy: bool = False, brakes: list[FinishedSend] | None = None, broken: bool = False) -> None:
        self.busy, self.brakes, self.broken = busy, brakes or [], broken

    def busy_elsewhere(self) -> bool:
        return self.busy

    def read_brakes(self) -> list[FinishedSend]:
        if self.broken:
            raise ValueError("frenos-meta.json")
        return self.brakes


class _Admit:
    def __init__(self, rows: list[FlowEngineNativeAdmissionRow] | None = None, error: Exception | None = None) -> None:
        self.rows, self.error, self.calls = rows or [], error, []

    async def __call__(self, instance: dict[str, Any], event: dict[str, Any], **kwargs: Any) -> Any:
        self.calls.append((instance, event, kwargs))
        if self.error is not None:
            raise self.error
        return tuple(self.rows)


def _row(outcome: str, reason: str | None = None) -> FlowEngineNativeAdmissionRow:
    started = outcome == "motor_arranco"
    return FlowEngineNativeAdmissionRow(flow_id=None if outcome == "sin_flujo" else FLOW_ID, outcome=outcome,
                                        reason=reason, recovery_case_id="c" * 8 if started else None,
                                        scheduled_action_id="a" * 8 if started else None,
                                        created=True if started else None)


def _trigger(admit: _Admit, *, health: dict[str, Any] | None = GREEN, lock: _Lock | None = None,
             priority: bool = False) -> kft.KeywordFlowTrigger:
    return kft.KeywordFlowTrigger(admit=admit, instance={"tenant_ref": "lancemos", "ally_ref": "att1"},
                                  account_id=2, inbox_id=11, health=lambda: health, portfolio=lock or _Lock(),
                                  priority_wins=priority, clock=lambda: NOW)


def _start(trigger: kft.KeywordFlowTrigger, *, destination: str = "1202555040101", ref: object = "4501") -> str:
    return asyncio.run(trigger.start(rule=SERVED, conversation_id=455, message_ref=ref, destination=destination))


def test_the_event_has_the_shape_the_base_admits() -> None:
    admit = _Admit([_row("motor_arranco")])
    assert _start(_trigger(admit, priority=True)) == kft.OUTCOME_STARTED
    [(instance, event, kwargs)] = admit.calls
    assert instance == {"tenant_ref": "lancemos", "ally_ref": "att1"}
    assert kwargs == {"keyword": True, "priority_wins": True}
    assert event == {
        "fuente": "palabra_clave", "evento_id": "palabra_clave:455:4501", "regla_id": RULE_ID, "regla_clave": "soporte",
        "momento": NOW.isoformat(),
        "chatwoot": {"account_id": 2, "inbox_id": 11, "conversation_id": 455, "message_id": 4501},
        "destino": "1202555040101",
    }
    # Las mismas claves que exige la base (la lista de _flow_engine_admit_native_keyword_v1).
    sql = MIGRATION.read_text(encoding="utf-8")
    keys = re.search(r"is distinct from array\[('chatwoot', 'destino'[^\]]*)\]", sql)
    assert keys is not None and sorted(event) == [k.strip(" '") for k in keys.group(1).split(",")]
    assert "array['account_id', 'conversation_id', 'inbox_id', 'message_id']" in sql


@pytest.mark.parametrize(("health", "lock", "reason"), [
    ({**GREEN, "calidad": "RED"}, None, "meta_salud:red"),
    ({**GREEN, "leido_en": (NOW - timedelta(hours=30)).isoformat()}, None, "meta_salud:vieja"),
    (None, None, "meta_salud:sin_lectura"),
    (GREEN, _Lock(busy=True), kft.BRAKE_PORTFOLIO_BUSY),
    (GREEN, _Lock(brakes=[FinishedSend(send_ref="masivo-1", braked_at=NOW - timedelta(hours=2),
                                       reason="meta_codigo:131049")]), kft.BRAKE_PORTFOLIO_META),
    (GREEN, _Lock(broken=True), kft.BRAKE_PORTFOLIO_UNREADABLE),
], ids=["salud-roja", "salud-vieja", "sin-lectura", "masivo-en-curso", "freno-24h", "candado-ilegible"])
def test_a_brake_outside_the_base_starts_nothing(health: Any, lock: _Lock | None, reason: str,
                                                  caplog: pytest.LogCaptureFixture) -> None:
    admit = _Admit([_row("motor_arranco")])
    caplog.set_level("INFO", logger="bridge.keyword_flow_trigger")
    assert _start(_trigger(admit, health=health, lock=lock)) == kft.OUTCOME_BRAKED
    assert admit.calls == []
    assert f"reason={reason}" in caplog.text and "1202555040101" not in caplog.text


def test_without_a_destination_or_a_message_nothing_starts() -> None:
    admit = _Admit([_row("motor_arranco")])
    assert _start(_trigger(admit), destination="abc") == kft.OUTCOME_BRAKED
    assert _start(_trigger(admit), ref="no-es-un-id") == kft.OUTCOME_BRAKED
    assert admit.calls == []


@pytest.mark.parametrize(("rows", "error", "outcome"), [
    ([_row("motor_no_arranca", "baja")], None, kft.OUTCOME_NOT_STARTED),
    ([_row("sin_flujo")], None, kft.OUTCOME_NO_FLOW),
    ([], FlowEngineAdmissionUnavailable("admit_flow_engine_event_v1_unavailable"), kft.OUTCOME_FAILED),
    ([], SupabasePermanentError("x", reason="invalid_flow_engine_event"), kft.OUTCOME_FAILED),
    ([], RuntimeError("cualquier cosa"), kft.OUTCOME_FAILED),
], ids=["no-arranca", "sin-flujo", "base-caida", "rechazo", "error"])
def test_the_turn_never_breaks_for_the_flow(rows: list[FlowEngineNativeAdmissionRow], error: Exception | None,
                                            outcome: str) -> None:
    trigger = _trigger(_Admit(rows, error))
    assert _start(trigger) == outcome
    assert trigger.status_line().startswith("enabled,")


def _setup_settings(**changes: Any) -> Any:
    class _S:
        keyword_flow_trigger_enabled = True
        chatwoot_account_id = 2
        chatwoot_inbox_id = 11
    settings = _S()
    for key, value in changes.items():
        setattr(settings, key, value)
    return settings


class _Runtime:
    instance = {"tenant_ref": "lancemos", "ally_ref": "att1"}
    native_priority_wins = False


class _Supabase:
    async def admit_flow_engine_event(self, *args: Any, **kwargs: Any) -> tuple[()]:
        return ()


def test_the_setup_needs_the_three_flags_and_fails_closed(tmp_path: Path) -> None:
    health = tmp_path / "estado.json"
    health.write_text(json.dumps({"att1": GREEN}), encoding="utf-8")
    environ = {"FLOW_AUDIENCE_META_HEALTH_PATH": str(health), "FLOW_AUDIENCE_META_HEALTH_KEY": "att1",
               "BULK_SEND_PORTFOLIO_LOCK_DIR": str(tmp_path)}
    live = object()
    assert kft.keyword_flow_trigger_setup(_setup_settings(keyword_flow_trigger_enabled=False), keyword_rules=live,
                                          flow_engine_runtime=_Runtime(), supabase=_Supabase(), environ=environ) is None
    assert kft.keyword_flow_trigger_setup(_setup_settings(), keyword_rules=None, flow_engine_runtime=_Runtime(),
                                          supabase=_Supabase(), environ=environ) is None
    assert kft.keyword_flow_trigger_setup(_setup_settings(), keyword_rules=live, flow_engine_runtime=None,
                                          supabase=_Supabase(), environ=environ) is None
    for missing in ("FLOW_AUDIENCE_META_HEALTH_PATH", "BULK_SEND_PORTFOLIO_LOCK_DIR"):
        with pytest.raises(kft.KeywordFlowConfigError, match=missing):
            kft.keyword_flow_trigger_setup(_setup_settings(), keyword_rules=live, flow_engine_runtime=_Runtime(),
                                           supabase=_Supabase(),
                                           environ={k: v for k, v in environ.items() if k != missing})
    with pytest.raises(kft.KeywordFlowConfigError, match="to exist"):
        kft.keyword_flow_trigger_setup(_setup_settings(), keyword_rules=live, flow_engine_runtime=_Runtime(),
                                       supabase=_Supabase(),
                                       environ={**environ, "BULK_SEND_PORTFOLIO_LOCK_DIR": str(tmp_path / "no")})
    with pytest.raises(kft.KeywordFlowConfigError, match="CHATWOOT_INBOX_ID"):
        kft.keyword_flow_trigger_setup(_setup_settings(chatwoot_inbox_id=None), keyword_rules=live,
                                       flow_engine_runtime=_Runtime(), supabase=_Supabase(), environ=environ)
    trigger = kft.keyword_flow_trigger_setup(_setup_settings(), keyword_rules=live, flow_engine_runtime=_Runtime(),
                                             supabase=_Supabase(), environ=environ)
    assert isinstance(trigger, kft.KeywordFlowTrigger)
    # La salud se lee del estado.json real con la clave de la instancia; sin masivo, nada frena.
    assert trigger._brake("1202555040101", datetime.now(UTC)) in (None, "meta_salud:vieja")


# ---------------------------------------------------------------------------
# El turno: la regla que se aplico va al disparador, con la conversacion y quien escribio.
# ---------------------------------------------------------------------------

class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def start(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return kft.OUTCOME_STARTED

    def status_line(self) -> str:
        return "enabled,started=1"


def test_the_turn_hands_the_rule_and_the_conversation_to_the_flow(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(kft, "keyword_flow_trigger_setup", lambda *args, **kwargs: recorder)
    shadow, _ = _turn(_settings(tmp_path), text=LINK_TEXT)
    [call] = recorder.calls
    assert call["rule"].key == "soporte" and call["rule"].rule_id == RULE["rule_id"]
    assert call["conversation_id"] == 200 and str(call["message_ref"]).isdigit()
    assert isinstance(call["destination"], str) and call["destination"].isdigit()
    # El turno sigue como con la regla sola: la nota va ultima.
    [blocks] = shadow.extra
    assert blocks[-1].kind == keyword_rules.NOTE_KIND


def test_a_turn_without_the_link_text_does_not_reach_the_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(kft, "keyword_flow_trigger_setup", lambda *args, **kwargs: recorder)
    _turn(_settings(tmp_path / "otro"), text="Hola, una consulta")
    _turn(_settings(tmp_path / "plantilla"), text=LINK_TEXT, opened_by_us=True)
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# La pagina /w/: el flujo que arranca.
# ---------------------------------------------------------------------------

FOUND = {"status": "publicada", "fingerprint": "f" * 64, "rule": RULE["rule"]}
STORE = {"flows": [
    {"product_ref": "att1", "flow_key": "soporte_bienvenida", "state": "cohorte", "active_version": 2,
     "versions": {"1": {"nombre": "Vieja", "dispara": {"palabra_clave": "soporte"}},
                  "2": {"nombre": "Bienvenida de soporte", "dispara": {"palabra_clave": "soporte"}}}},
    {"product_ref": "att1", "flow_key": "alumnas", "state": "prendido", "active_version": 1,
     "versions": {"1": {"nombre": "Alumnas", "dispara": {"palabra_clave": "alumnos"}}}},
    {"product_ref": "att1", "flow_key": "carrito", "state": "prendido", "active_version": 1,
     "versions": {"1": {"nombre": "Carrito", "dispara": {"evento": "carrito"}}}},
]}


def test_the_page_shows_the_flow_the_rule_starts() -> None:
    flows = keyword_rule_approval.flows_of_rule(STORE, "soporte")
    assert flows == [{"nombre": "Bienvenida de soporte", "producto": "att1", "flujo": "soporte_bienvenida",
                      "estado": "cohorte", "version": 2}]
    html = keyword_rule_approval.render_rule(FOUND, RULE_ID, serving="served", flows=flows)
    assert "Qué flujo arranca" in html and "Bienvenida de soporte" in html and "en prueba, solo la cohorte" in html
    assert "ni arranca ningún flujo" not in html
    empty = keyword_rule_approval.render_rule(FOUND, RULE_ID, serving="served", flows=[])
    assert "Ningún flujo arranca con esta regla" in empty


def test_without_the_flag_the_page_is_the_one_of_before() -> None:
    before = keyword_rule_approval.render_rule(FOUND, RULE_ID, serving="served")
    assert keyword_rule_approval.render_rule(FOUND, RULE_ID, serving="served", flows=None) == before
    assert "No manda ningún mensaje ni arranca ningún flujo." in before and "Qué flujo arranca" not in before


def test_the_flow_page_names_the_rule_that_starts_the_flow() -> None:
    from bridge.flow_approval import _trigger_text

    assert _trigger_text({"dispara": {"palabra_clave": "soporte"}}) == (
        "cuando alguien empieza la conversación con el link de la regla «soporte»")
    assert _trigger_text({"dispara": {"silencio": {}}}) == "cuando una conversación queda en silencio"


# ---------------------------------------------------------------------------
# La simulacion de la vista previa (deuda de #531).
# ---------------------------------------------------------------------------

INSTANCE = {"tenant_ref": "lancemos", "ally_ref": "att1"}


class _NoCounts:
    """La tienda de flow_change_api: una palabra clave no tiene conteos que leer."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def read_new_flow_counts(self, instance: Any, product: str, trigger: str, since: Any) -> Any:
        self.asked.append(trigger)
        raise AssertionError("la palabra clave no se cuenta")


def _new_keyword_item() -> dict[str, Any]:
    return {"producto": "att1", "flujo": "soporte", "tipo": "nuevo", "definicion": _keyword(), "prender": "cohorte"}


def test_a_new_keyword_flow_simulates_without_counting_and_the_page_can_publish_it() -> None:
    from bridge import flow_change_api
    from bridge.flow_approval import _simulation_summary
    from bridge.flow_change_simulation import build_new_flows_simulation

    store = _NoCounts()
    data = asyncio.run(flow_change_api._new_flow_input(store, INSTANCE, "att1", _new_keyword_item()))  # noqa: SLF001
    # Antes: {"simulation_unavailable": "simulation_new_flow_counts_invalid"}, y la pagina no publicaba.
    assert data == {"new_flow_uncounted": True} and store.asked == []
    [item] = fd.parse_items([_new_keyword_item()])
    other = fd.WorldFlow(flow_id=FLOW_ID, product_ref="att1", flow_key="bienvenida_soporte", state="cohorte",
                         active_version=1, versions={1: _keyword()}, native=True)
    built = build_new_flows_simulation([data], [item], world=fv.FlowWorld(flows=(other,)))
    assert built["status"] == "built", built
    summary = built["summary"]
    assert (summary["disparador"], summary["clave"], summary["nace_en"], summary["cohorte_al_nacer"],
            summary["alcance_al_nacer"]) == ("palabra_clave", "soporte", "cohorte", 0, 0)
    assert "disparos" not in summary and "personas" not in summary
    assert summary["comparte_el_evento_con"] == [{"flujo": "att1/bienvenida_soporte", "estado": "cohorte"}]
    html = _simulation_summary(built)
    assert "no se pudo armar" not in html and "--sin-simulacion" not in html
    assert "<strong>0</strong> contactos" in html and "<strong>soporte</strong>" in html


def test_turning_on_a_keyword_flow_simulates_without_counting() -> None:
    from bridge.flow_change_simulation import native_flow_counts_request

    store = {"flows": [{"flow_id": FLOW_ID, "product_ref": "att1", "flow_key": "soporte", "state": "cohorte",
                        "native": True, "active_version": 1, "versions": {"1": _keyword()}}]}
    raw = {"producto": "att1", "flujo": "soporte", "tipo": "prender", "flow_id": FLOW_ID}
    # Antes: {"simulation_unavailable": "simulation_native_flow_counts_invalid"}.
    assert native_flow_counts_request(store, [raw]) == {"native_uncounted": True}
    from bridge.flow_change_simulation import build_native_uncounted_simulation

    world = fv.FlowWorld(flows=(fd.WorldFlow(flow_id=FLOW_ID, product_ref="att1", flow_key="soporte",
                                             state="cohorte", active_version=1, versions={1: _keyword()},
                                             native=True),))
    built = build_native_uncounted_simulation(fd.parse_items([raw]), world=world)
    assert built["status"] == "built", built
    assert (built["summary"]["tipo"], built["summary"]["queda_en"]) == ("prender", "prendido")


def test_the_operator_script_agrees_on_what_is_not_counted() -> None:
    from test_flow_change_operator_script import script

    from bridge.flow_change_simulation import new_uncounted_flow

    bad = {**_new_keyword_item(), "definicion": _keyword(dispara={"palabra_clave": "Soporte!"})}
    registration = {**_new_keyword_item(), "definicion": _keyword(
        dispara={"evento": "intencion", "registro": "clase-28-11"})}
    short = {**_new_keyword_item(), "definicion": _keyword(dispara={"evento": "intencion", "registro": "ab"})}
    for items in ([_new_keyword_item()], [bad], [{**_new_keyword_item(), "tipo": "version"}], [],
                  [registration], [short]):
        assert script._new_uncounted_flow(items) == new_uncounted_flow(items), items  # noqa: SLF001
    assert new_uncounted_flow([registration]) == ("registro", "clase-28-11")
    assert new_uncounted_flow([short]) is None


# -- Las piezas de punta a punta (revision de las deudas de Z1) -----------------------------------------------

_VALIDATE_ENV = {"SETTER_VERSION": "1.40.1", "GIT_SHA": "0386f8b"}
_INSTANCE_IN = {"tenant_ref": "lancemos", "ally_ref": "att1", "products": ["att1"],
                "time_zone": "America/Mexico_City"}


def _keyword_store(state: str = "cohorte", key: str = "soporte", product: str = "att1",
                   flow_key: str = "soporte", flow_id: str = FLOW_ID) -> dict[str, Any]:
    return {"flow_id": flow_id, "product_ref": product, "flow_key": flow_key, "state": state, "native": True,
            "active_version": 1, "generation": 2, "changed_at": "2026-11-27T14:00:00Z",
            "changed_by_kind": "operador",
            "versions": {"1": {**_keyword(dispara={"palabra_clave": key}), "version": 1, "estado": "publicada"}}}


def _validate(inputs: dict[str, Any], items: list[dict[str, Any]], store: dict[str, Any]) -> dict[str, Any]:
    from bridge.flow_change_operator import validate_proposal

    return validate_proposal({
        **inputs, "instance": _INSTANCE_IN, "store": store,
        "proposal": {"id": "00000000-0000-4000-8000-000000000531", "revision": 1, "items": items,
                     "draft_base": {}, "purpose": "el flujo de soporte", "requested_by_kind": "automation_expert"},
        "catalog": None,
    }, environ=_VALIDATE_ENV)["preview"]["simulacion"]


def test_the_validator_builds_the_simulation_of_a_new_and_of_an_existing_keyword_flow() -> None:
    new = _validate({"new_flow_uncounted": True}, [_new_keyword_item()], {"flows": []})
    assert new["status"] == "built", new
    assert (new["summary"]["disparador"], new["summary"]["alcance_al_nacer"]) == ("palabra_clave", 0)
    raw = {"producto": "att1", "flujo": "soporte", "tipo": "prender", "flow_id": FLOW_ID}
    native = _validate({"native_uncounted": True}, [raw], {"flows": [_keyword_store()]})
    assert native["status"] == "built", native
    assert (native["summary"]["tipo"], native["summary"]["clave"]) == ("prender", "soporte")


def test_the_bridge_asks_for_no_reading_to_turn_on_a_keyword_flow() -> None:
    from bridge import flow_change_api

    raw = {"producto": "att1", "flujo": "soporte", "tipo": "prender", "flow_id": FLOW_ID}
    data = asyncio.run(flow_change_api._simulation_inputs(  # noqa: SLF001
        _NoCounts(), INSTANCE, [raw], {"flows": [_keyword_store()]}))
    assert data == {"native_uncounted": True}


def test_the_operator_script_reads_nothing_for_a_keyword_flow() -> None:
    import io

    from test_flow_change_new_flow import _ENV
    from test_flow_change_operator_script import FakeDocker, FakeTerminal, script

    docker = FakeDocker()
    ops = script.Ops(script.Config.from_env({**_ENV}), docker=docker, terminal=FakeTerminal(), out=io.StringIO(),
                     code=lambda: "abc123")
    assert ops.read_engine_inputs([_new_keyword_item()]) == {"new_flow_uncounted": True}
    raw = {"producto": "att1", "flujo": "soporte", "tipo": "prender", "flow_id": FLOW_ID}
    assert ops.read_engine_inputs([raw], {"flows": [_keyword_store()]}) == {"native_uncounted": True}
    assert script._native_flow_counts_request({"flows": [_keyword_store()]}, [raw]) == \
        {"native_uncounted": True}  # noqa: SLF001
    assert not [stdin for _, stdin in docker.calls if "read_flow_engine_native_trigger_counts_v1" in stdin]


def _world_flow(state: str = "cohorte", key: str = "soporte", product: str = "att1", flow_key: str = "soporte",
                flow_id: str = FLOW_ID) -> fd.WorldFlow:
    return fd.WorldFlow(flow_id=flow_id, product_ref=product, flow_key=flow_key, state=state, active_version=1,
                        versions={1: _keyword(dispara={"palabra_clave": key})}, native=True)


def test_the_same_rule_is_shared_across_products_and_a_flow_does_not_share_with_itself() -> None:
    from bridge.flow_change_simulation import build_native_uncounted_simulation

    other_id = "0bf45aa4-4dfe-4ab7-88ed-49d2f25af3a3"
    third_id = "0bf45aa4-4dfe-4ab7-88ed-49d2f25af3a4"
    world = fv.FlowWorld(flows=(_world_flow(), _world_flow("prendido", product="maribel", flow_key="otro",
                                                           flow_id=other_id),
                                _world_flow("prendido", key="ventas", flow_key="ventas", flow_id=third_id)))
    raw = {"producto": "att1", "flujo": "soporte", "tipo": "prender", "flow_id": FLOW_ID}
    built = build_native_uncounted_simulation(fd.parse_items([raw]), world=world)
    assert built["status"] == "built", built
    # D-39.16: la misma regla en otro producto comparte; la otra regla no; el flujo mismo tampoco.
    assert built["summary"]["comparte_el_evento_con"] == [{"flujo": "maribel/otro", "estado": "prendido"}]


def test_a_keyword_version_says_the_reach_and_the_structure_without_counting() -> None:
    from bridge.flow_approval import _simulation_summary
    from bridge.flow_change_simulation import build_native_uncounted_simulation

    world = fv.FlowWorld(flows=(_world_flow("prendido"),))
    changed = _keyword(dispara={"palabra_clave": "soporte"}, nombre="Soporte nuevo")
    raw = {"producto": "att1", "flujo": "soporte", "tipo": "version", "flow_id": FLOW_ID, "definicion": changed,
           "en_curso": "seguir"}
    built = build_native_uncounted_simulation(fd.parse_items([raw]), world=world)
    assert built["status"] == "built", built
    summary = built["summary"]
    assert (summary["tipo"], summary["queda_en"], summary["alcance"]) == ("version", "prendido", "todos")
    assert "disparos" not in summary and "comparte_el_evento_con" not in summary
    html = _simulation_summary(built)
    assert "le llega a todos" in html
    # El flujo ya existe: el porque no dice que falta un flujo de la regla.
    assert "mientras no hay un flujo de la regla" not in html
    assert "no cuenta los arranques por palabra clave" in html
    cohort = build_native_uncounted_simulation(fd.parse_items([raw]), world=fv.FlowWorld(flows=(_world_flow(),)))
    assert "solo le llega a la cohorte" in _simulation_summary(cohort)


def test_two_new_flows_with_the_same_rule_name_each_other() -> None:
    from bridge.flow_change_simulation import build_new_flows_simulation

    second = {**_new_keyword_item(), "flujo": "soporte_dos"}
    third = {**_new_keyword_item(), "flujo": "ventas", "definicion": _keyword(dispara={"palabra_clave": "ventas"})}
    items = fd.parse_items([_new_keyword_item(), second, third])
    built = build_new_flows_simulation([{"new_flow_uncounted": True}] * 3, items, world=fv.FlowWorld(flows=()))
    assert built["status"] == "built", built
    first, two, other = built["summary"]["flujos"]
    # D-39.16: con un mensaje arranca uno solo; la otra regla no es gemela.
    assert first["comparte_el_evento_con"] == [{"flujo": "att1/soporte_dos", "estado": "nuevo"}]
    assert two["comparte_el_evento_con"] == [{"flujo": "att1/soporte", "estado": "nuevo"}]
    assert "comparte_el_evento_con" not in other
