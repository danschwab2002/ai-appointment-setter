"""El freno del descuento del carrito por una campana sin cupon (E-410 C8, decision de Dan del 2026-11-10).

``[oferta].frenar_descuento_carrito`` (por defecto ``false``) hace que una campana
vigente **sin** ``[oferta].cupon`` active los mismos frenos de E-339 que hoy
activa una con cupon: A1 del carrito sin el 10 %, A2 y Z que no salen (con el
cierre del caso), el seguimiento con cupon fijo (``TIROIDES10``) salteado, el
boton «Quiero mi 10 %» que no adelanta Z y la renovacion del cupon por
respuesta. Sin el campo, o en ``false``, una campana sin cupon no frena nada:
como hoy.

Las pruebas son las mismas de E-339 (``tests/test_segment_cart_followups.py`` y
``tests/test_segment_link_coupon.py``, con sus capturas: la conversacion 200 del
inbox 9, las plantillas A1, A2 y Z del carrito de ATT1, la respuesta 471 de ATT1
al A2) corridas con el segmento ficticio de ``tests/fixtures/segments`` sin el
cupon y con el campo.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import test_segment_cart_followups as e339
import test_segment_link_coupon as coupon_wiring
from test_segment_bridge import SEGMENT

from bridge.agent_campaigns import parse_campaign
from bridge.payment_link_coupon import (
    SegmentCoupon,
    segment_cart_discount_brake,
    segment_coupon_for_labels,
    segment_link_coupon,
)
from bridge.segment_knowledge import SegmentError, SegmentKnowledge

LABEL = coupon_wiring.LABEL
COUPON_LINE = 'cupon = "PRUEBA11"\n'


def _segment_without_coupon(folder: Path, stop: bool | None) -> Path:
    text = SEGMENT.read_text(encoding="utf-8")
    assert text.count(COUPON_LINE) == 1
    line = "" if stop is None else f"frenar_descuento_carrito = {'true' if stop else 'false'}\n"
    folder.mkdir()
    path = folder / SEGMENT.name
    path.write_text(text.replace(COUPON_LINE, line), encoding="utf-8")
    return path


@pytest.fixture
def stop_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = _segment_without_coupon(tmp_path / "segmento-que-frena", True)
    monkeypatch.setattr(coupon_wiring, "SEGMENT", path)
    return path


@pytest.fixture(params=[None, False], ids=["sin-el-campo", "en-false"])
def stop_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Path:
    path = _segment_without_coupon(tmp_path / "segmento-que-no-frena", request.param)
    monkeypatch.setattr(coupon_wiring, "SEGMENT", path)
    return path


# ------------------------------------------------------------- el campo


def test_the_field_is_a_boolean_and_false_by_default() -> None:
    import tomllib

    raw = tomllib.loads(SEGMENT.read_text(encoding="utf-8"))
    segment = SegmentKnowledge.from_mapping(raw, time_zone="America/Mexico_City")
    assert segment.offer is not None and segment.offer.stop_cart_discount is False
    raw["oferta"]["frenar_descuento_carrito"] = True
    assert SegmentKnowledge.from_mapping(raw, time_zone="America/Mexico_City").offer.stop_cart_discount is True
    # No cambia lo que lee el agente.
    assert SegmentKnowledge.from_mapping(raw, time_zone="America/Mexico_City").render() == segment.render()
    raw["oferta"]["frenar_descuento_carrito"] = "si"
    with pytest.raises(SegmentError, match="frenar_descuento_carrito"):
        SegmentKnowledge.from_mapping(raw, time_zone="America/Mexico_City")


def test_the_campaign_of_the_ae_keeps_it_only_when_true() -> None:
    base = {
        "clave": "black-november-2026", "nombre": "Black November", "etiqueta": "black-november",
        "macro_etiqueta": 77, "desde": "2026-11-20T00:00:00-06:00", "hasta": "2026-12-01T00:00:00-06:00",
        "palabras_clave": ["Black November"], "asesoramiento": ["x"],
    }
    on = parse_campaign({**base, "oferta": {"codigo_oferta": "wmkq4ba9", "frenar_descuento_carrito": True}})
    off = parse_campaign({**base, "oferta": {"codigo_oferta": "wmkq4ba9", "frenar_descuento_carrito": False}})
    assert on["oferta"] == {"codigo_oferta": "wmkq4ba9", "frenar_descuento_carrito": True}
    # false es lo de hoy: el borrador queda igual que sin el campo.
    assert off == parse_campaign({**base, "oferta": {"codigo_oferta": "wmkq4ba9"}})


# ------------------------------------------------------------- la funcion pura


def test_with_the_field_the_campaign_without_coupon_brakes(tmp_path: Path, stop_on: Path) -> None:
    catalog = coupon_wiring._settings(tmp_path).agent_segment_catalog
    entries = [{"key": LABEL, "version": 1}]

    assert segment_cart_discount_brake(catalog, entries) == SegmentCoupon(LABEL, None, None)
    assert segment_coupon_for_labels(catalog, [LABEL], now=e339._now()) == SegmentCoupon(LABEL, None, None)
    # El link no lleva cupon: la campana solo frena.
    assert segment_link_coupon(catalog, entries) is None


def test_without_the_field_or_in_false_nothing_brakes(tmp_path: Path, stop_off: Path) -> None:
    catalog = coupon_wiring._settings(tmp_path).agent_segment_catalog

    assert segment_cart_discount_brake(catalog, [{"key": LABEL, "version": 1}]) is None
    assert segment_coupon_for_labels(catalog, [LABEL], now=e339._now()) is None


def test_with_a_coupon_the_brake_is_the_one_of_today(tmp_path: Path) -> None:
    catalog = coupon_wiring._settings(tmp_path).agent_segment_catalog
    entries = [{"key": LABEL, "version": 1}]

    assert segment_cart_discount_brake(catalog, entries) == segment_link_coupon(catalog, entries)


# ------------------------------------------- con el campo en true: los frenos de E-339


@pytest.mark.parametrize("step_key", e339.STEPS)
def test_a2_and_z_do_not_go_out(
    tmp_path: Path, stop_on: Path, step_key: str, caplog: pytest.LogCaptureFixture
) -> None:
    e339.test_a2_and_z_do_not_go_out_to_a_person_of_a_campaign_with_a_coupon(tmp_path, step_key, caplog)


def test_a1_goes_without_the_discount(tmp_path: Path, stop_on: Path, caplog: pytest.LogCaptureFixture) -> None:
    e339.test_a1_reaches_a_person_of_a_campaign_without_the_coupon(tmp_path, caplog)


def test_the_fixed_coupon_followup_is_skipped(
    tmp_path: Path, stop_on: Path, caplog: pytest.LogCaptureFixture
) -> None:
    e339.test_the_fixed_coupon_followup_is_skipped_for_a_person_of_a_campaign(tmp_path, caplog)


def test_the_button_does_not_bring_z_forward(
    tmp_path: Path, stop_on: Path, caplog: pytest.LogCaptureFixture
) -> None:
    e339.test_the_button_does_not_bring_z_forward_and_the_agent_answers(tmp_path, caplog)


def test_the_cart_coupon_is_not_renewed_by_reply(
    tmp_path: Path, stop_on: Path, caplog: pytest.LogCaptureFixture
) -> None:
    coupon_wiring.test_with_a_segment_coupon_the_cart_coupon_is_not_offered(tmp_path, caplog)


# ------------------------------------- sin el campo o en false: como hoy, sin freno


@pytest.mark.parametrize("step_key", e339.STEPS)
def test_a2_and_z_go_out_as_today(tmp_path: Path, stop_off: Path, step_key: str) -> None:
    catalog = coupon_wiring._settings(tmp_path).agent_segment_catalog
    chatwoot, _ = e339._dispatch(step_key, tmp_path, labels=(LABEL,), catalog=catalog)
    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["template_params"]["name"] == e339.TEMPLATE_OF[step_key]  # type: ignore[index]


def test_the_followup_goes_out_as_today(tmp_path: Path, stop_off: Path) -> None:
    import asyncio

    sweeper, chatwoot, supabase = e339._sweep(tmp_path, labels=[LABEL])
    assert asyncio.run(sweeper.run_once()) == 1
    assert len(supabase.claims) == 1 and len(chatwoot.sent) == 1


def test_the_button_brings_z_forward_as_today(tmp_path: Path, stop_off: Path) -> None:
    supabase, shadow = e339._press(
        coupon_wiring._settings(tmp_path, recovery_cart_followups_enabled=True), [LABEL]
    )
    assert len(supabase.renewals) == 1
    assert shadow.calls == []


def test_a1_keeps_the_discount_as_today(tmp_path: Path, stop_off: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="bridge")
    chatwoot, authority, _ = e339._first_touch(
        tmp_path, catalog=coupon_wiring._settings(tmp_path).agent_segment_catalog, contact=[LABEL]
    )
    assert "coupon" in authority.events
    assert "segment_first_touch_without_coupon" not in caplog.text



def test_the_judge_reads_the_field_and_knows_it_is_not_an_item() -> None:
    import tomllib

    from bridge import segment_check as sc

    raw = tomllib.loads(SEGMENT.read_text(encoding="utf-8"))
    raw["oferta"]["frenar_descuento_carrito"] = True
    sc.parse_segment(raw)
    raw["oferta"]["frenar_descuento_carrito"] = "si"
    with pytest.raises(sc.SegmentCheckError, match="frenar_descuento_carrito"):
        sc.parse_segment(raw)
    assert "oferta.frenar_descuento_carrito = true es una regla del sistema" in sc.JUDGE_INSTRUCTIONS
