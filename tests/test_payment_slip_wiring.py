"""El cableado de C1, la ficha de pago: las RPC, el worker de resolucion y H3.

Datos capturados, no inventados: los 13 ``PURCHASE_BILLET_PRINTED`` de att1-db
desde el 12/10, anonimizados (``hotmart_payment_slip_printed_att1_20261018.json``),
contra el manifiesto de ATT1 de ``tests/fixtures/instances/att1/instancia.toml``
(producto 5071808, las tres ofertas). Las respuestas de PostgREST siguen la
forma de las RPC de 20261019000100, que valida en PGlite
``tests/sql/followup_engine/validate_portable_payment_slip_recovery.mjs``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from bridge.instance_manifest import InstanceManifest
from bridge.payment_slip import parse_payment_slip_methods
from bridge.payment_slip_first_touch import PaymentSlipPlanningConfig
from bridge.supabase import (
    PaymentSlipAdmissionResult,
    PaymentSlipSendContext,
    PilotBoundaryConfig,
    PilotPlanRejectedError,
    PilotRequestStartRejectedError,
    SupabaseClient,
    SupabaseError,
    SupabasePermanentError,
)
from bridge.worker import ResolutionWorker

FIXTURES = Path(__file__).parent / "fixtures"
SLIPS: list[dict[str, Any]] = json.loads(
    (FIXTURES / "hotmart_payment_slip_printed_att1_20261018.json").read_text(encoding="utf-8")
)["eventos"]
MANIFEST = InstanceManifest.from_toml_file(FIXTURES / "instances" / "att1" / "instancia.toml")
ALLY = MANIFEST.to_commercial_ally_config()
OXXO_ROW: dict[str, object] = {
    "medio": "CASHPAYMENT",
    "pais": "MX",
    "tienda": "OXXO",
    "zona_horaria": "America/Mexico_City",
    "referencia_largo": 34,
    "vencimiento_digitos": [19, 26],
    "vencimiento_dias_si_falta": 3,
    "boton_prefijo": "https://customer-local-latam.ebanx.com/print/oxxo?hash=",
    "habilitada": True,
}
METHODS = parse_payment_slip_methods([OXXO_ROW])
RECOVERY = PilotBoundaryConfig(
    scope_key="att1-recuperacion",
    scope_version=5,
    tenant_key=ALLY.tenant_ref,
    channel_provider="waba",
    channel_account_ref=f"chatwoot-inbox:{ALLY.chatwoot_inbox_id}",
)
SLIP_SCOPE = PilotBoundaryConfig(
    scope_key="att1-ficha",
    scope_version=1,
    tenant_key=ALLY.tenant_ref,
    channel_provider="waba",
    channel_account_ref=f"chatwoot-inbox:{ALLY.chatwoot_inbox_id}",
)
PLANNING = PaymentSlipPlanningConfig(
    methods=METHODS,
    policy_key="att1-ficha-un-toque",
    policy_version=1,
    pilot_boundary=SLIP_SCOPE,
)
EVENT_ID = "6f1d2c3b-0000-4000-8000-000000000103"
CASE_ID = "00000000-0000-4000-8000-000000000011"
ACTION_ID = "3a4b5c6d-7e8f-4a1b-9c2d-3e4f5a6b7c8d"
NOW = "2026-10-13T04:56:30+00:00"


def _slip(index: int) -> dict[str, Any]:
    return json.loads(json.dumps(SLIPS[index]["payload"]))


def _supabase(responses: list[tuple[int, object]]) -> tuple[SupabaseClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []
    pending = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status, body = pending.pop(0)
        return httpx.Response(status, json=body)

    return (
        SupabaseClient(
            base_url="https://supabase.example.test",
            service_role_key="service-role",
            transport=httpx.MockTransport(handler),
        ),
        requests,
    )


# ------------------------------------------------------------ las RPC


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"outcome": "inserted", "webhook_event_id": EVENT_ID},
         PaymentSlipAdmissionResult("inserted", EVENT_ID)),
        ({"outcome": "duplicate", "webhook_event_id": EVENT_ID},
         PaymentSlipAdmissionResult("duplicate", EVENT_ID)),
        ({"outcome": "ignored_legacy_slip", "webhook_event_id": EVENT_ID},
         PaymentSlipAdmissionResult("ignored_legacy_slip", EVENT_ID)),
        ({"outcome": "ignored_binding_mismatch", "webhook_event_id": None},
         PaymentSlipAdmissionResult("ignored_binding_mismatch", None)),
    ],
)
def test_the_admission_v2_sends_the_identity_of_the_payload(
    row: dict[str, Any], expected: PaymentSlipAdmissionResult
) -> None:
    client, requests = _supabase([(200, [row])])
    payload = _slip(2)

    result = asyncio.run(client.admit_portable_hotmart_payment_slip_v2(
        config=ALLY, external_event_id=payload["id"], payload=payload,
        normalized_email="persona.ficha3@example.com", normalized_phone="520000000103",
    ))

    assert result == expected
    assert requests[0].url.path == "/rest/v1/rpc/admit_portable_hotmart_payment_slip_v2"
    assert json.loads(requests[0].content) == {
        "p_tenant_ref": ALLY.tenant_ref,
        "p_funnel_ref": ALLY.funnel_ref,
        "p_binding_version": ALLY.binding_version,
        "p_external_event_id": payload["id"],
        "p_payload": payload,
        "p_normalized_email": "persona.ficha3@example.com",
        "p_normalized_phone": "520000000103",
    }


def test_the_admission_v2_rejections_are_permanent_or_not() -> None:
    client, _ = _supabase([
        (400, {"code": "22023", "message": "portable_hotmart_payment_slip_identity_mismatch"}),
        (503, {"message": "down"}),
        (200, [{"outcome": "sent", "webhook_event_id": EVENT_ID}]),
    ])

    def admit() -> PaymentSlipAdmissionResult:
        return asyncio.run(client.admit_portable_hotmart_payment_slip_v2(
            config=ALLY, external_event_id="x", payload={}, normalized_email=None,
            normalized_phone="520000000103",
        ))

    with pytest.raises(SupabasePermanentError) as rejected:
        admit()
    assert rejected.value.reason == "portable_hotmart_payment_slip_identity_mismatch"
    with pytest.raises(SupabaseError) as down:
        admit()
    assert not isinstance(down.value, SupabasePermanentError)
    with pytest.raises(SupabaseError, match="invalid_outcome"):
        admit()


def test_the_plan_goes_to_the_derived_rpc_with_the_scope_of_the_slip() -> None:
    client, requests = _supabase([
        (200, [{"recovery_case_id": CASE_ID, "followup_sequence_id": "seq",
                "scheduled_action_id": ACTION_ID, "created": True}]),
        (400, {"code": "55000", "message": "pilot_scope_rejected",
               "details": "pilot_audience_intent_unresolved"}),
    ])

    def plan() -> object:
        return asyncio.run(client.plan_payment_slip_recovery(
            webhook_event_id=EVENT_ID, contact_id="contact", external_product_id="5071808",
            product_name="Alimenta tu Tiroides", offer_code="2uafw5bg",
            policy_key="att1-ficha-un-toque", policy_version=1,
            abandoned_at="2026-10-13T03:56:18+00:00", chatwoot_account_id=2,
            chatwoot_inbox_id=11, external_user_id="520000000103", pilot_boundary=SLIP_SCOPE,
        ))

    planned = plan()
    assert planned.created is True and planned.scheduled_action_id == ACTION_ID  # type: ignore[attr-defined]
    assert requests[0].url.path == "/rest/v1/rpc/plan_portable_payment_slip_recovery"
    body = json.loads(requests[0].content)
    assert body["p_failed_at"] == "2026-10-13T03:56:18+00:00"
    assert (body["p_scope_key"], body["p_scope_version"]) == ("att1-ficha", 1)
    assert (body["p_policy_key"], body["p_policy_version"]) == ("att1-ficha-un-toque", 1)
    with pytest.raises(PilotPlanRejectedError) as rejected:
        plan()
    assert rejected.value.reason == "pilot_scope_rejected:pilot_audience_intent_unresolved"


def test_the_plan_needs_the_scoped_identity() -> None:
    client, requests = _supabase([])
    with pytest.raises(SupabaseError, match="requires_scoped_identity"):
        asyncio.run(client.plan_payment_slip_recovery(
            webhook_event_id=EVENT_ID, contact_id="contact", external_product_id="5071808",
            product_name="Alimenta tu Tiroides", offer_code="2uafw5bg",
            policy_key="att1-ficha-un-toque", policy_version=1,
            abandoned_at="2026-10-13T03:56:18+00:00",
        ))
    assert requests == []


def test_the_send_context_is_read_under_the_lease() -> None:
    client, requests = _supabase([
        (200, [{"webhook_event_id": EVENT_ID, "payment_type": "CASHPAYMENT",
                "checkout_country_iso": "MX",
                "billet_url": "https://customer-local-latam.ebanx.com/print/oxxo?hash=" + "f" * 48,
                "billet_barcode": "0000000000000000002026101500000000",
                "generated_at": "2026-10-13T03:56:18+00:00"}]),
        (400, {"code": "55000", "message": "payment_slip_details_missing"}),
        (200, [{"webhook_event_id": EVENT_ID}]),
    ])

    def read() -> PaymentSlipSendContext:
        return asyncio.run(client.get_portable_payment_slip_send_context(
            action_id=ACTION_ID, worker_id="att1-dispatcher", lease_generation=2, now=NOW,
        ))

    context = read()
    assert context.payment_type == "CASHPAYMENT"
    assert context.generated_at is not None and context.generated_at.utcoffset() is not None
    assert requests[0].url.path == "/rest/v1/rpc/get_portable_payment_slip_send_context"
    assert json.loads(requests[0].content) == {
        "p_action_id": ACTION_ID, "p_worker_id": "att1-dispatcher",
        "p_lease_generation": 2, "p_now": NOW,
    }
    with pytest.raises(SupabasePermanentError) as missing:
        read()
    assert missing.value.reason == "payment_slip_details_missing"
    with pytest.raises(SupabaseError):
        read()


def test_the_runtime_status_reads_the_scope_of_the_slip() -> None:
    client, requests = _supabase([
        (200, [{"configured": True, "runtime_state": "armed", "runtime_generation": 3,
                "reason_code": "pilot_runtime_armed"}]),
    ])

    status = asyncio.run(client.get_portable_payment_slip_pilot_runtime_status(
        pilot_boundary=SLIP_SCOPE
    ))

    assert (status.configured, status.runtime_state) == (True, "armed")
    assert requests[0].url.path == "/rest/v1/rpc/get_portable_payment_slip_pilot_runtime_status"
    assert json.loads(requests[0].content)["p_scope_key"] == "att1-ficha"


def test_the_request_start_of_a_slip_goes_through_its_own_rpc() -> None:
    attempt = {
        "id": "attempt", "action_id": ACTION_ID, "lease_generation": 2,
        "idempotency_key": "payment_slip:first_contact:case", "attempt_number": 1,
        "channel": "whatsapp", "mode": "approved_template", "phase": "request_started",
        "expected_case_version": 1, "expected_sequence_revision": 1,
        "pilot_authorization_id": "auth", "pilot_runtime_generation": 3,
        "pilot_authorization_replayed": False,
    }
    client, requests = _supabase([
        (200, [attempt]),
        (400, {"code": "55000", "message": "pilot_request_start_rejected",
               "details": "pilot_chatwoot_opt_out_stop"}),
    ])

    def start() -> object:
        return asyncio.run(client.mark_followup_request_started(
            action_id=ACTION_ID, attempt_id="attempt", worker_id="att1-dispatcher",
            lease_generation=2, now=NOW, pilot_boundary=RECOVERY, anchor_type="payment_slip",
        ))

    assert start().phase == "request_started"  # type: ignore[attr-defined]
    assert requests[0].url.path == "/rest/v1/rpc/mark_portable_payment_slip_request_started"
    with pytest.raises(PilotRequestStartRejectedError) as rejected:
        start()
    assert rejected.value.reason == "pilot_chatwoot_opt_out_stop"
    with pytest.raises(SupabaseError, match="payment_slip_pilot_boundary_required"):
        asyncio.run(client.mark_followup_request_started(
            action_id=ACTION_ID, attempt_id="attempt", worker_id="att1-dispatcher",
            lease_generation=2, now=NOW, anchor_type="payment_slip",
        ))


# ------------------------------------------------- el worker de resolucion


class _Base:
    """Lo que el worker toca de la base para un evento de Hotmart."""

    def __init__(self, *, cart_rejection: str | None = None) -> None:
        self.statuses: list[dict[str, object]] = []
        self.slip_plans: list[dict[str, object]] = []
        self.cart_plans: list[dict[str, object]] = []
        self.cart_rejection = cart_rejection

    async def find_contact_by_email(self, _value: str) -> None:
        return None

    async def find_contact_by_phones(self, _values: tuple[str, ...]) -> None:
        return None

    async def create_contact(self, **_: object) -> str:
        return "00000000-0000-4000-8000-000000000010"

    async def create_contact_point(self, **_: object) -> None:
        return None

    async def plan_payment_slip_recovery(self, **kwargs: object) -> object:
        self.slip_plans.append(kwargs)
        return type("Plan", (), {"recovery_case_id": CASE_ID})()

    async def plan_cart_recovery(self, **kwargs: object) -> object:
        self.cart_plans.append(kwargs)
        if self.cart_rejection is not None:
            raise PilotPlanRejectedError(self.cart_rejection)
        return type("Plan", (), {"recovery_case_id": CASE_ID})()

    async def fetch_conversations(self, **_: object) -> list[object]:
        return []

    async def fetch_recovery_cases(self, **_: object) -> list[object]:
        return []

    async def fetch_channel_identities(self, **_: object) -> list[object]:
        return []

    async def update_event_status(self, **kwargs: object) -> None:
        self.statuses.append(kwargs)


def _worker(base: _Base, *, payment_slip: PaymentSlipPlanningConfig | None = PLANNING) -> ResolutionWorker:
    return ResolutionWorker(
        supabase=base,  # type: ignore[arg-type]
        policy_key="att1-recuperacion-un-toque",
        policy_version=5,
        allowed_jid=None,
        chatwoot_account_id=ALLY.chatwoot_account_id,
        chatwoot_inbox_id=ALLY.chatwoot_inbox_id,
        pilot_boundary=RECOVERY,
        commercial_ally_config=ALLY,
        payment_failure_enabled=True,
        payment_slip=payment_slip,
    )


def _process(worker: ResolutionWorker, payload: dict[str, Any]) -> None:
    asyncio.run(worker._process_one({
        "id": EVENT_ID, "event_type": payload["event"], "payload": payload,
    }))


@pytest.mark.parametrize("index", [2, 3, 6, 8, 12])
def test_an_oxxo_slip_of_mexico_is_planned_under_its_own_scope_and_policy(index: int) -> None:
    base = _Base()

    _process(_worker(base), _slip(index))

    [plan] = base.slip_plans
    assert plan["pilot_boundary"] is SLIP_SCOPE
    assert (plan["policy_key"], plan["policy_version"]) == ("att1-ficha-un-toque", 1)
    assert plan["external_product_id"] == "5071808"
    assert plan["offer_code"] == _slip(index)["data"]["purchase"]["offer"]["code"]
    assert plan["external_user_id"] == _slip(index)["data"]["buyer"]["checkout_phone"]
    assert base.cart_plans == []
    assert base.statuses == [{"event_id": EVENT_ID, "status": "processed"}]


@pytest.mark.parametrize(
    "index",
    # YAPE de Peru, MERCADO_PAGO de Mexico y Argentina, NEQUI de Colombia y la
    # de efectivo de Chile: ninguno tiene fila habilitada.
    [0, 1, 4, 5, 7, 9, 10, 11],
)
def test_a_slip_of_a_method_or_country_not_enabled_is_ignored_with_its_reason(
    index: int,
) -> None:
    base = _Base()

    _process(_worker(base), _slip(index))

    assert base.slip_plans == [] and base.cart_plans == []
    assert base.statuses == [{
        "event_id": EVENT_ID, "status": "ignored", "error": "payment_slip_method_not_enabled",
    }]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda payment: payment.__setitem__(
            "billet_url", "https://api.ebanx.com/ws/redirect?hash=" + "f" * 48),
         "payment_slip_url_prefix_mismatch"),
        (lambda payment: payment.__setitem__(
            "billet_url",
            "https://customer-local-latam.ebanx.com/print/oxxo?hash=ff%20ff"),
         "payment_slip_button_suffix_invalid"),
    ],
)
def test_a_slip_of_mexico_on_another_base_is_ignored(change: Any, reason: str) -> None:
    # DERIVADOS de la ficha 3, cambiando solo el link.
    payload = _slip(2)
    change(payload["data"]["purchase"]["payment"])
    base = _Base()

    _process(_worker(base), payload)

    assert base.slip_plans == []
    assert base.statuses == [{"event_id": EVENT_ID, "status": "ignored", "error": reason}]


def test_without_the_flag_the_slip_is_discarded_as_today() -> None:
    base = _Base()

    _process(_worker(base, payment_slip=None), _slip(2))

    assert base.slip_plans == [] and base.cart_plans == []
    assert base.statuses == [{
        "event_id": EVENT_ID, "status": "failed", "error": "unsupported_persisted_event_type",
    }]


def test_h3_a_cart_over_an_open_slip_case_fails_legibly_without_a_message() -> None:
    # H3 del diseno (medido en PGlite, validate_portable_payment_slip_recovery.mjs
    # seccion 9): con el caso de la ficha abierto, el planificador del piloto
    # del carrito aborta con 55000 pilot_case_binding_conflict. El worker lo deja
    # failed con ese motivo y sigue: ni excepcion ni mensaje.
    client, _ = _supabase([(400, {"code": "55000", "message": "pilot_case_binding_conflict"})])
    with pytest.raises(PilotPlanRejectedError) as rejected:
        asyncio.run(client.plan_cart_recovery(
            webhook_event_id=EVENT_ID, contact_id="contact", external_product_id="5071808",
            product_name="Alimenta tu Tiroides", offer_code="2uafw5bg",
            policy_key="att1-recuperacion-un-toque", policy_version=5,
            abandoned_at="2026-10-13T03:56:18+00:00", chatwoot_account_id=2,
            chatwoot_inbox_id=11, external_user_id="520000000103", pilot_boundary=RECOVERY,
        ))
    assert rejected.value.reason == "pilot_case_binding_conflict"

    base = _Base(cart_rejection="pilot_case_binding_conflict")
    cart = _slip(2)
    cart["event"] = "PURCHASE_OUT_OF_SHOPPING_CART"
    cart["data"]["offer"] = cart["data"]["purchase"]["offer"]
    asyncio.run(_worker(base)._process_one({
        "id": EVENT_ID, "event_type": "PURCHASE_OUT_OF_SHOPPING_CART", "payload": cart,
    }))

    assert len(base.cart_plans) == 1 and base.slip_plans == []
    assert base.statuses == [{
        "event_id": EVENT_ID, "status": "failed", "error": "pilot_case_binding_conflict",
    }]
