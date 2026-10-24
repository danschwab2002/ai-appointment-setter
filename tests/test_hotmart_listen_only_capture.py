"""El modo «solo guardar» de Hotmart (E-057, migracion 20261024000100).

Con PORTABLE_HOTMART_CAPTURE_ENABLED, una instancia con todos sus flujos en
false (Maribel, toda aliada nueva) guarda el carrito y el pago rechazado con la
admision portable de hoy y los deja ignored / listen_only_capture, sin atarse a
[flujos] ni a [plantillas]. Sin el flag, todo sigue como en la 1.17.1.

Fixtures capturados:
- el carrito: tests/fixtures/hotmart_routing_att1_20261016.json (att1-db, el
  2026-10-16, reducido adentro de la base; comprador de prueba);
- el pago rechazado: tests/fixtures/hotmart_purchase_canceled_att1_20261019.json
  (att1-db, el 2026-10-19, anonimizado por lista blanca).
El manifiesto es el de ATT1 de tests/fixtures/instances/att1/instancia.toml, que
tiene los seis flujos en false: es la forma de una instancia en solo escucha.

Que el worker no tome nunca lo capturado (ni con los flujos prendidos despues)
lo prueba la base: validate_portable_hotmart_listen_only_capture.mjs.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge.app import Settings, create_app
from bridge.instance_manifest import InstanceManifest
from bridge.supabase import SupabaseClient, SupabaseError, SupabasePermanentError

FIXTURES = Path(__file__).parent / "fixtures"
ATT1_TOML = FIXTURES / "instances" / "att1" / "instancia.toml"
TOKEN = "capture-test-token"
CART = next(
    event["payload"]
    for event in json.loads(
        (FIXTURES / "hotmart_routing_att1_20261016.json").read_text(encoding="utf-8")
    )["eventos"]
    if event["caso"] == "carrito_2uafw5bg"
)
CANCELED = json.loads(
    (FIXTURES / "hotmart_purchase_canceled_att1_20261019.json").read_text(
        encoding="utf-8"
    )
)["eventos"][0]["payload"]
# Lo que no puede salir en ninguna linea del log: el comprador de los fixtures.
PERSONAL = (
    "comprador.prueba@example.test",
    "5215550000001",
    "persona.prueba1@example.com",
    "520000000001",
    "Comprador Prueba",
    "Persona Prueba",
)


def _fresh(payload: dict) -> dict:
    # Las capturas son de octubre: el webhook rechaza lo viejo (stale).
    payload = copy.deepcopy(payload)
    payload["creation_date"] = int(time.time() * 1000)
    return payload


def _manifest(**flows: bool) -> InstanceManifest:
    payload = tomllib.loads(ATT1_TOML.read_text(encoding="utf-8"))
    payload["flujos"].update(flows)
    return InstanceManifest.from_mapping(payload)


def _settings(manifest: InstanceManifest | None = None, **overrides: object) -> Settings:
    manifest = manifest or _manifest()
    values: dict[str, object] = {
        "webhook_secret": "webhook-secret",
        "allowed_jid": None,
        "capture_dir": Path("/tmp/listen-only-capture-tests"),
        "max_age_seconds": 300,
        "instance_manifest": manifest,
        "commercial_ally_config": manifest.to_commercial_ally_config(),
        "commercial_ally_manifest_path": Path("/runtime/instancia.toml"),
        "hermes_model_name": manifest.agent_model_name,
        "hotmart_hottok": TOKEN,
        "portable_hotmart_purchase_stop_enabled": True,
        "portable_hotmart_capture_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


class _Admissions:
    """Cuenta cada admision; ninguna escribe nada."""

    def __init__(self, capture: object = None) -> None:
        self.captures: list[dict] = []
        self.carts: list[dict] = []
        self.failures: list[dict] = []
        self.purchases: list[dict] = []
        self._capture = capture or SimpleNamespace(
            outcome="inserted",
            webhook_event_id="0b6c1c84-35ab-4c55-9d43-4c49cc6f6a01",
        )

    async def admit_portable_hotmart_listen_only_capture(self, **kwargs: object) -> object:
        self.captures.append(kwargs)
        if isinstance(self._capture, Exception):
            raise self._capture
        return self._capture

    async def admit_portable_hotmart_cart_abandonment(self, **kwargs: object) -> object:
        self.carts.append(kwargs)
        return SimpleNamespace(outcome="inserted", webhook_event_id="cart-event")

    async def admit_portable_hotmart_payment_failure(self, **kwargs: object) -> object:
        self.failures.append(kwargs)
        return SimpleNamespace(outcome="inserted", webhook_event_id="failure-event")

    async def admit_portable_hotmart_purchase_approved(self, **kwargs: object) -> object:
        self.purchases.append(kwargs)
        return SimpleNamespace(outcome="inserted", webhook_event_id="purchase-event")

    def flow_admissions(self) -> list[dict]:
        return self.carts + self.failures


def _post(app: object, payload: dict) -> httpx.Response:
    with TestClient(app) as client:  # type: ignore[arg-type]
        return client.post(
            "/webhooks/hotmart",
            content=json.dumps(payload),
            headers={"X-HOTMART-HOTTOK": TOKEN},
        )


# ------------------------------------------------------------- el arranque


def test_capture_boots_on_an_instance_with_every_flow_off() -> None:
    manifest = _manifest()
    assert not any(manifest.flow_enabled(flow) for flow in (
        "inbound", "precheckout", "carrito", "pago_fallido", "reactivacion", "descuento",
    ))

    assert create_app(_settings(manifest), supabase_client=_Admissions()) is not None  # type: ignore[arg-type]


def test_the_flow_flags_still_need_their_flow_with_capture_on() -> None:
    # El techo del manifiesto no se mueve: capturar no habilita el flag del flujo.
    with pytest.raises(ValueError, match="portable_hotmart_recovery_enabled->carrito"):
        create_app(_settings(portable_hotmart_recovery_enabled=True))
    with pytest.raises(
        ValueError, match="portable_hotmart_payment_failure_enabled->pago_fallido"
    ):
        create_app(_settings(portable_hotmart_payment_failure_enabled=True))


def test_capture_requires_the_purchase_stop() -> None:
    with pytest.raises(ValueError):
        create_app(_settings(portable_hotmart_purchase_stop_enabled=False))


def test_capture_requires_an_instance_manifest() -> None:
    settings = _settings(
        instance_manifest=None,
        commercial_ally_manifest_path=None,
        hotmart_hottok=None,
        portable_hotmart_purchase_stop_enabled=False,
    )
    with pytest.raises(ValueError):
        create_app(settings)


def test_capture_environment_contract_is_default_off() -> None:
    root = Path(__file__).resolve().parents[1]
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    compose = (root / "compose.yaml").read_text(encoding="utf-8")

    assert "PORTABLE_HOTMART_CAPTURE_ENABLED=false" in env_example
    assert (
        "PORTABLE_HOTMART_CAPTURE_ENABLED: "
        "${PORTABLE_HOTMART_CAPTURE_ENABLED:-false}" in compose
    )
    assert Settings.__dataclass_fields__[
        "portable_hotmart_capture_enabled"
    ].default is False


# ------------------------------------------------------------- el webhook


@pytest.mark.parametrize(
    ("payload", "event_type"),
    [(CART, "PURCHASE_OUT_OF_SHOPPING_CART"), (CANCELED, "PURCHASE_CANCELED")],
)
def test_with_capture_the_cart_and_the_rejection_are_stored_without_a_flow(
    payload: dict, event_type: str, caplog: pytest.LogCaptureFixture
) -> None:
    supabase = _Admissions()
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]
    event = _fresh(payload)

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        response = _post(app, event)

    assert response.status_code == 202
    assert response.json() == {"status": "captured", "event_id": event["id"]}
    assert len(supabase.captures) == 1
    call = supabase.captures[0]
    assert call["external_event_id"] == event["id"]
    assert call["payload"] == event
    assert call["config"].tenant_ref == "lancemos"
    assert call["config"].funnel_ref == _manifest().to_commercial_ally_config().funnel_ref
    # La identidad normalizada, la misma que la admision del flujo.
    assert call["normalized_email"] is not None
    assert call["normalized_phone"] is not None
    # Ninguna admision del flujo: nada que el worker vaya a planificar.
    assert supabase.flow_admissions() == []
    lines = [record.getMessage() for record in caplog.records]
    assert any(
        "hotmart_listen_only_captured" in line
        and f"event_type={event_type}" in line
        and "outcome=inserted" in line
        for line in lines
    ), lines
    assert not any(value in line for line in lines for value in PERSONAL), lines


@pytest.mark.parametrize("payload", [CART, CANCELED])
def test_without_capture_both_events_are_filtered_as_today_and_leave_their_line(
    payload: dict, caplog: pytest.LogCaptureFixture
) -> None:
    supabase = _Admissions()
    app = create_app(
        _settings(portable_hotmart_capture_enabled=False),
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    event = _fresh(payload)

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        response = _post(app, event)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "reason": "portable_purchase_stop_event_ignored",
    }
    assert supabase.captures == []
    assert supabase.flow_admissions() == []
    lines = [record.getMessage() for record in caplog.records]
    assert any(
        "hotmart_event_filtered" in line
        and f"event_id={event['id']}" in line
        and f"event_type={event['event']}" in line
        for line in lines
    ), lines
    assert not any(value in line for line in lines for value in PERSONAL), lines


def test_the_purchase_still_goes_by_the_purchase_stop_with_capture_on() -> None:
    supabase = _Admissions()
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]
    purchase = next(
        event["payload"]
        for event in json.loads(
            (FIXTURES / "hotmart_routing_att1_20261016.json").read_text(encoding="utf-8")
        )["eventos"]
        if event["caso"] == "compra_83utgyow"
    )

    response = _post(app, _fresh(purchase))

    assert response.status_code == 202
    assert len(supabase.purchases) == 1
    assert supabase.captures == []


def test_with_the_flow_on_the_flow_wins_over_capture() -> None:
    supabase = _Admissions()
    app = create_app(
        _settings(
            _manifest(carrito=True),
            portable_hotmart_recovery_enabled=True,
        ),
        supabase_client=supabase,  # type: ignore[arg-type]
    )

    response = _post(app, _fresh(CART))

    assert response.status_code == 202
    assert len(supabase.carts) == 1
    assert supabase.captures == []


def test_a_capture_rejected_by_the_binding_is_ignored_with_its_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    supabase = _Admissions(
        SupabasePermanentError(
            "rejected",
            reason="portable_hotmart_payment_failure_binding_mismatch",
        )
    )
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]
    event = _fresh(CANCELED)

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        response = _post(app, event)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "event_id": event["id"],
        "reason": "portable_hotmart_payment_failure_binding_mismatch",
    }
    assert any(
        "hotmart_event_rejected" in record.getMessage()
        and "binding_mismatch" in record.getMessage()
        for record in caplog.records
    )


def test_a_capture_that_could_not_be_written_answers_503_for_hotmart_to_retry() -> None:
    supabase = _Admissions(SupabaseError("listen_only_capture_mark_failed"))
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]

    response = _post(app, _fresh(CART))

    assert response.status_code == 503
    assert response.json() == {"detail": "listen_only_capture_unavailable"}


def test_a_retried_capture_is_a_duplicate() -> None:
    supabase = _Admissions(
        SimpleNamespace(
            outcome="duplicate",
            webhook_event_id="0b6c1c84-35ab-4c55-9d43-4c49cc6f6a01",
        )
    )
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]
    event = _fresh(CART)

    response = _post(app, event)

    assert response.status_code == 200
    assert response.json() == {"status": "duplicate", "event_id": event["id"]}


def test_a_cart_of_an_offer_outside_the_binding_never_reaches_the_base() -> None:
    supabase = _Admissions()
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]
    event = _fresh(CART)
    event["data"]["offer"]["code"] = "zzzz9999"

    response = _post(app, event)

    assert response.status_code == 200
    assert supabase.captures == []


# ------------------------------------------------------------- el cliente


def _client(handler) -> SupabaseClient:  # type: ignore[no-untyped-def]
    return SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )


def _capture(client: SupabaseClient) -> object:
    return asyncio.run(client.admit_portable_hotmart_listen_only_capture(
        config=_manifest().to_commercial_ally_config(),
        external_event_id=CART["id"],
        payload=CART,
        normalized_email="comprador.prueba@example.test",
        normalized_phone="5215550000001",
    ))


def test_the_client_calls_the_capture_rpc_with_the_binding_identity() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{
            "outcome": "inserted",
            "webhook_event_id": "0b6c1c84-35ab-4c55-9d43-4c49cc6f6a01",
        }])

    result = _capture(_client(handler))

    config = _manifest().to_commercial_ally_config()
    assert result.outcome == "inserted"  # type: ignore[attr-defined]
    assert requests[0].url.path == (
        "/rest/v1/rpc/admit_portable_hotmart_listen_only_capture_v1"
    )
    assert json.loads(requests[0].content) == {
        "p_tenant_ref": config.tenant_ref,
        "p_funnel_ref": config.funnel_ref,
        "p_binding_version": config.binding_version,
        "p_external_event_id": CART["id"],
        "p_payload": CART,
        "p_normalized_email": "comprador.prueba@example.test",
        "p_normalized_phone": "5215550000001",
    }


def test_the_client_maps_a_data_exception_to_permanent_and_the_rest_to_retry() -> None:
    def rejected(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "code": "22023", "message": "portable_hotmart_cart_binding_mismatch",
        })

    def mark_failed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "code": "55000", "message": "listen_only_capture_mark_failed",
        })

    with pytest.raises(SupabasePermanentError) as permanent:
        _capture(_client(rejected))
    assert permanent.value.reason == "portable_hotmart_cart_binding_mismatch"
    with pytest.raises(SupabaseError) as retry:
        _capture(_client(mark_failed))
    assert not isinstance(retry.value, SupabasePermanentError)
