import json
from decimal import Decimal
from pathlib import Path

import pytest

from bridge.commercial_ally import CommercialAllyConfig


def _config(**overrides: object) -> CommercialAllyConfig:
    values: dict[str, object] = {
        "tenant_ref": "att1",
        "funnel_ref": "att1-main",
        "binding_version": 1,
        "ally_ref": "ally-one",
        "lead_ally_name": "Ally One",
        "lead_site": "ally-one-site",
        "lead_landing_id": "main",
        "lead_page_host": "ally-one.example",
        "lead_page_path": "/offer/main",
        "product_hotlink": "ATT1HOTLINK",
        "product_name": "ATT1 Offer",
        "product_price": Decimal("49"),
        "currency": "USD",
        "offer_code": "att1offer",
        "consent_copy_version": "att1-whatsapp-v1",
        "hotmart_product_id": 123456,
        "chatwoot_account_id": 42,
        "chatwoot_inbox_id": 24,
        "inbound_scope_key": "att1-inbound",
        "inbound_scope_version": 1,
    }
    values.update(overrides)
    return CommercialAllyConfig(**values)  # type: ignore[arg-type]


def test_commercial_ally_config_accepts_one_complete_single_tenant_binding() -> None:
    config = _config()

    assert config.tenant_ref == "att1"
    assert config.product_price == Decimal("49")
    assert config.lead_page_url == "https://ally-one.example/offer/main"
    assert config.checkout_url == (
        "https://pay.hotmart.com/ATT1HOTLINK?off=att1offer"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_ref", ""),
        ("binding_version", 0),
        ("binding_version", 1.5),
        ("lead_page_host", "https://ally-one.example"),
        ("lead_page_path", "offer/main"),
        ("product_price", Decimal("NaN")),
        ("product_price", Decimal("0")),
        ("currency", "usd"),
        ("hotmart_product_id", 0),
        ("hotmart_product_id", 123456.5),
        ("chatwoot_account_id", 0),
        ("chatwoot_account_id", 42.5),
        ("chatwoot_inbox_id", 0),
        ("chatwoot_inbox_id", 24.5),
        ("inbound_scope_key", "ATT1 inbound"),
        ("inbound_scope_version", 0),
        ("inbound_scope_version", 1.5),
    ],
)
def test_commercial_ally_config_rejects_incomplete_or_noncanonical_binding(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError):
        _config(**{field: value})


def test_commercial_ally_config_loads_an_exact_non_secret_json_manifest(
    tmp_path: Path,
) -> None:
    manifest = {
        "tenant_ref": "att1",
        "funnel_ref": "att1-main",
        "binding_version": 1,
        "ally_ref": "ally-one",
        "lead_ally_name": "Ally One",
        "lead_site": "ally-one-site",
        "lead_landing_id": "main",
        "lead_page_host": "ally-one.example",
        "lead_page_path": "/offer/main",
        "product_hotlink": "ATT1HOTLINK",
        "product_name": "ATT1 Offer",
        "product_price": "49",
        "currency": "USD",
        "offer_code": "att1offer",
        "consent_copy_version": "att1-whatsapp-v1",
        "hotmart_product_id": 123456,
        "chatwoot_account_id": 42,
        "chatwoot_inbox_id": 24,
        "inbound_scope_key": "att1-inbound",
        "inbound_scope_version": 1,
    }
    path = tmp_path / "commercial-ally.json"
    path.write_text(json.dumps(manifest))

    loaded = CommercialAllyConfig.from_json_file(path)

    assert loaded == _config()


def test_commercial_ally_config_rejects_unknown_manifest_keys(tmp_path: Path) -> None:
    path = tmp_path / "commercial-ally.json"
    path.write_text(json.dumps({"unexpected": True}))

    with pytest.raises(ValueError, match="exactly the supported keys"):
        CommercialAllyConfig.from_json_file(path)
