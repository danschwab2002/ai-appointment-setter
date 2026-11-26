"""5.4 (agent-global-pause-v1): las herramientas del AE para pausar al agente comercial.

Quien pide la pausa lo pone el plugin desde la sesion de Slack (nunca el
modelo); que sea aprobador lo decide el bridge (`not_an_approver`). Sin el flag
del bridge las rutas no existen y la herramienta lo dice. Las respuestas del
bridge son las de ``agent_global_pause.register_agent_pause_routes``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from test_automation_expert_approval_button import _Server, world  # noqa: F401

PATH = "/internal/automation-expert/v1/agent-pause"
PAUSE_ID = "0000000a-0000-4000-8000-000000005401"
VIEW = {"pause_id": PAUSE_ID, "alcance": "todas", "valor": None, "etiqueta": None, "estado": "activa",
        "desde": "2026-11-26T14:00:00Z", "hasta": None, "motivo": "Vivo de las 19", "pedida_por": "U0123456789",
        "levantada_at": None, "levantada_por": None, "motivo_al_levantar": None,
        "conversaciones_retenidas": 0, "mensajes_retenidos": 0}


def _envelope(data: dict) -> dict:
    return {"contract": "agent-global-pause-v1", "contract_version": "1.0.0", "data": data}


def test_pause_sends_the_slack_identity_and_returns_the_pause(world: SimpleNamespace) -> None:  # noqa: F811
    _Server.routes[f"POST {PATH}"] = (201, _envelope({"pause": VIEW, "replayed": False, "warnings": []}))
    out = json.loads(world.tools.pausar_agente({"alcance": "todas", "motivo": " Vivo de las 19 "}))
    assert out == {"pause": VIEW, "replayed": False, "warnings": []}
    [seen] = _Server.seen
    assert json.loads(seen["body"]) == {"alcance": "todas", "motivo": "Vivo de las 19",
                                        "requested_by": {"slack_user": "U0123456789"}}


def test_a_label_pause_with_until(world: SimpleNamespace) -> None:  # noqa: F811
    _Server.routes[f"POST {PATH}"] = (201, _envelope({"pause": VIEW, "replayed": False, "warnings": []}))
    world.tools.pausar_agente({"alcance": "etiqueta", "valor": "black", "hasta": "2026-11-29T21:00:00-06:00",
                               "motivo": "Corte"})
    [seen] = _Server.seen
    assert json.loads(seen["body"])["valor"] == "black"
    assert json.loads(seen["body"])["hasta"] == "2026-11-29T21:00:00-06:00"


@pytest.mark.parametrize("params", [
    {"alcance": "todas"},
    {"alcance": "algunas", "motivo": "x"},
    {"alcance": "todas", "motivo": "x", "requested_by": {"slack_user": "U0OTRO00001"}},
])
def test_a_bad_request_does_not_reach_the_bridge(world: SimpleNamespace, params: dict) -> None:  # noqa: F811
    assert json.loads(world.tools.pausar_agente(params)) == {"error": "invalid_body"}
    assert _Server.seen == []


def test_a_motive_with_a_phone_does_not_reach_the_bridge(world: SimpleNamespace) -> None:  # noqa: F811
    out = json.loads(world.tools.pausar_agente({"alcance": "todas", "motivo": "avisale al 11 5555 1234"}))
    assert out == {"error": "identifier_not_allowed"}
    assert _Server.seen == []


@pytest.mark.parametrize(("status", "detail"), [
    (403, "not_an_approver"), (422, "agent_pause_product_without_label"), (422, "chatwoot_label_not_found"),
    (409, "agent_pause_open_limit"), (422, "agent_pause_until_invalid"),
])
def test_the_bridge_refusals_come_back_as_codes(world: SimpleNamespace, status: int, detail: str) -> None:  # noqa: F811
    _Server.routes[f"POST {PATH}"] = (status, {"detail": detail})
    assert json.loads(world.tools.pausar_agente({"alcance": "todas", "motivo": "x"})) == {"error": detail}


def test_without_the_bridge_flag_it_says_so(world: SimpleNamespace) -> None:  # noqa: F811
    _Server.routes[f"POST {PATH}"] = (404, {"detail": "Not Found"})
    _Server.routes[f"GET {PATH}"] = (404, {"detail": "Not Found"})
    assert json.loads(world.tools.pausar_agente({"alcance": "todas", "motivo": "x"})) == {
        "error": "agent_pause_not_enabled"}
    assert json.loads(world.tools.estado_pausa({})) == {"error": "agent_pause_not_enabled"}


def test_resume_and_state(world: SimpleNamespace) -> None:  # noqa: F811
    lifted = {**VIEW, "estado": "levantada", "levantada_por": "U0123456789"}
    _Server.routes[f"POST {PATH}/{PAUSE_ID}/resume"] = (200, _envelope({"pause": lifted, "replayed": False}))
    _Server.routes[f"GET {PATH}"] = (200, _envelope({"pausado": False, "vivas": [], "recientes": [lifted],
                                                     "productos": [], "este_bridge": "none,refresh=ok"}))
    out = json.loads(world.tools.reanudar_agente({"pause_id": PAUSE_ID, "motivo": "Termino"}))
    assert out["pause"]["estado"] == "levantada"
    assert json.loads(_Server.seen[0]["body"]) == {"motivo": "Termino", "requested_by": {"slack_user": "U0123456789"}}
    assert json.loads(world.tools.estado_pausa({}))["pausado"] is False
    assert json.loads(world.tools.reanudar_agente({"pause_id": "x"})) == {"error": "invalid_pause_id"}


def test_outside_slack_there_is_no_requester(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "cli")
    assert json.loads(world.tools.pausar_agente({"alcance": "todas", "motivo": "x"})) == {
        "error": "requested_by_unavailable"}
    assert json.loads(world.tools.reanudar_agente({"pause_id": PAUSE_ID})) == {"error": "requested_by_unavailable"}
    assert _Server.seen == []
