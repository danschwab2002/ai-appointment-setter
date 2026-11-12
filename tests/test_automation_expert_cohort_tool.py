"""E-486 (8d, D14 y D15): el AE suma a alguien al grupo de prueba de un flujo.

La herramienta acepta el numero de conversacion o el telefono (uno de los
dos), manda la identidad de Slack de la sesion, y nunca devuelve el telefono.
Que quien habla sea aprobador lo decide el bridge (`not_an_approver`); sin el
flag del bridge la ruta no existe y la herramienta lo dice.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from test_automation_expert_approval_button import _Server, world  # noqa: F401

PATH = "/internal/automation-expert/v1/flows/att1/bienvenida/cohort"
OK = {"contract_version": "1.0.0", "status": "added", "members": 2, "max_members": 5}
PHONE = "+52 1 55 1234 5678"


def _route(status: int, payload: object) -> None:
    _Server.routes[f"POST {PATH}"] = (status, payload)


def test_by_conversation_number(world: SimpleNamespace) -> None:  # noqa: F811
    _route(200, OK)
    out = json.loads(world.tools.add_to_test_group(
        {"product": "att1", "flow": "bienvenida", "conversation_id": 726}))
    assert out == {"status": "added", "members": 2, "max_members": 5}
    [seen] = _Server.seen
    assert json.loads(seen["body"]) == {"requested_by": {"slack_user": "U0123456789"}, "conversation_id": 726}


def test_by_phone_never_returns_it(world: SimpleNamespace) -> None:  # noqa: F811
    _route(200, {**OK, "status": "already", "phone": "5215512345678"})
    raw = world.tools.add_to_test_group({"product": "att1", "flow": "bienvenida", "phone": PHONE})
    assert json.loads(raw) == {"status": "already", "members": 2, "max_members": 5}
    assert "5512345678" not in raw
    [seen] = _Server.seen
    assert json.loads(seen["body"])["phone"] == "+5215512345678"


@pytest.mark.parametrize(
    "params",
    [
        {"product": "att1", "flow": "bienvenida"},
        {"product": "att1", "flow": "bienvenida", "conversation_id": 1, "phone": PHONE},
        {"product": "ATT1", "flow": "bienvenida", "conversation_id": 1},
        {"product": "att1", "flow": "../x", "conversation_id": 1},
    ],
)
def test_one_person_one_way_and_clean_keys(world: SimpleNamespace, params: dict) -> None:  # noqa: F811
    assert json.loads(world.tools.add_to_test_group(params)) == {"error": "invalid_body"}
    assert _Server.seen == []


@pytest.mark.parametrize("phone", ["123", "llamame", "+52 55 " + "1" * 30])
def test_a_phone_that_is_not_one_does_not_reach_the_bridge(world: SimpleNamespace, phone: str) -> None:  # noqa: F811
    out = json.loads(world.tools.add_to_test_group({"product": "att1", "flow": "bienvenida", "phone": phone}))
    assert out == {"error": "flow_cohort_phone_invalid"}
    assert _Server.seen == []


@pytest.mark.parametrize(
    "detail", ["not_an_approver", "flow_cohort_full", "flow_cohort_contact_ambiguous", "flow_cohort_needs_native_flow"]
)
def test_the_bridge_refusals_come_back_as_codes(world: SimpleNamespace, detail: str) -> None:  # noqa: F811
    _route(403 if detail == "not_an_approver" else 409, {"detail": detail})
    out = json.loads(world.tools.add_to_test_group(
        {"product": "att1", "flow": "bienvenida", "conversation_id": 9}))
    assert out == {"error": detail}


def test_without_the_bridge_flag_it_says_so(world: SimpleNamespace) -> None:  # noqa: F811
    _route(404, {"detail": "Not Found"})
    out = json.loads(world.tools.add_to_test_group(
        {"product": "att1", "flow": "bienvenida", "conversation_id": 9}))
    assert out == {"error": "cohort_tool_not_enabled"}


def test_outside_slack_there_is_no_requester(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "cli")
    out = json.loads(world.tools.add_to_test_group(
        {"product": "att1", "flow": "bienvenida", "conversation_id": 9}))
    assert out == {"error": "requested_by_unavailable"}
    assert _Server.seen == []
