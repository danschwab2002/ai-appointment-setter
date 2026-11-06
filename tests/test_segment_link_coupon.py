"""El cupon del segmento en el link del agente y el freno del cupon del carrito (PR-A2, E-338).

Diseno E-310 §2.6 (D8): un segmento vigente con ``[oferta].cupon`` hace que el
link de ``send_payment_link`` salga con ``&offDiscount=<cupon>`` (el armado de
``payment_link_coupon.payment_link_with_coupon``, medido en el checkout real de
Hotmart, ``hotmart_checkout_coupon_maribel_20261027.json``), y mientras va no se
pide la renovacion del cupon del carrito por respuesta ni va ``recovery_coupon``
en la entrada (E-267). Sin segmentos todo sale como antes.

Datos: el webhook es la captura de la conversacion 200 del inbox 9
(``_text_webhook``), con la etiqueta en ``conversation.labels`` como en
``test_segment_bridge``. El segmento es el FICTICIO de ``tests/fixtures/segments``
(cupon ``PRUEBA11``), con la vigencia reescrita para no depender del reloj. El
link de la base es el del stub de siempre (``StubPaymentLinkSupabase``). El A2 y
el cupon del carrito son los de ``test_recovery_coupon_reply`` (respuestas reales
de ATT1).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_campaign_inbound_entry_bridge import _Answers, _turn as _entry_turn
from test_campaign_inbound_entry_bridge import _settings as _entry_settings
from test_recovery_coupon_reply import ATT1_471, CODE, _a2_message
from test_agent_link_product import _LinkSupabase
from test_checkout_link_for_funnel import _two_products_settings
from test_segment_bridge import ALWAYS, FUTURE, PAST, SEGMENT, _SegmentShadow
from test_segment_knowledge import sellar_chequeo
from test_webhook import StubChatwootClient, _post, _signed_headers
from test_whatsapp_inbound_equivalence import (
    FORM_ID,
    PAYMENT_LINK_PROPOSAL,
    SECRET,
    _manifest_settings,
    _PaymentLinkSupabase,
    _text_webhook,
)

from bridge.app import Settings, create_app
from bridge.instance_manifest import InstanceManifest
from bridge.payment_link_coupon import (
    SegmentCoupon,
    mark_segment_link_coupon,
    segment_coupon_fits_product,
    segment_link_coupon,
)
from bridge.segment_knowledge import SEGMENT_CONTEXT_KEY, load_segment_catalog
from bridge.supabase import RecoveryCouponForConversation

LABEL = "campana-de-prueba"
SEGMENT_COUPON = "PRUEBA11"
BASE_LINK = (
    "https://pay.hotmart.com/F106691755G?off=bxjge6zq"
    "&checkoutMode=10&src=hermes"
    "&sck=hermes%7Cv1%7C01K3F8QW7N2VYB4M6X9CDPTZRA"
)
LINK_REPLY = PAYMENT_LINK_PROPOSAL["reply"]


def _settings(
    tmp_path: Path,
    *,
    coupon: bool = True,
    enabled: bool = True,
    window: tuple[str, str] = ALWAYS,
    product: str | None = None,
    second: tuple[str, str, int] | None = None,
    base: Settings | None = None,
    **overrides: Any,
) -> Settings:
    """El manifiesto de prueba con el segmento ficticio declarado (como test_segment_bridge).

    ``product``: ``[oferta].producto`` del segmento. ``second``: (etiqueta,
    cupon, prioridad) de un segundo segmento, copia del primero con otra clave.
    ``base``: otros settings de partida (por ejemplo, dos productos).
    """

    base = base or _manifest_settings(tmp_path)
    instance = tmp_path / "instancia"
    folder = instance / "conocimiento" / "segmentos"
    folder.mkdir()
    text = SEGMENT.read_text(encoding="utf-8")
    text = text.replace("desde = 2026-11-20T00:00:00-06:00", f"desde = {window[0]}")
    text = text.replace("hasta = 2026-12-01T00:00:00-06:00", f"hasta = {window[1]}")
    if not coupon:
        assert f'cupon = "{SEGMENT_COUPON}"\n' in text
        text = text.replace(f'cupon = "{SEGMENT_COUPON}"\n', "")
    if product is not None:
        text = text.replace("[oferta]\n", f'[oferta]\nproducto = "{product}"\n', 1)
    (folder / SEGMENT.name).write_text(text, encoding="utf-8")
    segments = [(LABEL, SEGMENT.name, 10)]
    if second is not None:
        label, code, priority = second
        other = text.replace(f'clave = "{LABEL}"', f'clave = "{label}"').replace(
            f'cupon = "{SEGMENT_COUPON}"', f'cupon = "{code}"'
        )
        (folder / f"{label}-v1.toml").write_text(other, encoding="utf-8")
        segments.append((label, f"{label}-v1.toml", priority))
    # E-336/E-337: el cargador exige el chequeo_sha. Cada segmento se sella con el calculo
    # real, con los otros declarados como ``otros`` (los que se pisan entran en la huella).
    assert base.commercial_knowledge is not None
    written = {name: (folder / name).read_text(encoding="utf-8") for _, name, _ in segments}
    for name, content in written.items():
        rest = tuple(other for key, other in written.items() if key != name)
        (folder / name).write_text(
            sellar_chequeo(content, base.commercial_knowledge, rest), encoding="utf-8"
        )
    manifest_path = instance / "instancia.toml"
    anchor = 'conocimiento = "conocimiento/knowledge-v1.toml"\n'
    declaration = "".join(
        f'\n[[agente.segmentos]]\nclave = "{label}"\netiqueta = "{label}"\n'
        f'conocimiento = "conocimiento/segmentos/{name}"\nprioridad = {priority}\n'
        for label, name, priority in segments
    )
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(anchor, anchor + declaration),
        encoding="utf-8",
    )
    assert base.instance_manifest is not None and base.commercial_knowledge is not None
    manifest = replace(
        base.instance_manifest,
        agent_segments=InstanceManifest.from_toml_file(manifest_path).agent_segments,
    )
    return replace(
        base,
        instance_manifest=manifest,
        agent_segments_enabled=enabled,
        agent_segment_catalog=(
            load_segment_catalog(manifest, instance, base.commercial_knowledge) if enabled else None
        ),
        payment_link_enabled=True,
        **overrides,
    )


def _process(
    supabase: Any,
    *,
    settings: Settings,
    webhook: dict[str, Any],
    history: list[dict[str, Any]],
    proposal: dict[str, object],
) -> tuple[StubChatwootClient, _SegmentShadow]:
    """``_process`` de la equivalencia, con el doble que acepta ``extra_system``."""

    shadow = _SegmentShadow(proposal)
    chatwoot = StubChatwootClient(messages=history)
    app = create_app(
        settings,
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        supabase_client=supabase,
    )
    raw_body = json.dumps(webhook, separators=(",", ":")).encode("utf-8")
    response = _post(app, raw_body, _signed_headers(raw_body, secret=SECRET, delivery="inbound-200"))
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())
    return chatwoot, shadow


def _link(
    tmp_path: Path,
    settings: Settings,
    labels: list[str] | None,
    *,
    proposal: dict[str, object] | None = None,
    supabase: Any = None,
    shadow_out: list[_SegmentShadow] | None = None,
) -> str:
    """La persona pide el link y el agente decide send_payment_link: lo que se escribio."""

    webhook, history = _text_webhook("Pasame el link para pagar")
    if labels is not None:
        webhook["conversation"]["labels"] = labels
    chatwoot, shadow = _process(
        supabase or _PaymentLinkSupabase(identities=(FORM_ID,)),
        settings=settings,
        webhook=webhook,
        history=history,
        proposal=proposal or PAYMENT_LINK_PROPOSAL,
    )
    if shadow_out is not None:
        shadow_out.append(shadow)
    [reply] = chatwoot.reply_calls
    content = str(reply["content"])
    assert content.startswith(f"{LINK_REPLY}\n")
    return content.removeprefix(f"{LINK_REPLY}\n")


# ------------------------------------------------------------- la funcion pura


def test_the_coupon_is_the_one_of_the_first_segment_of_the_turn_that_has_one(tmp_path: Path) -> None:
    catalog = _settings(tmp_path).agent_segment_catalog
    entries = [{"key": LABEL, "version": 1}]

    assert segment_link_coupon(catalog, entries) == SegmentCoupon(LABEL, SEGMENT_COUPON, None)
    assert segment_link_coupon(catalog, []) is None
    assert segment_link_coupon(catalog, None) is None
    assert segment_link_coupon(catalog, [{"key": LABEL, "version": 2}]) is None
    # Sin flag no hay catalogo.
    assert segment_link_coupon(None, entries) is None


def test_a_segment_without_coupon_gives_none(tmp_path: Path) -> None:
    catalog = _settings(tmp_path, coupon=False).agent_segment_catalog

    assert segment_link_coupon(catalog, [{"key": LABEL, "version": 1}]) is None


def test_the_agent_is_told_only_when_the_turn_already_decided_coupon_applied() -> None:
    decided: dict[str, object] = {"payment_link_action": {"enabled": True, "coupon_applied": False}}
    undecided: dict[str, object] = {"payment_link_action": {"enabled": True}}
    mark_segment_link_coupon(decided)
    mark_segment_link_coupon(undecided)
    assert decided["payment_link_action"] == {"enabled": True, "coupon_applied": True}
    assert undecided["payment_link_action"] == {"enabled": True}


# ------------------------------------------------------------- el link por la app


def test_with_a_segment_in_force_the_link_carries_its_coupon(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="bridge")

    url = _link(tmp_path, _settings(tmp_path), [LABEL])

    assert url == BASE_LINK + "&offDiscount=" + SEGMENT_COUPON
    assert f"segment_link_coupon key={LABEL} conversation=200" in caplog.text
    # El codigo no va al log.
    assert SEGMENT_COUPON not in "\n".join(
        record.getMessage() for record in caplog.records if record.getMessage().startswith("segment_")
    )


@pytest.mark.parametrize(
    ("labels", "kwargs"),
    [
        (["otra"], {}),  # sin la etiqueta
        ([LABEL], {"enabled": False}),  # el flag apagado: ATT1, Maribel y Johanna hoy
        ([LABEL], {"window": PAST}),  # vencido
        ([LABEL], {"window": FUTURE}),  # por empezar
        ([LABEL], {"coupon": False}),  # vigente sin cupon
    ],
)
def test_without_a_segment_coupon_the_link_is_the_one_of_today(
    tmp_path: Path, labels: list[str], kwargs: dict[str, Any]
) -> None:
    assert _link(tmp_path, _settings(tmp_path, **kwargs), labels) == BASE_LINK


def test_the_segment_coupon_replaces_the_fixed_coupon_of_the_instance(tmp_path: Path) -> None:
    """D1: Hotmart toma un solo offDiscount; el de la campana es el que nombra el agente."""

    settings = _settings(tmp_path, payment_link_coupon_code="PROTOCOLO10+")

    assert _link(tmp_path, settings, [LABEL]) == BASE_LINK + "&offDiscount=" + SEGMENT_COUPON


def test_without_the_label_the_fixed_coupon_of_the_instance_is_as_today(tmp_path: Path) -> None:
    settings = _settings(tmp_path, payment_link_coupon_code="PROTOCOLO10+")

    assert _link(tmp_path, settings, ["otra"]) == BASE_LINK + "&offDiscount=PROTOCOLO10%2B"


def test_the_segment_coupon_does_not_wait_for_a_recovery_touch(tmp_path: Path) -> None:
    """D2: con el flag de Maribel y sin toque de recuperacion, la campana igual va con cupon."""

    settings = _settings(
        tmp_path,
        payment_link_coupon_code="PROTOCOLO10+",
        payment_link_coupon_recovery_only=True,
    )

    assert _link(tmp_path, settings, [LABEL]) == BASE_LINK + "&offDiscount=" + SEGMENT_COUPON


# ------------------------------------------------------ el freno del cupon del carrito


class _CartCoupon(_Answers):
    """La base de la palabra clave (E-317) con las dos RPC del cupon del carrito (E-267)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.by_reply: list[dict[str, object]] = []
        self.lookups: list[dict[str, object]] = []

    async def request_cart_renewal(self, **kwargs: object) -> str:
        return "no_pending_last_touch"

    async def request_cart_renewal_by_reply(self, **kwargs: object) -> str:
        self.by_reply.append(kwargs)
        return "no_pending_last_touch"

    async def get_recovery_coupon_for_conversation(self, **kwargs: object) -> RecoveryCouponForConversation:
        self.lookups.append(kwargs)
        return RecoveryCouponForConversation(
            state="valid",
            code=CODE,
            discount=Decimal("0.100"),
            expires_at=datetime.now(UTC) + timedelta(hours=20),
            ordinal=2,
        )


_CART = {
    "recovery_cart_followups_enabled": True,
    "recovery_cart_last_touch_template_name": "att1_ultima_renovacion_01",
    "recovery_coupon_by_reply_enabled": True,
}


def _cart_answer(tmp_path: Path, settings: Settings, labels: list[str]) -> tuple[_CartCoupon, dict[str, Any]]:
    """La persona contesta el A2 del carrito con texto (la respuesta real 471 de ATT1)."""

    supabase = _CartCoupon(identities=(FORM_ID,))
    webhook, history = _text_webhook(ATT1_471)
    webhook["conversation"]["labels"] = labels
    _, shadow = _process(
        supabase,
        settings=settings,
        webhook=webhook,
        history=[_a2_message(history[0]), *history],
        proposal={"decision": "reply", "reply": "Te leemos."},
    )
    [(_, context)] = shadow.calls
    return supabase, context


def test_with_a_segment_coupon_the_cart_coupon_is_not_offered(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="bridge")

    supabase, context = _cart_answer(tmp_path, _settings(tmp_path, **_CART), [LABEL])

    assert supabase.by_reply == [] and supabase.lookups == []
    assert "recovery_coupon" not in context
    assert context[SEGMENT_CONTEXT_KEY] == [{"key": LABEL, "version": 1}]
    assert f"segment_recovery_coupon_skipped key={LABEL} conversation=200" in caplog.text


@pytest.mark.parametrize(
    ("labels", "kwargs"),
    [(["otra"], {}), ([LABEL], {"enabled": False}), ([LABEL], {"coupon": False})],
)
def test_without_a_segment_coupon_the_cart_coupon_is_as_today(
    tmp_path: Path, labels: list[str], kwargs: dict[str, Any]
) -> None:
    supabase, context = _cart_answer(tmp_path, _settings(tmp_path, **kwargs, **_CART), labels)

    assert len(supabase.by_reply) == 1 and len(supabase.lookups) == 1
    entry = context["recovery_coupon"]
    assert isinstance(entry, dict) and entry["code"] == CODE


def test_the_keyword_of_a_campaign_with_coupon_also_stops_the_cart_coupon(tmp_path: Path) -> None:
    """D9: la etiqueta que pone la palabra clave (E-317) llega antes que el cupon del carrito."""

    settings = replace(_entry_settings(tmp_path), payment_link_enabled=True, **_CART)
    supabase = _CartCoupon(identities=(FORM_ID,))

    shadow, chatwoot, _ = _entry_turn(
        settings, text="Hola! Vengo por la CAMPAÑA DE PRUEBA", supabase=supabase
    )

    assert chatwoot.macro_runs  # la palabra clave puso la etiqueta
    [(_, context)] = shadow.calls
    assert context[SEGMENT_CONTEXT_KEY] == [{"key": LABEL, "version": 1}]
    assert supabase.lookups == []
    assert "recovery_coupon" not in context


# --------------------------------------------- el producto del cupon (revision del #424)


def test_the_coupon_fits_only_the_product_of_its_campaign() -> None:
    """D10: un cupon de Hotmart es de un producto; sin producto, el del principal."""

    of_att2 = SegmentCoupon(LABEL, SEGMENT_COUPON, "att2")
    of_principal = SegmentCoupon(LABEL, SEGMENT_COUPON, None)

    assert segment_coupon_fits_product(of_att2, link_product="att2", principal_product="att1")
    assert not segment_coupon_fits_product(of_att2, link_product="att1", principal_product="att1")
    assert segment_coupon_fits_product(of_principal, link_product="att1", principal_product="att1")
    assert not segment_coupon_fits_product(of_principal, link_product="att2", principal_product="att1")
    # Sin [[productos]] la instancia tiene un solo producto: siempre es ese.
    assert segment_coupon_fits_product(of_att2, link_product=None, principal_product=None)


def _two(tmp_path: Path, **kwargs: Any) -> Settings:
    return _settings(tmp_path, base=_two_products_settings(tmp_path), **kwargs)


@pytest.mark.parametrize(
    ("segment_product", "link_product", "fits"),
    [
        ("att1", "att1", True),
        ("att1", "att2", False),
        ("att2", "att2", True),
        ("att2", "att1", False),
        (None, "att1", True),  # sin producto: el principal
        (None, "att2", False),
    ],
)
def test_with_two_products_the_coupon_goes_only_in_the_link_of_its_product(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    segment_product: str | None,
    link_product: str,
    fits: bool,
) -> None:
    caplog.set_level(logging.INFO, logger="bridge")

    url = _link(
        tmp_path,
        _two(tmp_path, product=segment_product),
        [LABEL],
        proposal={**PAYMENT_LINK_PROPOSAL, "product": link_product},
        supabase=_LinkSupabase(),
    )

    assert url.endswith("&offDiscount=" + SEGMENT_COUPON) is fits
    assert ("offDiscount" in url) is fits
    if not fits:
        assert (
            f"segment_link_coupon_other_product key={LABEL} product={link_product} conversation=200"
            in caplog.text
        )


def test_a_link_of_another_product_keeps_the_fixed_coupon_of_the_instance(tmp_path: Path) -> None:
    url = _link(
        tmp_path,
        _two(tmp_path, product="att1", payment_link_coupon_code="PROTOCOLO10+"),
        [LABEL],
        proposal={**PAYMENT_LINK_PROPOSAL, "product": "att2"},
        supabase=_LinkSupabase(),
    )

    assert url.endswith("&offDiscount=PROTOCOLO10%2B")


# ------------------------------------------- lo que ve el agente (revision del #424)


def _link_context(tmp_path: Path, settings: Settings, labels: list[str]) -> dict[str, Any]:
    shadows: list[_SegmentShadow] = []
    _link(tmp_path, settings, labels, shadow_out=shadows)
    [(_, context)] = shadows[0].calls
    return context


def test_with_the_recovery_flag_the_agent_is_told_the_campaign_link_carries_the_coupon(
    tmp_path: Path,
) -> None:
    """D2: la conversacion la abrio la persona (sin toque de recuperacion), y la campana va igual."""

    settings = _settings(
        tmp_path, payment_link_coupon_code="PROTOCOLO10+", payment_link_coupon_recovery_only=True
    )

    context = _link_context(tmp_path, settings, [LABEL])

    assert context["payment_link_action"]["coupon_applied"] is True


def test_with_the_recovery_flag_and_without_the_label_the_agent_is_told_as_today(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path, payment_link_coupon_code="PROTOCOLO10+", payment_link_coupon_recovery_only=True
    )

    context = _link_context(tmp_path, settings, ["otra"])

    assert context["payment_link_action"]["coupon_applied"] is False


def test_with_two_products_the_agent_is_not_told_before_it_names_the_product(tmp_path: Path) -> None:
    """D11: con varios productos el producto del link lo dice el agente despues."""

    settings = _two(
        tmp_path,
        product="att1",
        payment_link_coupon_code="PROTOCOLO10+",
        payment_link_coupon_recovery_only=True,
    )
    shadows: list[_SegmentShadow] = []
    _link(
        tmp_path,
        settings,
        [LABEL],
        proposal={**PAYMENT_LINK_PROPOSAL, "product": "att1"},
        supabase=_LinkSupabase(),
        shadow_out=shadows,
    )
    [(_, context)] = shadows[0].calls

    assert context["payment_link_action"]["coupon_applied"] is False


# ------------------------------------------------------------ D3 y D4 por la app


def test_with_two_coupon_segments_the_first_by_priority_wins(tmp_path: Path) -> None:
    settings = _settings(tmp_path, second=("campana-dos", "DOS22", 5))

    url = _link(tmp_path, settings, [LABEL, "campana-dos"])

    assert url == BASE_LINK + "&offDiscount=DOS22"


def test_a_coupon_already_in_the_link_of_the_base_still_wins(tmp_path: Path) -> None:
    personal = BASE_LINK + "&offDiscount=ATT1K7Q2M9XP"
    supabase = _PaymentLinkSupabase(identities=(FORM_ID,))
    supabase.reservation_url_override = personal

    assert _link(tmp_path, _settings(tmp_path), [LABEL], supabase=supabase) == personal
