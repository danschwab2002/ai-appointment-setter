"""El ruteo por producto de los eventos de Hotmart y de los formularios (paso 2c-2).

Fixtures:
- tests/fixtures/hotmart_routing_att1_20261016.json: un evento real de cada caso,
  leido de att1-db el 2026-10-16 y reducido adentro de la base a lo que leen el
  ruteo y los parsers (el comprador y la transaccion son de prueba). Trae los dos
  lugares de la oferta (data.offer en el carrito, data.purchase.offer en el resto)
  y la compra de 83utgyow, la oferta que ningun producto declara.
- Los envios reales de GHL de tests/fixtures/ghl (ads-a y la landing D, del
  2026-09-29), por los helpers de test_ghl_precheckout_adapter_http.

El segundo producto es de prueba (ver test_multi_product_manifest): ATT2 nunca
mando un evento a att1-db. Los eventos del segundo producto se derivan de los
reales de ATT1 cambiando el producto y la oferta, y se nombran asi.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from bridge import worker as worker_module
from bridge.app import Settings, create_app
from bridge.instance_manifest import InstanceManifest
from bridge.product_routes import (
    HOTMART_OFFER_NOT_DECLARED,
    HOTMART_PRODUCT_NOT_DECLARED,
    HotmartUnrouted,
    ProductRoutes,
)
from bridge.worker import ResolutionWorker
from test_ghl_precheckout_adapter_http import (
    ADS_A_FORM,
    LANDING_D_FORM,
    TOKEN,
    _PortableFake,
    _ads_a,
    _landing_d,
    _post as _post_ghl,
)
from test_multi_product_manifest import SECOND

FIXTURES = Path(__file__).parent / "fixtures"
ATT1_TOML = FIXTURES / "instances" / "att1" / "instancia.toml"
CAPTURED = {
    event["caso"]: event["payload"]
    for event in json.loads(
        (FIXTURES / "hotmart_routing_att1_20261016.json").read_text(encoding="utf-8")
    )["eventos"]
}


def _event(caso: str, **changes: object) -> dict:
    """Un evento capturado; con cambios, el derivado para el segundo producto."""

    payload = copy.deepcopy(CAPTURED[caso])
    if "product_id" in changes:
        payload["data"]["product"]["id"] = changes["product_id"]
    if "offer" in changes:
        holder = (
            payload["data"]
            if payload["event"] == "PURCHASE_OUT_OF_SHOPPING_CART"
            else payload["data"]["purchase"]
        )
        holder["offer"]["code"] = changes["offer"]
    if changes:
        payload["id"] = f"{payload['id']}-derivado"
    return payload


def _payload(**flows: bool) -> dict:
    payload = tomllib.loads(ATT1_TOML.read_text(encoding="utf-8"))
    payload["flujos"].update(flows)
    payload["productos"] = [{"clave": "att1"}, copy.deepcopy(SECOND)]
    for product in payload["productos"]:
        product.pop("etiqueta", None)
        product.pop("macro_etiqueta", None)
    return payload


def _manifest(**flows: bool) -> InstanceManifest:
    return InstanceManifest.from_mapping(_payload(**flows))


def _routes(manifest: InstanceManifest | None = None) -> ProductRoutes:
    manifest = manifest or _manifest()
    return ProductRoutes.build(manifest.to_commercial_ally_config(), manifest)


# ------------------------------------------------------------- for_hotmart


def test_the_captured_events_of_att1_go_to_the_principal() -> None:
    routes = _routes()

    for caso in ("carrito_2uafw5bg", "carrito_bmaztyhg", "ficha_impresa_2uafw5bg"):
        assert routes.for_hotmart(_event(caso)) is routes.principal, caso


def test_a_purchase_of_an_offer_no_product_declares_goes_to_its_owner() -> None:
    # 83utgyow (la recuperacion de GHL) no esta en ningun producto: la compra
    # frena con cualquier oferta del producto, como antes de varios productos.
    routes = _routes()

    assert routes.for_hotmart(_event("compra_83utgyow")) is routes.principal
    assert (
        routes.for_hotmart(_event("compra_83utgyow", product_id=8437428)).key == "att2"
    )


def test_each_event_of_the_second_product_goes_to_its_binding() -> None:
    routes = _routes()

    for caso in ("carrito_2uafw5bg", "ficha_impresa_2uafw5bg", "compra_83utgyow"):
        route = routes.for_hotmart(_event(caso, product_id=8437428, offer="prueba2a"))
        assert route.key == "att2", caso
        assert route.config.funnel_ref == "att2"


def test_what_no_product_declares_has_no_route() -> None:
    routes = _routes()

    cart = routes.for_hotmart(_event("carrito_2uafw5bg", product_id=8437428))
    slip = routes.for_hotmart(_event("ficha_impresa_2uafw5bg", offer="zzzz9999"))
    other = routes.for_hotmart(_event("compra_83utgyow", product_id=8104005))

    assert cart == HotmartUnrouted(HOTMART_OFFER_NOT_DECLARED, 8437428, "2uafw5bg")
    assert slip == HotmartUnrouted(HOTMART_OFFER_NOT_DECLARED, 5071808, "zzzz9999")
    assert other == HotmartUnrouted(HOTMART_PRODUCT_NOT_DECLARED, 8104005, "83utgyow")


def test_two_products_in_the_same_hotmart_product_split_by_offer() -> None:
    payload = _payload()
    payload["productos"][1]["hotmart"].update(
        {
            "product_id": 5071808,
            "hotlink": "D98014973Y",
            "product_name": "Alimenta tu Tiroides",
        }
    )
    payload["productos"][1]["hotmart"]["ofertas"][0]["codigo"] = "7szu9m58"
    routes = _routes(InstanceManifest.from_mapping(payload))

    assert routes.for_hotmart(_event("carrito_bmaztyhg")) is routes.principal
    assert routes.for_hotmart(_event("carrito_bmaztyhg", offer="7szu9m58")).key == "att2"
    # La compra de una oferta que no es de ninguno: la del principal, el primero.
    assert routes.for_hotmart(_event("compra_83utgyow")) is routes.principal


def test_with_one_product_everything_goes_to_the_principal_as_before() -> None:
    manifest = InstanceManifest.from_toml_file(ATT1_TOML)
    routes = ProductRoutes.build(manifest.to_commercial_ally_config(), manifest)

    for payload in (
        _event("carrito_2uafw5bg", product_id=8437428),
        _event("compra_83utgyow", product_id=8104005),
        {"event": "PURCHASE_APPROVED"},
        "no es un objeto",
    ):
        assert routes.for_hotmart(payload) is routes.principal


def test_a_payload_without_a_product_goes_to_the_principal_for_the_parser_to_reject() -> None:
    routes = _routes()

    broken = _event("carrito_2uafw5bg")
    broken["data"]["product"]["id"] = "5071808"
    assert routes.for_hotmart(broken) is routes.principal
    assert routes.for_hotmart({"event": "PURCHASE_APPROVED", "data": []}) is routes.principal


def test_a_landing_belongs_to_one_product() -> None:
    routes = _routes()

    assert routes.for_landing("metodoraizana", "ads-a") is routes.principal
    assert routes.for_landing("metodoraizana", "att2-prueba-a").key == "att2"
    assert routes.for_landing("metodoraizana", "otra") is None
    assert routes.for_landing(None, "ads-a") is None
    assert routes.owner_of_landing_id("att2-prueba-b").key == "att2"
    assert routes.owner_of_landing_id("otra") is None


# ------------------------------------------------------------ el webhook


def _fresh(payload: dict) -> dict:
    # La captura es del 14 al 16/10: el webhook rechaza lo viejo (stale).
    payload = copy.deepcopy(payload)
    payload["creation_date"] = int(time.time() * 1000)
    return payload


class _Admissions:
    def __init__(self) -> None:
        self.carts: list[dict] = []
        self.purchases: list[dict] = []

    async def admit_portable_hotmart_cart_abandonment(self, **kwargs: object) -> object:
        self.carts.append(kwargs)
        return SimpleNamespace(outcome="inserted", webhook_event_id="cart-event")

    async def admit_portable_hotmart_purchase_approved(self, **kwargs: object) -> object:
        self.purchases.append(kwargs)
        return SimpleNamespace(outcome="inserted", webhook_event_id="purchase-event")


def _settings(manifest: InstanceManifest, tmp_path: Path) -> Settings:
    return Settings(
        webhook_secret="webhook-secret",
        allowed_jid=None,
        capture_dir=tmp_path,
        max_age_seconds=300,
        instance_manifest=manifest,
        commercial_ally_config=manifest.to_commercial_ally_config(),
        commercial_ally_manifest_path=Path("/runtime/instancia.toml"),
        hermes_model_name=manifest.agent_model_name,
        hotmart_hottok="routing-test-token",
        portable_hotmart_purchase_stop_enabled=True,
        portable_hotmart_recovery_enabled=True,
    )


def _post_hotmart(app: object, payload: dict) -> object:
    with TestClient(app) as client:  # type: ignore[arg-type]
        return client.post(
            "/webhooks/hotmart",
            content=json.dumps(payload),
            headers={"X-HOTMART-HOTTOK": "routing-test-token"},
        )


def test_the_webhook_admits_each_cart_in_the_funnel_of_its_product(
    tmp_path: Path
) -> None:
    supabase = _Admissions()
    app = create_app(_settings(_manifest(carrito=True), tmp_path), supabase_client=supabase)  # type: ignore[arg-type]

    first = _post_hotmart(app, _fresh(_event("carrito_2uafw5bg")))
    second = _post_hotmart(
        app, _fresh(_event("carrito_2uafw5bg", product_id=8437428, offer="prueba2a"))
    )

    assert (first.status_code, second.status_code) == (202, 202)
    assert [call["config"].funnel_ref for call in supabase.carts] == ["att1", "att2"]


def test_the_webhook_stops_with_a_purchase_of_any_offer_of_the_product(
    tmp_path: Path
) -> None:
    supabase = _Admissions()
    app = create_app(_settings(_manifest(carrito=True), tmp_path), supabase_client=supabase)  # type: ignore[arg-type]

    response = _post_hotmart(app, _fresh(_event("compra_83utgyow")))

    assert response.status_code == 202
    assert [call["config"].funnel_ref for call in supabase.purchases] == ["att1"]


def test_an_event_of_no_product_is_ignored_and_leaves_its_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    supabase = _Admissions()
    app = create_app(_settings(_manifest(carrito=True), tmp_path), supabase_client=supabase)  # type: ignore[arg-type]

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        response = _post_hotmart(app, _fresh(_event("carrito_2uafw5bg", product_id=8104005)))

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "hotmart_event_unrouted"}
    assert supabase.carts == []
    lines = [record.getMessage() for record in caplog.records]
    assert any(
        "hotmart_event_unrouted" in line
        and "product_id=8104005" in line
        and "offer=2uafw5bg" in line
        and "reason=product_not_declared" in line
        for line in lines
    ), lines
    # Sin datos personales en la linea.
    assert not any("comprador" in line or "5215550000001" in line for line in lines)


def test_with_one_product_an_event_of_another_product_is_rejected_as_before(
    tmp_path: Path,
) -> None:
    payload = tomllib.loads(ATT1_TOML.read_text(encoding="utf-8"))
    payload["flujos"]["carrito"] = True
    manifest = InstanceManifest.from_mapping(payload)
    supabase = _Admissions()
    app = create_app(_settings(manifest, tmp_path), supabase_client=supabase)  # type: ignore[arg-type]

    response = _post_hotmart(app, _fresh(_event("carrito_2uafw5bg", product_id=8437428)))

    assert response.status_code == 200
    assert response.json()["reason"] == "invalid_cart_abandonment_payload"
    assert supabase.carts == []


# ---------------------------------------------------------------- el worker


class _Statuses:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    async def update_event_status(self, **kwargs: object) -> None:
        self.updates.append(kwargs)


def test_the_worker_does_not_plan_an_event_of_a_product_without_flows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved: list[str] = []

    async def resolve(**kwargs: object) -> object:
        resolved.append(str(kwargs["webhook_event_id"]))
        return SimpleNamespace(event_id=kwargs["webhook_event_id"])

    monkeypatch.setattr(worker_module, "resolve_event", resolve)
    supabase = _Statuses()
    resolution = ResolutionWorker(supabase=supabase, product_routes=_routes())  # type: ignore[arg-type]

    second = _event("carrito_2uafw5bg", product_id=8437428, offer="prueba2a")
    asyncio.run(
        resolution._process_one(
            {"id": "event-att2", "payload": second, "event_type": second["event"]}
        )
    )
    first = _event("carrito_2uafw5bg")
    asyncio.run(
        resolution._process_one(
            {"id": "event-att1", "payload": first, "event_type": first["event"]}
        )
    )

    assert supabase.updates == [
        {"event_id": "event-att2", "status": "ignored", "error": "product_without_recovery_flows"}
    ]
    assert resolved == ["event-att1"]


def test_with_one_product_the_worker_does_not_route() -> None:
    manifest = InstanceManifest.from_toml_file(ATT1_TOML)
    routes = ProductRoutes.build(manifest.to_commercial_ally_config(), manifest)
    resolution = ResolutionWorker(supabase=_Statuses(), product_routes=routes)  # type: ignore[arg-type]

    assert len(resolution._product_routes) == 1


# ------------------------------------------------------------------- GHL


def _ghl_manifest(mode: str | None = None) -> InstanceManifest:
    """ATT1 con la landing D movida al segundo producto de prueba."""

    payload = _payload()
    payload["eventos"] = [*payload["eventos"], "intencion"]
    landing_d = next(
        offer for offer in payload["hotmart"]["ofertas"] if offer["codigo"] == "2uafw5bg"
    )
    payload["hotmart"]["ofertas"].remove(landing_d)
    payload["productos"][1]["hotmart"]["ofertas"] = [
        {**landing_d, "codigo": "prueba2a", "por_defecto": True}
    ]
    ghl: dict[str, object] = {"formularios": [ADS_A_FORM, LANDING_D_FORM]}
    if mode is not None:
        ghl["landing_por_formulario"] = {LANDING_D_FORM: "alimenta-tu-tiroides-d"}
        ghl["landing_por_formulario_modo"] = mode
    payload["adaptadores"] = {"ghl": ghl}
    return InstanceManifest.from_mapping(payload)


def _ghl_app(manifest: InstanceManifest) -> tuple[object, _PortableFake]:
    settings = replace(
        Settings(
            webhook_secret="chatwoot-webhook-secret",
            allowed_jid=None,
            capture_dir=Path("/tmp/ghl-routing"),
            max_age_seconds=300,
        ),
        commercial_ally_config=manifest.to_commercial_ally_config(),
        commercial_ally_manifest_path=Path("/runtime/instancia.toml"),
        instance_manifest=manifest,
        hermes_model_name=manifest.agent_model_name,
        ghl_precheckout_adapter_enabled=True,
        ghl_precheckout_adapter_token=TOKEN,
    )
    fake = _PortableFake()
    return create_app(settings, supabase_client=fake), fake  # type: ignore[arg-type]


def _ads_a_url() -> str:
    return _ads_a()["attributionSource"]["url"]


def test_ghl_admits_each_real_submission_in_the_funnel_of_its_landing(
) -> None:
    app, fake = _ghl_app(_ghl_manifest())

    ads_a = _post_ghl(app, _ads_a())
    landing_d = _post_ghl(app, _landing_d())

    assert (ads_a.status_code, landing_d.status_code) == (200, 200)
    assert [call["config"].funnel_ref for call in fake.calls] == ["att1", "att2"]
    assert fake.calls[1]["canonical_payload"]["commerce"]["offer_ref"] == "prueba2a"


def test_ghl_in_active_mode_follows_the_landing_of_the_form() -> None:
    # El formulario de la landing D llega con la URL de ads-a (pasa: GHL manda la
    # pagina donde empezo la visita). En activo manda el formulario.
    body = _landing_d()
    body["attributionSource"]["url"] = _ads_a_url()
    active, active_fake = _ghl_app(_ghl_manifest("activo"))
    shadow, shadow_fake = _ghl_app(_ghl_manifest("sombra"))

    assert _post_ghl(active, copy.deepcopy(body)).status_code == 200
    assert _post_ghl(shadow, copy.deepcopy(body)).status_code == 200

    assert [call["config"].funnel_ref for call in active_fake.calls] == ["att2"]
    # En sombra la landing sigue saliendo de la URL, como siempre.
    assert [call["config"].funnel_ref for call in shadow_fake.calls] == ["att1"]


def test_ghl_with_a_url_of_no_product_is_the_rejection_of_the_principal(
) -> None:
    app, fake = _ghl_app(_ghl_manifest())
    body = _ads_a()
    body["attributionSource"]["url"] = "https://otra.example.test/pagina"

    response = _post_ghl(app, body)

    assert response.status_code == 422
    assert response.json()["detail"] == "ghl_landing_unknown"
    assert fake.calls == []
