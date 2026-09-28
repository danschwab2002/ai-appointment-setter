"""Varias ofertas por binding en el runtime portable (migracion 20260928000200).

Fixture: tests/fixtures/hotmart_cart_abandonment_rejected_v1.json, capturado del
panel de Hotmart el 2026-09-21. Es exactamente el caso: un carrito con una oferta
que no era la unica del scope, rechazado. Aca se cambian solo producto y oferta.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path

import httpx
import pytest

from bridge.commercial_ally import CommercialAllyConfig
from bridge.hotmart import (
    parse_hotmart_payload,
    parse_hotmart_payment_failure_payload,
    parse_hotmart_purchase_payload,
)
from bridge.supabase import SupabaseClient, SupabaseError

ROOT = Path(__file__).resolve().parents[1]
CAPTURED_CART = json.loads(
    (ROOT / "tests" / "fixtures" / "hotmart_cart_abandonment_rejected_v1.json").read_text(
        encoding="utf-8"
    )
)["payload"]
MIGRATION = ROOT / "supabase" / "migrations" / "20260928000200_commercial_ally_additional_offers.sql"


def _config(**overrides: object) -> CommercialAllyConfig:
    values = dict(
        tenant_ref="lancemos",
        funnel_ref="att1",
        binding_version=1,
        ally_ref="att1",
        lead_ally_name="Dra. Nina Garza",
        lead_site="metodoraizana",
        lead_landing_id="ads-a",
        lead_page_host="www.metodoraizana.com",
        lead_page_path="/att1/evg/vsl/ads-a",
        product_hotlink="D98014973Y",
        product_name="Alimenta Tu Tiroides",
        product_price=Decimal("47"),
        currency="USD",
        offer_code="gopi6lh7",
        consent_copy_version="att1-whatsapp-contact-v1",
        hotmart_product_id=5071808,
        chatwoot_account_id=2,
        chatwoot_inbox_id=11,
        inbound_scope_key="att1-inbound",
        inbound_scope_version=1,
        additional_offer_codes=("bmaztyhg", "2uafw5bg"),
    )
    values.update(overrides)
    return CommercialAllyConfig(**values)  # type: ignore[arg-type]


def _cart(offer: str) -> dict:
    payload = copy.deepcopy(CAPTURED_CART)
    payload["data"]["product"] = {"id": 5071808, "name": "Alimenta Tu Tiroides"}
    payload["data"]["offer"] = {"code": offer}
    return payload


def _failure(offer: str) -> dict:
    return {
        "id": f"pf-{offer}",
        "creation_date": 1790560000000,
        "event": "PURCHASE_CANCELED",
        "version": "2.0.0",
        "data": {
            "buyer": {"name": "Compradora", "email": "compradora@example.invalid", "checkout_phone": "525555555555"},
            "product": {"id": 5071808, "name": "Alimenta Tu Tiroides"},
            "purchase": {
                "transaction": "HP1234567890",
                "status": "CANCELED",
                "offer": {"code": offer},
                "payment": {"refusal_reason": "insufficient_funds"},
            },
        },
    }


def _purchase(offer: str, product_id: int = 5071808) -> dict:
    return {
        "id": f"buy-{offer}",
        "creation_date": 1790560000001,
        "event": "PURCHASE_APPROVED",
        "version": "2.0.0",
        "data": {
            "product": {"id": product_id, "ucode": "ATT1"},
            "buyer": {"email": "compradora@example.invalid", "checkout_phone": "525555555555"},
            "purchase": {
                "approved_date": 1790560000000,
                "status": "APPROVED",
                "transaction": "HP1234567891",
                "offer": {"code": offer},
            },
        },
    }


# ------------------------------------------------------------------ binding


def test_accepted_offers_put_the_default_first() -> None:
    assert _config().accepted_offer_codes == ("gopi6lh7", "bmaztyhg", "2uafw5bg")
    assert _config(additional_offer_codes=()).accepted_offer_codes == ("gopi6lh7",)


@pytest.mark.parametrize(
    "additional",
    [("gopi6lh7",), ("bmaztyhg", "bmaztyhg"), ("bad code",), ("abc",), tuple(f"offer{i:02d}" for i in range(17))],
)
def test_invalid_additional_offers_are_rejected(additional: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="additional_offer_codes"):
        _config(additional_offer_codes=additional)


def test_json_manifest_keeps_additional_offers_optional(tmp_path: Path) -> None:
    config = _config()
    payload = {
        "tenant_ref": config.tenant_ref, "funnel_ref": config.funnel_ref,
        "binding_version": 1, "ally_ref": "att1", "lead_ally_name": config.lead_ally_name,
        "lead_site": config.lead_site, "lead_landing_id": config.lead_landing_id,
        "lead_page_host": config.lead_page_host, "lead_page_path": config.lead_page_path,
        "product_hotlink": config.product_hotlink, "product_name": config.product_name,
        "product_price": "47", "currency": "USD", "offer_code": "gopi6lh7",
        "consent_copy_version": config.consent_copy_version, "hotmart_product_id": 5071808,
        "chatwoot_account_id": 2, "chatwoot_inbox_id": 11,
        "inbound_scope_key": "att1-inbound", "inbound_scope_version": 1,
    }
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert CommercialAllyConfig.from_json_file(path).additional_offer_codes == ()

    path.write_text(json.dumps({**payload, "additional_offer_codes": ["bmaztyhg", "2uafw5bg"]}), encoding="utf-8")
    assert CommercialAllyConfig.from_json_file(path) == config

    path.write_text(json.dumps({**payload, "additional_offer_codes": "bmaztyhg"}), encoding="utf-8")
    with pytest.raises(ValueError, match="additional_offer_codes"):
        CommercialAllyConfig.from_json_file(path)


def _resolve(row: dict, expected: CommercialAllyConfig) -> CommercialAllyConfig:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[row])

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="test-service-role",
        transport=httpx.MockTransport(handler),
    )
    return asyncio.run(client.resolve_commercial_ally_runtime_binding(expected))


def _row(config: CommercialAllyConfig) -> dict:
    row = {name: getattr(config, name) for name in config.__dataclass_fields__}
    row["product_price"] = "47.00"
    row["additional_offer_codes"] = list(config.additional_offer_codes)
    row["status"] = "active"
    return row


def test_durable_binding_resolves_with_its_additional_offers() -> None:
    config = _config()

    assert _resolve(_row(config), config) == config


def test_durable_binding_with_different_offers_is_drift() -> None:
    config = _config()
    row = _row(config)
    row["additional_offer_codes"] = ["bmaztyhg"]

    with pytest.raises(SupabaseError, match="config_drift"):
        _resolve(row, config)


def test_durable_binding_without_the_column_fails_closed() -> None:
    config = _config()
    row = _row(config)
    del row["additional_offer_codes"]

    with pytest.raises(SupabaseError, match="invalid_row"):
        _resolve(row, config)


# ------------------------------------------------------------------ parsers


@pytest.mark.parametrize("offer", ["gopi6lh7", "bmaztyhg", "2uafw5bg"])
def test_cart_of_any_binding_offer_is_parsed(offer: str) -> None:
    parsed = parse_hotmart_payload(_cart(offer), config=_config())

    assert parsed is not None and parsed.offer_code == offer


def test_cart_of_an_offer_outside_the_binding_is_not_parsed() -> None:
    assert parse_hotmart_payload(_cart("83utgyow"), config=_config()) is None


@pytest.mark.parametrize("offer", ["gopi6lh7", "bmaztyhg"])
def test_payment_failure_of_any_binding_offer_is_parsed(offer: str) -> None:
    assert parse_hotmart_payment_failure_payload(_failure(offer), config=_config()) is not None


def test_payment_failure_outside_the_binding_is_not_parsed() -> None:
    assert parse_hotmart_payment_failure_payload(_failure("83utgyow"), config=_config()) is None


def test_purchase_through_any_offer_of_the_product_stops_recovery() -> None:
    parsed = parse_hotmart_purchase_payload(_purchase("83utgyow"), config=_config())

    assert parsed is not None and parsed.offer_code == "83utgyow"
    assert parse_hotmart_purchase_payload(_purchase("gopi6lh7", 999), config=_config()) is None


def test_single_offer_binding_behaves_as_before() -> None:
    config = replace(_config(), additional_offer_codes=())

    assert parse_hotmart_payload(_cart("gopi6lh7"), config=config) is not None
    assert parse_hotmart_payload(_cart("bmaztyhg"), config=config) is None


# ---------------------------------------------------------------- migration


def test_migration_only_changes_the_offer_comparison_and_seeds_nothing() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert sql.count("create or replace function public.admit_portable_hotmart_") == 3
    assert "insert into" not in sql.split("create or replace function")[0]
    assert "is distinct from v_binding.offer_code" not in sql
    assert "offer_ref = v_binding.offer_code" not in sql
    for customer_value in ("5071808", "D98014973Y", "gopi6lh7", "8104005", "bxjge6zq", "johanna"):
        assert customer_value not in sql
