from pathlib import Path

from fastapi.testclient import TestClient

from bridge.app import Settings, create_app
from bridge.supabase import PilotRuntimeStatus


class SupabaseStatusStub:
    def __init__(
        self,
        status: PilotRuntimeStatus | None = None,
        error: Exception | None = None,
    ) -> None:
        self.status = status
        self.error = error
        self.calls = 0

    async def get_pilot_runtime_status(self, **_: object) -> PilotRuntimeStatus:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.status is not None
        return self.status


def _settings(*, pilot_enabled: bool) -> Settings:
    return Settings(
        webhook_secret="test-secret",
        allowed_jid="disabled@s.whatsapp.net",
        capture_dir=Path("data/captures"),
        max_age_seconds=300,
        pilot_boundary_enabled=pilot_enabled,
        pilot_scope_key="lancemos-cart-recovery" if pilot_enabled else None,
        pilot_scope_version=1 if pilot_enabled else None,
        pilot_tenant_key="lancemos" if pilot_enabled else None,
        pilot_channel_provider="waba" if pilot_enabled else None,
        pilot_channel_account_ref="opaque-ref" if pilot_enabled else None,
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
