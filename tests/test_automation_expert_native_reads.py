"""automation-expert-read-v1 1.2.0: el AE ve el motor nativo y las ventas por flujo.

La base se simula con la salida real de las tres lecturas nuevas (migracion
20261025000100), capturada por
tests/sql/followup_engine/validate_automation_expert_native_reads.mjs
--emit-fixture sobre la cadena completa de migraciones
(tests/fixtures/automation_expert_native/). El resto, con los fixtures de las
pruebas de la 1a (tests/test_automation_expert_read_app.py).
"""

from __future__ import annotations

import json
from pathlib import Path
import re

from bridge.automation_expert_read import CONTRACT_VERSION, attach_sales, build_native_flows
from test_automation_expert_read_app import AUTH, FakeChatwoot, FakeReader, _client, _lead_a, _rpc

NATIVE = Path(__file__).parent / "fixtures" / "automation_expert_native"


def _native(name: str) -> dict[str, object]:
    return json.loads((NATIVE / f"{name}.json").read_text())["value"]


def _reader(**overrides: object) -> FakeReader:
    params: dict[str, object] = {
        "native_flows": _native("rpc_native_flows"),
        "native_history": _native("rpc_native_history"),
        "sales": _native("rpc_flow_sales"),
    }
    params.update(overrides)
    return FakeReader(**params)  # type: ignore[arg-type]


def _flows_body(reader: FakeReader) -> dict[str, object]:
    response = _client(reader).get("/internal/automation-expert/v1/flows", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_contract_is_1_2_0() -> None:
    assert CONTRACT_VERSION == "1.2.0"


def test_list_flows_shows_the_native_flow_with_its_cohort_and_sales() -> None:
    reader = _reader()
    body = _flows_body(reader)
    assert body["contract_version"] == "1.2.0"
    assert body["source"] == "mixed"
    flows = {flow["flow_key"]: flow for flow in body["data"]["flows"]}
    # Los cinco de siempre siguen, y la bienvenida se suma.
    assert {"precheckout", "carrito", "pago_fallido", "reactivacion", "descuento"} < set(flows)
    welcome = flows["bienvenida"]
    assert welcome["native"] is True
    assert welcome["source"] == "stored_definition"
    assert welcome["status"] == {"state": "cohort", "detail": "cohorte", "since": welcome["status"]["since"]}
    assert welcome["version"] == {"definition": 1}
    assert welcome["cohort"] == {"members": 1, "max_members": 5, "applies": True}
    assert welcome["observed"]["runs_started"] == 1 and welcome["observed"]["accepted_by_chatwoot"] == 1
    assert welcome["usage"]["pool"] == "att1-bienvenida"
    assert welcome["definition"]["dispara"] == {"evento": "compra"}
    assert welcome["provenance"]["/status"] == {"from": "database", "ref": "flow_controls"}
    # Las ventas, con la regla dicha.
    assert welcome["sales"]["purchases_after_send"] == 1
    assert welcome["sales"]["by_identity"] == 1 and welcome["sales"]["by_case"] == 0
    assert welcome["sales"]["rule"] == "purchase_after_accepted_send_v1"
    assert welcome["sales"]["by_step"] == {"first_contact": 1}
    # Las ventas se piden para los ultimos 7 dias.
    assert len(reader.sales_since) == 1
    # Los flujos del legado no traen ventas (no corrieron en el motor en este fixture).
    assert "sales" not in flows["carrito"]
    assert "native" not in flows["carrito"]


def test_one_native_flow_by_its_key() -> None:
    response = _client(_reader()).get("/internal/automation-expert/v1/flows/bienvenida", headers=AUTH)
    assert response.status_code == 200, response.text
    assert [flow["flow_key"] for flow in response.json()["data"]["flows"]] == ["bienvenida"]
    missing = _client(_reader()).get("/internal/automation-expert/v1/flows/no_existe", headers=AUTH)
    assert missing.status_code == 404


def test_without_native_flows_the_answer_is_the_one_of_today() -> None:
    """ATT1 hoy: sin flujos nativos, los flujos y los huecos son los de la 1.1.0."""
    before = _flows_body(FakeReader())
    assert before["source"] == "projection"
    assert [flow["flow_key"] for flow in before["data"]["flows"]] == [
        "precheckout", "carrito", "pago_fallido", "reactivacion", "descuento"]
    assert not any("sales" in flow for flow in before["data"]["flows"])
    assert not {gap["code"] for gap in before["gaps"]} & {"native_flows_not_read", "flow_sales_not_read"}


def test_a_base_without_the_migration_leaves_its_gap_and_the_old_flows() -> None:
    from bridge.supabase import SupabaseError

    body = _flows_body(FakeReader(native_failure=SupabaseError("automation_expert_native_flows_failed: HTTP 404")))
    assert [flow["flow_key"] for flow in body["data"]["flows"]] == [
        "precheckout", "carrito", "pago_fallido", "reactivacion", "descuento"]
    codes = {gap["code"] for gap in body["gaps"]}
    assert {"native_flows_not_read", "flow_sales_not_read"} <= codes


def test_the_history_shows_the_native_run_and_the_cohort() -> None:
    response = _client(_reader(), FakeChatwoot({})).get(
        f"/internal/automation-expert/v1/leads/{_lead_a()}/history", headers=AUTH)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    native = [moment for moment in data["past"] if moment["flow_key"] == "bienvenida"]
    assert [moment["kind"] for moment in native] == [
        "entered_flow", "scheduled", "start_authorized", "accepted_by_chatwoot"]
    assert all(moment["flow_version"]["definition"] == 1 for moment in native)
    assert native[0]["explanation"].startswith("Arrancó el recorrido del flujo nativo")
    assert native[0]["evidence"]["table"] == "flow_engine_runs"
    assert data["native"] == {"cohort_of": ["bienvenida"]}
    codes = {gap["code"] for gap in response.json()["gaps"]}
    assert "native_upcoming_not_projected" in codes
    assert "unknown_reason_code" not in codes
    # Los momentos del legado no cambian de forma: su flow_version no trae definition.
    legacy = [moment for moment in data["past"] if moment["flow_key"] not in (None, "bienvenida")]
    assert legacy and all("definition" not in (moment["flow_version"] or {}) for moment in legacy)


def test_the_history_without_native_runs_is_the_one_of_today() -> None:
    response = _client(FakeReader(), FakeChatwoot({})).get(
        f"/internal/automation-expert/v1/leads/{_lead_a()}/history", headers=AUTH)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["native"] == {"cohort_of": []}
    assert all(moment["flow_key"] != "bienvenida" for moment in data["past"])


def test_the_history_without_the_native_read_leaves_its_gap() -> None:
    from bridge.supabase import SupabaseError

    response = _client(FakeReader(native_failure=SupabaseError("x")), FakeChatwoot({})).get(
        f"/internal/automation-expert/v1/leads/{_lead_a()}/history", headers=AUTH)
    assert response.status_code == 200, response.text
    assert "native_flows_not_read" in {gap["code"] for gap in response.json()["gaps"]}


def test_a_native_flow_with_a_legacy_key_or_bad_state_is_rejected() -> None:
    import pytest

    from bridge.automation_expert_read import InvalidReadEvidence

    raw = _native("rpc_native_flows")
    bad = json.loads(json.dumps(raw))
    bad["flows"][0]["flow_key"] = "carrito"
    with pytest.raises(InvalidReadEvidence):
        build_native_flows(bad)
    bad = json.loads(json.dumps(raw))
    bad["flows"][0]["state"] = "armed"
    with pytest.raises(InvalidReadEvidence):
        build_native_flows(bad)


def test_sales_of_a_legacy_flow_carry_the_motor_only_gap() -> None:
    sales = _native("rpc_flow_sales")
    cart = json.loads(json.dumps(sales["flows"][0]))
    cart.update({"flow_key": "carrito", "native": False})
    flows: list[dict[str, object]] = [{"flow_key": "carrito", "provenance": {}}]
    gaps = attach_sales(flows, {**sales, "flows": [cart]})
    assert flows[0]["sales"]["rule"] == "purchase_after_accepted_send_v1"  # type: ignore[index]
    assert [gap["code"] for gap in gaps] == ["flow_sales_motor_only"]


def test_the_native_fixtures_carry_nothing_of_a_person() -> None:
    history = _rpc("rpc_history_lead_a.json")
    text = "".join((NATIVE / f"{name}.json").read_text()
                   for name in ("rpc_native_flows", "rpc_native_history", "rpc_flow_sales"))
    assert "@" not in text
    assert "1202555" not in text
    assert str(history["lead"]["lead_ref"]) not in text  # type: ignore[index]


# ------------------------------------------------- prender con a: cohorte (1.8.0)

FLOW_ID = "7fee6d5f-d98f-457c-8ff7-f33b7fe7b099"


def test_prender_carries_a_cohorte_and_nothing_else() -> None:
    import pytest

    from bridge.flow_definition import ProposalItem, ProposalItemsInvalid

    item = ProposalItem.from_json({"producto": "att1", "flujo": "bienvenida", "flow_id": FLOW_ID, "tipo": "prender",
                                   "a": "cohorte"})
    assert item.to_json() == {"producto": "att1", "flujo": "bienvenida", "flow_id": FLOW_ID, "tipo": "prender",
                              "a": "cohorte"}
    plain = ProposalItem.from_json({"producto": "att1", "flujo": "bienvenida", "flow_id": FLOW_ID, "tipo": "prender"})
    assert "a" not in plain.to_json()
    for bad in ({"tipo": "prender", "a": "prendido"}, {"tipo": "apagar", "a": "cohorte"}):
        with pytest.raises(ProposalItemsInvalid):
            ProposalItem.from_json({"producto": "att1", "flujo": "bienvenida", "flow_id": FLOW_ID, **bad})


def test_the_preview_says_it_goes_back_to_the_cohort() -> None:
    from bridge.flow_change_preview import _state_after

    assert _state_after("prender", "pausado", "cohorte") == "cohorte"
    assert _state_after("prender", "pausado") == "prendido"


# ------------------------------------------------------------- la biblioteca

READ_PLUGIN = Path(__file__).resolve().parents[1] / "profiles" / "automation-expert" / "plugins" / "automation-expert-read"


def _tools():  # el modulo del plugin, sin Hermes
    import importlib.util

    spec = importlib.util.spec_from_file_location("automation_expert_read_tools_e080", READ_PLUGIN / "tools.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_read_library_gives_the_index_and_each_page() -> None:
    tools = _tools()
    index = json.loads(tools.read_library({}))
    assert index["tema"] == "indice" and index["topics"] == ["hotmart", "ghl-formularios", "chatwoot-whatsapp"]
    for topic in index["topics"]:
        page = json.loads(tools.read_library({"tema": topic}))
        assert page["tema"] == topic and page["page"].startswith("# ")
        assert f"`{topic}`" in index["page"]
    assert json.loads(tools.read_library({"tema": "../SOUL"}))["error"] == "library_topic_unknown"
    assert json.loads(tools.read_library({"tema": "indice/../../x"}))["error"] == "library_topic_unknown"


def test_the_library_matches_the_code() -> None:
    """Cada nombre que la biblioteca da por cierto existe en el codigo."""
    from bridge import hotmart
    from bridge.app import GHL_PRECHECKOUT_ADAPTER_PATH
    from bridge.automation_expert_read import META_ERROR_EXPLANATIONS
    from bridge.flow_definition import EVENTS

    pages = {path.stem: path.read_text(encoding="utf-8") for path in (READ_PLUGIN / "biblioteca").glob("*.md")}
    assert set(pages) == {"indice", "hotmart", "ghl-formularios", "chatwoot-whatsapp"}
    text = "\n".join(pages.values())
    events = set(re.findall(r"`(PURCHASE_[A-Z_]+)`", text))
    known = {hotmart.EVENT_CART_ABANDONMENT, hotmart.EVENT_PURCHASE_APPROVED, hotmart.EVENT_PURCHASE_CANCELED,
             hotmart.EVENT_PURCHASE_BILLET_PRINTED, hotmart.EVENT_PURCHASE_EXPIRED}
    assert events and events <= known, events - known
    assert f"`{hotmart.EVENT_VERSION}`" in pages["hotmart"] or hotmart.EVENT_VERSION in pages["hotmart"]
    assert GHL_PRECHECKOUT_ADAPTER_PATH in pages["ghl-formularios"]
    for code in re.findall(r"\(código (\d{6})\)|\(\w+ con (\d{6})\)", text):
        number = next(part for part in code if part)
        assert number in META_ERROR_EXPLANATIONS, number
    for trigger in re.findall(r"^- `([a-z_]+)` \(", pages["indice"], flags=re.MULTILINE):
        assert trigger in EVENTS, trigger
    # Sin datos de una persona ni de una instancia.
    assert "@" not in text
    assert not re.findall(r"\d{7,}", text)



def test_the_operator_shows_cohort_before_the_yes() -> None:
    """El aprobador ve a donde va el control antes del si: con a: cohorte, cohorte y no prendido."""
    import importlib.util
    from types import SimpleNamespace

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("flow_change_operator_script_e080", root / "scripts/flow_change_operator.py")
    import sys

    script = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[spec.name] = script  # type: ignore[union-attr]
    spec.loader.exec_module(script)  # type: ignore[union-attr]
    shown: list[tuple[str, object]] = []
    ops = SimpleNamespace(say=lambda text: None, show=lambda label, value: shown.append((label.strip(), value)))
    world = {"store": {"flows": [{"flow_id": FLOW_ID, "active_version": 1, "state": "pausado", "generation": 3,
                                  "versions": {}}]}}
    base = {"producto": "att1", "flujo": "bienvenida", "flow_id": FLOW_ID, "tipo": "prender"}
    script.show_items(ops, [{**base, "a": "cohorte"}], world)
    script.show_items(ops, [base], world)
    assert shown == [("estado despues", "cohorte"), ("estado despues", "prendido")]
