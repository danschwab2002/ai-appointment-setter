"""Las herramientas del masivo en el plugin de cambios del AE (encargo 6 del masivo manejado por el AE).

Contra el bridge falso de ``test_automation_expert_change_plugin`` (un servidor HTTP local que contesta con
la forma de las rutas y anota lo que recibe). Las rutas de verdad las prueba ``test_bulk_send_ae_routes``.
"""

from __future__ import annotations

import json

import pytest
from test_automation_expert_change_plugin import PREFIX, _Bridge, _envelope, _load_plugin, _tools, bridge  # noqa: F401

SEND = "6e1f9a0b-2c3d-4e5f-8a9b-0c1d2e3f4a5b"
BULK = f"{PREFIX}/bulk-sends"
DRAFT = {"template_name": "att1_carrito_abandonado_01", "template_variables": [{"de": "nombre"}, "el Protocolo"],
         "scheduled_at": "2026-11-29T19:00:00-06:00", "campaign_label": "vivo-29-11"}


def test_propose_sends_who_asks_from_the_session_and_only_the_draft(bridge: type[_Bridge]) -> None:  # noqa: F811
    bridge.routes[f"POST {BULK}"] = (201, _envelope({"send_id": SEND, "state": "borrador",
                                                     "upload_url": f"https://x.test/flow-approval/masivos/{SEND}"}))
    result = json.loads(_tools().propose_bulk_send(dict(DRAFT)))
    assert result["data"]["upload_url"].endswith(f"/flow-approval/masivos/{SEND}")
    sent = json.loads(str(bridge.seen[0]["body"]))
    assert sent == {**DRAFT, "requested_by": {"slack_user": "U0123456789"}}
    assert bridge.seen[0]["path"] == BULK and bridge.seen[0]["method"] == "POST"


def test_without_the_opt_out_button_the_tool_says_what_to_offer(bridge: type[_Bridge]) -> None:  # noqa: F811
    bridge.routes[f"POST {BULK}"] = (422, {"detail": "bulk_send_template_without_opt_out",
                                           "reason": {"field": "template_name", "problem": "no_opt_out_quick_reply"}})
    result = json.loads(_tools().propose_bulk_send(dict(DRAFT)))
    assert result["error"] == "bulk_send_template_without_opt_out"
    assert result["reason"] == {"field": "template_name", "problem": "no_opt_out_quick_reply"}
    assert result["next"]["skill"] == "pedir-plantilla-a-meta" and "No más mensajes" in result["next"]["note"]


@pytest.mark.parametrize(("params", "error"), [
    ({**DRAFT, "phones": ["5215512345678"]}, "invalid_body"),
    ({**DRAFT, "list": "a,b"}, "invalid_body"),
    ({**DRAFT, "requested_by": {"slack_user": "UOTRAPERSONA"}}, "invalid_body"),
    ({"scheduled_at": DRAFT["scheduled_at"]}, "invalid_body"),
    ({"template_name": "x"}, "invalid_body"),
    ({**DRAFT, "template_variables": [{"de": "nombre"}, "escribile al 55 1234 5678"]}, "identifier_not_allowed"),
    ({**DRAFT, "template_variables": "nombre"}, "bulk_send_variables_invalid"),
])
def test_a_list_a_phone_or_a_wrong_draft_never_reach_the_network(bridge: type[_Bridge], params: dict,  # noqa: F811
                                                                  error: str) -> None:
    assert json.loads(_tools().propose_bulk_send(params)) == {"error": error}
    assert bridge.seen == []


def test_outside_a_slack_session_nothing_is_proposed(bridge: type[_Bridge],  # noqa: F811
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "cli")
    assert json.loads(_tools().propose_bulk_send(dict(DRAFT))) == {"error": "requested_by_unavailable"}
    assert bridge.seen == []


def test_the_reads_go_to_their_routes(bridge: type[_Bridge]) -> None:  # noqa: F811
    bridge.routes[f"GET {BULK}/{SEND}"] = (200, _envelope({"send_id": SEND, "state": "borrador"}))
    bridge.routes[f"GET {BULK}?limit=5"] = (200, _envelope({"bulk_sends": []}))
    tools = _tools()
    assert json.loads(tools.get_bulk_send({"send_id": SEND}))["data"]["state"] == "borrador"
    assert json.loads(tools.list_bulk_sends({"limit": 5}))["data"] == {"bulk_sends": []}
    assert json.loads(tools.get_bulk_send({"send_id": "no-es-uuid"})) == {"error": "invalid_bulk_send_id"}
    for limit in (0, 51, "5", True):
        assert json.loads(tools.list_bulk_sends({"limit": limit})) == {"error": "invalid_query"}
    assert [seen["path"] for seen in bridge.seen] == [f"{BULK}/{SEND}", f"{BULK}?limit=5"]


def test_a_bridge_without_the_flag_says_so(bridge: type[_Bridge]) -> None:  # noqa: F811
    for path in (f"POST {BULK}", f"GET {BULK}/{SEND}", f"GET {BULK}?limit=20"):
        bridge.routes[path] = (404, {"detail": "Not Found"})
    tools = _tools()
    assert json.loads(tools.propose_bulk_send(dict(DRAFT))) == {"error": "bulk_send_not_enabled"}
    assert json.loads(tools.get_bulk_send({"send_id": SEND})) == {"error": "bulk_send_not_enabled"}
    assert json.loads(tools.list_bulk_sends({})) == {"error": "bulk_send_not_enabled"}


def test_a_missing_send_is_not_the_flag(bridge: type[_Bridge]) -> None:  # noqa: F811
    bridge.routes[f"GET {BULK}/{SEND}"] = (404, {"detail": "bulk_send_not_found"})
    assert json.loads(_tools().get_bulk_send({"send_id": SEND})) == {"error": "bulk_send_not_found"}


def test_the_schemas_ask_for_no_list_and_no_phone() -> None:
    schemas = __import__(_load_plugin().__name__ + ".schemas", fromlist=["x"])
    propose = schemas.PROPOSE_BULK_SEND
    assert propose["parameters"]["required"] == ["template_name", "scheduled_at"]
    assert propose["parameters"]["additionalProperties"] is False
    properties = set(propose["parameters"]["properties"])
    assert properties == {"template_name", "template_language", "template_variables", "header_image_url",
                          "scheduled_at", "campaign_label", "product_refs", "window_mode"}
    assert not any(word in name for name in properties for word in ("phone", "telefono", "list", "file", "csv"))
    text = propose["description"]
    assert "No más mensajes" in text and "pedir-plantilla-a-meta" in text and "upload_url" in text
    assert "Nunca pidas la lista, un teléfono ni un mail" in text
    assert "No confirma, no arranca, no frena" in text
    for schema in (schemas.GET_BULK_SEND, schemas.LIST_BULK_SENDS):
        assert "Nunca trae un teléfono" in schema["description"] and "Solo lee" in schema["description"]


def test_the_skill_derives_the_list_from_elsewhere_and_never_confirms() -> None:
    from pathlib import Path

    from bridge import flow_change_api

    skills = Path(__file__).resolve().parents[1] / "profiles" / "automation-expert" / "skills" / "flujos"
    scheduled = (skills / "programar-un-envio" / "SKILL.md").read_text(encoding="utf-8")
    line = next(x for x in scheduled.splitlines() if "de otro lado" in x)
    assert "`envio-masivo`" in line and "no se puede" not in line
    text = (skills / "envio-masivo" / "SKILL.md").read_text(encoding="utf-8")
    for needed in ("`propose_bulk_send`", "`get_bulk_send`", "`list_bulk_sends`", "`upload_url`",
                   "`bulk_send_template_without_opt_out`", "`pedir-plantilla-a-meta`", "`bulk_send_not_enabled`",
                   "**Vos nunca lo confirmás, nunca lo arrancás y nunca lo frenás**", "«No más mensajes»",
                   "Nunca pidas el archivo, un teléfono ni un mail", "**Miralo en la primera ronda**"):
        assert needed in text, needed
    # Los numeros de la guia son los del codigo.
    assert flow_change_api.BULK_SCHEDULE_MIN_AHEAD.total_seconds() == 3600 and "al menos **una hora**" in text
    assert flow_change_api.BULK_SCHEDULE_MAX_AHEAD.days == 30 and "30 días" in text
    assert "2,5 personas por segundo" in text  # rate_millis 2500 por defecto en bulk_sends (20261125000200)
    assert round(4000 / 2.5 / 60) == 27 and "unos 27 minutos" in text
