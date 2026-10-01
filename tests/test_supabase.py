"""Supabase client contract for sanitary Johanna funnel observations.

Y, al final, el de las RPC del primer contacto portable tras el formulario.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from bridge.instance_manifest import InstanceManifest
from bridge.supabase import (
    PilotBoundaryConfig,
    PilotRequestStartRejectedError,
    PrecheckoutAdmissionResult,
    SupabaseClient,
    SupabaseCommittedResponseError,
    SupabaseError,
    SupabasePermanentError,
)


PARAMS = {
    "version": "1.0.0",
    "event_id": "01K4N9YQ2T7W3H5J8M6P0R1SVC",
    "event_type": "preform_opened",
    "occurred_at": "2026-09-08T08:00:00Z",
    "anonymous_session_id": "01K4N9YQ2T7W3H5J8M6P0R1SVD",
    "landing_ref": "ads-a",
    "offer_ref": "bxjge6zq",
    "utm_source": "meta",
    "utm_medium": "paid_social",
    "utm_campaign": None,
    "utm_content": None,
    "utm_term": None,
}


def test_client_uses_only_the_atomic_rpc_and_closed_scalar_arguments() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{
            "outcome": "inserted",
            "event_id": PARAMS["event_id"],
        }])

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(client.admit_johanna_funnel_event(**PARAMS))  # type: ignore[arg-type]

    assert result.outcome == "inserted"
    assert result.event_id == PARAMS["event_id"]
    assert len(requests) == 1
    assert requests[0].url.path == "/rest/v1/rpc/admit_johanna_funnel_event_v1"
    assert json.loads(requests[0].content) == {
        "p_version": PARAMS["version"],
        "p_event_id": PARAMS["event_id"],
        "p_event_type": PARAMS["event_type"],
        "p_occurred_at": PARAMS["occurred_at"],
        "p_anonymous_session_id": PARAMS["anonymous_session_id"],
        "p_landing_ref": PARAMS["landing_ref"],
        "p_offer_ref": PARAMS["offer_ref"],
        "p_utm_source": PARAMS["utm_source"],
        "p_utm_medium": PARAMS["utm_medium"],
        "p_utm_campaign": None,
        "p_utm_content": None,
        "p_utm_term": None,
    }
    assert b"payload" not in requests[0].content
    assert b"email" not in requests[0].content
    assert b"phone" not in requests[0].content
    assert b"fbclid" not in requests[0].content


@pytest.mark.parametrize("row", [
    {"outcome": "unknown", "event_id": PARAMS["event_id"]},
    {"outcome": "inserted", "event_id": "not-the-request-event"},
    {"outcome": "inserted", "event_id": PARAMS["event_id"], "extra": True},
])
def test_client_rejects_malformed_committed_rpc_rows(row: dict[str, object]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[row], request=request)

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SupabaseError, match="johanna_funnel_event_admission_invalid_row"):
        asyncio.run(client.admit_johanna_funnel_event(**PARAMS))  # type: ignore[arg-type]


# ------------------------------------------- primer contacto tras el formulario
# Las RPC son las de la migracion 20261001000200. Las filas tienen la forma del
# "returns table" de cada funcion (no son un payload externo). El binding es el
# del manifiesto de ATT1 (tests/fixtures/instances/att1/instancia.toml).

ATT1 = InstanceManifest.from_toml_file(
    Path(__file__).parent / "fixtures" / "instances" / "att1" / "instancia.toml"
).to_commercial_ally_config()
SUBMISSION_ID = "bfc778e7-5c9f-45e6-a910-651f92312157"
INTENT_ID = "1f581f3a-c469-45da-8208-9483d1b26f0b"
ACTION_ID = "0b6f1d4c-6a51-4f43-9f36-0d8b3c1f2a11"
ATTEMPT_ID = "7d1b0c52-2e0e-4a55-8a0e-6c7d4a0c9e22"
BOUNDARY = PilotBoundaryConfig(
    scope_key="att1-primer-contacto",
    scope_version=1,
    tenant_key="lancemos",
    channel_provider="waba",
    channel_account_ref="chatwoot-inbox:11",
)


def _recording_client(
    rows: list[dict[str, object]] | None = None, *, status: int = 200
) -> tuple[SupabaseClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json=rows if rows is not None else [])

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    return client, requests


def _admit_and_plan(client: SupabaseClient) -> object:
    return asyncio.run(
        client.admit_and_plan_portable_lead_precheckout(
            config=ATT1,
            external_submission_id="01K3F8QW7N2VYB4M6X9CDPTZRA",
            raw_payload={"event": "lead.precheckout"},
            canonical_payload={"version": "1.1.0"},
            scope_key="att1-primer-contacto",
            scope_version=1,
        )
    )


def _plan_row(**overrides: object) -> dict[str, object]:
    return {
        "outcome": "inserted",
        "submission_id": SUBMISSION_ID,
        "purchase_intent_id": INTENT_ID,
        "plan_outcome": "planned",
        "plan_reason": "first_contact_scheduled",
        **overrides,
    }


def test_admit_and_plan_sends_the_binding_and_the_first_contact_scope() -> None:
    client, requests = _recording_client([_plan_row()])

    result = _admit_and_plan(client)

    assert result == PrecheckoutAdmissionResult(
        outcome="inserted",
        submission_id=SUBMISSION_ID,
        purchase_intent_id=INTENT_ID,
        plan_outcome="planned",
        plan_reason="first_contact_scheduled",
    )
    [request] = requests
    assert request.url.path == "/rest/v1/rpc/admit_and_plan_portable_lead_precheckout"
    assert json.loads(request.content) == {
        "p_tenant_ref": "lancemos",
        "p_funnel_ref": "att1",
        "p_binding_version": 1,
        "p_external_submission_id": "01K3F8QW7N2VYB4M6X9CDPTZRA",
        "p_raw_payload": {"event": "lead.precheckout"},
        "p_canonical_payload": {"version": "1.1.0"},
        "p_scope_key": "att1-primer-contacto",
        "p_scope_version": 1,
    }


@pytest.mark.parametrize(
    ("plan_outcome", "plan_reason"),
    [
        ("not_planned", "pilot_runtime_not_armed"),
        ("not_planned", "precheckout_prior_opt_out"),
        ("plan_failed", "channel_identity_inbox_mismatch"),
    ],
)
def test_admit_and_plan_returns_why_a_form_was_not_planned(
    plan_outcome: str, plan_reason: str
) -> None:
    client, _ = _recording_client(
        [_plan_row(plan_outcome=plan_outcome, plan_reason=plan_reason)]
    )

    result = _admit_and_plan(client)

    assert result.outcome == "inserted"  # type: ignore[attr-defined]
    assert (result.plan_outcome, result.plan_reason) == (plan_outcome, plan_reason)  # type: ignore[attr-defined]


@pytest.mark.parametrize("outcome", ["duplicate", "semantic_conflict"])
def test_admit_and_plan_accepts_a_repeated_form_without_a_stored_plan(
    outcome: str,
) -> None:
    client, _ = _recording_client(
        [_plan_row(outcome=outcome, plan_outcome=None, plan_reason=None)]
    )

    result = _admit_and_plan(client)

    assert result.outcome == outcome  # type: ignore[attr-defined]
    assert result.plan_outcome is None and result.plan_reason is None  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("row", "error"),
    [
        (_plan_row(outcome="planned"), "invalid_outcome"),
        (_plan_row(submission_id=None), "invalid_submission_id"),
        (_plan_row(purchase_intent_id=""), "invalid_purchase_intent_id"),
        # Un envio nuevo siempre trae su plan.
        (_plan_row(plan_outcome=None, plan_reason=None), "invalid_plan"),
        (_plan_row(plan_outcome="sent"), "invalid_plan"),
        (_plan_row(plan_reason=None), "invalid_plan"),
        # El motivo es siempre un codigo: un texto libre no se acepta.
        (_plan_row(plan_reason="El telefono +52 55 no coincide"), "invalid_plan"),
        (_plan_row(outcome="duplicate", plan_outcome="planned", plan_reason=None), "invalid_plan"),
    ],
)
def test_admit_and_plan_rejects_a_malformed_row(
    row: dict[str, object], error: str
) -> None:
    client, _ = _recording_client([row])

    with pytest.raises(
        SupabaseError, match=f"portable_lead_precheckout_admission_and_plan_{error}"
    ):
        _admit_and_plan(client)


def test_admit_and_plan_surfaces_a_database_failure() -> None:
    client, _ = _recording_client(status=400)

    with pytest.raises(
        SupabaseError,
        match="portable_lead_precheckout_admission_and_plan_failed: HTTP 400",
    ):
        _admit_and_plan(client)


_DECISION = {
    "action_id": ACTION_ID,
    "decision": "cancel",
    "reason_code": "intent_purchased",
    "case_version": 2,
    "sequence_revision": 2,
}


@pytest.mark.parametrize(
    ("anchor_type", "rpc"),
    [
        ("precheckout_intent", "reevaluate_portable_precheckout_action"),
        ("cart_abandonment", "reevaluate_followup_action"),
        ("payment_failure", "reevaluate_followup_action"),
        ("accepted_outbound_message", "reevaluate_followup_action"),
        (None, "reevaluate_followup_action"),
    ],
)
def test_the_reevaluation_rpc_is_chosen_by_the_anchor(
    anchor_type: str | None, rpc: str
) -> None:
    client, requests = _recording_client([_DECISION])

    decision = asyncio.run(
        client.reevaluate_followup_action(
            action_id=ACTION_ID,
            worker_id="att1-dispatcher",
            lease_generation=3,
            now="2026-10-01T15:00:00+00:00",
            chatwoot_evidence={"p_chatwoot_conversation_id": "22"},
            anchor_type=anchor_type,
        )
    )

    assert (decision.decision, decision.reason_code) == ("cancel", "intent_purchased")
    [request] = requests
    assert request.url.path == f"/rest/v1/rpc/{rpc}"
    # Las dos funciones reciben los mismos argumentos: el ancla elige la RPC y
    # no viaja en el cuerpo.
    assert json.loads(request.content) == {
        "p_action_id": ACTION_ID,
        "p_worker_id": "att1-dispatcher",
        "p_lease_generation": 3,
        "p_now": "2026-10-01T15:00:00+00:00",
        "p_chatwoot_checked": True,
        "p_chatwoot_conversation_id": "22",
    }


def test_a_failed_precheckout_reevaluation_names_its_rpc() -> None:
    client, _ = _recording_client(status=400)

    with pytest.raises(
        SupabaseError, match="reevaluate_portable_precheckout_action_failed: HTTP 400"
    ):
        asyncio.run(
            client.reevaluate_followup_action(
                action_id=ACTION_ID,
                worker_id="att1-dispatcher",
                lease_generation=3,
                now="2026-10-01T15:00:00+00:00",
                anchor_type="precheckout_intent",
            )
        )


_STARTED = {
    "id": ATTEMPT_ID,
    "action_id": ACTION_ID,
    "idempotency_key": "precheckout_first_contact:case",
    "attempt_number": 1,
    "channel": "whatsapp",
    "mode": "approved_template",
    "phase": "request_started",
    "lease_generation": 3,
    "expected_case_version": 1,
    "expected_sequence_revision": 1,
    "pilot_authorization_id": "5a0a0a52-0f0e-4a55-8a0e-6c7d4a0c9e33",
    "pilot_runtime_generation": 1,
    "pilot_authorization_replayed": False,
}


@pytest.mark.parametrize(
    ("anchor_type", "rpc"),
    [
        ("precheckout_intent", "mark_portable_precheckout_request_started"),
        ("payment_failure", "mark_portable_payment_failure_request_started"),
        ("cart_abandonment", "mark_lancemos_pilot_request_started"),
        (None, "mark_lancemos_pilot_request_started"),
    ],
)
def test_the_request_start_rpc_is_chosen_by_the_anchor(
    anchor_type: str | None, rpc: str
) -> None:
    client, requests = _recording_client([_STARTED])

    started = asyncio.run(
        client.mark_followup_request_started(
            action_id=ACTION_ID,
            attempt_id=ATTEMPT_ID,
            worker_id="att1-dispatcher",
            lease_generation=3,
            now="2026-10-01T15:01:00+00:00",
            pilot_boundary=BOUNDARY,
            anchor_type=anchor_type,
        )
    )

    assert started.phase == "request_started"
    [request] = requests
    assert request.url.path == f"/rest/v1/rpc/{rpc}"
    assert json.loads(request.content) == {
        "p_action_id": ACTION_ID,
        "p_attempt_id": ATTEMPT_ID,
        "p_worker_id": "att1-dispatcher",
        "p_lease_generation": 3,
        "p_now": "2026-10-01T15:01:00+00:00",
    }


# La admision de siempre (v2) y la portable (con manifiesto, H7): mismo cuerpo,
# misma lectura de la respuesta y mismo mapeo de errores; cambia solo la RPC.
INBOUND_ADMISSIONS = (
    ("admit_inbound_commercial_case", "admit_inbound_commercial_case_v2"),
    (
        "admit_portable_inbound_commercial_case",
        "admit_portable_inbound_commercial_case_v1",
    ),
)
INBOUND_ADMISSION_METHODS = [method for method, _ in INBOUND_ADMISSIONS]


def _admit_inbound(
    client: SupabaseClient, method: str = "admit_inbound_commercial_case"
) -> object:
    return asyncio.run(
        getattr(client, method)(
            scope_key="att1-inbound",
            scope_version=1,
            external_conversation_id=200,
            external_user_id="520000000200",
        )
    )


_ADMITTED = {
    "outcome": "created",
    "commercial_case_id": "00000000-0000-0000-0000-000000000401",
    "contact_id": "00000000-0000-0000-0000-000000000402",
    "channel_identity_id": "00000000-0000-0000-0000-000000000403",
    "conversation_id": "00000000-0000-0000-0000-000000000404",
    "automation_status": "draft_only",
}


@pytest.mark.parametrize(("method", "rpc"), INBOUND_ADMISSIONS)
def test_each_inbound_admission_posts_the_same_body_to_its_own_rpc(
    method: str, rpc: str
) -> None:
    client, requests = _recording_client([_ADMITTED])

    admission = _admit_inbound(client, method)

    [request] = requests
    assert request.method == "POST"
    assert request.url.path == f"/rest/v1/rpc/{rpc}"
    assert json.loads(request.content) == {
        "p_scope_key": "att1-inbound",
        "p_scope_version": 1,
        "p_external_conversation_id": 200,
        "p_external_user_id": "520000000200",
    }
    assert admission.outcome == "created"  # type: ignore[attr-defined]
    assert admission.commercial_case_id == _ADMITTED["commercial_case_id"]  # type: ignore[attr-defined]
    assert admission.automation_status == "draft_only"  # type: ignore[attr-defined]


@pytest.mark.parametrize("method", INBOUND_ADMISSION_METHODS)
@pytest.mark.parametrize(
    ("row", "error"),
    [
        ({**_ADMITTED, "outcome": "adopted"}, "inbound_commercial_case_admission_invalid_outcome"),
        ({**_ADMITTED, "automation_status": "enabled"}, "inbound_commercial_case_admission_not_draft_only"),
        ({**_ADMITTED, "outcome": "blocked"}, "inbound_commercial_case_admission_blocked_state_invalid"),
    ],
)
def test_each_inbound_admission_reads_the_answer_the_same_way(
    method: str, row: dict[str, object], error: str
) -> None:
    # La portable no devuelve nada que la v2 no devuelva: una conversacion que
    # no quedo en draft_only (o un outcome nuevo) no llega al agente.
    client, _ = _recording_client([row])

    with pytest.raises(SupabaseError) as failed:
        _admit_inbound(client, method)

    assert str(failed.value) == error


@pytest.mark.parametrize(
    ("status", "code", "message"),
    [
        # La conversacion existe y no es una entrante draft_only: asi la deja
        # una plantilla del dispatcher.
        (400, "22000", "inbound_canonical_conversation_conflict"),
        (400, "22000", "inbound_canonical_identity_conflict"),
        (409, "23505", "inbound_external_conversation_owned_by_another_identity"),
        (400, "21000", "inbound_external_conversation_ownership_ambiguous"),
    ],
)
@pytest.mark.parametrize("method", INBOUND_ADMISSION_METHODS)
def test_an_inbound_admission_the_base_rejects_for_good_carries_its_reason(
    status: int, code: str, message: str, method: str
) -> None:
    # La forma con que PostgREST devuelve un raise (code/message/details/hint).
    # La portable que no adopta delega en la v2 y devuelve su mismo rechazo.
    client = _rejecting_client(
        {"code": code, "message": message, "details": None, "hint": None}, status=status
    )

    with pytest.raises(SupabasePermanentError) as rejected:
        _admit_inbound(client, method)

    assert rejected.value.reason == message
    # El texto del error es el de siempre: quien no mira el tipo no nota nada.
    assert str(rejected.value) == f"inbound_commercial_case_admission_failed: HTTP {status}"


@pytest.mark.parametrize(
    ("status", "body"),
    [
        # Un 23505 a secas es un indice unico tocado por una carrera: reintentar
        # puede pasar.
        (409, {"code": "23505", "message": "duplicate key value violates unique constraint"}),
        (503, {"code": "57P03", "message": "the database system is starting up"}),
        (500, {"code": "55000", "message": "inbound_commercial_scope_unavailable"}),
        # Un mensaje que no es un codigo no se copia a ningun lado.
        (400, {"code": "22000", "message": "Compradora 5215555550101"}),
        (502, ["not", "an", "object"]),
        # La RPC todavia no existe (PostgREST, funcion fuera del cache).
        (404, {"code": "PGRST202", "message": "Could not find the function"}),
    ],
)
@pytest.mark.parametrize("method", INBOUND_ADMISSION_METHODS)
def test_any_other_failed_inbound_admission_stays_retryable(
    status: int, body: object, method: str
) -> None:
    # Incluido el 404 de PostgREST cuando el bridge sale antes que la
    # migracion 000400: la portable no existe todavia y el entrante se
    # reintenta hasta que se aplique.
    client = _rejecting_client(body, status=status)

    with pytest.raises(SupabaseError) as failed:
        _admit_inbound(client, method)

    assert not isinstance(failed.value, SupabasePermanentError)
    assert str(failed.value) == f"inbound_commercial_case_admission_failed: HTTP {status}"


def _rejecting_client(body: object, *, status: int = 500) -> SupabaseClient:
    # La forma con que PostgREST devuelve un raise de SQLSTATE 55000
    # (code/message/details/hint, HTTP 500): el precedente de
    # tests/test_resolution_pilot_plan_rejection.py. No es un payload de
    # Hotmart ni de Chatwoot.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )


def _start(client: SupabaseClient, *, anchor_type: str | None, boundary: object = BOUNDARY) -> object:
    return asyncio.run(
        client.mark_followup_request_started(
            action_id=ACTION_ID,
            attempt_id=ATTEMPT_ID,
            worker_id="att1-dispatcher",
            lease_generation=3,
            now="2026-10-01T15:01:00+00:00",
            pilot_boundary=boundary,  # type: ignore[arg-type]
            anchor_type=anchor_type,
        )
    )


@pytest.mark.parametrize(
    ("anchor_type", "reason"),
    [
        ("precheckout_intent", "precheckout_conversation_handoff"),
        ("precheckout_intent", "intent_purchase_ambiguous"),
        ("payment_failure", "pilot_daily_budget_exhausted"),
        ("cart_abandonment", "pilot_runtime_not_armed"),
        (None, "pilot_audience_consented_intent_prior_opt_out"),
    ],
)
def test_a_rejected_request_start_keeps_its_reason(
    anchor_type: str | None, reason: str
) -> None:
    client = _rejecting_client(
        {
            "code": "55000",
            "message": "pilot_request_start_rejected",
            "details": reason,
            "hint": None,
        }
    )

    with pytest.raises(PilotRequestStartRejectedError) as rejected:
        _start(client, anchor_type=anchor_type)

    assert rejected.value.reason == reason
    assert str(rejected.value) == f"pilot_request_start_rejected:{reason}"
    assert not isinstance(rejected.value, SupabaseCommittedResponseError)


@pytest.mark.parametrize(
    "body",
    [
        # Otro error de la base: no es un rechazo del arranque.
        {"code": "55000", "message": "pilot_authorization_without_request_start", "details": None},
        {"code": "P0002", "message": "pilot_request_start_rejected", "details": "pilot_runtime_not_armed"},
        # Un detail que no es un codigo no se copia a ningun lado.
        {"code": "55000", "message": "pilot_request_start_rejected", "details": "Compradora 5215555550101"},
        {"code": "55000", "message": "pilot_request_start_rejected", "details": None},
        ["not", "an", "object"],
    ],
)
def test_any_other_failed_request_start_stays_a_generic_failure(body: object) -> None:
    client = _rejecting_client(body)

    with pytest.raises(SupabaseError) as failed:
        _start(client, anchor_type="precheckout_intent")

    assert not isinstance(failed.value, PilotRequestStartRejectedError)
    assert str(failed.value) == "mark_portable_precheckout_request_started_failed: HTTP 500"


def test_without_the_pilot_boundary_a_failed_start_is_never_read_as_a_rejection() -> None:
    # Johanna (sin frontera) llama a mark_followup_request_started: su fallo
    # sale como siempre, diga lo que diga el cuerpo.
    client = _rejecting_client(
        {
            "code": "55000",
            "message": "pilot_request_start_rejected",
            "details": "pilot_runtime_not_armed",
        }
    )

    with pytest.raises(SupabaseError) as failed:
        _start(client, anchor_type=None, boundary=None)

    assert not isinstance(failed.value, PilotRequestStartRejectedError)
    assert str(failed.value) == "mark_followup_request_started_failed: HTTP 500"


def test_a_precheckout_request_never_starts_without_the_pilot_boundary() -> None:
    # Sin frontera la marca compartida arrancaria el envio sin autorizar contra
    # el scope ni volver a mirar los frenos: no se llama a ninguna RPC.
    client, requests = _recording_client([_STARTED])

    with pytest.raises(SupabaseError, match="precheckout_intent_pilot_boundary_required"):
        asyncio.run(
            client.mark_followup_request_started(
                action_id=ACTION_ID,
                attempt_id=ATTEMPT_ID,
                worker_id="att1-dispatcher",
                lease_generation=3,
                now="2026-10-01T15:01:00+00:00",
                pilot_boundary=None,
                anchor_type="precheckout_intent",
            )
        )
    assert requests == []


_STATUS = {
    "configured": True,
    "runtime_state": "armed",
    "runtime_generation": 4,
    "reason_code": "pilot_runtime_armed",
}


def test_the_first_contact_scope_status_uses_its_own_rpc() -> None:
    client, requests = _recording_client([_STATUS])

    status = asyncio.run(
        client.get_portable_precheckout_pilot_runtime_status(pilot_boundary=BOUNDARY)
    )

    assert (status.configured, status.runtime_state, status.runtime_generation) == (
        True, "armed", 4,
    )
    assert status.reason_code == "pilot_runtime_armed"
    [request] = requests
    assert request.url.path == (
        "/rest/v1/rpc/get_portable_precheckout_pilot_runtime_status"
    )
    assert json.loads(request.content) == {
        "p_scope_key": "att1-primer-contacto",
        "p_scope_version": 1,
        "p_tenant_key": "lancemos",
        "p_channel_provider": "waba",
        "p_channel_account_ref": "chatwoot-inbox:11",
    }


def test_the_recovery_scope_status_keeps_its_rpc_and_its_errors() -> None:
    client, requests = _recording_client([_STATUS])

    status = asyncio.run(client.get_pilot_runtime_status(pilot_boundary=BOUNDARY))

    assert status.runtime_state == "armed"
    assert requests[0].url.path == "/rest/v1/rpc/get_lancemos_pilot_runtime_status"

    failing, _ = _recording_client(status=503)
    with pytest.raises(SupabaseError, match="^pilot_runtime_status_failed: HTTP 503$"):
        asyncio.run(failing.get_pilot_runtime_status(pilot_boundary=BOUNDARY))
    empty, _ = _recording_client([])
    with pytest.raises(SupabaseError, match="^pilot_runtime_status_invalid_shape$"):
        asyncio.run(empty.get_pilot_runtime_status(pilot_boundary=BOUNDARY))


def test_a_malformed_first_contact_scope_status_is_refused() -> None:
    client, _ = _recording_client([{**_STATUS, "runtime_state": "sending"}])

    with pytest.raises(
        SupabaseCommittedResponseError,
        match="portable_precheckout_pilot_runtime_status",
    ):
        asyncio.run(
            client.get_portable_precheckout_pilot_runtime_status(pilot_boundary=BOUNDARY)
        )
    failing, _ = _recording_client(status=404)
    with pytest.raises(
        SupabaseError,
        match="portable_precheckout_pilot_runtime_status_failed: HTTP 404",
    ):
        asyncio.run(
            failing.get_portable_precheckout_pilot_runtime_status(pilot_boundary=BOUNDARY)
        )


# --------------------------------------- modo de audiencia del scope del piloto
# La RPC es la de la migracion 20261001000300: devuelve un texto (PostgREST lo
# manda como un escalar JSON) o null si la version no esta publicada. El bridge
# la lee para la guarda del adaptador de GHL sin aceptacion del riesgo y falla
# cerrado ante cualquier otra cosa.


def _scalar_client(
    body: bytes, *, status: int = 200
) -> tuple[SupabaseClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status, content=body, headers={"content-type": "application/json"}
        )

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    return client, requests


@pytest.mark.parametrize(
    "mode", ["manual_cohort", "consented_intent_in_cohort", "consented_intent"]
)
def test_the_pilot_scope_audience_mode_is_read_by_scope_and_version(mode: str) -> None:
    client, requests = _scalar_client(json.dumps(mode).encode())
    # El scope de recuperacion de ATT1 en su segunda version (la del E2E).
    boundary = PilotBoundaryConfig(
        scope_key="att1-recuperacion",
        scope_version=2,
        tenant_key="lancemos",
        channel_provider="waba",
        channel_account_ref="chatwoot-inbox:11",
    )

    observed = asyncio.run(client.get_pilot_scope_audience_mode(pilot_boundary=boundary))

    assert observed == mode
    [request] = requests
    assert request.method == "POST"
    assert request.url.path == "/rest/v1/rpc/get_lancemos_pilot_scope_audience_mode"
    # Solo la clave y la version: la funcion no recibe tenant ni canal.
    assert json.loads(request.content) == {
        "p_scope_key": "att1-recuperacion",
        "p_scope_version": 2,
    }


@pytest.mark.parametrize(
    ("body", "status", "error"),
    [
        # Una version que no esta publicada llega como null.
        (b"null", 200, "^pilot_scope_audience_mode_scope_not_published$"),
        (b'"everyone"', 200, "^pilot_scope_audience_mode_invalid_mode$"),
        (b'""', 200, "^pilot_scope_audience_mode_invalid_mode$"),
        (b'["manual_cohort"]', 200, "^pilot_scope_audience_mode_invalid_mode$"),
        (b'{"audience_mode": "manual_cohort"}', 200, "^pilot_scope_audience_mode_invalid_mode$"),
        (b"true", 200, "^pilot_scope_audience_mode_invalid_mode$"),
        (b"not json", 200, "^pilot_scope_audience_mode_invalid_json$"),
        (b'"manual_cohort"', 404, "^pilot_scope_audience_mode_failed: HTTP 404$"),
        (b'"manual_cohort"', 503, "^pilot_scope_audience_mode_failed: HTTP 503$"),
    ],
)
def test_an_audience_mode_that_cannot_be_trusted_is_refused(
    body: bytes, status: int, error: str
) -> None:
    client, _ = _scalar_client(body, status=status)

    with pytest.raises(SupabaseError, match=error):
        asyncio.run(client.get_pilot_scope_audience_mode(pilot_boundary=BOUNDARY))
