"""Las herramientas de la exportacion en el plugin de cambios del AE (contact-export-v1, funcion 1.3).

Contra el bridge falso de ``test_automation_expert_change_plugin`` (un servidor HTTP local que contesta con
la forma de las rutas y anota lo que recibe). Las rutas de verdad las prueba ``test_contact_export``.
"""

from __future__ import annotations

import json

import pytest
from test_automation_expert_change_plugin import PREFIX, _Bridge, _envelope, _load_plugin, _tools, bridge  # noqa: F401

EXPORT = "0000000f-0000-4000-8000-0000000e0001"
EXPORTS = f"{PREFIX}/contact-exports"
ASK = {"filtros": {"compro": False, "etiquetas": ["black-2026"]}, "columnas": ["nombre", "telefono", "email"]}


def test_export_sends_who_asks_from_the_session_and_only_the_filters(bridge: type[_Bridge]) -> None:  # noqa: F811
    bridge.routes[f"POST {EXPORTS}"] = (201, _envelope({"export_id": EXPORT, "matched_count": 12,
                                                        "page_url": f"https://x.test/flow-approval/exportar/{EXPORT}"}))
    result = json.loads(_tools().export_contacts(dict(ASK)))
    assert result["data"]["matched_count"] == 12
    sent = json.loads(str(bridge.seen[0]["body"]))
    assert sent == {**ASK, "requested_by": {"slack_user": "U0123456789"}}
    assert bridge.seen[0]["path"] == EXPORTS and bridge.seen[0]["method"] == "POST"


@pytest.mark.parametrize(("params", "error"), [
    ({**ASK, "telefonos": ["5215512345678"]}, "invalid_body"),
    ({**ASK, "requested_by": {"slack_user": "UOTRAPERSONA"}}, "invalid_body"),
    ({"filtros": "los que compraron"}, "contact_export_filter_invalid"),
    ({"filtros": {"etiquetas": ["55 1234 5678"]}}, "identifier_not_allowed"),
    ({"filtros": {"producto": "ana@example.test"}}, "identifier_not_allowed"),
])
def test_a_phone_or_a_wrong_request_never_reach_the_network(bridge: type[_Bridge], params: dict,  # noqa: F811
                                                            error: str) -> None:
    assert json.loads(_tools().export_contacts(params)) == {"error": error}
    assert bridge.seen == []


def test_outside_a_slack_session_nothing_is_asked(bridge: type[_Bridge],  # noqa: F811
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "cli")
    assert json.loads(_tools().export_contacts(dict(ASK))) == {"error": "requested_by_unavailable"}
    assert bridge.seen == []


def test_the_read_and_the_bridge_codes(bridge: type[_Bridge]) -> None:  # noqa: F811
    bridge.routes[f"GET {EXPORTS}/{EXPORT}"] = (200, _envelope({"export_id": EXPORT, "state": "bajado"}))
    tools = _tools()
    assert json.loads(tools.get_contact_export({"export_id": EXPORT}))["data"]["state"] == "bajado"
    assert json.loads(tools.get_contact_export({"export_id": "no"})) == {"error": "invalid_contact_export_id"}
    bridge.routes[f"POST {EXPORTS}"] = (503, {"detail": "contact_export_labels_unavailable"})
    assert json.loads(tools.export_contacts({})) == {"error": "contact_export_labels_unavailable"}
    bridge.routes[f"POST {EXPORTS}"] = (422, {"detail": "contact_export_filter_invalid",
                                              "reason": {"field": "producto", "problem": "needs_compro_or_formulario"}})
    assert json.loads(tools.export_contacts({"filtros": {"producto": "att1"}})) == {
        "error": "contact_export_filter_invalid",
        "reason": {"field": "producto", "problem": "needs_compro_or_formulario"}}


def test_a_bridge_without_the_flag_says_so(bridge: type[_Bridge]) -> None:  # noqa: F811
    for path in (f"POST {EXPORTS}", f"GET {EXPORTS}/{EXPORT}"):
        bridge.routes[path] = (404, {"detail": "Not Found"})
    tools = _tools()
    assert json.loads(tools.export_contacts(dict(ASK))) == {"error": "contact_export_not_enabled"}
    assert json.loads(tools.get_contact_export({"export_id": EXPORT})) == {"error": "contact_export_not_enabled"}


def test_no_tool_hands_out_personal_data() -> None:
    schemas = __import__(_load_plugin().__name__ + ".schemas", fromlist=["EXPORT_CONTACTS"])
    text = json.dumps([schemas.EXPORT_CONTACTS, schemas.GET_CONTACT_EXPORT], ensure_ascii=False)
    assert "nunca pegues ni pidas teléfonos" in text and "No manda nada a nadie" in text
