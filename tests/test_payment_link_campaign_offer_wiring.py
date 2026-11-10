"""El link del agente con la oferta de la campana, por la app (E-410, C5).

Con ``AGENT_CAMPAIGN_OFFER_LINK_ENABLED`` y un segmento vigente con
``[oferta].codigo_oferta`` en el turno, ``send_payment_link`` reserva con
``offer_code``, ``campaign_ref`` (la clave del segmento) y el ``funnel_ref`` del
producto del link (con un solo producto, el del principal: D10 de E-408). Sin el
flag, o sin el campo, la reserva recibe exactamente los kwargs de hoy.
``blocked_campaign_offer`` deriva con ``payment_link_campaign_offer_unavailable``
y no manda nada a Chatwoot.

Datos: el webhook es la captura de la conversacion 200 del inbox 9
(``_text_webhook``), con la etiqueta en ``conversation.labels``; el segmento es
el FICTICIO de ``tests/fixtures/segments`` con ``codigo_oferta`` sumado (una
oferta declarada de ATT1, ``bmaztyhg``, porque el cargador exige que este
declarada); el link que devuelve la base es la URL de la oferta de Black que se
verifico en el checkout publico (F1,
``tests/fixtures/hotmart_checkout_offer_black_att1_20261110.json``), con el
marcador del stub de la base.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
import test_segment_link_coupon as coupon_wiring
from test_agent_link_product import _LinkSupabase
from test_segment_bridge import ALWAYS, FUTURE, PAST, SEGMENT
from test_segment_link_coupon import LABEL, _link, _settings, _two
from test_whatsapp_inbound_equivalence import FORM_ID, PAYMENT_LINK_PROPOSAL, _PaymentLinkSupabase

from bridge.payment_link_coupon import SegmentOfferLink, segment_link_offer

F1 = json.loads(
    (Path(__file__).parent / "fixtures" / "hotmart_checkout_offer_black_att1_20261110.json")
    .read_text(encoding="utf-8")
)
OFFER = "bmaztyhg"
# La URL de la oferta de la campana verificada en el checkout publico (F1), con
# el marcador del stub de la base (el ULID y la | del stub de test_webhook).
CAMPAIGN_URL = F1["link_de_campana_esperado"].split("&sck=")[0] + (
    "&sck=hermes%7Cv1%7C01K3F8QW7N2VYB4M6X9CDPTZRA"
)
PRINCIPAL_FUNNEL = "att1"


@pytest.fixture
def with_offer_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """El segmento ficticio con ``codigo_oferta``, en el lugar del de siempre."""

    folder = tmp_path / "segmento-con-oferta"
    folder.mkdir()
    text = SEGMENT.read_text(encoding="utf-8")
    anchor = 'cupon = "PRUEBA11"\n'
    assert text.count(anchor) == 1
    path = folder / SEGMENT.name
    path.write_text(text.replace(anchor, anchor + f'codigo_oferta = "{OFFER}"\n'), encoding="utf-8")
    monkeypatch.setattr(coupon_wiring, "SEGMENT", path)
    return path


def _supabase(**kwargs: Any) -> _PaymentLinkSupabase:
    supabase = _PaymentLinkSupabase(identities=(FORM_ID,))
    supabase.reservation_url_override = CAMPAIGN_URL
    for key, value in kwargs.items():
        setattr(supabase, key, value)
    return supabase


def _reservation(tmp_path: Path, settings: Any, labels: list[str], **kwargs: Any) -> tuple[dict, str]:
    supabase = kwargs.pop("supabase", None) or _supabase()
    url = _link(tmp_path, settings, labels, supabase=supabase, **kwargs)
    [reservation] = supabase.candidate_calls
    return reservation, url


def _stable(reservation: dict[str, Any]) -> dict[str, Any]:
    """Los kwargs sin lo que cambia en cada turno (el ULID y la hora): las mismas claves."""

    return {key: (value if key not in {"issuance_ulid", "now"} else "<turno>")
            for key, value in reservation.items()}


def _today_kwargs(tmp_path: Path) -> dict[str, Any]:
    """Los kwargs de la reserva sin la campana: los de hoy."""

    other = tmp_path / "hoy"
    other.mkdir()
    reservation, _ = _reservation(other, _settings(other, coupon=False), ["otra"])
    return reservation


# ------------------------------------------------------------- la funcion pura


def test_the_offer_is_the_one_of_the_first_segment_of_the_turn_that_has_one(
    tmp_path: Path, with_offer_code: Path
) -> None:
    catalog = _settings(tmp_path).agent_segment_catalog
    assert segment_link_offer(catalog, [{"key": LABEL, "version": 1}]) == SegmentOfferLink(LABEL, OFFER, None)
    assert segment_link_offer(catalog, []) is None
    assert segment_link_offer(catalog, None) is None
    assert segment_link_offer(catalog, [{"key": LABEL, "version": 2}]) is None
    assert segment_link_offer(None, [{"key": LABEL, "version": 1}]) is None


def test_a_segment_without_the_offer_code_gives_none(tmp_path: Path) -> None:
    catalog = _settings(tmp_path).agent_segment_catalog
    assert segment_link_offer(catalog, [{"key": LABEL, "version": 1}]) is None


# ------------------------------------------------------------- el link por la app


def test_with_the_flag_the_reserve_gets_the_offer_the_campaign_and_the_funnel(
    tmp_path: Path, with_offer_code: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="bridge")
    settings = _settings(tmp_path, coupon=False, agent_campaign_offer_link_enabled=True)

    reservation, url = _reservation(tmp_path, settings, [LABEL])

    assert reservation["offer_code"] == OFFER
    assert reservation["campaign_ref"] == LABEL
    assert reservation["funnel_ref"] == PRINCIPAL_FUNNEL
    assert reservation["phone_equivalence"] is True
    assert url == CAMPAIGN_URL
    assert f"segment_link_offer key={LABEL} conversation=200" in caplog.text
    # El codigo no va al log.
    assert OFFER not in "\n".join(
        record.getMessage() for record in caplog.records if record.getMessage().startswith("segment_")
    )


@pytest.mark.parametrize(
    ("labels", "kwargs"),
    [
        ([LABEL], {}),  # la campana con oferta, el flag apagado (como hoy en todas)
        (["otra"], {"agent_campaign_offer_link_enabled": True}),  # sin la etiqueta
        ([LABEL], {"agent_campaign_offer_link_enabled": True, "window": PAST}),  # vencida
        ([LABEL], {"agent_campaign_offer_link_enabled": True, "window": FUTURE}),  # por empezar
    ],
)
def test_without_the_flag_or_the_campaign_the_reserve_gets_exactly_the_kwargs_of_today(
    tmp_path: Path, with_offer_code: Path, labels: list[str], kwargs: dict[str, Any]
) -> None:
    window = kwargs.pop("window", ALWAYS)
    settings = _settings(tmp_path, coupon=False, window=window, **kwargs)

    reservation, _ = _reservation(tmp_path, settings, labels)

    assert _stable(reservation) == _stable(_today_kwargs(tmp_path))
    assert "offer_code" not in reservation and "campaign_ref" not in reservation
    assert "funnel_ref" not in reservation


def test_a_segment_without_the_offer_code_and_the_flag_on_is_the_reserve_of_today(tmp_path: Path) -> None:
    settings = _settings(tmp_path, coupon=False, agent_campaign_offer_link_enabled=True)

    reservation, _ = _reservation(tmp_path, settings, [LABEL])

    assert _stable(reservation) == _stable(_today_kwargs(tmp_path))


def test_an_offer_the_base_cannot_issue_goes_to_a_person_and_nothing_is_sent(
    tmp_path: Path, with_offer_code: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="bridge")
    supabase = _supabase(candidate_outcome="blocked_campaign_offer")
    settings = _settings(tmp_path, coupon=False, agent_campaign_offer_link_enabled=True)

    with pytest.raises(ValueError):
        # _link exige una respuesta en Chatwoot: no hay ninguna.
        _link(tmp_path, settings, [LABEL], supabase=supabase)

    [reservation] = supabase.candidate_calls
    assert reservation["offer_code"] == OFFER
    assert "payment_link_handoff reason=blocked_campaign_offer" in caplog.text
    [handoff] = supabase.handoff_calls
    assert handoff["reason_code"] == "commercial_exception"
    assert supabase.prepare_calls == []


# ------------------------------------------------------------- varios productos


def test_the_offer_of_another_product_does_not_go_in_the_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="bridge")
    folder = tmp_path / "segmento-att2"
    folder.mkdir()
    text = SEGMENT.read_text(encoding="utf-8").replace('cupon = "PRUEBA11"\n', 'cupon = "PRUEBA11"\ncodigo_oferta = "prueba2b"\n')
    (folder / SEGMENT.name).write_text(text, encoding="utf-8")
    monkeypatch.setattr(coupon_wiring, "SEGMENT", folder / SEGMENT.name)
    calls = {}
    for link_product in ("att1", "att2"):
        place = tmp_path / link_product
        place.mkdir()
        settings = _two(place, product="att2", agent_campaign_offer_link_enabled=True)
        supabase = _LinkSupabase()
        _link(place, settings, [LABEL], proposal={**PAYMENT_LINK_PROPOSAL, "product": link_product},
              supabase=supabase)
        [calls[link_product]] = supabase.candidate_calls

    assert "offer_code" not in calls["att1"] and calls["att1"]["funnel_ref"] == "att1"
    assert f"segment_link_offer_other_product key={LABEL} product=att1 conversation=200" in caplog.text
    assert calls["att2"]["offer_code"] == "prueba2b" and calls["att2"]["funnel_ref"] == "att2"
    assert calls["att2"]["campaign_ref"] == LABEL


# ------------------------------------------------------------- el flag


def test_the_flag_is_off_by_default_and_needs_the_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    from bridge import app as app_module
    from bridge.app import Settings

    assert "agent_campaign_offer_link_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    for name, value in {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "CHATWOOT_INBOX_ID": "9",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AGENT_SEGMENTS_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_CAMPAIGN_OFFER_LINK_ENABLED", raising=False)
    assert Settings.from_env().agent_campaign_offer_link_enabled is False
    monkeypatch.setenv("AGENT_CAMPAIGN_OFFER_LINK_ENABLED", "yes")
    assert Settings.from_env().agent_campaign_offer_link_enabled is False
    monkeypatch.setenv("AGENT_CAMPAIGN_OFFER_LINK_ENABLED", " TRUE ")
    with pytest.raises(ValueError, match="AGENT_CAMPAIGN_OFFER_LINK_ENABLED requires AGENT_SEGMENTS_ENABLED"):
        Settings.from_env()
