"""Varios productos por aliada en el manifiesto y sus rutas (paso 2c-1).

El principal sigue en ``[hotmart]`` y en ``[instancia]``; desde el segundo, cada
producto trae su funnel y su Hotmart. El manifiesto de ATT1
(tests/fixtures/instances/att1) no cambia.

El segundo producto es de prueba. El id de Hotmart 8437428 es el de ATT2
(relevado el 2026-10-15 en el catalogo de Lancemos), y las ofertas 7szu9m58 y
42urk6fc son las viejas de ATT2 dentro del producto 5071808 de ATT1 (mismo
relevamiento). El hotlink, el nombre, el precio y las landings del segundo
producto son de prueba: ATT2 nunca mando un evento a att1-db (medido el
2026-10-16), asi que no hay un dato real de su ficha.
"""

from __future__ import annotations

import copy
import shutil
import tomllib
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bridge import product_routes
from bridge.app import Settings, create_app
from bridge.commercial_ally import CommercialAllyConfig
from bridge.instance_cli import validate_instance
from bridge.instance_manifest import InstanceManifest, ManifestError
from bridge.product_routes import ProductRoute, ProductRoutes

ATT1 = Path(__file__).parent / "fixtures" / "instances" / "att1"

SECOND = {
    "clave": "att2",
    "funnel_ref": "att2",
    "binding_version": 1,
    "etiqueta": "att2",
    "macro_etiqueta": 8,
    "hotmart": {
        "product_id": 8437428,
        "hotlink": "P8437428PRUEBA",
        "product_name": "Producto de prueba ATT2",
        "moneda": "USD",
        "precio": "67",
        "ofertas": [
            {
                "codigo": "prueba2a",
                "site": "metodoraizana",
                "landing_id": "att2-prueba-a",
                "url": "https://www.metodoraizana.com/att2/prueba-a",
                "origen": "pauta",
                "por_defecto": True,
            },
            {
                "codigo": "prueba2b",
                "site": "metodoraizana",
                "landing_id": "att2-prueba-b",
                "url": "https://www.metodoraizana.com/att2/prueba-b",
                "origen": "organico",
            },
        ],
    },
}


def _payload(*extra: dict) -> dict:
    payload = tomllib.loads((ATT1 / "instancia.toml").read_text(encoding="utf-8"))
    payload["productos"] = [{"clave": "att1", "etiqueta": "att1", "macro_etiqueta": 7}]
    payload["productos"].extend(copy.deepcopy(product) for product in extra)
    return payload


def _second(**changes: object) -> dict:
    product = copy.deepcopy(SECOND)
    hotmart_changes = changes.pop("hotmart", {})
    product.update(changes)
    product["hotmart"].update(hotmart_changes)
    return product


def _two() -> InstanceManifest:
    return InstanceManifest.from_mapping(_payload(SECOND))


# ------------------------------------------------------------------ manifiesto


def test_the_att1_manifest_is_one_product_and_one_binding() -> None:
    manifest = InstanceManifest.from_toml_file(ATT1 / "instancia.toml")

    assert manifest.to_commercial_ally_configs() == (manifest.to_commercial_ally_config(),)
    routes = ProductRoutes.build(manifest.to_commercial_ally_config(), manifest)
    assert len(routes) == 1
    assert routes.principal == ProductRoute(
        key=None, config=manifest.to_commercial_ally_config(), principal=True
    )


def test_the_principal_alone_in_productos_is_still_one_binding() -> None:
    manifest = InstanceManifest.from_mapping(_payload())

    assert manifest.products[0].commerce is None
    assert manifest.to_commercial_ally_configs() == (manifest.to_commercial_ally_config(),)
    routes = ProductRoutes.build(manifest.to_commercial_ally_config(), manifest)
    assert [(route.key, route.principal) for route in routes] == [("att1", True)]


def test_a_second_product_brings_its_funnel_and_its_hotmart() -> None:
    manifest = _two()

    second = manifest.products[1]
    assert second.key == "att2"
    assert (second.chatwoot_label, second.chatwoot_label_macro_id) == ("att2", 8)
    assert second.commerce is not None
    assert second.commerce.funnel_ref == "att2"
    assert second.commerce.hotmart_product_id == 8437428
    assert second.commerce.price == Decimal("67")
    assert [offer.code for offer in second.commerce.offers] == ["prueba2a", "prueba2b"]
    # El principal no cambia: sigue saliendo de [hotmart] y de [instancia].
    assert manifest.funnel_ref == "att1"
    assert manifest.hotmart_product_id == 5071808


def test_each_product_is_its_own_binding_with_the_instance_in_common() -> None:
    manifest = _two()

    principal, second = manifest.to_commercial_ally_configs()

    assert principal == manifest.to_commercial_ally_config()
    assert (
        second.funnel_ref,
        second.binding_version,
        second.hotmart_product_id,
        second.product_hotlink,
        second.product_name,
        second.product_price,
        second.currency,
        second.offer_code,
        second.additional_offer_codes,
        (second.lead_site, second.lead_landing_id, second.lead_page_host, second.lead_page_path),
    ) == (
        "att2",
        1,
        8437428,
        "P8437428PRUEBA",
        "Producto de prueba ATT2",
        Decimal("67"),
        "USD",
        "prueba2a",
        ("prueba2b",),
        ("metodoraizana", "att2-prueba-a", "www.metodoraizana.com", "/att2/prueba-a"),
    )
    # Lo que es de la instancia es lo mismo en los dos bindings.
    for field in (
        "tenant_ref",
        "ally_ref",
        "lead_ally_name",
        "consent_copy_version",
        "chatwoot_account_id",
        "chatwoot_inbox_id",
        "inbound_scope_key",
        "inbound_scope_version",
    ):
        assert getattr(second, field) == getattr(principal, field), field


def test_a_second_product_can_live_inside_the_hotmart_product_of_the_first() -> None:
    # ATT2 se vendio con ofertas del producto de ATT1 hasta el 04/09: el ruteo va
    # por producto y oferta, y las ofertas no se pisan.
    inside = _second(
        hotmart={
            "product_id": 5071808,
            "hotlink": "D98014973Y",
            "product_name": "Alimenta tu Tiroides",
            "ofertas": [
                {
                    "codigo": "7szu9m58",
                    "site": "metodoraizana",
                    "landing_id": "att2-prueba-a",
                    "url": "https://www.metodoraizana.com/att2/prueba-a",
                    "origen": "pauta",
                    "por_defecto": True,
                },
                {
                    "codigo": "42urk6fc",
                    "site": "metodoraizana",
                    "landing_id": "att2-prueba-b",
                    "url": "https://www.metodoraizana.com/att2/prueba-b",
                    "origen": "pauta",
                },
            ],
        }
    )

    manifest = InstanceManifest.from_mapping(_payload(inside))

    principal, second = manifest.to_commercial_ally_configs()
    assert principal.hotmart_product_id == second.hotmart_product_id == 5071808
    assert second.offer_code == "7szu9m58"


def _offer(**changes: object) -> dict:
    offer = copy.deepcopy(SECOND["hotmart"]["ofertas"][0])
    offer.update(changes)
    return offer


@pytest.mark.parametrize(
    ("second", "message"),
    [
        (_second(funnel_ref="att1"), "mismo funnel_ref"),
        (_second(clave="att1"), "misma clave"),
        (_second(etiqueta="att1"), "misma etiqueta"),
        (_second(macro_etiqueta=7), "misma macro_etiqueta"),
        (
            _second(hotmart={"product_id": 5071808, "hotlink": "OTROHOTLINK"}),
            "distinto hotlink o product_name",
        ),
        (
            _second(
                hotmart={
                    "product_id": 5071808,
                    "hotlink": "D98014973Y",
                    "product_name": "Alimenta tu Tiroides",
                    "ofertas": [_offer(codigo="gopi6lh7")],
                }
            ),
            "la oferta gopi6lh7 de 5071808 esta en dos productos",
        ),
        (
            _second(hotmart={"ofertas": [_offer(site="metodoraizana", landing_id="ads-a")]}),
            "la landing metodoraizana/ads-a esta en dos productos",
        ),
        (
            _second(
                hotmart={"ofertas": [_offer(url="https://www.metodoraizana.com/att1/evg/vsl/ads-a")]}
            ),
            "la pagina www.metodoraizana.com/att1/evg/vsl/ads-a esta en dos productos",
        ),
        (_second(hotmart={"ofertas": []}), r"productos\[1\].hotmart.ofertas debe tener"),
        (_second(hotmart={"precio": "0"}), r"productos\[1\].hotmart.precio debe ser positivo"),
        (_second(hotmart={"moneda": "usd"}), r"productos\[1\].hotmart.moneda"),
        (_second(binding_version=0), r"productos\[1\].binding_version"),
        (_second(funnel_ref="ATT2"), r"productos\[1\].funnel_ref"),
        ({k: v for k, v in SECOND.items() if k != "hotmart"}, "faltan hotmart"),
        (_second(hotmart={"extra": 1}), r"productos\[1\].hotmart: claves no soportadas extra"),
    ],
)
def test_products_that_the_routing_could_not_tell_apart_do_not_load(
    second: dict, message: str
) -> None:
    with pytest.raises(ManifestError, match=message):
        InstanceManifest.from_mapping(_payload(second))


def test_the_principal_cannot_bring_a_funnel_or_a_hotmart_of_its_own() -> None:
    payload = _payload()
    payload["productos"][0]["hotmart"] = copy.deepcopy(SECOND["hotmart"])

    with pytest.raises(ManifestError, match=r"productos\[0\] es el producto principal: su hotmart"):
        InstanceManifest.from_mapping(payload)


def test_more_products_than_the_limit_do_not_load() -> None:
    extras = [
        _second(
            clave=f"p{index}",
            funnel_ref=f"p{index}",
            etiqueta=f"p{index}",
            macro_etiqueta=100 + index,
            hotmart={
                "product_id": 9000000 + index,
                "ofertas": [
                    _offer(
                        codigo=f"oferta{index}",
                        landing_id=f"prueba-{index}",
                        url=f"https://www.metodoraizana.com/prueba/{index}",
                    )
                ],
            },
        )
        for index in range(8)
    ]

    assert len(InstanceManifest.from_mapping(_payload(*extras[:7])).products) == 8
    with pytest.raises(ManifestError, match="admite hasta 8 productos"):
        InstanceManifest.from_mapping(_payload(*extras))


# ------------------------------------------------------------------- las rutas


def test_the_routes_find_each_product_by_its_key_and_its_funnel() -> None:
    manifest = _two()
    principal_config = manifest.to_commercial_ally_config()

    routes = ProductRoutes.build(principal_config, manifest)

    assert len(routes) == 2
    assert routes.principal.key == "att1"
    assert routes.principal.config is principal_config
    assert routes.by_key("att2").config == manifest.to_commercial_ally_configs()[1]
    assert routes.by_key("att2").principal is False
    assert routes.by_funnel("att1") is routes.principal
    assert routes.by_funnel("att2") is routes.by_key("att2")
    assert routes.by_key("att3") is None
    assert routes.by_funnel("att3") is None


def _route(key: str | None, funnel: str, principal: bool) -> ProductRoute:
    config = replace(_two().to_commercial_ally_config(), funnel_ref=funnel)
    return ProductRoute(key=key, config=config, principal=principal)


@pytest.mark.parametrize(
    "routes",
    [
        (),
        (_route("att2", "att2", False),),
        (_route("att1", "att1", True), _route("att2", "att2", True)),
        (_route("att1", "att1", True), _route("att2", "att1", False)),
        (_route("att1", "att1", True), _route("att1", "att2", False)),
        (_route(None, "att1", True), _route("att2", "att2", False)),
    ],
)
def test_routes_that_could_not_be_told_apart_are_refused(routes: tuple) -> None:
    with pytest.raises(ValueError):
        ProductRoutes(routes)


# ------------------------------------------------------------------- el bridge


def _two_unlabeled() -> InstanceManifest:
    # Sin etiquetas: la etiqueta exige el control de Chatwoot, y estas pruebas
    # miran solo los bindings.
    payload = _payload({k: v for k, v in SECOND.items() if k not in ("etiqueta", "macro_etiqueta")})
    payload["productos"][0] = {"clave": "att1"}
    return InstanceManifest.from_mapping(payload)


def _settings(manifest: InstanceManifest, tmp_path: Path) -> Settings:
    return Settings(
        webhook_secret="webhook-secret",
        allowed_jid="573000000158@s.whatsapp.net",
        capture_dir=tmp_path,
        max_age_seconds=300,
        agent_bot_id=1,
        chatwoot_account_id=2,
        chatwoot_inbox_id=11,
        instance_manifest=manifest,
        commercial_ally_config=manifest.to_commercial_ally_config(),
        hermes_model_name=manifest.agent_model_name,
    )


def test_the_bridge_does_not_start_with_two_products_until_the_routing_is_complete(
    tmp_path: Path,
) -> None:
    assert product_routes.ROUTING_COMPLETE is False
    with pytest.raises(ValueError, match="espera el ruteo por producto"):
        create_app(_settings(_two_unlabeled(), tmp_path))


class _Bindings:
    """La lectura del binding: devuelve el config que le piden, o falla para un funnel."""

    def __init__(self, failing_funnel: str | None = None) -> None:
        self.failing_funnel = failing_funnel
        self.resolved: list[str] = []

    async def resolve_commercial_ally_runtime_binding(
        self, config: CommercialAllyConfig
    ) -> CommercialAllyConfig:
        if config.funnel_ref == self.failing_funnel:
            raise RuntimeError("binding missing")
        self.resolved.append(config.funnel_ref)
        return config


def test_ready_checks_the_binding_of_every_product(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(product_routes, "ROUTING_COMPLETE", True)
    bindings = _Bindings()
    app = create_app(_settings(_two_unlabeled(), tmp_path), supabase_client=bindings)  # type: ignore[arg-type]

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200, response.text
    assert response.json()["commercial_ally_binding"] == "active"
    assert response.json()["commercial_ally_bindings"] == "2"
    assert bindings.resolved == ["att1", "att2"]


def test_ready_fails_when_the_binding_of_another_product_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(product_routes, "ROUTING_COMPLETE", True)
    app = create_app(
        _settings(_two_unlabeled(), tmp_path),
        supabase_client=_Bindings(failing_funnel="att2"),  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["detail"] == "commercial_ally_binding_unavailable"


def test_ready_with_one_product_does_not_change(tmp_path: Path) -> None:
    manifest = InstanceManifest.from_toml_file(ATT1 / "instancia.toml")
    bindings = _Bindings()
    app = create_app(_settings(manifest, tmp_path), supabase_client=bindings)  # type: ignore[arg-type]

    with TestClient(app) as client:
        body = client.get("/ready").json()

    assert body["commercial_ally_binding"] == "active"
    assert "commercial_ally_bindings" not in body
    assert bindings.resolved == ["att1"]


# --------------------------------------------------------------------- el CLI


def test_validate_shows_the_funnel_and_hotmart_of_each_product_and_refuses_to_start(
    tmp_path: Path,
) -> None:
    instance = tmp_path / "setter-instancia-att1"
    shutil.copytree(ATT1, instance)
    manifest_path = instance / "instancia.toml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8")
        + """
[[productos]]
clave = "att1"
etiqueta = "att1"
macro_etiqueta = 7

[[productos]]
clave = "att2"
funnel_ref = "att2"
binding_version = 1

[productos.hotmart]
product_id = 8437428
hotlink = "P8437428PRUEBA"
product_name = "Producto de prueba ATT2"
moneda = "USD"
precio = "67"

[[productos.hotmart.ofertas]]
codigo = "prueba2a"
site = "metodoraizana"
landing_id = "att2-prueba-a"
url = "https://www.metodoraizana.com/att2/prueba-a"
origen = "pauta"
por_defecto = true
""",
        encoding="utf-8",
    )

    report = validate_instance(instance)

    assert report["manifiesto"]["productos"][0] == {
        "clave": "att1",
        "etiqueta": "att1",
        "macro_etiqueta": 7,
    }
    assert report["manifiesto"]["productos"][1] == {
        "clave": "att2",
        "etiqueta": None,
        "macro_etiqueta": None,
        "funnel_ref": "att2",
        "hotmart": "8437428 / P8437428PRUEBA / USD 67",
        "ofertas": [
            {"codigo": "prueba2a", "landing": "metodoraizana/att2-prueba-a", "por_defecto": True}
        ],
    }
    assert report["valida"] is False
    assert any("espera el ruteo por producto" in error for error in report["errores"])
