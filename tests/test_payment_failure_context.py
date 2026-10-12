"""El ultimo pago rechazado en la entrada del agente (1.6.0).

Decision del 2026-10-12 «como se resuelve cada pago que no se completa»: el
agente recibe el motivo, el medio, las cuotas y el pais del pago rechazado, y la
ruta «Compra fallida» del SOUL comun los usa con la seccion de pagos del
conocimiento. Los valores salen de los cuatro PURCHASE_CANCELED reales de ATT1
(tests/fixtures/hotmart_purchase_canceled_att1_20261012.json); la consulta en
si se prueba contra la base en
tests/sql/followup_engine/validate_portable_payment_failure_context.mjs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from bridge.app import recent_payment_failure_context
from bridge.supabase import RecentPaymentFailure, SupabaseClient, SupabaseError
from test_hotmart import _config
from test_whatsapp_inbound_equivalence import (
    FORM_ID,
    WA_ID,
    _manifest_settings,
    _process,
    _Supabase,
    _text_webhook,
)

CAPTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "hotmart_purchase_canceled_att1_20261012.json").read_text(
        encoding="utf-8"
    )
)


def _captured_failure(index: int = 0) -> RecentPaymentFailure:
    event = CAPTURE["eventos"][index]
    purchase = event["payload"]["data"]["purchase"]
    return RecentPaymentFailure(
        failed_at=datetime.fromisoformat(event["recibido"].replace("Z", "+00:00")),
        offer_code=purchase["offer"]["code"],
        payment_type=purchase["payment"]["type"],
        installments_number=purchase["payment"]["installments_number"],
        refusal_reason=purchase["payment"].get("refusal_reason"),
        checkout_country_iso=purchase["checkout_country"]["iso"],
    )


# ------------------------------------------------------------ lo que ve el agente


def test_the_agent_gets_method_installments_reason_and_country_as_captured() -> None:
    assert recent_payment_failure_context(_captured_failure(0)) == {
        "failed_at": "2026-10-11T15:07:00Z",
        "payment_method": "CREDIT_CARD",
        "installments": 3,
        "refusal_reason": "Transaction refused",
        "checkout_country": "MX",
    }


def test_the_reason_reaches_the_agent_in_the_language_it_came() -> None:
    panama = next(
        index
        for index, event in enumerate(CAPTURE["eventos"])
        if event["payload"]["data"]["purchase"]["checkout_country"]["iso"] == "PA"
    )

    context = recent_payment_failure_context(_captured_failure(panama))

    assert context["checkout_country"] == "PA"
    assert context["refusal_reason"].startswith("Saldo insuficiente para o cartão")


def test_a_missing_value_is_left_out_not_sent_as_null() -> None:
    failure = replace(_captured_failure(0), refusal_reason=None, installments_number=None)

    context = recent_payment_failure_context(failure)

    assert "refusal_reason" not in context
    assert "installments" not in context
    assert set(context) == {"failed_at", "payment_method", "checkout_country"}


def test_the_offer_and_nothing_personal_stay_out_of_the_context() -> None:
    context = recent_payment_failure_context(_captured_failure(0))

    assert "offer_code" not in context
    assert not any(key in context for key in ("email", "phone", "name", "transaction"))


# ------------------------------------------------------------- el cliente


def _client(handler) -> SupabaseClient:
    return SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "failed_at": "2026-10-11T15:07:44.863408+00:00",
        "offer_code": "2uafw5bg",
        "payment_type": "CREDIT_CARD",
        "installments_number": 3,
        "refusal_reason": "Transaction refused",
        "checkout_country_iso": "MX",
    }
    row.update(overrides)
    return row


def test_the_lookup_sends_the_binding_and_the_wa_id() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        observed["body"] = json.loads(request.content)
        return httpx.Response(200, json=[_row()])

    failure = asyncio.run(
        _client(handler).get_portable_recent_payment_failure(
            config=_config(), whatsapp_phone=WA_ID
        )
    )

    assert observed == {
        "path": "/rest/v1/rpc/get_portable_recent_payment_failure_v1",
        "body": {"p_tenant_ref": "att1", "p_funnel_ref": "att1-main", "p_whatsapp_phone": WA_ID},
    }
    assert failure == RecentPaymentFailure(
        failed_at=datetime(2026, 10, 11, 15, 7, 44, 863408, tzinfo=UTC),
        offer_code="2uafw5bg",
        payment_type="CREDIT_CARD",
        installments_number=3,
        refusal_reason="Transaction refused",
        checkout_country_iso="MX",
    )


def test_no_row_means_no_failure() -> None:
    failure = asyncio.run(
        _client(lambda _: httpx.Response(200, json=[])).get_portable_recent_payment_failure(
            config=_config(), whatsapp_phone=WA_ID
        )
    )

    assert failure is None


@pytest.mark.parametrize(
    "row",
    [
        _row(extra="x"),
        _row(installments_number="3"),
        _row(installments_number=True),
        _row(failed_at=None),
        _row(failed_at="ayer"),
        _row(refusal_reason=7),
    ],
)
def test_an_unexpected_row_is_an_error_not_a_context(row: dict[str, object]) -> None:
    with pytest.raises(SupabaseError):
        asyncio.run(
            _client(lambda _: httpx.Response(200, json=[row])).get_portable_recent_payment_failure(
                config=_config(), whatsapp_phone=WA_ID
            )
        )


# ------------------------------------------------------ el turno del agente


class _FailureSupabase(_Supabase):
    def __init__(
        self,
        *,
        failure: RecentPaymentFailure | None = None,
        failure_error: bool = False,
        identities: tuple[str, ...] = (),
    ) -> None:
        super().__init__(identities=identities)
        self.failure = failure
        self.failure_error = failure_error
        self.failure_lookups: list[dict[str, object]] = []

    async def get_portable_recent_payment_failure(
        self, **kwargs: object
    ) -> RecentPaymentFailure | None:
        self.failure_lookups.append(kwargs)
        if self.failure_error:
            raise SupabaseError("portable_recent_payment_failure_get_failed: HTTP 503")
        return self.failure


def _turn(tmp_path: Path, supabase: _FailureSupabase, *, settings=None):
    webhook, history = _text_webhook("Me rechazaron la tarjeta, ¿qué hago?")
    return _process(tmp_path, supabase, settings=settings, webhook=webhook, history=history)


def _payment_failure_settings(tmp_path: Path):
    settings = _manifest_settings(tmp_path)
    manifest = settings.instance_manifest
    assert manifest is not None
    return replace(
        settings,
        instance_manifest=replace(manifest, flows={**manifest.flows, "pago_fallido": True}),
        portable_hotmart_payment_failure_enabled=True,
    )


def test_the_agent_turn_carries_the_recent_failure(tmp_path: Path) -> None:
    supabase = _FailureSupabase(failure=_captured_failure(0))
    settings = _payment_failure_settings(tmp_path)

    _, shadow, _ = _turn(tmp_path, supabase, settings=settings)

    [lookup] = supabase.failure_lookups
    assert lookup["whatsapp_phone"] == WA_ID
    assert lookup["config"] == settings.commercial_ally_config
    assert shadow.calls[0][1]["recent_payment_failure"] == recent_payment_failure_context(
        _captured_failure(0)
    )


def test_the_lookup_uses_the_identity_the_form_stored(tmp_path: Path) -> None:
    # 52 + 10 del formulario; la base compara las dos formas.
    supabase = _FailureSupabase(failure=_captured_failure(0), identities=(FORM_ID,))

    _turn(tmp_path, supabase, settings=_payment_failure_settings(tmp_path))

    assert [lookup["whatsapp_phone"] for lookup in supabase.failure_lookups] == [FORM_ID]


def test_without_a_recent_failure_the_context_is_as_before(tmp_path: Path) -> None:
    supabase = _FailureSupabase(failure=None)

    _, shadow, _ = _turn(tmp_path, supabase, settings=_payment_failure_settings(tmp_path))

    assert len(supabase.failure_lookups) == 1
    assert "recent_payment_failure" not in shadow.calls[0][1]


def test_a_failed_lookup_does_not_stop_the_reply(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    supabase = _FailureSupabase(failure_error=True)

    with caplog.at_level(logging.WARNING):
        chatwoot, shadow, _ = _turn(
            tmp_path, supabase, settings=_payment_failure_settings(tmp_path)
        )

    assert len(shadow.calls) == 1
    assert "recent_payment_failure" not in shadow.calls[0][1]
    assert chatwoot.reply_calls
    assert "recent_payment_failure_lookup_failed" in caplog.text
    assert WA_ID not in caplog.text


def test_without_the_payment_failure_flow_nothing_is_looked_up(tmp_path: Path) -> None:
    supabase = _FailureSupabase(failure=_captured_failure(0))

    _, shadow, _ = _turn(tmp_path, supabase)

    assert supabase.failure_lookups == []
    assert "recent_payment_failure" not in shadow.calls[0][1]
