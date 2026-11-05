"""El chequeo del alta converge (reglas segment-check-juez-v2).

Con las reglas v1, el juez real (moonshotai/kimi-k2.6, 3 corridas) no llegaba a limpio sobre
la campana sintetica prueba-bn-sintetico: en cuatro vueltas del 2026-11-05 volvio a preguntar
cosas que el equipo ya habia contestado en [[preguntas_del_alta]] (los meses sin intereses,
el cupon) y marco como contradiccion que oferta.cupon no este en oferta.reemplaza, cosa que el
producto no admite (REPLACEABLE_FIELDS). Las salidas son las REALES de esas corridas
(tests/fixtures/segment_judge_kimi_20261105/, sin tocar) y el segmento es el que leyo el juez
(tests/fixtures/segments/prueba-bn-sintetico-v1.toml, sintetico: no es ninguna oferta real).
El base es el v14 de ATT1 del fixture: las citas al base que se usan aca estan iguales en el v15.
"""

from __future__ import annotations

import json
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bridge import segment_check as sc
from bridge.commercial_knowledge import CommercialKnowledge

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
SEGMENT = FIXTURES / "segments" / "prueba-bn-sintetico-v1.toml"
KIMI = FIXTURES / "segment_judge_kimi_20261105"
SOUL = (ROOT / sc.SOUL_RELATIVE_PATH).read_text(encoding="utf-8")
NOW = datetime(2026, 11, 5, 14, 46, tzinfo=timezone.utc)
MESES = "c-meses-sin-intereses-cad65ade"
MESES_EXCEPCION = "c-meses-sin-intereses-vs-promesa-prohibida-f16f4a81"


@pytest.fixture(scope="module")
def base() -> CommercialKnowledge:
    return CommercialKnowledge.from_toml_file(
        FIXTURES / "instances" / "att1-b7580bc-20261103" / "conocimiento" / "knowledge-v14.toml"
    )


@pytest.fixture(scope="module")
def text() -> str:
    return SEGMENT.read_text(encoding="utf-8")


def _kimi(name: str) -> dict:
    return json.loads(json.loads((KIMI / name).read_text(encoding="utf-8"))["content"])


def _content(*items: dict) -> str:
    return json.dumps({"items": list(items)}, ensure_ascii=False)


class Spy:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.calls: list[list[dict[str, str]]] = []

    def __call__(self, messages):
        self.calls.append(messages)
        return sc.JudgeAnswer("grabado", self.answers.pop(0))


def _run(text: str, base: CommercialKnowledge, answers: list[str]) -> tuple[sc.Report, Spy]:
    spy = Spy(answers)
    report = sc.chequear(
        text, base=base, soul_texto=SOUL, juez=spy, flujos=[], plantillas={}, casos=[], corridas=len(answers), ahora=NOW
    )
    return report, spy


EMPTY = _content()


# ------------------------------------------------------------ lo que recibe el juez


def test_the_team_answers_reach_the_judge_as_resolved_with_their_key(text, base) -> None:
    report, spy = _run(text, base, [EMPTY])

    system, user = spy.calls[0]
    assert '<fuente id="respuestas"' in user["content"]
    assert f"[{MESES}] Pregunta: ¿Los pagos a 4 meses" in user["content"]
    assert "esta resuelta: no la vuelvas a preguntar" in system["content"]
    assert '"reabre"' in system["content"]
    assert report.to_json()["fuentes"]["leidas_por_el_juez"][:2] == ["segmento", "respuestas"]
    assert report.clean


def test_the_campaign_coupon_is_legitimate_without_reemplaza(text, base) -> None:
    _report, spy = _run(text, base, [EMPTY])

    system, user = spy.calls[0]
    # La lista sale de REPLACEABLE_FIELDS: si el producto suma el cupon, el juez se entera solo.
    assert f"({', '.join(sc.REPLACEABLE_FIELDS)})" in system["content"]
    assert "oferta.cupon es el cupon de la campana: que exista, o que sea distinto de un cupon del base, no es un item" in system["content"]
    assert "oferta.cupon = PRUEBABN31 es el cupon de esta campana" in user["content"]
    assert "cupon" not in sc.REPLACEABLE_FIELDS  # el cambio del producto es otro PR


def test_a_segment_without_keyed_answers_has_no_answers_source(base) -> None:
    text = (FIXTURES / "segments" / "campana-de-prueba-v1.toml").read_text(encoding="utf-8")
    assert not sc.resolved_answers(sc.parse_segment(tomllib.loads(text)))

    _report, spy = _run(text, base, [EMPTY])

    assert '<fuente id="respuestas"' not in spy.calls[0][1]["content"]


# --------------------------------------------------------------- reabrir una respuesta


def test_reopening_an_answer_without_citing_it_does_not_count(text, base) -> None:
    # 143749, corrida 3: vuelve a los meses sin intereses que el equipo contesto en
    # [c-meses-sin-intereses-cad65ade], citando el base y el segmento pero no la respuesta.
    item = _kimi("143749-respuesta-3.json")["items"][0]
    item["reabre"] = MESES

    report, _spy = _run(text, base, [_content(item), EMPTY, EMPTY])

    assert report.items == [] and report.clean and report.chequeo_sha
    assert any(f"[{MESES}]" in w and "sin citar un fragmento de la respuesta" in w for w in report.warnings)


def test_reopening_an_answer_that_cites_it_and_the_text_against_it_stays_open(text, base) -> None:
    # 144638, corrida 3, D2: la respuesta cita del base una frase que no esta («salvo… el bloque
    # de una campaña vigente»). Cita la respuesta y el texto del segmento: es un item de verdad.
    item = next(i for i in _kimi("144638-respuesta-3.json")["items"] if i["clave"] == "meses-sin-intereses-excepcion")
    item["reabre"] = MESES_EXCEPCION

    report, _spy = _run(text, base, [_content(item), EMPTY, EMPTY])

    assert len(report.items) == 1 and not report.clean
    assert report.items[0].reopens == MESES_EXCEPCION and report.items[0].to_json()["reabre"] == MESES_EXCEPCION


def test_the_same_slug_as_an_answered_question_is_a_reopening(text, base) -> None:
    # Sin reabre: el juez le pone a la duda el mismo slug que tenia la pregunta contestada.
    item = _kimi("143749-respuesta-3.json")["items"][0]
    item["clave"] = "meses-sin-intereses"

    report, _spy = _run(text, base, [_content(item)])

    assert report.items == [] and any(f"[{MESES}]" in w for w in report.warnings)


def test_a_reopening_that_only_cites_the_team_answers_does_not_count(text, base) -> None:
    answer = "Sin intereses, a propósito y solo en esta promo"
    item = {
        "tipo": "duda",
        "clave": "msi",
        "titulo": "La respuesta no dice el total",
        "citas": [{"fuente": "respuestas", "texto": answer}, {"fuente": "segmento", "texto": answer}],
        "pregunta": "¿Cuál es el total?",
        "campo": "oferta.medios",
        "reabre": f"[{MESES}]",
    }

    report, _spy = _run(text, base, [_content(item)])

    assert report.items == [] and any("sin citar un texto que la contradiga" in w for w in report.warnings)


def test_an_item_that_reopens_an_unknown_key_is_a_normal_item(text, base) -> None:
    item = _kimi("144638-respuesta-2.json")["items"][1]  # el falso tuteo de «dile que escriba»
    item["reabre"] = "c-no-existe-00000000"

    report, _spy = _run(text, base, [_content(item)])

    assert len(report.items) == 1 and report.items[0].reopens == "c-no-existe-00000000"


def test_the_real_v1_outputs_without_reabre_still_parse(text, base) -> None:
    # Las salidas grabadas con las reglas v1 no traen reabre: se leen igual, y sin reabre ni el
    # mismo slug el codigo no decide la equivalencia (la decide el juez con las reglas v2).
    answers = [json.loads((KIMI / f"144638-respuesta-{n}.json").read_text(encoding="utf-8"))["content"] for n in (1, 2, 3)]

    report, _spy = _run(text, base, answers)

    assert report.valid_runs == 3
    assert sorted(item.number for item in report.items) == ["C1", "C2", "D1", "D2"]


@pytest.mark.parametrize("bad", [1, None, ["x"]])
def test_reabre_must_be_text(text, base, bad) -> None:
    item = _kimi("143749-respuesta-3.json")["items"][0]
    item["reabre"] = bad
    sources = sc.build_sources(
        segment_text=text, segment=sc.parse_segment(tomllib.loads(text)), base=base, soul_text=SOUL, now=NOW
    )
    with pytest.raises(ValueError, match="reabre"):
        sc.parse_judge_output(_content(item), sources, run=1)


def test_the_rules_version_changed() -> None:
    # Cambiar las instrucciones del juez cambia todos los chequeo_sha (ningun segmento aprobado hoy).
    assert sc.CHECK_RULES_VERSION == "segment-check-juez-v2"
