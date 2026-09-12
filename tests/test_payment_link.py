from datetime import UTC, datetime, timedelta

import pytest

from bridge.payment_link import (
    PaymentLinkConfig,
    PaymentLinkUnavailable,
    build_payment_link,
    render_payment_link_reply,
)


EVENT_ULID = "01K3F8QW7N2VYB4M6X9CDPTZRA"
BASE_URL = (
    "https://pay.hotmart.com/F106691755G?off=bxjge6zq&checkoutMode=10"
    "&name=Ana%20Perez&email=ana%40example.com"
    "&utm_source=meta-ads&utm_medium=cpc&utm_campaign=campana-x"
    "&sck=meta-ads.cpc.campana-x&fbclid=IwAR-example"
)


def test_build_payment_link_appends_src_without_changing_existing_bytes() -> None:
    result = build_payment_link(
        checkout_url=BASE_URL,
        sequence_origin_event_id=EVENT_ULID,
        submitted_at=datetime(2026, 9, 10, 12, tzinfo=UTC),
        now=datetime(2026, 9, 11, 12, tzinfo=UTC),
        config=PaymentLinkConfig(),
    )

    assert result.final_url == f"{BASE_URL}&src=hermes-{EVENT_ULID}"
    assert result.tracking_field == "src"
    assert result.tracking_value == f"hermes-{EVENT_ULID}"


def test_build_payment_link_uses_configured_fallback_when_src_exists() -> None:
    original = f"{BASE_URL}&src=cashinbot-existing"

    result = build_payment_link(
        checkout_url=original,
        sequence_origin_event_id=EVENT_ULID,
        submitted_at=datetime(2026, 9, 10, 12, tzinfo=UTC),
        now=datetime(2026, 9, 11, 12, tzinfo=UTC),
        config=PaymentLinkConfig(),
    )

    assert result.final_url == f"{original}&xcod=hermes-{EVENT_ULID}"
    assert result.tracking_field == "xcod"


def test_build_payment_link_blocks_when_all_authorized_fields_exist() -> None:
    with pytest.raises(PaymentLinkUnavailable, match="tracking_fields_occupied"):
        build_payment_link(
            checkout_url=f"{BASE_URL}&src=existing&xcod=existing",
            sequence_origin_event_id=EVENT_ULID,
            submitted_at=datetime(2026, 9, 10, 12, tzinfo=UTC),
            now=datetime(2026, 9, 11, 12, tzinfo=UTC),
            config=PaymentLinkConfig(),
        )


def test_build_payment_link_allows_exactly_seven_days() -> None:
    submitted_at = datetime(2026, 9, 1, 12, tzinfo=UTC)

    result = build_payment_link(
        checkout_url=BASE_URL,
        sequence_origin_event_id=EVENT_ULID,
        submitted_at=submitted_at,
        now=submitted_at + timedelta(days=7),
        config=PaymentLinkConfig(),
    )

    assert result.tracking_value == f"hermes-{EVENT_ULID}"


def test_build_payment_link_blocks_after_seven_days() -> None:
    submitted_at = datetime(2026, 9, 1, 12, tzinfo=UTC)

    with pytest.raises(PaymentLinkUnavailable, match="checkout_url_stale"):
        build_payment_link(
            checkout_url=BASE_URL,
            sequence_origin_event_id=EVENT_ULID,
            submitted_at=submitted_at,
            now=submitted_at + timedelta(days=7, microseconds=1),
            config=PaymentLinkConfig(),
        )


def test_payment_link_config_rejects_arbitrary_tracking_fields() -> None:
    with pytest.raises(ValueError, match="tracking_fields"):
        PaymentLinkConfig(tracking_fields=("utm_source",))


@pytest.mark.parametrize(
    "tracking_fields",
    [(), ("src",), ("xcod",), ("xcod", "src")],
)
def test_payment_link_config_requires_exact_ordered_fallback(
    tracking_fields: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="tracking_fields"):
        PaymentLinkConfig(tracking_fields=tracking_fields)


def test_build_payment_link_rejects_non_ulid_sequence_origin() -> None:
    with pytest.raises(PaymentLinkUnavailable, match="invalid_sequence_origin_event_id"):
        build_payment_link(
            checkout_url=BASE_URL,
            sequence_origin_event_id="conversation-123",
            submitted_at=datetime(2026, 9, 10, 12, tzinfo=UTC),
            now=datetime(2026, 9, 11, 12, tzinfo=UTC),
            config=PaymentLinkConfig(),
        )


def test_build_payment_link_rejects_url_without_existing_query() -> None:
    with pytest.raises(PaymentLinkUnavailable, match="invalid_checkout_url"):
        build_payment_link(
            checkout_url="https://pay.hotmart.com/F106691755G",
            sequence_origin_event_id=EVENT_ULID,
            submitted_at=datetime(2026, 9, 10, 12, tzinfo=UTC),
            now=datetime(2026, 9, 11, 12, tzinfo=UTC),
            config=PaymentLinkConfig(),
        )


@pytest.mark.parametrize("control", ["\n", "\r", "\t", "\x00", "\x9f", "\u2028"])
def test_build_payment_link_rejects_control_characters_in_opaque_url(
    control: str,
) -> None:
    with pytest.raises(PaymentLinkUnavailable, match="invalid_checkout_url"):
        build_payment_link(
            checkout_url=f"{BASE_URL}{control}https://evil.example/path",
            sequence_origin_event_id=EVENT_ULID,
            submitted_at=datetime(2026, 9, 10, 12, tzinfo=UTC),
            now=datetime(2026, 9, 11, 12, tzinfo=UTC),
            config=PaymentLinkConfig(),
        )


def test_render_payment_link_reply_injects_exact_url_after_agent_preamble() -> None:
    final_url = f"{BASE_URL}&src=hermes-{EVENT_ULID}"

    assert render_payment_link_reply(
        preamble="Sí, claro. Podés completar tu compra acá:  ",
        final_url=final_url,
    ) == f"Sí, claro. Podés completar tu compra acá:\n{final_url}"


def test_render_payment_link_reply_rejects_url_in_agent_preamble() -> None:
    with pytest.raises(PaymentLinkUnavailable, match="agent_reply_contains_url"):
        render_payment_link_reply(
            preamble="Usá https://pay.hotmart.com/inventado",
            final_url=f"{BASE_URL}&src=hermes-{EVENT_ULID}",
        )


@pytest.mark.parametrize(
    "preamble",
    [
        "Pagá en pay.hotmart.com/FAKE",
        "Más info en www.example.com/ayuda",
        "Pagá en evil.example.",
    ],
)
def test_render_payment_link_reply_rejects_scheme_less_url_in_agent_preamble(
    preamble: str,
) -> None:
    with pytest.raises(PaymentLinkUnavailable, match="agent_reply_contains_url"):
        render_payment_link_reply(
            preamble=preamble,
            final_url=f"{BASE_URL}&src=hermes-{EVENT_ULID}",
        )
