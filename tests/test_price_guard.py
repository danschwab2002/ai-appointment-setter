"""La guarda de precio: el extractor, los permitidos y la comparacion (E-564, MP-04).

Contrato: docs/contracts/price-guard-v1.md. Datos reales:

- ``fixtures/price_guard/agent_replies_*_20261113.json`` (F2 de E-555): todas las
  respuestas del agente de ATT1 (bandeja 11) y Maribel (bandeja 13) con algun
  numero, capturadas el 13/11 en solo lectura y anonimizadas. Estaban bien: la
  guarda no puede bloquear ninguna.
- ``fixtures/price_guard/agent_campaigns_20261113.json``: la campana Black de ATT1
  (r3) tal como esta en la base.
- El conocimiento y el manifiesto de cada instancia de ``fixtures/instances/``
  (ATT1 v14 en b7580bc, Maribel v9 en fbad25e), el texto que recibe el agente.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from bridge.agent_campaigns import CampaignContext, load_campaign_segment, parse_campaign
from bridge.commercial_knowledge import CommercialKnowledge
from bridge.instance_manifest import InstanceManifest, ManifestError
from bridge.price_guard import (
    Amount,
    PriceGuard,
    PriceGuardConfigurationError,
    allowed_text,
    correction_detail,
    extract_amounts,
    guard_from_manifest,
    parse_mode,
)
from bridge.segment_knowledge import SegmentCatalog, TurnSegment

FIXTURES = Path(__file__).parent / "fixtures"
PRICE_GUARD = FIXTURES / "price_guard"
ATT1 = FIXTURES / "instances" / "att1-b7580bc-20261103"
MARIBEL = FIXTURES / "instances" / "maribel-fbad25e-20261113"
INSTANCES = {
    "att1": (ATT1, "knowledge-v14.toml"),
    "maribel": (MARIBEL, "knowledge-v9.toml"),
}


def _labels(amounts: object) -> list[str]:
    return sorted(amount.label() for amount in amounts)  # type: ignore[attr-defined]


def _cases(instance: str) -> list[dict[str, object]]:
    data = json.loads(
        (PRICE_GUARD / f"agent_replies_{instance}_20261113.json").read_text(encoding="utf-8")
    )
    assert data["instancia"] == instance
    return data["casos"]


def _guard_and_allowed(instance: str) -> tuple[PriceGuard, frozenset[Amount]]:
    root, knowledge = INSTANCES[instance]
    manifest = InstanceManifest.from_toml_file(root / "instancia.toml")
    rendered = CommercialKnowledge.from_toml_file(root / "conocimiento" / knowledge).render()
    guard = guard_from_manifest(manifest)
    return guard, guard.allowed_for_turn(texts=(rendered,))


# ------------------------------------------------------------- el extractor (§2)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Las formas reales de F2.
        ("Alimenta Tu Tiroides cuesta USD 47.", ["USD 47"]),
        ("el enlace de Alimenta Tu Tiroides (USD 47) para que retomes", ["USD 47"]),
        ("cuesta MXN 1.397 con IVA incluido", ["MXN 1.397"]),
        ("cuesta MXN 1,397 con IVA incluido", ["MXN 1.397"]),
        ("puede mostrar MXN 1.204,31 más impuestos", ["MXN 1.204,31"]),
        # Las del contrato.
        ("199 USD", ["USD 199"]),
        ("USD 199.00", ["USD 199"]),
        ("$1,397", ["$ 1.397"]),
        ("US$ 47", ["USD 47"]),
        ("€ 50", ["EUR 50"]),
        ("1.397.000 COP", ["COP 1.397.000"]),
        ("199 dólares", ["USD 199"]),
        ("1397 pesos", ["$ 1.397"]),
        ("1397 pesos mexicanos", ["MXN 1.397"]),
        ("1.397.000 pesos colombianos", ["COP 1.397.000"]),
        ("1397.50 MXN", ["MXN 1.397,5"]),
        ("12 cuotas de $166", ["$ 166"]),
        ("12 de 166 en cuotas", ["$ 166"]),
        ("12x 166", ["$ 166"]),
        ("12 pagos de 166 USD", ["USD 166"]),
        # Las formas de Mexico con el codigo pegado al signo, y el codigo despues
        # de un «$» (revision del PR: se escapaban).
        ("USD$97", ["USD 97"]),
        ("MXN$1,397", ["MXN 1.397"]),
        ("COL$ 100.000", ["COP 100.000"]),
        ("U$S 97", ["USD 97"]),
        ("1397 $MXN", ["MXN 1.397"]),
        ("97 dls", ["USD 97"]),
        ("1397$", ["$ 1.397"]),
        ("Son $1,397 USD", ["USD 1.397"]),
    ],
)
def test_the_extractor_reads_amounts_with_their_currency(text: str, expected: list[str]) -> None:
    assert _labels(extract_amounts(text)) == expected


@pytest.mark.parametrize(
    "text",
    [
        # Lo que no es un monto (§2), con las variantes reales de F2.
        "un 10% de descuento",
        "un 10 % sobre el precio",
        "https://pay.hotmart.com/<PRODUCTO>?off=u8ikyx78&checkoutMode=10&src=hermes",
        "con el código TIROIDES10 o PROTOCOLO10+",
        "a 3, 6, 9 o 12 meses",
        "son 8 módulos y la garantía es de 7 días",
        "pensado para mujeres desde los 40",
        "vale hasta el 15 de noviembre",
        "la prueba interna número 0911",
        "promo 2x1",
        "a las 10:30",
        # Unidades que no son plata, y el total de cuotas (revision del PR).
        "sesiones de 1x 60 minutos",
        "4x 30 días de acceso",
        "3 x 15 minutos",
        "12 pagos de 30 días",
        "los primeros 3 meses de 2026",
        "pagaste 1 de 12 cuotas",
        "Llevas 5 de 12 mensualidades",
        "el módulo 4 de 12 cuotas",
        # Un numero que no se puede leer entero no se parte: «$1397.505» no es $1397.
        "$1397.505",
    ],
)
def test_numbers_without_currency_are_not_amounts(text: str) -> None:
    assert extract_amounts(text) == frozenset()


# --------------------------------------------- F2: las respuestas que estaban bien


@pytest.mark.parametrize("instance", ["att1", "maribel"])
def test_the_guard_blocks_none_of_the_real_replies(instance: str) -> None:
    guard, allowed = _guard_and_allowed(instance)
    blocked = {
        case["caso"]: _labels(guard.blocked(case["respuesta"], allowed))
        for case in _cases(instance)
        if guard.blocked(case["respuesta"], allowed)
    }
    assert blocked == {}


@pytest.mark.parametrize(
    ("instance", "with_amount", "allowed"),
    [
        ("att1", 59, ["USD 47"]),
        ("maribel", 7, ["MXN 1.204,31", "MXN 1.397"]),
    ],
)
def test_the_extractor_finds_the_same_amounts_as_the_capture(
    instance: str, with_amount: int, allowed: list[str]
) -> None:
    """Las de `con_monto` tienen monto; las de `numeros_sin_moneda`, ninguno."""
    cases = _cases(instance)
    found = {
        case["caso"]: _labels(extract_amounts(case["respuesta"]))  # type: ignore[arg-type]
        for case in cases
    }
    for case in cases:
        if case["clase"] == "con_monto":
            assert found[case["caso"]] == [
                label.replace(",", ".") for label in case["montos_detectados_en_la_captura"]  # type: ignore[union-attr]
            ], case["caso"]
        else:
            assert found[case["caso"]] == [], case["caso"]
    assert sum(1 for labels in found.values() if labels) == with_amount
    assert _labels(_guard_and_allowed(instance)[1]) == allowed


def test_an_invented_amount_in_a_real_reply_is_blocked() -> None:
    # att1-005 de F2, con el precio cambiado (la mutacion es del test).
    guard, allowed = _guard_and_allowed("att1")
    [case] = [case for case in _cases("att1") if case["caso"] == "att1-005"]
    reply = str(case["respuesta"])
    assert "USD 47" in reply

    assert guard.blocked(reply, allowed) == ()
    assert _labels(guard.blocked(reply.replace("USD 47", "USD 97"), allowed)) == ["USD 97"]
    # Otra moneda con el mismo numero tampoco pasa: el precio es en dolares.
    assert _labels(guard.blocked(reply.replace("USD 47", "MXN 47"), allowed)) == ["MXN 47"]
    # Sin codigo, el numero permitido pasa en cualquier moneda.
    assert guard.blocked(reply.replace("USD 47", "$47"), allowed) == ()


def test_an_allowed_dollar_sign_counts_for_any_currency() -> None:
    allowed = PriceGuard().allowed_for_turn(texts=("Cuesta $1,397.",))
    assert PriceGuard.blocked("Son MXN 1.397.", allowed) == ()
    assert _labels(PriceGuard.blocked("Son MXN 1.398.", allowed)) == ["MXN 1.398"]


# --------------------------------------------------- de donde salen los permitidos


def _black_campaign_block() -> TurnSegment:
    manifest = InstanceManifest.from_toml_file(ATT1 / "instancia.toml")
    base = CommercialKnowledge.from_toml_file(ATT1 / "conocimiento" / "knowledge-v14.toml")
    context = CampaignContext(
        manifest=manifest,
        base=base,
        manifest_catalog=SegmentCatalog((), len(base.render().encode("utf-8"))),
        offer_link_enabled=True,
    )
    row = json.loads(
        (PRICE_GUARD / "agent_campaigns_20261113.json").read_text(encoding="utf-8")
    )["att1"][0]
    assert row["campaign_key"] == "black-november-2026"
    draft = dict(row["draft"])
    # El anonimizador tapo desde/hasta; las fechas reales son las de la fila.
    draft["desde"] = datetime.fromisoformat(row["valid_from"]).isoformat()
    draft["hasta"] = datetime.fromisoformat(row["valid_until"]).isoformat()
    segment = load_campaign_segment(
        parse_campaign(draft), version=row["segment_version"], context=context
    )
    return TurnSegment(segment=segment, agent_replies_before=0)


def test_the_campaign_offer_price_is_allowed_while_it_is_in_the_turn() -> None:
    block = _black_campaign_block()
    allowed = PriceGuard().allowed_for_turn(texts=(), blocks=(block,))

    assert _labels(allowed) == ["USD 47"]
    assert PriceGuard.blocked("El Black cuesta USD 47.", allowed) == ()
    # Sin la campana en el turno, el mismo texto no tiene de donde salir.
    assert _labels(PriceGuard.blocked("El Black cuesta USD 47.", frozenset())) == ["USD 47"]


def test_the_forbidden_promises_do_not_allow_their_amounts() -> None:
    """D2: la campana Black dice «Citar el precio tachado de USD 97» como prohibido."""
    block = _black_campaign_block()
    assert "precio tachado de USD 97" in block.rendered
    allowed = PriceGuard().allowed_for_turn(texts=(), blocks=(block,))

    assert _labels(PriceGuard.blocked("Antes costaba USD 97.", allowed)) == ["USD 97"]


def test_allowed_text_drops_only_the_forbidden_and_unconfirmed_sections() -> None:
    text = (
        "## Oferta\n\n- Precio: USD 47.\n\n## No confirmado\n\n- USD 10\n\n"
        "## Promesas prohibidas\n\n- USD 97\n\n## Preguntas frecuentes\n\n- R: USD 47"
    )
    kept = allowed_text(text)
    assert "USD 97" not in kept and "USD 10" not in kept
    assert kept.count("USD 47") == 2


def test_the_context_counts_but_not_the_messages_nor_the_known_fields() -> None:
    context = {
        "conversation_ref": "61",
        "messages": [{"actor": "prospect", "text": "me dijeron que eran USD 30"}],
        "known_fields": {"business_model": "vendo a USD 20"},
        "products": [{"key": "forever-young", "name": "Combo USD 199"}],
    }
    allowed = PriceGuard().allowed_for_turn(texts=(), context=context)
    assert _labels(allowed) == ["USD 199"]


def test_the_structured_prices_of_the_manifest_are_allowed() -> None:
    manifest = InstanceManifest.from_toml_file(MARIBEL / "instancia.toml")
    assert _labels(guard_from_manifest(manifest).structured) == ["MXN 1.397"]


# ---------------------------------------------- [guarda_precio] montos_extra (§8)


def _maribel_with(tmp_path: Path, extra: str) -> Path:
    path = tmp_path / "instancia.toml"
    path.write_text(
        (MARIBEL / "instancia.toml").read_text(encoding="utf-8") + extra, encoding="utf-8"
    )
    return path


def test_extra_amounts_from_the_manifest_are_allowed(tmp_path: Path) -> None:
    path = _maribel_with(
        tmp_path,
        '\n[guarda_precio]\nmontos_extra = [\n'
        '  { monto = "79", moneda = "USD", que_es = "precio de referencia en dolares" },\n]\n',
    )
    manifest = InstanceManifest.from_toml_file(path)

    [extra] = manifest.price_guard_extra_amounts
    assert (extra.value, extra.currency, extra.description) == (
        Decimal("79"), "USD", "precio de referencia en dolares"
    )
    assert _labels(guard_from_manifest(manifest).structured) == ["MXN 1.397", "USD 79"]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ('{ monto = "0", moneda = "USD", que_es = "x" }', "debe ser positivo"),
        ('{ monto = "79", moneda = "usd", que_es = "x" }', "ISO 4217"),
        ('{ monto = "79", moneda = "USD", que_es = "" }', "texto no vacio"),
        ('{ monto = "79", moneda = "USD" }', "faltan que_es"),
    ],
)
def test_a_bad_extra_amount_does_not_load(tmp_path: Path, extra: str, message: str) -> None:
    path = _maribel_with(tmp_path, f"\n[guarda_precio]\nmontos_extra = [{extra}]\n")
    with pytest.raises(ManifestError, match=message):
        InstanceManifest.from_toml_file(path)


def test_without_the_table_there_are_no_extra_amounts() -> None:
    manifest = InstanceManifest.from_toml_file(MARIBEL / "instancia.toml")
    assert manifest.price_guard_extra_amounts == ()


# ----------------------------------------------------------- el modo y la correccion


@pytest.mark.parametrize(("raw", "mode"), [(None, "off"), ("", "off"), (" OFF ", "off"), ("bloquea", "bloquea")])
def test_the_mode(raw: str | None, mode: str) -> None:
    assert parse_mode(raw) == mode


@pytest.mark.parametrize("raw", ["registra", "true", "on"])
def test_any_other_mode_does_not_start(raw: str) -> None:
    with pytest.raises(PriceGuardConfigurationError, match="price_guard_configuration_invalid"):
        parse_mode(raw)


def test_the_correction_names_only_catalog_amounts() -> None:
    allowed = frozenset({Amount("MXN", Decimal("1397")), Amount("MXN", Decimal("1204.31"))})
    detail = correction_detail((Amount("MXN", Decimal("1257.3")),), allowed)
    assert detail == (
        "el monto MXN 1.257,3 no esta en lo que te pasamos; "
        "usa solo estos: MXN 1.204,31, MXN 1.397."
    )
    assert correction_detail((Amount("USD", Decimal("97")),), frozenset()) == (
        "el monto USD 97 no esta en lo que te pasamos; no escribas ningun monto."
    )


def test_a_code_after_the_dollar_sign_keeps_its_currency() -> None:
    """«$1,397 USD» con el precio en MXN: es otra moneda y no pasa (§3)."""
    allowed = frozenset({Amount("MXN", Decimal("1397"))})
    assert _labels(PriceGuard.blocked("Son $1,397 USD", allowed)) == ["USD 1.397"]
    assert PriceGuard.blocked("Son $1,397", allowed) == ()
