from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from bridge.app import Settings, create_app
from bridge.supabase import (
    PilotRuntimeStatus,
    PrecheckoutDelayedFirstTouchReadiness,
)


class SupabaseStatusStub:
    def __init__(
        self,
        status: PilotRuntimeStatus | None = None,
        error: Exception | None = None,
        precheckout_status: PrecheckoutDelayedFirstTouchReadiness | None = None,
        precheckout_error: Exception | None = None,
    ) -> None:
        self.status = status
        self.error = error
        self.precheckout_status = precheckout_status
        self.precheckout_error = precheckout_error
        self.calls = 0
        self.precheckout_calls = 0

    async def get_pilot_runtime_status(self, **_: object) -> PilotRuntimeStatus:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.status is not None
        return self.status

    async def get_precheckout_delayed_first_touch_readiness(
        self,
    ) -> PrecheckoutDelayedFirstTouchReadiness:
        self.precheckout_calls += 1
        if self.precheckout_error is not None:
            raise self.precheckout_error
        assert self.precheckout_status is not None
        return self.precheckout_status


def _settings(
    *, pilot_enabled: bool, precheckout_enabled: bool = False
) -> Settings:
    return Settings(
        webhook_secret="test-secret",
        allowed_jid="disabled@s.whatsapp.net",
        capture_dir=Path("data/captures"),
        max_age_seconds=300,
        pilot_boundary_enabled=pilot_enabled,
        pilot_scope_key="lancemos-cart-recovery" if pilot_enabled else None,
        pilot_scope_version=1 if pilot_enabled else None,
        pilot_tenant_key="lancemos" if pilot_enabled else None,
        pilot_channel_provider=(
            "waba" if pilot_enabled or precheckout_enabled else None
        ),
        pilot_channel_account_ref="opaque-ref" if pilot_enabled else None,
        chatwoot_account_id=1 if precheckout_enabled else None,
        chatwoot_inbox_id=9 if precheckout_enabled else None,
        hotmart_abandonment_timer_worker_enabled=precheckout_enabled,
        precheckout_delayed_first_touch_enabled=precheckout_enabled,
    )


def test_readiness_is_healthy_but_default_off_without_pilot_config() -> None:
    stub = SupabaseStatusStub(error=AssertionError("must not call database"))
    app = create_app(_settings(pilot_enabled=False), supabase_client=stub)  # type: ignore[arg-type]

    response = TestClient(app).get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "pilot_boundary": "disabled",
        "automation_state": "default_off",
        "reason_code": "pilot_boundary_disabled",
        "precheckout_delayed_first_touch": "disabled",
    }
    assert stub.calls == 0


def test_readiness_reports_inactive_pilot_as_deployable_default_off() -> None:
    stub = SupabaseStatusStub(PilotRuntimeStatus(
        configured=True,
        runtime_state="inactive",
        runtime_generation=4,
        reason_code="pilot_runtime_inactive",
    ))
    app = create_app(_settings(pilot_enabled=True), supabase_client=stub)  # type: ignore[arg-type]

    response = TestClient(app).get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "pilot_boundary": "configured",
        "automation_state": "inactive",
        "reason_code": "pilot_runtime_inactive",
        "precheckout_delayed_first_touch": "disabled",
    }
    assert stub.calls == 1


def test_readiness_fails_closed_for_scope_or_runtime_mismatch() -> None:
    stub = SupabaseStatusStub(PilotRuntimeStatus(
        configured=False,
        runtime_state=None,
        runtime_generation=None,
        reason_code="pilot_active_scope_mismatch",
    ))
    app = create_app(_settings(pilot_enabled=True), supabase_client=stub)  # type: ignore[arg-type]

    response = TestClient(app).get("/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "pilot_active_scope_mismatch"}


def test_readiness_sanitizes_dependency_failures() -> None:
    stub = SupabaseStatusStub(error=RuntimeError("secret database detail"))
    app = create_app(_settings(pilot_enabled=True), supabase_client=stub)  # type: ignore[arg-type]

    response = TestClient(app).get("/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "pilot_readiness_unavailable"}
    assert "secret database detail" not in response.text


def _precheckout_status(
    *,
    first_touch_binding_enabled: bool,
    reason_code: str,
) -> PrecheckoutDelayedFirstTouchReadiness:
    return PrecheckoutDelayedFirstTouchReadiness(
        migration_tracking_complete=True,
        scope_configured=True,
        runtime_state="inactive",
        runtime_generation=0,
        timer_binding_enabled=True,
        timer_binding_generation=2,
        first_touch_binding_enabled=first_touch_binding_enabled,
        due_count=0,
        reserved_count=0,
        request_started_count=0,
        delivery_unknown_count=0,
        reason_code=reason_code,
    )


def test_readiness_reports_exact_precheckout_activation_state() -> None:
    stub = SupabaseStatusStub(
        precheckout_status=_precheckout_status(
            first_touch_binding_enabled=True,
            reason_code="precheckout_first_touch_ready",
        )
    )
    settings = _settings(pilot_enabled=False, precheckout_enabled=True)
    app = create_app(
        settings,
        supabase_client=stub,  # type: ignore[arg-type]
        message_sender=object(),  # type: ignore[arg-type]
    )

    response = TestClient(app).get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "pilot_boundary": "disabled",
        "automation_state": "default_off",
        "reason_code": "pilot_boundary_disabled",
        "precheckout_delayed_first_touch": "enabled",
        "precheckout_delayed_database": "precheckout_first_touch_ready",
        "precheckout_delayed_due": "0",
        "precheckout_delayed_reserved": "0",
        "precheckout_delayed_request_started": "0",
        "precheckout_delayed_delivery_unknown": "0",
    }
    assert stub.precheckout_calls == 1


def test_readiness_fails_closed_when_precheckout_process_precedes_binding() -> None:
    stub = SupabaseStatusStub(
        precheckout_status=_precheckout_status(
            first_touch_binding_enabled=False,
            reason_code="first_touch_binding_disabled",
        )
    )
    settings = _settings(pilot_enabled=False, precheckout_enabled=True)
    app = create_app(
        settings,
        supabase_client=stub,  # type: ignore[arg-type]
        message_sender=object(),  # type: ignore[arg-type]
    )

    response = TestClient(app).get("/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "first_touch_binding_disabled"}


def test_readiness_rejects_contradictory_precheckout_ready_snapshot() -> None:
    contradictory = replace(
        _precheckout_status(
            first_touch_binding_enabled=True,
            reason_code="precheckout_first_touch_ready",
        ),
        scope_configured=False,
    )
    stub = SupabaseStatusStub(precheckout_status=contradictory)
    app = create_app(
        _settings(pilot_enabled=False, precheckout_enabled=True),
        supabase_client=stub,  # type: ignore[arg-type]
        message_sender=object(),  # type: ignore[arg-type]
    )

    response = TestClient(app).get("/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "precheckout_delayed_state_mismatch"}
