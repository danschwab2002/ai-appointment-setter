"""El cliente de las funciones del motor de flujos (la T2.4, commit 3).

Contrato: ``docs/contracts/flow-engine-v1.md``, secciones 7.1 a 7.4. Las filas
salen de la base real: ``tests/fixtures/flow_engine/bridge_rpc_rows.json`` lo
escribe ``validate_flow_engine_bridge_reads_v1.mjs --emit-bridge-fixtures`` con
las RPC reales sobre la cadena de migraciones (PGlite), y la suite SQL verifica
que siga igual (``--check``). El doble no esconde los controles del real.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from bridge.supabase import (
    FLOW_ENGINE_ADMISSION_OUTCOMES,
    FlowEngineAdmissionUnavailable,
    FlowEngineRunTerminated,
    PilotBoundaryConfig,
    PilotRequestStartRejectedError,
    SupabaseClient,
    SupabaseCommittedResponseError,
    SupabaseError,
)

ROOT = Path(__file__).resolve().parents[1]
ROWS = json.loads((ROOT / "tests" / "fixtures" / "flow_engine" / "bridge_rpc_rows.json").read_text())
BOUNDARY = PilotBoundaryConfig(
    scope_key="att1-recuperacion",
    scope_version=1,
    tenant_key="att1",
    channel_provider="waba",
    channel_account_ref="chatwoot-inbox:11",
)
INSTANCE = {"tenant_ref": "att1", "ally_ref": "att1"}
ADMIT_ARGS: dict[str, Any] = {
    "webhook_event_id": "11111111-1111-4111-8111-111111111111",
    "contact_id": "22222222-2222-4222-8222-222222222222",
    "external_product_id": "6157011",
    "product_name": "Producto",
    "offer_code": "abc123",
    "abandoned_at": "2026-10-22T15:00:00+00:00",
    "chatwoot_account_id": 1,
    "chatwoot_inbox_id": 11,
    "destination_phone": "12025550101",
    "pilot_boundary": BOUNDARY,
    "legacy_policy_key": "att1-carrito-tres-toques",
    "legacy_policy_version": 3,
}


def _client(responder: Any, requests: list[httpx.Request] | None = None) -> SupabaseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return responder(request)

    return SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )


def _rows(*rows: object) -> Any:
    return lambda request: httpx.Response(200, json=list(rows))


# ------------------------------------------------------------------ admision


@pytest.mark.parametrize("outcome", sorted(FLOW_ENGINE_ADMISSION_OUTCOMES))
def test_every_admission_row_of_the_base_parses(outcome: str) -> None:
    row = ROWS["admission"][outcome]
    requests: list[httpx.Request] = []
    admission = asyncio.run(_client(_rows(row), requests).admit_flow_engine_cart(**ADMIT_ARGS))

    assert admission.outcome == outcome
    assert admission.recovery_case_id == row["recovery_case_id"]
    assert admission.created == row["created"]
    assert admission.reason == row["reason"]
    assert admission.divergence_id == row["divergence_id"]
    assert admission.divergence_first == row["divergence_first"]
    assert admission.scheduled_action_id == row["scheduled_action_id"]
    assert requests[0].url.path == "/rest/v1/rpc/admit_flow_engine_hotmart_v1"


def test_the_admission_maps_the_arguments_of_the_contract() -> None:
    requests: list[httpx.Request] = []
    asyncio.run(_client(_rows(ROWS["admission"]["legado"]), requests).admit_flow_engine_cart(
        **ADMIT_ARGS, force_legacy=True
    ))

    assert json.loads(requests[0].content) == {
        "p_webhook_event_id": ADMIT_ARGS["webhook_event_id"],
        "p_contact_id": ADMIT_ARGS["contact_id"],
        "p_external_product_id": "6157011",
        "p_product_name": "Producto",
        "p_offer_code": "abc123",
        "p_abandoned_at": "2026-10-22T15:00:00+00:00",
        "p_chatwoot_account_id": 1,
        "p_chatwoot_inbox_id": 11,
        "p_destination_phone": "12025550101",
        "p_legacy_scope_key": "att1-recuperacion",
        "p_legacy_scope_version": 1,
        "p_legacy_policy_key": "att1-carrito-tres-toques",
        "p_legacy_policy_version": 3,
        "p_force_legacy": True,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"outcome": "motor_quizas"},
        {"reason": "No Entra"},
        {"recovery_case_id": None},
        {"created": "true"},
        {"divergence_first": None},
    ],
)
def test_an_admission_row_outside_the_contract_is_a_committed_error(mutation: dict[str, object]) -> None:
    base = "legado_por_divergencia" if "divergence_first" in mutation else (
        "motor_no_arranca" if "reason" in mutation else "motor_arranco"
    )
    row = {**copy.deepcopy(ROWS["admission"][base]), **mutation}
    with pytest.raises(SupabaseCommittedResponseError):
        asyncio.run(_client(_rows(row)).admit_flow_engine_cart(**ADMIT_ARGS))


def test_a_no_start_with_case_columns_is_a_committed_error() -> None:
    row = {**ROWS["admission"]["motor_no_arranca"], "recovery_case_id": ROWS["admission"]["legado"]["recovery_case_id"]}
    with pytest.raises(SupabaseCommittedResponseError):
        asyncio.run(_client(_rows(row)).admit_flow_engine_cart(**ADMIT_ARGS))


@pytest.mark.parametrize(
    "responder",
    [
        lambda request: (_ for _ in ()).throw(httpx.ConnectTimeout("timeout", request=request)),
        lambda request: httpx.Response(503, json={"message": "unavailable"}),
        lambda request: httpx.Response(500, json={"code": "57014", "message": "canceling statement"}),
    ],
)
def test_an_admission_without_an_answer_is_unavailable(responder: Any) -> None:
    with pytest.raises(FlowEngineAdmissionUnavailable):
        asyncio.run(_client(responder).admit_flow_engine_cart(**ADMIT_ARGS))


def test_an_invalid_input_is_a_plain_error_not_unavailable() -> None:
    responder = lambda request: httpx.Response(  # noqa: E731
        400, json={"code": "22023", "message": "invalid_flow_engine_admission_parameters"}
    )
    with pytest.raises(SupabaseError) as raised:
        asyncio.run(_client(responder).admit_flow_engine_cart(**ADMIT_ARGS))
    assert not isinstance(raised.value, FlowEngineAdmissionUnavailable)


# ------------------------------------------------------------------- arranque


def _start(row: dict[str, Any]) -> Any:
    call = ROWS["request_start"]["request_started"]["_call"]
    clean = {key: value for key, value in row.items() if key != "_call"}
    return asyncio.run(_client(_rows(clean)).mark_flow_engine_request_started(
        action_id=call["action_id"] if row is ROWS["request_start"]["request_started"] else row["action_id"],
        attempt_id=call["attempt_id"] if row is ROWS["request_start"]["request_started"] else row["id"],
        worker_id="att1-dispatcher",
        lease_generation=call["lease_generation"],
        now="2026-10-22T15:00:00+00:00",
    ))


def test_request_started_is_the_attempt_of_today() -> None:
    row = ROWS["request_start"]["request_started"]
    attempt = _start(row)

    assert attempt.attempt_id == row["id"]
    assert attempt.action_id == row["action_id"]
    assert attempt.idempotency_key == row["idempotency_key"]
    assert attempt.mode == "approved_template"
    assert attempt.phase == "request_started"
    assert attempt.expected_case_version == row["expected_case_version"]


def test_rechazado_is_the_rejection_of_today() -> None:
    with pytest.raises(PilotRequestStartRejectedError) as raised:
        _start(ROWS["request_start"]["rechazado"])
    assert raised.value.reason == "flujo_pausado"


def test_rechazado_with_a_colon_reason_keeps_it() -> None:
    row = {**ROWS["request_start"]["rechazado"], "reason": "no_entra:permiso"}
    with pytest.raises(PilotRequestStartRejectedError) as raised:
        _start(row)
    assert raised.value.reason == "no_entra:permiso"


def test_terminado_is_its_own_error() -> None:
    with pytest.raises(FlowEngineRunTerminated) as raised:
        _start(ROWS["request_start"]["terminado"])
    assert raised.value.reason == "flujo_apagado"
    assert not isinstance(raised.value, PilotRequestStartRejectedError)


@pytest.mark.parametrize(
    "mutation",
    [{"outcome": "quizas"}, {"phase": "reserved"}, {"lease_generation": 99}, {"flow_authorization_id": None}],
)
def test_a_start_row_outside_the_contract_fails(mutation: dict[str, object]) -> None:
    row = {**ROWS["request_start"]["request_started"], **mutation}
    row = {key: value for key, value in row.items() if key != "_call"}
    call = ROWS["request_start"]["request_started"]["_call"]
    with pytest.raises(SupabaseError):
        asyncio.run(_client(_rows(row)).mark_flow_engine_request_started(
            action_id=call["action_id"], attempt_id=call["attempt_id"], worker_id="w",
            lease_generation=call["lease_generation"], now="2026-10-22T15:00:00+00:00",
        ))


# -------------------------------------------------------------------- contexto


@pytest.mark.parametrize("step_key", ["first_contact", "cart_second_touch", "cart_last_touch"])
def test_the_context_of_each_step_parses(step_key: str) -> None:
    row = ROWS["context"][step_key]
    context = asyncio.run(_client(_rows(row)).get_flow_engine_step_context("action"))

    assert context is not None
    assert context.step_key == step_key
    assert context.template_name == row["template_name"]
    assert context.template_language == row["template_language"]
    assert context.template_variables == tuple(row["template_variables"])
    assert context.template_button == row["template_button"]
    assert context.legacy_dispatch_ok is True


def test_no_rows_is_a_legacy_action() -> None:
    assert asyncio.run(_client(_rows()).get_flow_engine_step_context("action")) is None


def test_a_context_with_bad_variables_fails() -> None:
    row = {**ROWS["context"]["first_contact"], "template_variables": "nombre"}
    with pytest.raises(SupabaseError):
        asyncio.run(_client(_rows(row)).get_flow_engine_step_context("action"))


# --------------------------------------------------------------------- lecturas


def _object(payload: object) -> Any:
    return lambda request: httpx.Response(200, json=payload)


def test_the_routes_read_parses() -> None:
    requests: list[httpx.Request] = []
    routes = asyncio.run(_client(_object(ROWS["reads"]["routes"]), requests).read_flow_engine_routes(INSTANCE))

    assert len(routes) == 1
    assert routes[0].executor == "legado"
    assert routes[0].legacy_kind == "carrito"
    assert routes[0].legacy_scope_key == "att1-recuperacion"
    assert json.loads(requests[0].content) == {"p_instance": INSTANCE}


def test_a_route_with_an_unknown_executor_fails() -> None:
    payload = copy.deepcopy(ROWS["reads"]["routes"])
    payload["routes"][0]["executor"] = "otro"
    with pytest.raises(SupabaseError):
        asyncio.run(_client(_object(payload)).read_flow_engine_routes(INSTANCE))


def test_the_divergences_read_parses() -> None:
    requests: list[httpx.Request] = []
    read = asyncio.run(_client(_object(ROWS["reads"]["divergences"]), requests).read_flow_engine_divergences(
        INSTANCE, only_unnotified=True, limit=20
    ))

    assert read.unnotified == 1
    assert read.divergences[0].kind == "transitorio"
    assert read.divergences[0].reason == "transitorio:forzado_al_legado"
    assert json.loads(requests[0].content) == {"p_instance": INSTANCE, "p_only_unnotified": True, "p_limit": 20}


def test_the_divergences_limit_is_checked_before_the_call() -> None:
    with pytest.raises(ValueError):
        asyncio.run(_client(_object({})).read_flow_engine_divergences(INSTANCE, only_unnotified=True, limit=51))


@pytest.mark.parametrize("answer", [True, False])
def test_the_mark_returns_what_the_base_said(answer: bool) -> None:
    requests: list[httpx.Request] = []
    marked = asyncio.run(_client(_object(answer), requests).mark_flow_engine_divergence_notified(
        "d", notified_at="2026-10-22T15:00:00+00:00"
    ))
    assert marked is answer
    assert json.loads(requests[0].content) == {"p_id": "d", "p_notified_at": "2026-10-22T15:00:00+00:00"}
