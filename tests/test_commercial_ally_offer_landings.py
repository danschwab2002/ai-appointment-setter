"""El formulario del precheckout en cada landing del binding (migracion 20260930000200).

Fixture del binding: tests/fixtures/instances/att1/instancia.toml (ofertas, sitios y
URLs de ATT1 medidos el 2026-09-28). No hay ningun ``lead.precheckout`` capturado
todavia: el payload es el precedente inline del contrato
(docs/contracts/lead-precheckout-v1.md, tests/test_commercial_ally_portability.py),
con los valores de cada landing de ATT1. Reemplazarlo cuando haya captura.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path

import httpx
import pytest

from bridge.commercial_ally import CommercialAllyConfig, OfferLanding
from bridge.instance_manifest import InstanceManifest
from bridge.lead_precheckout import parse_lead_precheckout
from bridge.supabase import SupabaseClient, SupabaseError

ROOT = Path(__file__).resolve().parents[1]
ATT1_MANIFEST = ROOT / "tests" / "fixtures" / "instances" / "att1" / "instancia.toml"
MIGRATION = (
    ROOT / "supabase" / "migrations" / "20260930000200_portable_precheckout_offer_landings.sql"
)

# (oferta, sitio, landing, host, ruta) de las tres landings de ATT1, del fixture.
ATT1_LANDINGS = (
    ("gopi6lh7", "metodoraizana", "ads-a", "www.metodoraizana.com", "/att1/evg/vsl/ads-a"),
    ("bmaztyhg", "metodoraizana", "org-a", "www.metodoraizana.com", "/att1/evg/vsl/org-a"),
    (
        "2uafw5bg",
        "metodoraizana-mx",
        "alimenta-tu-tiroides-d",
        "site.metodoraizana.com.mx",
        "/alimenta-tu-tiroides-d",
    ),
)


def _config() -> CommercialAllyConfig:
    return InstanceManifest.from_toml_file(ATT1_MANIFEST).to_commercial_ally_config()


def _lead(
    offer: str,
    site: str,
    landing_id: str,
    host: str,
    path: str,
    *,
    email: str = "buyer@example.test",
) -> dict[str, object]:
    return {
        "id": "01K3F8QW7N2VYB4M6X9CDPTZRA",
        "event": "lead.precheckout",
        "version": "1.1.0",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": {
            "system": "landing",
            "site": site,
            "aliado": "Dra. Nina Garza",
            "landing_id": landing_id,
            "page_url": f"https://{host}{path}",
        },
        "data": {
            "buyer": {
                "name": "Test Buyer",
                "email": email,
                "phone": "+12025550123",
                "phone_country_code": "1",
                "phone_national": "2025550123",
            },
            "product": {
                "hotlink": "D98014973Y",
                "id": None,
                "name": "Alimenta Tu Tiroides",
                "price": 47,
                "currency": "USD",
            },
            "offer": {"code": offer},
            "checkout_url": f"https://pay.hotmart.com/D98014973Y?off={offer}&checkoutMode=10",
            "checkout_country": {"iso": "US", "source": "phone_country_code"},
            "attribution": {
                "utm_source": "test",
                "utm_medium": "test",
                "utm_campaign": "test",
                "utm_content": "test",
                "utm_term": "",
                "sck": "test.test.test",
                "fbclid": "fixture",
                "referrer": "https://example.test/",
            },
            "consent": {
                "marketing_optin": True,
                "whatsapp_contact": True,
                "copy_version": "att1-whatsapp-contact-v1",
            },
        },
        "dedupe_key": f"{site}:{offer}:{email}",
    }


# ------------------------------------------------------------------ binding


def test_att1_manifest_declares_the_landing_of_each_additional_offer() -> None:
    config = _config()

    assert [
        (item.offer_code, item.site, item.landing_id, item.page_host, item.page_path)
        for item in config.offer_landings
    ] == list(ATT1_LANDINGS)
    assert tuple(item.offer_code for item in config.additional_offer_landings) == (
        config.additional_offer_codes
    )


@pytest.mark.parametrize("landing", ATT1_LANDINGS)
def test_each_landing_resolves_to_its_own_offer(landing: tuple[str, ...]) -> None:
    offer, site, landing_id, host, path = landing

    resolved = _config().offer_landing(site, landing_id)

    assert resolved == OfferLanding(offer, site, landing_id, host, path)


def test_a_landing_of_another_site_is_not_in_the_binding() -> None:
    config = _config()

    assert config.offer_landing("metodoraizana-mx", "ads-a") is None
    assert config.offer_landing("metodoraizana", "alimenta-tu-tiroides-d") is None
    assert config.offer_landing("metodoraizana", "ads-b") is None


def _landing(**overrides: str) -> OfferLanding:
    values = dict(
        offer_code="bmaztyhg",
        site="metodoraizana",
        landing_id="org-a",
        page_host="www.metodoraizana.com",
        page_path="/att1/evg/vsl/org-a",
    )
    values.update(overrides)
    return OfferLanding(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"site": "Metodo Raizana"},
        {"landing_id": ""},
        {"page_host": "https://www.metodoraizana.com"},
        {"page_host": "localhost"},
        {"page_path": "att1/evg/vsl/org-a"},
        {"page_path": "/att1/evg/vsl/org-a?x=1"},
        {"page_path": "/att1/evg/vsl/org-a#cta"},
        {"offer_code": " "},
    ],
)
def test_malformed_landing_is_rejected(overrides: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="additional_offer_landings"):
        _landing(**overrides)


@pytest.mark.parametrize(
    "landings",
    [
        # Una sola landing para dos ofertas adicionales.
        lambda config: config.additional_offer_landings[:1],
        # El orden no es el de additional_offer_codes.
        lambda config: tuple(reversed(config.additional_offer_landings)),
        # Una oferta que no es adicional.
        lambda config: (
            _landing(offer_code="83utgyow"),
            config.additional_offer_landings[1],
        ),
    ],
)
def test_landings_must_follow_the_additional_offers(landings) -> None:
    config = _config()

    with pytest.raises(ValueError, match="additional_offer_landings"):
        replace(config, additional_offer_landings=landings(config))


def test_two_offers_cannot_share_a_landing() -> None:
    config = _config()
    clash_with_default = (
        _landing(landing_id="ads-a", page_path="/att1/evg/vsl/ads-a"),
        config.additional_offer_landings[1],
    )
    clash_between_additional = (
        config.additional_offer_landings[0],
        _landing(offer_code="2uafw5bg"),
    )

    for landings in (clash_with_default, clash_between_additional):
        with pytest.raises(ValueError, match="repeat a site and landing_id"):
            replace(config, additional_offer_landings=landings)


def test_landings_must_be_offer_landing_objects() -> None:
    config = _config()
    raw = tuple(
        {"offer_code": item.offer_code, "site": item.site}
        for item in config.additional_offer_landings
    )

    with pytest.raises(ValueError, match="tuple of OfferLanding"):
        replace(config, additional_offer_landings=raw)  # type: ignore[arg-type]


def test_without_landings_the_binding_keeps_one_form_landing() -> None:
    config = replace(_config(), additional_offer_landings=())

    assert config.accepted_offer_codes == ("gopi6lh7", "bmaztyhg", "2uafw5bg")
    assert [item.offer_code for item in config.offer_landings] == ["gopi6lh7"]
    assert config.offer_landing("metodoraizana", "org-a") is None


def _json_binding(config: CommercialAllyConfig) -> dict[str, object]:
    payload: dict[str, object] = {
        name: getattr(config, name)
        for name in config.__dataclass_fields__
        if name not in {"additional_offer_codes", "additional_offer_landings"}
    }
    payload["product_price"] = str(config.product_price)
    return payload


def _landing_json(item: OfferLanding) -> dict[str, str]:
    return {
        "offer_code": item.offer_code,
        "site": item.site,
        "landing_id": item.landing_id,
        "page_host": item.page_host,
        "page_path": item.page_path,
    }


def test_json_manifest_keeps_the_landings_optional(tmp_path: Path) -> None:
    config = _config()
    path = tmp_path / "binding.json"
    base = {**_json_binding(config), "additional_offer_codes": list(config.additional_offer_codes)}

    path.write_text(json.dumps(base), encoding="utf-8")
    assert CommercialAllyConfig.from_json_file(path).additional_offer_landings == ()

    path.write_text(
        json.dumps({
            **base,
            "additional_offer_landings": [
                _landing_json(item) for item in config.additional_offer_landings
            ],
        }),
        encoding="utf-8",
    )
    assert CommercialAllyConfig.from_json_file(path) == config


@pytest.mark.parametrize(
    "landings",
    [
        {"offer_code": "bmaztyhg"},
        ["bmaztyhg", "2uafw5bg"],
        [{"offer_code": "bmaztyhg", "site": "metodoraizana"}],
        [{"offer_code": "bmaztyhg", "site": "metodoraizana", "landing_id": "org-a",
          "page_host": "www.metodoraizana.com", "page_path": "/att1/evg/vsl/org-a",
          "url": "https://www.metodoraizana.com/att1/evg/vsl/org-a"}],
    ],
)
def test_json_manifest_rejects_malformed_landings(tmp_path: Path, landings: object) -> None:
    config = _config()
    path = tmp_path / "binding.json"
    path.write_text(
        json.dumps({
            **_json_binding(config),
            "additional_offer_codes": list(config.additional_offer_codes),
            "additional_offer_landings": landings,
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="additional_offer_landings"):
        CommercialAllyConfig.from_json_file(path)


# ---------------------------------------------------------- durable binding


def _row(config: CommercialAllyConfig) -> dict[str, object]:
    row: dict[str, object] = {
        name: getattr(config, name) for name in config.__dataclass_fields__
    }
    row["product_price"] = "47.00"
    row["additional_offer_codes"] = list(config.additional_offer_codes)
    row["additional_offer_landings"] = [
        _landing_json(item) for item in config.additional_offer_landings
    ]
    row["status"] = "active"
    return row


def _resolve(row: dict[str, object], expected: CommercialAllyConfig) -> CommercialAllyConfig:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[row])

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="test-service-role",
        transport=httpx.MockTransport(handler),
    )
    return asyncio.run(client.resolve_commercial_ally_runtime_binding(expected))


def test_durable_binding_resolves_with_its_landings() -> None:
    config = _config()

    assert _resolve(_row(config), config) == config


def test_durable_binding_without_landings_is_drift_for_a_manifest_that_declares_them() -> None:
    # El orden de despliegue: un bridge nuevo sobre la fila vieja (landings en su
    # valor por defecto) no arranca, en vez de rechazar formularios en silencio.
    config = _config()
    row = _row(config)
    row["additional_offer_landings"] = []

    with pytest.raises(SupabaseError, match="config_drift"):
        _resolve(row, config)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda row: row.pop("additional_offer_landings"),
        lambda row: row.update(additional_offer_landings={"offer_code": "bmaztyhg"}),
        lambda row: row["additional_offer_landings"][0].pop("page_path"),
        lambda row: row["additional_offer_landings"][0].update(page_host="Not A Host"),
    ],
)
def test_malformed_durable_landings_fail_closed(mutate) -> None:
    config = _config()
    row = _row(config)
    mutate(row)

    with pytest.raises(SupabaseError, match="invalid_row"):
        _resolve(row, config)


# ------------------------------------------------------------------ parser


@pytest.mark.parametrize("landing", ATT1_LANDINGS)
def test_the_form_of_each_landing_is_parsed_with_its_offer(landing: tuple[str, ...]) -> None:
    offer, site, landing_id, host, path = landing

    parsed = parse_lead_precheckout(_lead(*landing), config=_config())

    assert parsed is not None
    assert (parsed.offer_code, parsed.site, parsed.landing_id) == (offer, site, landing_id)
    canonical = parsed.as_canonical_payload()
    assert canonical["source"]["landing_ref"] == landing_id  # type: ignore[index]
    assert canonical["source"]["page_url"] == f"https://{host}{path}"  # type: ignore[index]
    assert canonical["commerce"]["offer_ref"] == offer  # type: ignore[index]
    assert canonical["source"]["tenant_ref"] == "lancemos"  # type: ignore[index]
    assert canonical["source"]["funnel_ref"] == "att1"  # type: ignore[index]


@pytest.mark.parametrize(
    ("label", "landing"),
    [
        # La oferta de otra landing en la pagina de ads-a.
        ("offer_of_other_landing", ("bmaztyhg",) + ATT1_LANDINGS[0][1:]),
        # La landing de .mx declarada con el sitio de .com.
        ("site_of_other_landing", ("2uafw5bg", "metodoraizana") + ATT1_LANDINGS[2][2:]),
        # La landing de .mx servida desde el host de .com.
        (
            "host_of_other_landing",
            ATT1_LANDINGS[2][:3] + ("www.metodoraizana.com", ATT1_LANDINGS[2][4]),
        ),
        # La ruta de org-a con la landing ads-a.
        ("path_of_other_landing", ATT1_LANDINGS[0][:4] + (ATT1_LANDINGS[1][4],)),
        # La oferta de la recuperacion de GHL, que queda afuera del binding.
        ("offer_outside_binding", ("83utgyow",) + ATT1_LANDINGS[1][1:]),
        # Una landing que la instancia no declara.
        (
            "undeclared_landing",
            ("bmaztyhg", "metodoraizana", "org-b", "www.metodoraizana.com", "/att1/evg/vsl/org-b"),
        ),
    ],
)
def test_crossed_pairs_are_rejected(label: str, landing: tuple[str, ...]) -> None:
    assert parse_lead_precheckout(_lead(*landing), config=_config()) is None, label


def test_without_landings_only_the_default_form_is_parsed() -> None:
    config = replace(_config(), additional_offer_landings=())

    assert parse_lead_precheckout(_lead(*ATT1_LANDINGS[0]), config=config) is not None
    assert parse_lead_precheckout(_lead(*ATT1_LANDINGS[1]), config=config) is None
    assert parse_lead_precheckout(_lead(*ATT1_LANDINGS[2]), config=config) is None


# ---------------------------------------------------------------- migration


def test_migration_seeds_no_customer_values() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    for customer_value in (
        "5071808",
        "D98014973Y",
        "gopi6lh7",
        "bmaztyhg",
        "2uafw5bg",
        "metodoraizana",
        "8104005",
        "bxjge6zq",
        "psicologajohanna",
    ):
        assert customer_value not in sql
