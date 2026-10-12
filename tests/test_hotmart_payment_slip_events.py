"""Los eventos de la ficha de pago se guardan (1.6.0).

PURCHASE_BILLET_PRINTED (se genero la ficha, por ejemplo de OXXO) y
PURCHASE_EXPIRED (vencio sin pagarse) son los que necesita el seguimiento de la
ficha sin pagar de la Etapa 1. Hasta la 1.5.0 el bridge los contestaba con 200
sin guardarlos (unsupported_event_type).

DERIVADO, no capturado: ATT1 todavia no los tenia suscriptos. Los payloads se
arman desde las dos compras reales pagadas en OXXO
(tests/fixtures/hotmart_cash_payment_approved_att1_20261012.json) cambiando
solo event, id, data.purchase.status y creation_date. El bridge no lee nada
propio de la ficha: guarda el payload entero.
"""

from __future__ import annotations

import json
import time
from copy import deepcopy
from pathlib import Path

import pytest

from bridge.app import create_app
from bridge.hotmart import PAYMENT_SLIP_EVENT_TYPES, classify_hotmart_event
from bridge.supabase import PaymentSlipAdmissionResult, SupabaseError, SupabasePermanentError
from test_hotmart import _SupabaseStub, _config, _post, _settings

CASH = json.loads(
    (Path(__file__).parent / "fixtures" / "hotmart_cash_payment_approved_att1_20261012.json").read_text(
        encoding="utf-8"
    )
)["eventos"]


def _slip(event_type: str = "PURCHASE_BILLET_PRINTED", index: int = 0) -> dict[str, object]:
    payload = deepcopy(CASH[index]["payload"])
    payload["id"] = f"slip-{event_type.lower()}-{index}"
    payload["event"] = event_type
    payload["creation_date"] = int(time.time() * 1000)
    payload["data"]["purchase"]["status"] = event_type.removeprefix("PURCHASE_")
    payload["data"]["purchase"].pop("approved_date", None)
    return payload


class _SlipSupabase(_SupabaseStub):
    def __init__(self, outcome: str = "inserted", error: Exception | None = None) -> None:
        super().__init__()
        self.outcome = outcome
        self.error = error
        self.slip_calls: list[dict[str, object]] = []

    async def admit_portable_hotmart_payment_slip_event(
        self, **kwargs: object
    ) -> PaymentSlipAdmissionResult:
        self.slip_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return PaymentSlipAdmissionResult(
            self.outcome,
            None if self.outcome == "ignored_binding_mismatch" else "6f1d2c3b-0000-4000-8000-000000000001",
        )


def _app(supabase: _SlipSupabase, **overrides: object) -> object:
    settings = _settings(portable_hotmart_payment_failure_enabled=True, **overrides)
    return create_app(settings, supabase_client=supabase)  # type: ignore[arg-type]


def test_the_cash_capture_is_the_shape_the_slip_events_share() -> None:
    for event in CASH:
        payment = event["payload"]["data"]["purchase"]["payment"]
        assert payment["type"] == "CASHPAYMENT"
        assert {"billet_url", "billet_barcode"} <= set(payment)
        assert event["payload"]["version"] == "2.0.0"


@pytest.mark.parametrize("event_type", sorted(PAYMENT_SLIP_EVENT_TYPES))
def test_a_slip_event_is_stored_whole_against_the_binding(event_type: str) -> None:
    supabase = _SlipSupabase()
    payload = _slip(event_type)

    response = _post(_app(supabase), payload)

    assert response.status_code == 202
    assert response.json() == {"status": "received", "event_id": payload["id"]}
    assert supabase.slip_calls == [
        {"config": _config(), "external_event_id": payload["id"], "payload": payload}
    ]
    assert supabase.payment_failure_calls == []
    assert supabase.calls == []


@pytest.mark.parametrize(
    ("outcome", "body"),
    [
        ("duplicate", {"status": "duplicate", "event_id": "slip-purchase_billet_printed-0"}),
        ("semantic_conflict", {"status": "semantic_conflict", "event_id": "slip-purchase_billet_printed-0"}),
        ("ignored_binding_mismatch", {"status": "ignored", "reason": "payment_slip_binding_mismatch"}),
    ],
)
def test_nothing_to_retry_answers_200(outcome: str, body: dict[str, object]) -> None:
    # Un 4xx lo cuenta Hotmart como falla del webhook entero.
    response = _post(_app(_SlipSupabase(outcome)), _slip())

    assert response.status_code == 200
    assert response.json() == body


def test_a_database_failure_asks_hotmart_to_retry() -> None:
    response = _post(_app(_SlipSupabase(error=SupabaseError("down"))), _slip())

    assert response.status_code == 503
    assert response.json() == {"detail": "payment_slip_admission_failed"}


def test_a_payload_the_database_rejects_is_dropped_with_200() -> None:
    error = SupabasePermanentError("rejected", reason="invalid_portable_hotmart_payment_slip_input")

    response = _post(_app(_SlipSupabase(error=error)), _slip())

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "invalid_payment_slip_payload"}


def test_without_the_payment_failure_flow_the_slip_is_ignored_as_before() -> None:
    supabase = _SlipSupabase()
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]

    response = _post(app, _slip())

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "unsupported_event_type"}
    assert supabase.slip_calls == []


def test_a_stale_slip_is_refused_like_any_event() -> None:
    supabase = _SlipSupabase()
    payload = _slip()
    payload["creation_date"] = int((time.time() - 3600) * 1000)

    response = _post(_app(supabase), payload)

    assert response.status_code == 401
    assert supabase.slip_calls == []


def test_the_classifier_accepts_slips_only_when_asked() -> None:
    payload = _slip("PURCHASE_EXPIRED")

    assert classify_hotmart_event(payload).reason == "unsupported_event_type"
    accepted = classify_hotmart_event(payload, payment_slip_events=True)
    assert accepted.accepted and accepted.event_type == "PURCHASE_EXPIRED"
    other = deepcopy(payload)
    other["event"] = "PURCHASE_REFUNDED"
    assert classify_hotmart_event(other, payment_slip_events=True).reason == "unsupported_event_type"
