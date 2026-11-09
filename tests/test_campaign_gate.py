"""Que frena una campana (criterio frenos-v1, E-364a).

Las salidas del juez son las REALES de moonshotai/kimi-k2.6 del 2026-11-05 sobre la campana
sintetica prueba-bn-sintetico (tests/fixtures/segment_judge_kimi_20261105/, sin tocar). El base es
el v14 de ATT1 del fixture. Las reglas se prueban ademas con citas escritas a mano sobre el mismo
segmento: una cita real de cada caso no existe todavia, la captura real viene con --grabar.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bridge import segment_check as sc
from bridge.campaign_approval import _check_block
from bridge.campaign_gate import CRITERIO, razon_de_freno
from bridge.commercial_knowledge import CommercialKnowledge

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
SEGMENT = FIXTURES / "segments" / "prueba-bn-sintetico-v1.toml"
KIMI = FIXTURES / "segment_judge_kimi_20261105"
SOUL = (ROOT / sc.SOUL_RELATIVE_PATH).read_text(encoding="utf-8")
NOW = datetime(2026, 11, 5, 14, 46, tzinfo=timezone.utc)
FORBIDDEN_IN_BASE = "Que los pagos a meses son sin intereses."
MEDIOS_ITEM = "En México, con tarjeta de crédito: hasta 4 meses sin intereses, solo en esta promo."


@pytest.fixture(scope="module")
def base() -> CommercialKnowledge:
    return CommercialKnowledge.from_toml_file(
        FIXTURES / "instances" / "att1-b7580bc-20261103" / "conocimiento" / "knowledge-v14.toml"
    )


@pytest.fixture(scope="module")
def text() -> str:
    return SEGMENT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ctx(text: str, base: CommercialKnowledge) -> sc.GateContext:
    return sc.GateContext(sc.read_segment_file(SEGMENT), text, base)


def _item(*citations: tuple[str, str], kind: str = "C") -> sc.Item:
    return sc.Item(kind, "k", "Titulo", [sc.Citation(source, quote) for source, quote in citations], "¿Pregunta?", "campo", [1])


def _real_items(text: str, base: CommercialKnowledge) -> list[sc.Item]:
    segment = sc.parse_segment(__import__("tomllib").loads(text))
    sources = sc.build_sources(segment_text=text, segment=segment, base=base, soul_text=SOUL, flows=[], templates={}, cases=[], now=NOW)
    items: list[sc.Item] = []
    for path in sorted(KIMI.glob("*.json")):
        items += sc.parse_judge_output(json.loads(path.read_text(encoding="utf-8"))["content"], sources, 1)
    return items


def test_the_seven_real_observations_give_one_stop_and_six_notices(text, base, ctx) -> None:
    items = _real_items(text, base)
    assert len(items) == 7 and all(item.verified for item in items)

    reasons = {item.key: razon_de_freno(item, ctx) for item in items}

    assert reasons == {
        "cupon-falta-en-reemplaza": None,
        "meses-sin-intereses-vs-promesa-prohibida": "promesa",
        "cupon-reemplaza-faltante": None,
        "cupon-sin-reemplaza": None,
        "tratamiento-usted-en-instruccion": None,
        "cupon-fuera-de-reemplaza": None,
        "meses-sin-intereses-excepcion": None,
    }


def _answers(name: str) -> str:
    return json.loads((KIMI / name).read_text(encoding="utf-8"))["content"]


def _run(text: str, base: CommercialKnowledge, answers: list[str], *, gate=razon_de_freno) -> sc.Report:
    judge = sc.recorded_judge(answers, model="grabado")
    return sc.chequear(text, base=base, soul_texto=SOUL, juez=judge, flujos=[], plantillas={}, casos=[], corridas=len(answers), ahora=NOW, compuerta=gate)


def test_with_the_gate_the_real_session_without_a_stop_is_clean_and_keeps_its_notices(text, base) -> None:
    report = _run(text, base, [_answers(f"144638-respuesta-{n}.json") for n in (1, 2, 3)])

    assert report.items == [] and report.clean is True and report.complete is True
    # Dos contradicciones de cupon comparten cita y se juntan: las 5 observaciones son 4 items.
    assert len(report.notices) == 4 and all(item.stop_reason is None for item in report.notices)
    assert report.chequeo_sha == sc.calcular_chequeo_sha(sc.read_segment_file(SEGMENT).raw, base_sha256=base.rendered_sha256)


def test_with_the_gate_the_real_contradiction_stops_the_campaign(text, base) -> None:
    # La respuesta del equipo a esa pregunta cierra el item en gate_reopened: la saco para ver el freno.
    no_answer = text[: text.rindex("[[preguntas_del_alta]]")]
    report = _run(no_answer, base, [_answers(f"143749-respuesta-{n}.json") for n in (1, 2, 3)])

    assert [item.stop_reason for item in report.items] == ["promesa"]
    assert report.clean is False and report.chequeo_sha is None
    assert report.notices == []  # el del cupon lo cierra gate_reopened: esa respuesta sigue


def test_without_a_gate_every_item_still_stops(text, base) -> None:
    report = _run(text, base, [_answers(f"144638-respuesta-{n}.json") for n in (1, 2, 3)], gate=None)

    assert len(report.items) == 4 and report.notices == [] and report.clean is False
    assert report.chequeo_sha is None and "notices" not in report.to_json() and "stop_reason" not in report.items[0].to_json()


@pytest.mark.parametrize(
    "citations",
    [
        (("segmento", 'precio = "31"'), ("segmento", "cuesta USD 35")),
        (("segmento", 'precio = "31"'), ("plantilla:promo", "cuesta USD 35")),  # la moneda sale de [oferta]
        (("base", "USD 47"), ("segmento:otra", "USD 30")),
        (("segmento", "10 %"), ("plantilla:promo", "15%")),
    ],
)
def test_two_different_prices_stop(ctx, citations) -> None:
    assert razon_de_freno(_item(*citations), ctx) == "precio"


@pytest.mark.parametrize(
    "citations",
    [
        (("segmento", "USD 31"), ("plantilla:promo", "31 USD")),  # la misma cifra escrita de otra manera
        (("segmento", 'precio = "31"'), ("plantilla:promo", "$31")),
        (("segmento", 'precio_antes = "USD 47"'), ("plantilla:promo", "USD 31")),  # el de antes no es otro precio
        (("segmento", "USD 31"), ("plantilla:promo", "$ 35")),  # $ sin moneda: en Mexico son pesos, no se compara con USD
        (("segmento", "cuesta USD 31 (antes USD 47)"), ("plantilla:promo", "USD 31")),
        (("segmento", "cuesta USD 31"), ("plantilla:promo", "MXN 577")),  # otra moneda: no se comparan
        (("base", "USD 47"), ("segmento", 'precio = "31"')),  # precio esta en reemplaza: es a proposito
        (("segmento", "USD 31"), ("soul", "USD 50")),  # el SOUL no fija un precio
    ],
)
def test_the_same_price_or_a_deliberate_one_does_not_stop(ctx, citations) -> None:
    assert razon_de_freno(_item(*citations), ctx) is None


@pytest.mark.parametrize(
    "citations",
    [
        (("segmento", "usa el cupón OTRO2026 en la página"),),
        (("plantilla:promo", "escribe PROTOCOLO10 en la página"),),
        (("segmento", "el cupón se suma con el del carrito"),),
    ],
)
def test_another_coupon_or_adding_it_up_stops(ctx, citations) -> None:
    assert razon_de_freno(_item(*citations), ctx) == "cupon"


def test_the_plus_is_part_of_the_coupon(text, base) -> None:
    own_plus = text.replace('cupon = "PRUEBABN31"', 'cupon = "PROTOCOLO10"')
    context = sc.GateContext(sc.parse_segment(__import__("tomllib").loads(own_plus)), own_plus, base)
    assert razon_de_freno(_item(("segmento", "escribe PROTOCOLO10+ en la página")), context) == "cupon"
    assert razon_de_freno(_item(("segmento", "escribe PROTOCOLO10 en la página")), context) is None


@pytest.mark.parametrize(
    "citations",
    [
        (("segmento", 'cupon = "PRUEBABN31"'), ("base", "el código TIROIDES10 en el campo del cupón")),  # que el base nombre otro no alcanza
        (("segmento", "No ofrezcas TIROIDES10 ni otro cupón: no se suman con este."),),
        (("segmento", "Sí: en la promo el cupón es PRUEBABN31 y reemplaza a TIROIDES10 (no se suman; Hotmart acepta uno solo)."),),  # alta
        (("segmento", "Que el cupón PRUEBABN31 se suma con TIROIDES10 o con otro cupón."),),  # una promesa prohibida del segmento
        (("segmento", "desde 2026-11-05T15:00:00+00:00"),),  # una fecha ISO no es un codigo
    ],
)
def test_naming_the_other_coupon_without_offering_it_does_not_stop(ctx, citations) -> None:
    assert razon_de_freno(_item(*citations), ctx) is None


@pytest.mark.parametrize(
    "citations",
    [
        (("segmento", "La garantía en esta promo es de 15 días, por Hotmart."), ("plantilla:promo", "tienes garantía de 30 días")),
        (("segmento", "La garantía en esta promo es de 15 días, por Hotmart."), ("plantilla:promo", "este producto no tiene garantía")),
    ],
)
def test_another_guarantee_or_denying_it_stops(ctx, citations) -> None:
    assert razon_de_freno(_item(*citations), ctx) == "garantia"


@pytest.mark.parametrize(
    "citations",
    [
        (("segmento", "tienes garantía de 1 mes"), ("plantilla:promo", "con reembolso a los 30 días")),  # el mismo plazo
        (("base", "Garantía de 7 días"), ("segmento", "La garantía en esta promo es de 15 días, por Hotmart.")),  # garantia en reemplaza
        (("segmento", "La garantía en esta promo es de 15 días, por Hotmart."),),  # una sola cita
    ],
)
def test_the_same_guarantee_does_not_stop(ctx, citations) -> None:
    assert razon_de_freno(_item(*citations), ctx) is None


@pytest.mark.parametrize(
    "citations",
    [
        (("base", FORBIDDEN_IN_BASE), ("segmento", MEDIOS_ITEM)),
        (("base", "pagos a meses son sin intereses"), ("plantilla:promo", "tienes 4 meses sin intereses")),  # un pedazo de la promesa
        (("segmento", "si la promo se extiende después del 07/11 a las 21:00, hora de México"), ("plantilla:promo", "seguro la extendemos")),
        (("soul", "Nunca prometas lo que no podés cumplir."), ("segmento", MEDIOS_ITEM)),
    ],
)
def test_breaking_a_written_promise_stops(ctx, citations) -> None:
    assert razon_de_freno(_item(*citations), ctx) == "promesa"


@pytest.mark.parametrize(
    "citations",
    [
        (("base", "Tratamiento: tú, respetuoso."), ("segmento", MEDIOS_ITEM)),  # el base dice otra cosa, pero no es promesa ni valor
        (("segmento", "Que el cupón PRUEBABN31 se suma con TIROIDES10 o con otro cupón."), ("segmento", "si la promo se extiende después del 07/11 a las 21:00, hora de México")),  # dos listas
        (("soul", "Una pregunta por respuesta."), ("segmento", MEDIOS_ITEM)),  # el SOUL no habla de prometer
        (("base", FORBIDDEN_IN_BASE), ("segmento", "Sí, a propósito: el bloque de la campaña manda sobre el base en los medios de pago")),  # solo el alta: el agente no la recibe
    ],
)
def test_it_does_not_stop_without_a_written_promise_and_a_text_that_breaks_it(ctx, citations) -> None:
    assert razon_de_freno(_item(*citations), ctx) is None


@pytest.mark.parametrize("kind", ["H", "D"])
def test_a_gap_or_a_doubt_never_stops(ctx, kind) -> None:
    assert razon_de_freno(_item(("base", FORBIDDEN_IN_BASE), ("segmento", MEDIOS_ITEM), kind=kind), ctx) is None


def test_a_contradiction_without_a_real_quote_never_stops(ctx) -> None:
    item = _item()
    item.discarded = [sc.Citation("segmento", "precio = \"99\"")]
    assert razon_de_freno(item, ctx) is None


def test_the_rules_version_is_untouched() -> None:
    # Cambiarla cambia todos los chequeo_sha y deja de servir las publicadas (agent_campaigns._load_row).
    assert sc.CHECK_RULES_VERSION == "segment-check-juez-v2"


# ------------------------------------------------------------------- la pagina

OLD_LIMPIO = "<p>Pasó el control de calidad: no choca con lo que el agente ya sabe.</p>"
OLD_ABIERTOS = ('<div class="notice notice--warn"><p>El control de calidad dejó preguntas. Contestalas con el agente: '
                "la vuelve a armar y se corre otra vez.</p><ul><li>¿Sirve en Colombia?</li></ul></div>")
OLD_SIN_VEREDICTO = '<div class="notice notice--warn"><p>El control de calidad no llegó a un resultado. Hay que volver a correrlo.</p></div>'


@pytest.mark.parametrize(
    ("report", "expected", "button"),
    [
        ({"veredicto": "limpio", "abiertos": 0, "items": []}, OLD_LIMPIO, True),
        ({"veredicto": "abiertos", "abiertos": 1, "items": [{"pregunta": "¿Sirve en Colombia?"}]}, OLD_ABIERTOS, False),
        ({"veredicto": "sin_veredicto", "abiertos": 0, "items": []}, OLD_SIN_VEREDICTO, False),
    ],
)
def test_an_old_row_without_criterio_renders_byte_for_byte_the_same(report, expected, button) -> None:
    # Copia literal del HTML de v1.33.0 (se corrio tambien contra ese codigo: ver el PR).
    assert _check_block({"check_report": report}, status="validada", check_stale=False) == (expected, button)


def _new(verdict: str, **more) -> dict:
    return {"veredicto": verdict, "abiertos": 0, "items": [], "criterio": CRITERIO, **more}


def test_clean_with_notices_lists_them_and_lets_publish() -> None:
    report = _new("limpio", avisos=[{"numero": "D1", "tipo": "D", "pregunta": "¿Tuteo? <img src=x>", "campo": "instruccion"}], avisos_total=1)
    html, publish = _check_block({"check_report": report}, status="validada", check_stale=False)
    assert publish is True
    assert "Pasó el control de calidad" in html and "Dejó 1 aviso que no frena" in html
    assert "¿Tuteo?" in html and "<img" not in html and "&lt;img" in html


def test_clean_without_notices_has_no_notice_box() -> None:
    html, publish = _check_block({"check_report": _new("limpio", avisos=[], avisos_total=0)}, status="validada", check_stale=False)
    assert publish is True and "aviso" not in html.lower()


def test_open_with_criterio_says_what_stops_and_lists_the_notices() -> None:
    report = _new("abiertos", abiertos=1, items=[{"pregunta": "Otro precio: ¿cuál va?", "razon": "precio"}],
                  avisos=[{"pregunta": "Falta decir el bono"}, {"pregunta": "Otra duda"}], avisos_total=2)
    html, publish = _check_block({"check_report": report}, status="validada", check_stale=False)
    assert publish is False
    assert "frena la publicación" in html and "Otro precio: ¿cuál va?" in html
    assert "Dejó 2 avisos que no frenan" in html and "Falta decir el bono" in html
    assert "dejó preguntas" not in html


def test_the_total_of_notices_is_the_real_one_when_the_list_is_cut() -> None:
    html, _ = _check_block({"check_report": _new("limpio", avisos=[{"pregunta": "x"}] * 40, avisos_total=55)}, status="validada", check_stale=False)
    assert "Dejó 55 avisos" in html


def test_stale_check_still_blocks_with_criterio() -> None:
    _, publish = _check_block({"check_report": _new("limpio", avisos=[])}, status="validada", check_stale=True)
    assert publish is False


# ------------------------------------------------------------- la revision adversarial del 2026-11-09

GUARANTEE_15 = "La garantía en esta promo es de 15 días, por Hotmart."


def _ctx_of(text: str, base: CommercialKnowledge) -> sc.GateContext:
    return sc.GateContext(sc.parse_segment(__import__("tomllib").loads(text)), text, base)


def _with_answer(text: str, answer: str, question: str = "¿Cuál es el valor final?") -> str:
    return text + f'\n[[preguntas_del_alta]]\npregunta = "{question}"\nrespuesta = "{answer}"\nfuente = "prueba"\n'


def _with_list_entry(text: str, table: str, entry: str) -> str:
    return text.replace(f"[{table}]\nitems = [\n", f'[{table}]\nitems = [\n  "{entry}",\n', 1) if f"[{table}]\nitems = [\n" in text \
        else text.replace(f"[{table}]\nitems = [", f'[{table}]\nitems = ["{entry}", ', 1)


@pytest.mark.parametrize(
    ("answer", "other", "reason"),
    [
        ("El precio final confirmado es USD 29", ("segmento", 'precio = "31"'), "precio"),
        ("El precio final confirmado es USD 29", ("plantilla:promo", "cuesta USD 31"), "precio"),
        ("La garantía confirmada es de 30 días", ("segmento", GUARANTEE_15), "garantia"),
    ],
)
def test_what_the_owner_confirmed_counts_against_the_campaign(text, base, answer, other, reason) -> None:
    context = _ctx_of(_with_answer(text, answer), base)
    assert razon_de_freno(_item(("segmento", answer), other), context) == reason


def test_what_the_owner_confirmed_against_the_base_or_only_the_question_does_not_stop(text, base) -> None:
    context = _ctx_of(_with_answer(text, "El precio final confirmado es USD 29", question="¿Es USD 40 o USD 29?"), base)
    assert razon_de_freno(_item(("segmento", "El precio final confirmado es USD 29"), ("base", "USD 47")), context) is None
    assert razon_de_freno(_item(("segmento", "¿Es USD 40 o USD 29?"), ("segmento", 'precio = "31"')), context) is None


def test_an_answer_about_the_price_of_always_is_not_another_price(ctx) -> None:
    quote = "el precio es el de siempre, USD 47"
    assert razon_de_freno(_item(("segmento", quote), ("segmento", 'precio = "31"')), ctx) is None


def test_the_coupon_rule_still_ignores_the_answers(text, base) -> None:
    answer = "Usa el cupón OTRO2026"
    assert razon_de_freno(_item(("segmento", answer)), _ctx_of(_with_answer(text, answer), base)) is None


@pytest.mark.parametrize(
    "quote",
    [
        "Solo antes del viernes: USD 35",
        "antes de medianoche USD 35",
        "antes del 20/11 cuesta USD 35",
    ],
)
def test_before_only_marks_the_amount_when_it_is_glued_to_it(ctx, quote) -> None:
    assert razon_de_freno(_item(("segmento", "USD 31"), ("plantilla:promo", quote)), ctx) == "precio"


@pytest.mark.parametrize(
    "quote",
    [
        "antes USD 47",
        "antes: USD 47",
        "(antes USD 47)",
        "antes costaba USD 47",
        "precio normal de USD 47",
        "en vez de USD 47",
        "en lugar de USD 47",
        "en vez de pagar USD 47",
    ],
)
def test_an_amount_marked_as_the_old_one_is_not_another_price(ctx, quote) -> None:
    assert razon_de_freno(_item(("segmento", "USD 31"), ("plantilla:promo", quote)), ctx) is None


def test_before_also_marks_a_bare_dollar_amount(ctx) -> None:
    assert razon_de_freno(_item(("segmento", "$ 31"), ("plantilla:promo", "antes $47")), ctx) is None


@pytest.mark.parametrize(
    "quote",
    [
        "Usa el cupón OTRO2026. No se combina con otras promociones.",
        "Si no lo tienes, usa OTRO2026",
    ],
)
def test_a_negation_elsewhere_does_not_cancel_the_other_coupon(ctx, quote) -> None:
    assert razon_de_freno(_item(("segmento", quote)), ctx) == "cupon"


@pytest.mark.parametrize(
    "quote",
    [
        "No uses el cupón OTRO2026",
        "el cupón OTRO2026 no aplica en esta promo",
        "no se combina con el cupón OTRO2026",
        "este cupón reemplaza al cupón OTRO2026",
    ],
)
def test_a_negation_that_touches_the_code_does_not_offer_it(ctx, quote) -> None:
    assert razon_de_freno(_item(("segmento", quote)), ctx) is None


@pytest.mark.parametrize("quote", ["la garantía no aplica", "la garantía no está disponible", "el reembolso ya no aplica en esta promo"])
def test_a_guarantee_denied_after_the_word_stops(ctx, quote) -> None:
    assert razon_de_freno(_item(("segmento", GUARANTEE_15), ("plantilla:promo", quote)), ctx) == "garantia"


@pytest.mark.parametrize(
    "quote",
    ["la garantía no aplica a compras anteriores", "Si no quedas conforme, tienes garantía", "Si no te gusta, garantía por Hotmart"],
)
def test_a_limit_or_a_loose_no_is_not_denying_the_guarantee(ctx, quote) -> None:
    assert razon_de_freno(_item(("segmento", GUARANTEE_15), ("plantilla:promo", quote)), ctx) is None


def test_a_short_promise_counts(text, base) -> None:
    extended = _with_list_entry(text, "promesas_prohibidas", "Cura la tiroides")
    extended = _with_list_entry(extended, "se_puede_decir", "este programa cura la tiroides")
    context = _ctx_of(extended, base)
    assert razon_de_freno(_item(("segmento", "Cura la tiroides"), ("segmento", "este programa cura la tiroides")), context) == "promesa"


def test_a_short_entry_counts_when_it_is_the_whole_entry(text, base) -> None:
    context = _ctx_of(_with_list_entry(_with_list_entry(text, "no_confirmado", "Gratis"), "se_puede_decir", "el bono es sin costo"), base)
    assert razon_de_freno(_item(("segmento", "Gratis"), ("segmento", "el bono es sin costo")), context) == "promesa"


@pytest.mark.parametrize("quote", ["USD 47", "07/11", "10 %", "hasta el 07/11"])
def test_a_quote_that_is_only_a_number_is_not_a_promise(text, base, quote) -> None:
    context = _ctx_of(_with_list_entry(text, "no_confirmado", f"Que el valor sea {quote}"), base)
    assert razon_de_freno(_item(("segmento", quote), ("segmento", "Díselo una vez y sin presionar.")), context) is None


def test_a_list_entry_that_is_only_an_amount_is_not_a_promise(text, base) -> None:
    context = _ctx_of(_with_list_entry(text, "no_confirmado", "USD 47"), base)
    assert razon_de_freno(_item(("segmento", "USD 47"), ("segmento", "Díselo una vez y sin presionar.")), context) is None


def test_a_base_entry_needs_more_than_a_number(ctx) -> None:
    fake_base = SimpleNamespace(forbidden_promises=["Que cuesta USD 47 para siempre"], unconfirmed=[], products=[])
    context = sc.GateContext(ctx.segment, ctx.segment_text, fake_base)  # type: ignore[arg-type]
    assert razon_de_freno(_item(("base", "USD 47"), ("segmento", MEDIOS_ITEM)), context) is None
    assert razon_de_freno(_item(("base", "cuesta USD 47 para siempre"), ("segmento", MEDIOS_ITEM)), context) == "promesa"


@pytest.mark.parametrize(
    ("citations", "expected"),
    [
        ((("base", "USD 47"), ("plantilla:promo", "cuesta USD 31")), None),  # el de la oferta, en una plantilla
        ((("base", "USD 47"), ("plantilla:promo", "cuesta USD 35")), "precio"),  # un tercer valor
        ((("base", "USD 47"), ("segmento", "cuesta USD 35")), "precio"),
        ((("base", "MXN 900"), ("segmento", "En México, el precio de la campaña es MXN 577.")), None),  # otra moneda que la oferta: no se puede afirmar
        ((("base", "Garantía de 7 días"), ("plantilla:promo", "tienes garantía de 15 días")), None),
        ((("base", "Garantía de 7 días"), ("plantilla:promo", "tienes garantía de 20 días")), "garantia"),
        ((("base", "Garantía de 7 días"), ("segmento", "este producto no tiene garantía")), "garantia"),
    ],
)
def test_the_replaces_exception_is_only_for_the_value_of_the_offer(ctx, citations, expected) -> None:
    assert razon_de_freno(_item(*citations), ctx) == expected


@pytest.mark.parametrize(
    "quote",
    ["GLP-1 ayuda al metabolismo", "T3-T4 en rango", "COQ10 en la cápsula", "llega en 48HRS", "con 12MSI", "con PROTOCOLO10 tienes el descuento"],
)
def test_a_token_with_the_shape_of_a_code_needs_coupon_context(ctx, quote) -> None:
    assert razon_de_freno(_item(("segmento", quote)), ctx) is None


@pytest.mark.parametrize(
    "quote",
    [
        "Cupón BLACK",
        "cupón: BLACK",
        "el cupón es BLACK",
        "código de descuento OTRO2026",
        "escribe BLACKFRIDAY",
        "usa OTRO2026",
        "promo BF2026",
    ],
)
def test_a_code_with_context_stops(ctx, quote) -> None:
    assert razon_de_freno(_item(("segmento", quote)), ctx) == "cupon"


@pytest.mark.parametrize("quote", ["escribe QUIERO por WhatsApp", "usa OXXO o tarjeta", "cupón PRUEBABN31", "cupón GRATIS", "la promo BLACK November"])
def test_a_short_word_after_a_verb_or_the_own_coupon_does_not_stop(ctx, quote) -> None:
    assert razon_de_freno(_item(("segmento", quote)), ctx) is None


def test_a_campaign_without_a_coupon_still_stops_a_named_one(text, base) -> None:
    context = _ctx_of(text.replace('cupon = "PRUEBABN31"\n', ""), base)
    assert context.segment.coupon is None
    assert razon_de_freno(_item(("segmento", "usa el cupón OTRO2026")), context) == "cupon"


@pytest.mark.parametrize(
    "quote",
    ["el bono se suma con el cupón", "la guía se suma a tu compra con este código", "se combina con tu rutina de comida"],
)
def test_adding_up_a_bonus_is_not_adding_up_a_coupon(ctx, quote) -> None:
    assert razon_de_freno(_item(("segmento", quote)), ctx) is None


@pytest.mark.parametrize(
    "quote",
    ["el cupón se suma con otro descuento", "este descuento es acumulable con la promo", "PRUEBABN31 se suma con otro"],
)
def test_adding_up_with_another_coupon_or_discount_stops(ctx, quote) -> None:
    assert razon_de_freno(_item(("segmento", quote)), ctx) == "cupon"


def test_no_se_combina_alone_is_not_adding_up(ctx) -> None:
    assert razon_de_freno(_item(("segmento", "Este cupón no se combina con otras promociones.")), ctx) is None


@pytest.mark.parametrize(
    "quote",
    ["bono valorado en USD 97", "ahorras USD 16", "4 cuotas de USD 8", "12 pagos de USD 8", "descuento de USD 16", "te ahorras hasta USD 16"],
)
def test_amounts_that_measure_something_else_are_not_another_price(ctx, quote) -> None:
    assert razon_de_freno(_item(("segmento", "USD 31"), ("plantilla:promo", quote)), ctx) is None


def test_a_percent_saved_is_still_a_percent(ctx) -> None:
    assert razon_de_freno(_item(("segmento", "ahorras 30 %"), ("plantilla:promo", "10 %")), ctx) == "precio"


def test_one_payment_of_another_amount_is_another_price(ctx) -> None:
    assert razon_de_freno(_item(("segmento", "USD 31"), ("plantilla:promo", "un solo pago de USD 35")), ctx) == "precio"


@pytest.mark.parametrize("table", ["no_confirmado", "promesas_prohibidas"])
def test_text_in_the_lists_is_not_a_value_for_the_price_rule(text, base, table) -> None:
    context = _ctx_of(_with_list_entry(text, table, "Que el precio baje a USD 9 la semana que viene"), base)
    assert razon_de_freno(_item(("segmento", "Que el precio baje a USD 9 la semana que viene"), ("segmento", 'precio = "31"')), context) is None


@pytest.mark.parametrize(
    "citations",
    [
        (("segmento", "el reembolso tarda 5 días en llegar"), ("segmento", GUARANTEE_15)),
        (("segmento", "el reembolso tarda 5 días en llegar"), ("plantilla:promo", "este producto no tiene garantía")),
        (("segmento", "tienes garantía de 12 meses"), ("plantilla:promo", "tienes garantía de 1 año")),
        (("segmento", "tienes garantía de 4 semanas"), ("plantilla:promo", "tienes garantía de 1 mes")),
    ],
)
def test_terms_are_compared_only_when_they_are_of_the_guarantee_and_in_the_same_time(ctx, citations) -> None:
    assert razon_de_freno(_item(*citations), ctx) is None


def test_a_term_of_a_year_against_a_different_one_still_stops(ctx) -> None:
    citations = (("segmento", "tienes garantía de 2 años"), ("plantilla:promo", "tienes garantía de 1 año"))
    assert razon_de_freno(_item(*citations), ctx) == "garantia"


@pytest.mark.parametrize(
    ("declared", "quote"),
    [
        ("usd", "cuesta USD 35"),
        ("Dólares", "cuesta USD 35"),
        ("US$", "cuesta USD 35"),
        ("USD", "cuesta USD 35"),
        ("mxn", "cuesta MXN 35"),
        ("MXN", "cuesta MXN 35"),
        ("Pesos mexicanos", "cuesta MXN 35"),
    ],
)
def test_the_currency_of_the_offer_is_normalized(text, base, declared, quote) -> None:
    context = _ctx_of(text.replace('moneda = "USD"', f'moneda = "{declared}"'), base)
    assert razon_de_freno(_item(("segmento", 'precio = "31"'), ("plantilla:promo", quote)), context) == "precio"
