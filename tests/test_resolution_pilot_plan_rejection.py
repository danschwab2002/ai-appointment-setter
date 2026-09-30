"""El evento guarda el motivo con que la planificacion del piloto lo rechaza (A1).

Antes, todo rechazo de plan_lancemos_pilot_cart_recovery y de
plan_portable_payment_failure_recovery (cohorte, oferta, correlacion...) quedaba
en webhook_events.processing_error como create_recovery_case_failed, igual que
una falla real de la base, y el motivo se perdia.

Fixtures:
- Carrito: tests/fixtures/hotmart_cart_abandonment_rejected_v1.json, capturado del
  panel de Hotmart el 2026-09-21. Se cambian solo producto y oferta por los de
  ATT1, como tests/test_commercial_ally_multi_offer.py:_cart.
- Instancia: tests/fixtures/instances/att1/instancia.toml (datos del 2026-09-28).
- Pago fallido: no hay ningun PURCHASE_CANCELED capturado en el repo. Se arma
  inline con la forma del precedente tests/test_commercial_ally_multi_offer.py:
  _failure (deuda previa, anotada en el commit).
- La respuesta de error es la forma con que PostgREST devuelve un raise de
  SQLSTATE 55000 (code/message/details/hint, HTTP 500). No es un payload de
  Hotmart ni de Chatwoot.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from bridge.hotmart import EVENT_CART_ABANDONMENT, EVENT_PURCHASE_CANCELED
from bridge.instance_manifest import InstanceManifest
from bridge.resolution import ResolutionError, resolve_event
from bridge.supabase import PilotBoundaryConfig, SupabaseClient

ROOT = Path(__file__).resolve().parents[1]
CAPTURED_CART = json.loads(
    (ROOT / "tests" / "fixtures" / "hotmart_cart_abandonment_rejected_v1.json").read_text(
        encoding="utf-8"
    )
)["payload"]
MANIFEST = InstanceManifest.from_toml_file(
    ROOT / "tests" / "fixtures" / "instances" / "att1" / "instancia.toml"
)
CONFIG = MANIFEST.to_commercial_ally_config()
BOUNDARY = PilotBoundaryConfig(
    scope_key="att1-recuperacion",
    scope_version=1,
    tenant_key=CONFIG.tenant_ref,
    channel_provider="waba",
    channel_account_ref=f"chatwoot-inbox:{CONFIG.chatwoot_inbox_id}",
)
EVENT_ID = "00000000-0000-4000-8000-000000000001"
CONTACT_ID = "00000000-0000-4000-8000-000000000010"


def _cart(offer: str) -> dict[str, Any]:
    payload = copy.deepcopy(CAPTURED_CART)
    payload["data"]["product"] = {
        "id": CONFIG.hotmart_product_id,
        "name": CONFIG.product_name,
    }
    payload["data"]["offer"] = {"code": offer}
    return payload


def _failure(offer: str) -> dict[str, Any]:
    return {
        "id": f"pf-{offer}",
        "creation_date": 1790560000000,
        "event": "PURCHASE_CANCELED",
        "version": "2.0.0",
        "data": {
            "buyer": {
                "name": "Compradora",
                "email": "compradora@example.invalid",
                "checkout_phone": "525555555555",
            },
            "product": {"id": CONFIG.hotmart_product_id, "name": CONFIG.product_name},
            "purchase": {
                "transaction": "HP1234567890",
                "status": "CANCELED",
                "offer": {"code": offer},
                "payment": {"refusal_reason": "insufficient_funds"},
            },
        },
    }


def _postgrest_error(message: str, details: str | None) -> dict[str, Any]:
    return {"code": "55000", "details": details, "hint": None, "message": message}


class _Recorder:
    """PostgREST falso: identidad nueva, el planificador responde ``planner``."""

    def __init__(self, planner: httpx.Response) -> None:
        self.planner = planner
        self.planner_paths: list[str] = []
        self.status_patches: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/rest/v1/contact_points":
            return httpx.Response(200, json=[], request=request)
        if request.method == "POST" and path == "/rest/v1/contacts":
            return httpx.Response(201, json=[{"id": CONTACT_ID}], request=request)
        if request.method == "POST" and path == "/rest/v1/contact_points":
            return httpx.Response(201, request=request)
        if request.method == "POST" and path.startswith("/rest/v1/rpc/plan_"):
            self.planner_paths.append(path)
            return self.planner
        if request.method == "PATCH" and path == "/rest/v1/webhook_events":
            self.status_patches.append(json.loads(request.content))
            return httpx.Response(204, request=request)
        raise AssertionError(f"unexpected request {request.method} {path}")

    def client(self) -> SupabaseClient:
        return SupabaseClient(
            base_url="https://supabase.example.test",
            service_role_key="test-key",
            transport=httpx.MockTransport(self.handler),
        )


def _resolve(
    recorder: _Recorder,
    payload: dict[str, Any],
    *,
    event_type: str = EVENT_CART_ABANDONMENT,
    portable: bool = True,
) -> str:
    kwargs: dict[str, Any] = dict(
        webhook_event_id=EVENT_ID,
        payload=payload,
        supabase=recorder.client(),
        policy_key="att1-recuperacion-un-toque",
        policy_version=1,
        allowed_jid=None,
        chatwoot_account_id=CONFIG.chatwoot_account_id,
        chatwoot_inbox_id=CONFIG.chatwoot_inbox_id,
        event_type=event_type,
    )
    if portable:
        kwargs.update(pilot_boundary=BOUNDARY, commercial_ally_config=CONFIG)
    with pytest.raises(ResolutionError) as raised:
        asyncio.run(resolve_event(**kwargs))
    return str(raised.value)


@pytest.mark.parametrize("offer", CONFIG.accepted_offer_codes)
@pytest.mark.parametrize(
    ("message", "details", "expected"),
    [
        (
            "pilot_scope_rejected",
            "pilot_contact_not_in_cohort",
            "pilot_scope_rejected:pilot_contact_not_in_cohort",
        ),
        (
            "pilot_scope_rejected",
            "pilot_offer_mismatch",
            "pilot_scope_rejected:pilot_offer_mismatch",
        ),
        ("pilot_case_binding_conflict", None, "pilot_case_binding_conflict"),
    ],
)
def test_cart_pilot_rejection_reason_reaches_the_event(
    offer: str, message: str, details: str | None, expected: str
) -> None:
    recorder = _Recorder(
        httpx.Response(500, json=_postgrest_error(message, details))
    )

    reason = _resolve(recorder, _cart(offer))

    assert reason == expected
    assert recorder.planner_paths == ["/rest/v1/rpc/plan_lancemos_pilot_cart_recovery"]
    assert recorder.status_patches == [
        {"processing_status": "failed", "processed_at": None, "processing_error": expected}
    ]


@pytest.mark.parametrize(
    ("message", "details", "expected"),
    [
        (
            "payment_failure_correlation_unresolved",
            None,
            "payment_failure_correlation_unresolved",
        ),
        (
            "pilot_scope_rejected",
            "pilot_contact_not_in_cohort",
            "pilot_scope_rejected:pilot_contact_not_in_cohort",
        ),
    ],
)
def test_payment_failure_pilot_rejection_reason_reaches_the_event(
    message: str, details: str | None, expected: str
) -> None:
    recorder = _Recorder(
        httpx.Response(500, json=_postgrest_error(message, details))
    )

    reason = _resolve(
        recorder,
        _failure(CONFIG.offer_code),
        event_type=EVENT_PURCHASE_CANCELED,
    )

    assert reason == expected
    assert recorder.planner_paths == [
        "/rest/v1/rpc/plan_portable_payment_failure_recovery"
    ]
    assert recorder.status_patches[-1]["processing_error"] == expected


@pytest.mark.parametrize(
    "planner",
    [
        # Another SQLSTATE is a real failure, not a pilot decision.
        httpx.Response(
            400,
            json={"code": "P0001", "details": None, "hint": None, "message": "boom"},
        ),
        # Free text or anything outside snake_case never reaches the column.
        httpx.Response(
            500,
            json=_postgrest_error("pilot_scope_rejected", "phone 525555555555"),
        ),
        httpx.Response(500, json=_postgrest_error("Pilot Scope Rejected", None)),
        httpx.Response(500, json=_postgrest_error("pilot_scope_rejected", "")),
        httpx.Response(500, json=["not", "an", "object"]),
        httpx.Response(503, text="upstream unavailable"),
    ],
)
def test_unknown_planner_failures_stay_generic(planner: httpx.Response) -> None:
    recorder = _Recorder(planner)

    reason = _resolve(recorder, _cart(CONFIG.offer_code))

    assert reason == "create_recovery_case_failed"
    assert recorder.status_patches[-1]["processing_error"] == (
        "create_recovery_case_failed"
    )


def test_without_pilot_boundary_the_rejection_stays_generic() -> None:
    """Johanna never calls the pilot planners: her failures keep the old reason."""
    recorder = _Recorder(
        httpx.Response(
            500,
            json=_postgrest_error("pilot_scope_rejected", "pilot_contact_not_in_cohort"),
        )
    )

    reason = _resolve(recorder, copy.deepcopy(CAPTURED_CART), portable=False)

    assert reason == "create_recovery_case_failed"
    assert recorder.planner_paths == ["/rest/v1/rpc/plan_cart_recovery"]
    assert recorder.status_patches[-1]["processing_error"] == (
        "create_recovery_case_failed"
    )
