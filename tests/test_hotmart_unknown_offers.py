"""Las ofertas huerfanas de Hotmart: el registro (hotmart-unknown-offers-v1, E-565).

Fixtures (todos capturados, ninguno inventado):
- tests/fixtures/hotmart_purchase_approved_att1_other_offer_hermes_20261110.json:
  una compra real de ATT1 (producto 5071808, oferta bmaztyhg, anonimizada con
  formas validas). Para Maribel (8443991) es de otro producto: el caso de una
  instancia que recibe un evento de un producto que no declara.
- tests/fixtures/hotmart_cart_abandonment_rejected_v1.json: el carrito real de
  Johanna (producto 8104005, oferta mgbqpp19) que el binding rechazo por la
  oferta (su ``root_cause``). Contra el manifiesto de Johanna es una oferta no
  declarada; contra ATT1, un producto no declarado.
- tests/fixtures/hotmart_routing_att1_20261016.json (la compra de 83utgyow, la
  oferta que ningun producto de ATT1 declara): es del producto de la instancia,
  frena con cualquier oferta y no es huerfana.
- tests/fixtures/hotmart_routing_att1_20261016.json, por los helpers de
  test_product_routing: la instancia de dos productos.

Los payloads «rotos» se derivan de los reales sacando un solo campo, y se nombran
asi. La base es falsa: el SQL se prueba en
tests/sql/followup_engine/validate_hotmart_unknown_offers.mjs.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_product_routing import (
    _event,
    _fresh,
    _manifest as _two_products,
    _post_hotmart,
    _settings,
)

from bridge import app as app_module
from bridge import hotmart_unknown_offers as unknown
from bridge.app import Settings, create_app
from bridge.hotmart import EVENT_CART_ABANDONMENT, EVENT_PURCHASE_APPROVED
from bridge.instance_manifest import InstanceManifest
from bridge.product_routes import HotmartUnrouted, ProductRoutes
from bridge.supabase import SupabaseError

FIXTURES = Path(__file__).parent / "fixtures"
INSTANCES = FIXTURES / "instances"


def _captured(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["payload"]


ATT1_PURCHASE = _captured("hotmart_purchase_approved_att1_other_offer_hermes_20261110")
JOHANNA_CART = _captured("hotmart_cart_abandonment_rejected_v1")


def _config(instance: str):
    return InstanceManifest.from_toml_file(
        INSTANCES / instance / "instancia.toml"
    ).to_commercial_ally_config()


def _instance_manifest(instance: str, **flows: bool) -> InstanceManifest:
    payload = tomllib.loads((INSTANCES / instance / "instancia.toml").read_text(encoding="utf-8"))
    payload["flujos"].update(flows)
    return InstanceManifest.from_mapping(payload)


def _without(payload: dict[str, Any], *path: str) -> dict[str, Any]:
    """El real, derivado sacando un solo campo (un payload roto de verdad)."""

    broken = copy.deepcopy(payload)
    node = broken
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    broken["id"] = f"{broken['id']}-derivado"
    return broken


# ------------------------------------------------------------- las funciones


def test_a_purchase_of_another_product_is_an_unknown_product() -> None:
    offer = unknown.from_parser_rejection(
        ATT1_PURCHASE,
        config=_config("maribel"),
        event_id=ATT1_PURCHASE["id"],
        event_type=EVENT_PURCHASE_APPROVED,
    )

    assert offer is not None
    assert (offer.hotmart_product_id, offer.offer_code, offer.event_type) == (
        5071808,
        "bmaztyhg",
        "PURCHASE_APPROVED",
    )
    assert (offer.motivo, offer.entrada) == ("product_not_declared", "parser_descarta")
    assert offer.event_ref_sha256 == unknown.event_ref_sha256("hotmart_event", ATT1_PURCHASE["id"])
    assert len(offer.event_ref_sha256) == 64


def test_a_cart_of_an_offer_the_product_does_not_declare_is_an_unknown_offer() -> None:
    # El root_cause capturado: mgbqpp19 no es una oferta del binding de Johanna.
    offer = unknown.from_parser_rejection(
        JOHANNA_CART,
        config=_config("johanna"),
        event_id=JOHANNA_CART["id"],
        event_type=EVENT_CART_ABANDONMENT,
    )

    assert offer is not None
    assert (offer.hotmart_product_id, offer.offer_code, offer.motivo) == (
        8104005,
        "mgbqpp19",
        "offer_not_declared",
    )


def test_the_same_cart_in_another_instance_is_an_unknown_product() -> None:
    offer = unknown.from_parser_rejection(
        JOHANNA_CART,
        config=_config("att1"),
        event_id=JOHANNA_CART["id"],
        event_type=EVENT_CART_ABANDONMENT,
    )

    assert offer is not None
    assert (offer.hotmart_product_id, offer.motivo) == (8104005, "product_not_declared")


def test_a_purchase_of_the_product_with_any_offer_is_not_orphan() -> None:
    # La compra frena con cualquier oferta del producto (bmaztyhg no es la por
    # defecto de ATT1): no es huerfana.
    assert (
        unknown.from_parser_rejection(
            ATT1_PURCHASE,
            config=_config("att1"),
            event_id=ATT1_PURCHASE["id"],
            event_type=EVENT_PURCHASE_APPROVED,
        )
        is None
    )


@pytest.mark.parametrize(
    ("payload", "event_type"),
    [
        pytest.param(
            _without(ATT1_PURCHASE, "data", "purchase", "transaction"),
            EVENT_PURCHASE_APPROVED,
            id="compra sin transaccion",
        ),
        pytest.param(
            _without(ATT1_PURCHASE, "data", "product", "id"),
            EVENT_PURCHASE_APPROVED,
            id="compra sin producto",
        ),
        pytest.param(
            _without(JOHANNA_CART, "data", "buyer"),
            EVENT_CART_ABANDONMENT,
            id="carrito sin comprador",
        ),
        pytest.param(
            _without(JOHANNA_CART, "data", "offer"),
            EVENT_CART_ABANDONMENT,
            id="carrito sin oferta",
        ),
    ],
)
def test_a_really_broken_payload_is_not_an_unknown_offer(
    payload: dict[str, Any], event_type: str
) -> None:
    assert (
        unknown.from_parser_rejection(
            payload, config=_config("maribel"), event_id=payload["id"], event_type=event_type
        )
        is None
    )


def test_an_unrouted_event_of_two_products_is_sin_ruta() -> None:
    manifest = _two_products(carrito=True)
    routes = ProductRoutes.build(manifest.to_commercial_ally_config(), manifest)
    route = routes.for_hotmart(JOHANNA_CART)
    assert isinstance(route, HotmartUnrouted)

    offer = unknown.from_unrouted(
        route, event_id=JOHANNA_CART["id"], event_type=JOHANNA_CART["event"]
    )

    assert offer is not None
    assert (offer.entrada, offer.motivo, offer.hotmart_product_id, offer.offer_code) == (
        "sin_ruta",
        "product_not_declared",
        8104005,
        "mgbqpp19",
    )


def test_the_dispatch_names_the_product_and_lets_the_base_read_the_type() -> None:
    offer = unknown.for_dispatch(
        offer_code="83utgyow",
        product_name="Alimenta tu Tiroides",
        configs=[_config("att1")],
        source_event_id="11111111-1111-4111-8111-111111111111",
    )

    assert offer is not None
    assert (offer.hotmart_product_id, offer.offer_code, offer.event_type) == (5071808, "83utgyow", None)
    assert (offer.entrada, offer.motivo) == ("despacho", "offer_not_declared")
    assert offer.source_event_id == "11111111-1111-4111-8111-111111111111"
    # Un nombre que ningun producto tiene: no hay producto que anotar.
    assert (
        unknown.for_dispatch(
            offer_code="83utgyow",
            product_name="Otro",
            configs=[_config("att1")],
            source_event_id="11111111-1111-4111-8111-111111111111",
        )
        is None
    )


def test_an_offer_code_that_is_not_from_a_catalog_is_not_sent() -> None:
    route = HotmartUnrouted("product_not_declared", 8104005, "persona@example.test")
    offer = unknown.from_unrouted(route, event_id="e", event_type="PURCHASE_APPROVED")
    assert offer is not None and offer.offer_code is None


# --------------------------------------------------------------- el recorder


class _Store:
    def __init__(self, *, fail: bool = False, rows: int = 0) -> None:
        self.fail = fail
        self.rows = rows
        self.records: list[dict[str, Any]] = []

    async def record_hotmart_unknown_offer(self, *, tenant_ref: str, offer: unknown.UnknownOffer) -> str:
        if self.fail:
            raise SupabaseError("hotmart_unknown_offer_record_failed: HTTP 404")
        self.records.append({"tenant_ref": tenant_ref, "offer": offer})
        return "inserted"

    async def list_hotmart_unknown_offers(self, *, tenant_ref: str, only_open: bool = True) -> list[dict]:
        if self.fail:
            raise SupabaseError("hotmart_unknown_offer_list_failed: HTTP 404")
        assert only_open is True
        return [{"conteo": 1}] * self.rows


def test_a_failure_of_the_record_never_reaches_the_event(caplog: pytest.LogCaptureFixture) -> None:
    recorder = unknown.HotmartUnknownOfferRecorder(_Store(fail=True), tenant_ref="lancemos")
    offer = unknown.from_parser_rejection(
        ATT1_PURCHASE, config=_config("maribel"), event_id="e", event_type=EVENT_PURCHASE_APPROVED
    )

    with caplog.at_level(logging.WARNING, logger="bridge.hotmart_unknown_offers"):
        asyncio.run(recorder.record(offer))

    assert [r.getMessage() for r in caplog.records] == [
        "hotmart_unknown_offer_record_failed entrada=parser_descarta error_type=SupabaseError"
    ]


def test_the_recorded_line_has_nothing_personal(caplog: pytest.LogCaptureFixture) -> None:
    store = _Store()
    recorder = unknown.HotmartUnknownOfferRecorder(store, tenant_ref="lancemos")
    offer = unknown.from_parser_rejection(
        ATT1_PURCHASE,
        config=_config("maribel"),
        event_id=ATT1_PURCHASE["id"],
        event_type=EVENT_PURCHASE_APPROVED,
    )

    with caplog.at_level(logging.INFO, logger="bridge.hotmart_unknown_offers"):
        asyncio.run(recorder.record(offer))
        asyncio.run(recorder.record(None))

    assert [r.getMessage() for r in caplog.records] == [
        "hotmart_unknown_offer_recorded entrada=parser_descarta motivo=product_not_declared "
        "product_id=5071808 offer=bmaztyhg outcome=inserted"
    ]
    assert len(store.records) == 1
    buyer = ATT1_PURCHASE["data"]["buyer"]
    text = json.dumps(store.records[0]["offer"].__dict__) + caplog.text
    for value in (buyer.get("email"), buyer.get("checkout_phone"), buyer.get("name"),
                  ATT1_PURCHASE["data"]["purchase"]["transaction"]):
        assert not value or value not in text


def test_the_flag_is_off_by_default_and_portable() -> None:
    assert Settings.__dataclass_fields__["hotmart_unknown_offers_enabled"].default is False
    assert "hotmart_unknown_offers_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    assert "hotmart_unknown_offer_recorded" in __import__(
        "bridge.event_log", fromlist=["LOGGED_INFO_EVENTS"]
    ).LOGGED_INFO_EVENTS


# ------------------------------------------------------------ el webhook


class _Base(_Store):
    """Las admisiones de test_product_routing, el binding de /ready y el registro."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.carts: list[dict] = []
        self.purchases: list[dict] = []
        self.list_calls = 0

    async def admit_portable_hotmart_cart_abandonment(self, **kwargs: object) -> object:
        self.carts.append(kwargs)
        return SimpleNamespace(outcome="inserted", webhook_event_id="cart-event")

    async def admit_portable_hotmart_purchase_approved(self, **kwargs: object) -> object:
        self.purchases.append(kwargs)
        return SimpleNamespace(outcome="inserted", webhook_event_id="purchase-event")

    async def resolve_commercial_ally_runtime_binding(self, config: object) -> object:
        return config

    async def list_hotmart_unknown_offers(self, **kwargs: Any) -> list[dict]:
        self.list_calls += 1
        return await super().list_hotmart_unknown_offers(**kwargs)


def _app(manifest: InstanceManifest, tmp_path: Path, base: _Base, *, enabled: bool) -> object:
    settings = _settings(manifest, tmp_path)
    if enabled:
        from dataclasses import replace

        settings = replace(settings, hotmart_unknown_offers_enabled=True)
    return create_app(settings, supabase_client=base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        pytest.param(ATT1_PURCHASE, "invalid_purchase_payload", id="compra de ATT1 en Maribel"),
        pytest.param(JOHANNA_CART, "invalid_cart_abandonment_payload", id="carrito de Johanna en Maribel"),
    ],
)
def test_with_one_route_another_product_still_answers_ignored_and_leaves_a_row(
    tmp_path: Path, payload: dict[str, Any], reason: str
) -> None:
    on, off = _Base(), _Base()
    fresh = _fresh(payload)

    answered_on = _post_hotmart(_app(_instance_manifest("maribel", carrito=True), tmp_path, on, enabled=True), fresh)
    answered_off = _post_hotmart(_app(_instance_manifest("maribel", carrito=True), tmp_path, off, enabled=False), fresh)

    # La respuesta a Hotmart es la de siempre, prendido o apagado.
    assert (answered_on.status_code, answered_on.json()) == (200, {"status": "ignored", "reason": reason})
    assert (answered_off.status_code, answered_off.content) == (answered_on.status_code, answered_on.content)
    assert on.carts == on.purchases == off.carts == off.purchases == []
    # Prendido deja la fila; apagado no llama a la base.
    [row] = on.records
    assert row["tenant_ref"] == "lancemos"
    assert (row["offer"].entrada, row["offer"].motivo) == ("parser_descarta", "product_not_declared")
    assert row["offer"].hotmart_product_id == payload["data"]["product"]["id"]
    assert off.records == []


def test_a_really_broken_payload_answers_as_before_and_leaves_no_row(tmp_path: Path) -> None:
    base = _Base()
    broken = _fresh(_without(ATT1_PURCHASE, "data", "purchase", "transaction"))

    answered = _post_hotmart(_app(_instance_manifest("maribel", carrito=True), tmp_path, base, enabled=True), broken)

    assert answered.json() == {"status": "ignored", "reason": "invalid_purchase_payload"}
    assert base.records == []


def test_the_product_of_the_instance_is_admitted_and_leaves_no_row(tmp_path: Path) -> None:
    base = _Base()

    # La compra de 83utgyow (fuera del binding): frena con cualquier oferta del producto.
    answered = _post_hotmart(
        _app(_instance_manifest("att1", carrito=True), tmp_path, base, enabled=True),
        _fresh(_event("compra_83utgyow")),
    )

    assert answered.status_code == 202
    assert len(base.purchases) == 1
    assert base.records == []


def test_with_two_products_an_unrouted_event_leaves_its_line_and_its_row(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    base = _Base()

    with caplog.at_level(logging.INFO):
        answered = _post_hotmart(
            _app(_two_products(carrito=True), tmp_path, base, enabled=True), _fresh(JOHANNA_CART)
        )

    assert answered.json() == {"status": "ignored", "reason": "hotmart_event_unrouted"}
    [row] = base.records
    assert (row["offer"].entrada, row["offer"].motivo, row["offer"].offer_code) == (
        "sin_ruta",
        "product_not_declared",
        "mgbqpp19",
    )
    lines = [record.getMessage() for record in caplog.records]
    assert any(line.startswith("hotmart_event_unrouted ") for line in lines)
    assert any(line.startswith("hotmart_unknown_offer_recorded entrada=sin_ruta ") for line in lines)


def test_the_retry_of_hotmart_sends_the_same_event_ref(tmp_path: Path) -> None:
    base = _Base()
    app = _app(_instance_manifest("maribel", carrito=True), tmp_path, base, enabled=True)
    fresh = _fresh(ATT1_PURCHASE)

    _post_hotmart(app, fresh)
    _post_hotmart(app, fresh)

    # La base no suma el segundo (validate_hotmart_unknown_offers.mjs, 1).
    assert [r["offer"].event_ref_sha256 for r in base.records] == [
        unknown.event_ref_sha256("hotmart_event", ATT1_PURCHASE["id"])
    ] * 2


def test_a_failing_record_does_not_change_the_answer(tmp_path: Path) -> None:
    base = _Base(fail=True)

    answered = _post_hotmart(
        _app(_instance_manifest("maribel", carrito=True), tmp_path, base, enabled=True), _fresh(ATT1_PURCHASE)
    )

    assert (answered.status_code, answered.json()) == (
        200,
        {"status": "ignored", "reason": "invalid_purchase_payload"},
    )


# ------------------------------------------------------------------- /ready


def _ready(app: object) -> Any:
    with TestClient(app) as client:  # type: ignore[arg-type]
        return client.get("/ready")


def test_off_ready_does_not_change_and_does_not_read(tmp_path: Path) -> None:
    base = _Base(rows=3)
    body = _ready(_app(_instance_manifest("maribel", carrito=True), tmp_path, base, enabled=False)).json()
    assert "hotmart_unknown_offers" not in body
    assert base.list_calls == 0


def test_on_ready_says_how_many_are_open(tmp_path: Path) -> None:
    response = _ready(_app(_instance_manifest("maribel", carrito=True), tmp_path, _Base(rows=2), enabled=True))
    assert response.status_code == 200
    assert response.json()["hotmart_unknown_offers"] == "abiertas:2"


def test_on_a_failing_read_ready_publishes_and_does_not_fall(tmp_path: Path) -> None:
    response = _ready(_app(_instance_manifest("maribel", carrito=True), tmp_path, _Base(fail=True), enabled=True))
    assert response.status_code == 200
    assert response.json()["hotmart_unknown_offers"] == "no_disponible"


def test_with_one_route_a_cart_of_an_undeclared_offer_leaves_an_offer_row(tmp_path: Path) -> None:
    # El carrito capturado de Johanna (mgbqpp19) en su propia instancia: el
    # producto es el suyo y la oferta no esta entre las declaradas (el caso del
    # Black de ATT1 con una oferta sin declarar, en el punto 2).
    base = _Base()

    answered = _post_hotmart(
        _app(_instance_manifest("johanna", carrito=True), tmp_path, base, enabled=True),
        _fresh(JOHANNA_CART),
    )

    assert answered.json() == {"status": "ignored", "reason": "invalid_cart_abandonment_payload"}
    [row] = base.records
    assert (row["offer"].entrada, row["offer"].motivo) == ("parser_descarta", "offer_not_declared")
    assert (row["offer"].hotmart_product_id, row["offer"].offer_code) == (8104005, "mgbqpp19")
