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


def test_a_planned_outcome_with_a_reason_or_a_lone_divergence_is_a_committed_error() -> None:
    for row in (
        {**ROWS["admission"]["motor_arranco"], "reason": "no_entra:permiso"},
        {**ROWS["admission"]["legado_por_divergencia"], "divergence_first": None},
        {**ROWS["admission"]["legado"], "divergence_first": True},
    ):
        with pytest.raises(SupabaseCommittedResponseError):
            asyncio.run(_client(_rows(row)).admit_flow_engine_cart(**ADMIT_ARGS))
    with pytest.raises(SupabaseCommittedResponseError):
        asyncio.run(_client(_rows(ROWS["admission"]["legado"], ROWS["admission"]["legado"]))
                    .admit_flow_engine_cart(**ADMIT_ARGS))


def test_a_divergence_fall_without_its_row_still_parses() -> None:
    # Si la ruta ya no se encuentra despues del rollback del camino del motor,
    # la funcion planifica el legado sin escribir la divergencia: el caso existe.
    row = {**ROWS["admission"]["legado_por_divergencia"], "divergence_id": None, "divergence_first": None}
    admission = asyncio.run(_client(_rows(row)).admit_flow_engine_cart(**ADMIT_ARGS))
    assert admission.outcome == "legado_por_divergencia"
    assert admission.recovery_case_id == row["recovery_case_id"]


def test_a_legacy_rejection_keeps_its_two_codes() -> None:
    reason = f"{'m' * 80}:{'d' * 80}"
    row = {**ROWS["admission"]["legado_rechazo"], "reason": reason}
    assert asyncio.run(_client(_rows(row)).admit_flow_engine_cart(**ADMIT_ARGS)).reason == reason


def test_a_no_start_with_case_columns_is_a_committed_error() -> None:
    row = {**ROWS["admission"]["motor_no_arranca"], "recovery_case_id": ROWS["admission"]["legado"]["recovery_case_id"]}
    with pytest.raises(SupabaseCommittedResponseError):
        asyncio.run(_client(_rows(row)).admit_flow_engine_cart(**ADMIT_ARGS))


@pytest.mark.parametrize(
    "responder",
    [
        lambda request: (_ for _ in ()).throw(httpx.ConnectTimeout("timeout", request=request)),
        lambda request: httpx.Response(503, json={"message": "unavailable"}),
        lambda request: httpx.Response(504, text="gateway timeout"),
        lambda request: httpx.Response(500, json={"code": "57014", "message": "canceling statement"}),
        lambda request: httpx.Response(500, json={"code": "55P03", "message": "lock_not_available"}),
    ],
)
def test_an_admission_without_an_answer_is_unavailable(responder: Any) -> None:
    with pytest.raises(FlowEngineAdmissionUnavailable):
        asyncio.run(_client(responder).admit_flow_engine_cart(**ADMIT_ARGS))


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (400, {"code": "22023", "message": "invalid_flow_engine_admission_parameters"}),
        # PostgREST contesta un raise con 500: es una respuesta, no una caida.
        (500, {"code": "XX000", "message": "internal"}),
        (500, {"code": "55000", "message": "pilot_scope_rejected"}),
        (500, "no es json"),
    ],
)
def test_an_error_of_the_function_is_a_plain_error_not_unavailable(status: int, body: object) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

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
    [
        {"outcome": "quizas"}, {"phase": "reserved"}, {"lease_generation": 99},
        {"flow_authorization_id": None}, {"id": "otro-intento"}, {"action_id": "otra-accion"},
        {"authorization_replayed": "no"},
    ],
)
def test_a_start_row_outside_the_contract_is_a_committed_error(mutation: dict[str, object]) -> None:
    # La funcion confirma antes de contestar: una respuesta que no se entiende
    # es comprometida, como en el arranque del legado, para que el despachador
    # no deje el intento varado en request_started.
    row = {**ROWS["request_start"]["request_started"], **mutation}
    row = {key: value for key, value in row.items() if key != "_call"}
    call = ROWS["request_start"]["request_started"]["_call"]
    with pytest.raises(SupabaseCommittedResponseError):
        asyncio.run(_client(_rows(row)).mark_flow_engine_request_started(
            action_id=call["action_id"], attempt_id=call["attempt_id"], worker_id="w",
            lease_generation=call["lease_generation"], now="2026-10-22T15:00:00+00:00",
        ))


def test_a_start_with_two_rows_or_a_bad_reason_is_a_committed_error() -> None:
    call = ROWS["request_start"]["request_started"]["_call"]
    row = {key: value for key, value in ROWS["request_start"]["request_started"].items() if key != "_call"}
    for payload in ([row, row], [{**ROWS["request_start"]["rechazado"], "reason": "Con Espacios"}]):
        with pytest.raises(SupabaseCommittedResponseError):
            asyncio.run(_client(lambda request, p=payload: httpx.Response(200, json=p))
                        .mark_flow_engine_request_started(
                action_id=call["action_id"], attempt_id=call["attempt_id"], worker_id="w",
                lease_generation=call["lease_generation"], now="2026-10-22T15:00:00+00:00",
            ))


def test_a_start_that_failed_by_http_is_a_plain_error() -> None:
    call = ROWS["request_start"]["request_started"]["_call"]
    with pytest.raises(SupabaseError) as raised:
        asyncio.run(_client(lambda request: httpx.Response(500, json={"code": "XX000"}))
                    .mark_flow_engine_request_started(
            action_id=call["action_id"], attempt_id=call["attempt_id"], worker_id="w",
            lease_generation=call["lease_generation"], now="2026-10-22T15:00:00+00:00",
        ))
    assert not isinstance(raised.value, SupabaseCommittedResponseError)


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


@pytest.mark.parametrize(
    "changes",
    [{"template_variables": "nombre"}, {"template_variables": ["nombre", ""]},
     {"template_button": "checkout"}, {"template_button": {"link": 1}}, {"version": 0}],
)
def test_a_context_outside_the_contract_fails(changes: dict[str, object]) -> None:
    row = {**ROWS["context"]["first_contact"], **changes}
    with pytest.raises(SupabaseError):
        asyncio.run(_client(_rows(row)).get_flow_engine_step_context("action"))


def test_two_contexts_fail() -> None:
    row = ROWS["context"]["first_contact"]
    with pytest.raises(SupabaseError):
        asyncio.run(_client(_rows(row, row)).get_flow_engine_step_context("action"))


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


@pytest.mark.parametrize(
    "changes",
    [{"generation": -1}, {"legacy_scope_key": 7}, {"flow_state": "otro"}],
)
def test_a_route_outside_the_contract_fails(changes: dict[str, object]) -> None:
    payload = copy.deepcopy(ROWS["reads"]["routes"])
    payload["routes"][0].update(changes)
    with pytest.raises(SupabaseError):
        asyncio.run(_client(_object(payload)).read_flow_engine_routes(INSTANCE))


def test_routes_without_a_list_fail() -> None:
    with pytest.raises(SupabaseError):
        asyncio.run(_client(_object({"routes": {}})).read_flow_engine_routes(INSTANCE))


@pytest.mark.parametrize(
    "changes",
    [{"unnotified": -1}, {"reason": "Mal Motivo"}, {"version": 0}, {"kind": "otra"}],
)
def test_divergences_outside_the_contract_fail(changes: dict[str, object]) -> None:
    payload = copy.deepcopy(ROWS["reads"]["divergences"])
    if "unnotified" in changes:
        payload.update(changes)
    else:
        payload["divergences"][0].update(changes)
    with pytest.raises(SupabaseError):
        asyncio.run(_client(_object(payload)).read_flow_engine_divergences(
            INSTANCE, only_unnotified=True, limit=20))


def test_a_mark_that_is_not_a_boolean_fails() -> None:
    with pytest.raises(SupabaseError):
        asyncio.run(_client(_object({"ok": True})).mark_flow_engine_divergence_notified(
            "d", notified_at="2026-10-22T15:00:00+00:00"))


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
