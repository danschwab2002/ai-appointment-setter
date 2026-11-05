"""`setter segmento chequear`: el chequeo del alta de un segmento con un juez de modelo
(E-310 §3, E-312b).

Sin red: el juez se simula con respuestas grabadas (tests/fixtures/segment_judge/,
SIMULADAS y marcadas asi, con citas copiadas de las fuentes reales). Las fuentes son
reales: el conocimiento v14 de ATT1 y su manifiesto, flujos, catalogo y casos
(tests/fixtures/instances/att1-b7580bc-20261103), las plantillas del carrito capturadas
el 18/10 y el SOUL comun de este repo. El segmento es ficticio
(tests/fixtures/segments/campana-ejemplo-v1.toml): no es la oferta de Black November.
La prueba contra el modelo real esta en test_segment_check_real.py, apagada.
"""

from __future__ import annotations

import copy
import json
import shutil
import tomllib
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from bridge import segment_check as sc
from bridge.commercial_knowledge import CommercialKnowledge

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
ATT1 = FIXTURES / "instances" / "att1-b7580bc-20261103"
EXAMPLE = FIXTURES / "segments" / "campana-ejemplo-v1.toml"
JUDGE = FIXTURES / "segment_judge"
CART_TEMPLATES = FIXTURES / "chatwoot_message_templates_inbox_11_carrito_20261018.json"
SOUL = (ROOT / sc.SOUL_RELATIVE_PATH).read_text(encoding="utf-8")
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
EMPTY = json.dumps({"items": []})


@pytest.fixture(scope="module")
def base() -> CommercialKnowledge:
    return CommercialKnowledge.from_toml_file(ATT1 / "conocimiento" / "knowledge-v14.toml")


@pytest.fixture
def example_text() -> str:
    return EXAMPLE.read_text(encoding="utf-8")


def _recorded(*names: str) -> list[str]:
    return [json.loads((JUDGE / name).read_text(encoding="utf-8"))["content"] for name in names]


class Spy:
    """Un juez grabado que ademas guarda los mensajes que recibio."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.calls: list[list[dict[str, str]]] = []

    def __call__(self, messages):
        self.calls.append(messages)
        if not self.answers:
            raise sc.JudgeError("sin respuestas")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return sc.JudgeAnswer("grabado", answer)


def _run(text: str, base: CommercialKnowledge, judge, **kwargs) -> sc.Report:
    kwargs.setdefault("flujos", [])
    kwargs.setdefault("plantillas", {})
    kwargs.setdefault("casos", [])
    kwargs.setdefault("ahora", NOW)
    return sc.chequear(text, base=base, soul_texto=SOUL, juez=judge, **kwargs)


def _with_key(text: str, old: str, new: str) -> str:
    assert old in text
    return text.replace(old, new, 1)


# ------------------------------------------------------------------- fuentes reales


def test_the_real_att1_knowledge_is_the_one_measured_in_e310(base) -> None:
    assert len(base.render().encode("utf-8")) == 29_091
    assert base.rendered_sha256.startswith("0914acdb1baf")


def test_the_recorded_answers_are_marked_as_simulated() -> None:
    for path in JUDGE.glob("*.json"):
        assert "SIMULADA" in json.loads(path.read_text(encoding="utf-8"))["_origen"], path.name


# ------------------------------------------------------------- lo que lee el juez


def test_the_judge_reads_every_source_whole(example_text, base, tmp_path) -> None:
    other = _with_key(example_text, 'clave = "campana-ejemplo"', 'clave = "otra-campana"')
    later = _with_key(
        _with_key(example_text, 'clave = "campana-ejemplo"', 'clave = "campana-de-diciembre"'),
        "desde = 2026-11-20T00:00:00-06:00\nhasta = 2026-12-01T00:00:00-06:00",
        "desde = 2026-12-10T00:00:00-06:00\nhasta = 2026-12-20T00:00:00-06:00",
    )
    flows = sc.find_labeling_flows(_labeling_cart_flow(tmp_path, "campana-ejemplo"), "campana-ejemplo")
    templates = sc.read_template_catalog(CART_TEMPLATES)
    del templates["att1_ultima_renovacion_01"]
    case = sc.HarnessCase("201-bn-precio", json.dumps({"id": "201-bn-precio", "espera": "USD 37"}))
    spy = Spy([EMPTY])

    _run(example_text, base, spy, otros=[other, later], flujos=flows, plantillas=templates, casos=[case], corridas=1)

    system, user = spy.calls[0]
    assert system["role"] == "system" and "duda" in system["content"]
    text = user["content"]
    assert SOUL.strip() in text
    assert base.render().strip() in text
    assert example_text.strip() in text  # con sus [[preguntas_del_alta]]: el equipo ya contesto
    assert '<fuente id="segmento:otra-campana"' in text
    assert "campana-de-diciembre" not in text  # no se pisa: no se lee
    assert '<fuente id="plantilla:att1_carrito_descuento_01"' in text
    assert "La plantilla att1_ultima_renovacion_01 no esta en el catalogo" in text
    assert "esta prendido, pone la etiqueta" in text
    assert '<fuente id="caso:201-bn-precio"' in text
    assert "alta-12: ¿Quién entra al segmento" in text


def test_the_extractor_is_only_a_hint(example_text, base) -> None:
    spy = Spy([EMPTY])
    _run(example_text, base, spy, corridas=1)

    text = spy.calls[0][1]["content"]
    hints = text.split("Datos que encontro el extractor", 1)[1]
    assert "- base:" in hints and "monto usd 47" in hints and "cupon TIROIDES10" in hints
    assert "- segmento:" in hints and "monto usd 37" in hints


# --------------------------------------------------------------- la salida del juez


def _sources(example_text, base):
    seg = sc.parse_segment(tomllib.loads(example_text))
    return sc.build_sources(segment_text=example_text, segment=seg, base=base, soul_text=SOUL, now=NOW)


def test_a_citation_that_is_not_in_its_source_is_discarded(example_text, base) -> None:
    content = _recorded("abierto-corrida-2.json")[0]

    items = sc.parse_judge_output(content, _sources(example_text, base), run=1)

    doubt = next(item for item in items if item.kind == "D")
    assert [c.text for c in doubt.citations] == ["«Clase de ejemplo de campaña»"]
    assert [c.text for c in doubt.discarded] == ["el bono vence el 25 de noviembre"]


def test_an_item_without_any_verifiable_citation_is_still_asked(example_text, base) -> None:
    content = json.dumps(
        {"items": [{"tipo": "duda", "clave": "x", "titulo": "t", "citas": [{"fuente": "base", "texto": "algo que no esta"}], "pregunta": "¿Es así?", "campo": "c"}]}
    )

    report = _run(example_text, base, Spy([content]), corridas=1)

    assert len(report.items) == 1 and not report.items[0].verified
    assert report.items[0].to_json()["sin_cita_verificable"] is True
    assert report.chequeo_sha is None


def test_citations_tolerate_spacing_and_quote_styles(example_text, base) -> None:
    content = json.dumps(
        {"items": [{"tipo": "hueco", "clave": "x", "titulo": "t", "citas": [{"fuente": "base", "texto": "Precio:   USD 47."}], "pregunta": "?", "campo": "c"}]}
    )

    (item,) = sc.parse_judge_output(content, _sources(example_text, base), run=1)

    assert item.verified


@pytest.mark.parametrize(
    "content",
    [
        "no es json",
        json.dumps({"items": [], "extra": 1}),
        json.dumps({"items": [{"tipo": "otro", "clave": "x", "titulo": "t", "citas": [{"fuente": "base", "texto": "x"}], "pregunta": "?", "campo": "c"}]}),
        json.dumps({"items": [{"tipo": "duda", "clave": "x", "titulo": "t", "citas": [], "pregunta": "?", "campo": "c"}]}),
        json.dumps({"items": [{"tipo": "duda", "clave": "x", "titulo": "t", "pregunta": "?", "campo": "c"}]}),
    ],
)
def test_a_judge_output_with_the_wrong_shape_is_rejected(example_text, base, content) -> None:
    with pytest.raises((ValueError, json.JSONDecodeError)):
        sc.parse_judge_output(content, _sources(example_text, base), run=1)


def test_a_fenced_json_answer_is_accepted(example_text, base) -> None:
    assert sc.parse_judge_output("```json\n" + EMPTY + "\n```", _sources(example_text, base), run=1) == []


# ---------------------------------------------------------- varias corridas juntas


def test_the_runs_are_merged_and_nothing_seen_once_is_lost(example_text, base) -> None:
    answers = _recorded("abierto-corrida-1.json", "abierto-corrida-2.json", "abierto-corrida-3.json")

    report = _run(example_text, base, Spy(answers))

    runs = {item.title: item.runs for item in report.items}
    assert report.valid_runs == 3 and report.clean is False and report.chequeo_sha is None
    # El hueco alta-09 de las corridas 1 y 3 pregunta lo que el equipo ya contesto en
    # [alta-09] sin citar la respuesta: con las reglas v2 sale como aviso, no como abierto.
    assert [item.number for item in report.items] == ["C1", "C2", "D1"]
    assert sum("[alta-09]" in w for w in report.warnings) == 2
    assert runs["El base prohibe precios distintos de USD 47 y la campana trae USD 37"] == [1, 3]
    assert runs["El base prohibe hablar de vencimientos y el segmento dice hasta cuando vale"] == [1, 2]
    assert runs["No queda claro hasta cuando esta el bono"] == [2]  # una sola corrida: se pregunta igual
    for item in report.items:
        payload = item.to_json()
        assert payload["mensaje_slack"].startswith(f"*{item.number}*")
        assert payload["para_registrar"].startswith(f"[{item.key}] ")


def test_the_item_keys_are_stable(example_text, base) -> None:
    answers = _recorded("abierto-corrida-1.json", "abierto-corrida-2.json", "abierto-corrida-3.json")

    first = [item.key for item in _run(example_text, base, Spy(answers)).items]
    second = [item.key for item in _run(example_text, base, Spy(answers)).items]

    assert first == second


def test_three_clean_runs_give_the_sha_the_loader_expects(example_text, base) -> None:
    spy = Spy([EMPTY, EMPTY, EMPTY])

    report = _run(example_text, base, spy)

    assert report.clean and len(spy.calls) == 3
    assert spy.calls[0] == spy.calls[1] == spy.calls[2]
    raw = tomllib.loads(example_text)
    assert report.chequeo_sha == sc.calcular_chequeo_sha(raw, base_sha256=base.rendered_sha256)


def test_one_run_with_an_item_keeps_it_open(example_text, base) -> None:
    answers = [EMPTY, _recorded("abierto-corrida-3.json")[0], EMPTY]

    report = _run(example_text, base, Spy(answers))

    assert not report.clean and len(report.items) == 1 and report.chequeo_sha is None


def test_a_bad_answer_is_retried_once(example_text, base) -> None:
    spy = Spy([_recorded("sin-json.json")[0], EMPTY])

    report = _run(example_text, base, spy, corridas=1)

    assert report.clean and len(spy.calls) == 2


def test_a_run_that_fails_twice_leaves_no_verdict(example_text, base) -> None:
    bad = _recorded("sin-json.json")[0]

    report = _run(example_text, base, Spy([EMPTY, bad, bad, EMPTY]))

    assert not report.items and not report.complete and report.chequeo_sha is None
    assert [run.valid for run in report.runs] == [True, False, True]
    assert "salida invalida" in report.runs[1].error


def test_a_judge_error_leaves_no_verdict(example_text, base) -> None:
    report = _run(example_text, base, Spy([EMPTY, sc.JudgeError("OpenRouter respondio HTTP 502"), EMPTY]))

    assert not report.complete and report.chequeo_sha is None and report.runs[1].error.endswith("502")


# ------------------------------------------------------------------ OpenRouter


def test_openrouter_judge_sends_the_key_only_in_the_header(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "clave-de-prueba-123")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "moonshotai/kimi-k2.6", "choices": [{"message": {"content": EMPTY}}]})

    judge = sc.openrouter_judge(transport=httpx.MockTransport(handler))
    answer = judge([{"role": "user", "content": "hola"}])

    assert answer == sc.JudgeAnswer("moonshotai/kimi-k2.6", EMPTY)
    assert seen["auth"] == "Bearer clave-de-prueba-123"
    assert seen["url"] == sc.OPENROUTER_URL
    assert seen["body"]["model"] == sc.DEFAULT_MODEL == "moonshotai/kimi-k2.6"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert "clave-de-prueba-123" not in json.dumps(seen["body"])


def test_openrouter_errors_do_not_leak_the_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "clave-de-prueba-123")
    judge = sc.openrouter_judge(transport=httpx.MockTransport(lambda r: httpx.Response(401, text="clave-de-prueba-123 invalida")))

    with pytest.raises(sc.JudgeError) as caught:
        judge([{"role": "user", "content": "hola"}])

    assert "401" in str(caught.value) and "clave-de-prueba-123" not in str(caught.value)


def test_without_the_key_the_judge_does_not_start(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(sc.JudgeError, match="OPENROUTER_API_KEY"):
        sc.openrouter_judge()


# --------------------------------------------------------------- flujos y casos


def _labeling_cart_flow(tmp_path: Path, label: str) -> Path:
    flows = tmp_path / "flujos" / "publicados"
    shutil.copytree(ATT1 / "flujos" / "publicados", flows)
    path = flows / "att1" / "carrito" / "v1.json"  # la version activa en el indice real
    definition = json.loads(path.read_text(encoding="utf-8"))
    definition["pasos"].insert(1, {"etiquetar": label})
    path.write_text(json.dumps(definition, ensure_ascii=False), encoding="utf-8")
    return flows


def test_the_flows_that_label_are_found_in_the_real_index(tmp_path: Path) -> None:
    flows = sc.find_labeling_flows(_labeling_cart_flow(tmp_path, "campana-ejemplo"), "campana-ejemplo")

    assert [flow.where for flow in flows] == ["flujo att1/carrito/v1.json"]
    assert flows[0].turned_on is True
    assert flows[0].templates == ("att1_carrito_descuento_01", "att1_carrito_dudas_01", "att1_ultima_renovacion_01")
    assert sc.find_labeling_flows(ATT1 / "flujos" / "publicados", "campana-ejemplo") == []


def test_cases_are_read_whole_by_segment(tmp_path: Path) -> None:
    path = ATT1 / "conocimiento" / "prueba" / "casos.json"
    assert sc.read_cases(path, "campana-ejemplo") == []
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.append({"id": "201-bn", "espera": "USD 37", "segmentos": ["campana-ejemplo"], "turnos": [["prospect", "¿Cuánto cuesta?"]]})
    mine = tmp_path / "casos.json"
    mine.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    (case,) = sc.read_cases(mine, "campana-ejemplo")

    assert case.case_id == "201-bn" and "USD 37" in case.text and "¿Cuánto cuesta?" in case.text


# ---------------------------------------------------------- el enchufe del cargador


def _approved(example: dict, base: CommercialKnowledge) -> dict:
    segment = copy.deepcopy(example)
    segment["cabecera"].update(
        {
            "estado": "aprobado",
            "aprobado_por": "Prueba",
            "aprobado_el": date(2026, 11, 3),
            "chequeo_sha": sc.calcular_chequeo_sha(example, base_sha256=base.rendered_sha256),
        }
    )
    return segment


@pytest.fixture
def example() -> dict:
    return tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))


def test_the_approval_does_not_change_the_sha(example, base) -> None:
    approved = _approved(example, base)

    assert sc.segment_content_sha(approved) == sc.segment_content_sha(example)
    sc.verificar_chequeo(approved, base=base)


def test_the_loader_rejects_an_approved_segment_without_sha(example, base) -> None:
    approved = _approved(example, base)
    del approved["cabecera"]["chequeo_sha"]

    with pytest.raises(sc.ChequeoError, match="sin chequeo_sha"):
        sc.verificar_chequeo(approved, base=base)


def test_the_loader_rejects_a_sha_of_other_content(example, base) -> None:
    approved = _approved(example, base)
    approved["oferta"]["precio"] = "35"

    with pytest.raises(sc.ChequeoError, match="no es el del contenido actual"):
        sc.verificar_chequeo(approved, base=base)


def test_the_loader_rejects_the_sha_when_an_overlapping_segment_appears(example, base) -> None:
    approved = _approved(example, base)
    other = copy.deepcopy(example)
    other["cabecera"]["clave"] = "otra-campana"

    with pytest.raises(sc.ChequeoError):
        sc.verificar_chequeo(approved, base=base, otros=[other])


def test_the_loader_rejects_the_sha_when_the_base_changes(example, base) -> None:
    approved = _approved(example, base)
    v13 = CommercialKnowledge.from_toml_file(ATT1 / "conocimiento" / "knowledge-v13.toml", require_approved=False)

    with pytest.raises(sc.ChequeoError):
        sc.verificar_chequeo(approved, base=v13)


def test_a_draft_passes_the_loader_check(example, base) -> None:
    sc.verificar_chequeo(example, base=base)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda s: s["vigencia"].update(desde=datetime(2026, 11, 20)), "con zona"),
        (lambda s: s.update(extra={"x": 1}), "claves no soportadas"),
        (lambda s: s["oferta"].update(reemplaza=["cupon"]), "no se reemplazan"),
        (lambda s: s["instruccion"].update(texto="x" * 601), "maximo es 600"),
        (lambda s: s["vigencia"].update(hasta=s["vigencia"]["desde"]), "anterior"),
        # Las reglas del cargador de PR-A (segment_knowledge, E-311), que es el que manda:
        (lambda s: s["oferta"].update(reemplaza=["garantia", "medios"]) or s["oferta"].pop("garantia"), "no lo trae"),
        (lambda s: s["oferta"].update(reemplaza=["precio", "precio"]), "repite"),
        (lambda s: s["oferta"].pop("moneda"), "van juntos"),
        (lambda s: s["oferta"].update(cupon="con espacios"), "codigo"),
        (lambda s: s["oferta"].update(producto="Alimenta Tu Tiroides"), "slug"),
        (lambda s: s.update(oferta={}), "vacia"),
        (lambda s: s.update(no_confirmado={"items": []}), "no puede estar vacia"),
    ],
)
def test_invalid_segments(example, mutate, message) -> None:
    segment = copy.deepcopy(example)
    mutate(segment)

    with pytest.raises(sc.SegmentCheckError, match=message):
        sc.parse_segment(segment)


# --------------------------------------------------------------------------- CLI


def test_the_header_may_carry_a_name_like_in_pr_a(example) -> None:
    segment = copy.deepcopy(example)
    segment["cabecera"]["nombre"] = "Campaña de ejemplo"
    segment["oferta"]["reemplaza"] = ["precio", "incluye"]

    sc.parse_segment(segment)
    # El nombre es contenido (titula el bloque): entra en la huella.
    assert sc.segment_content_sha(segment) != sc.segment_content_sha(example)


def test_a_file_with_another_name_does_not_get_checked(tmp_path: Path, example_text) -> None:
    segment = _instance_with(tmp_path, example_text)
    renamed = segment.with_name("campana.toml")
    segment.rename(renamed)

    assert sc.main(["chequear", str(renamed), "--grabadas", str(JUDGE / "limpio.json")]) == 2


def _instance_with(tmp_path: Path, segment_text: str) -> Path:
    instance = tmp_path / "setter-instancia-att1"
    shutil.copytree(ATT1, instance)
    target = instance / "conocimiento" / "segmentos" / "campana-ejemplo-v1.toml"
    target.parent.mkdir(parents=True)
    target.write_text(segment_text, encoding="utf-8")
    return target


def test_cli_clean_with_recorded_answers_exits_0(tmp_path: Path, example_text, capsys) -> None:
    segment = _instance_with(tmp_path, example_text)
    clean = str(JUDGE / "limpio.json")
    out = tmp_path / "reporte"

    code = sc.main(["chequear", str(segment), "--grabadas", clean, clean, clean, "--salida", str(out), "--grabar", str(tmp_path / "grab")])

    err = capsys.readouterr().err
    payload = json.loads((out / "campana-ejemplo-v1-chequeo.json").read_text(encoding="utf-8"))
    assert code == 0 and payload["abiertos"] == 0 and len(payload["corridas"]) == 3
    assert f"LIMPIO chequeo_sha={payload['chequeo_sha']}" in err
    assert payload["fuentes"]["base"]["sha256"].startswith("0914acdb1baf")
    assert payload["fuentes"]["leidas_por_el_juez"][:4] == ["segmento", "respuestas", "soul", "base"]
    assert sorted(p.name for p in (tmp_path / "grab").iterdir()) == ["respuesta-1.json", "respuesta-2.json", "respuesta-3.json"]


def test_cli_with_open_items_exits_1(tmp_path: Path, example_text, capsys) -> None:
    segment = _instance_with(tmp_path, example_text)
    answers = [str(JUDGE / f"abierto-corrida-{n}.json") for n in (1, 2, 3)]

    assert sc.main(["chequear", str(segment), "--grabadas", *answers, "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["abiertos"] == 3 and payload["chequeo_sha"] is None


def test_cli_without_the_key_exits_3(tmp_path: Path, example_text, monkeypatch, capsys) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    segment = _instance_with(tmp_path, example_text)

    assert sc.main(["chequear", str(segment)]) == 3
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err


def test_cli_bad_input_exits_2(tmp_path: Path) -> None:
    clean = str(JUDGE / "limpio.json")
    segment = _instance_with(tmp_path, "[cabecera]\nclave = 'x'\n")

    assert sc.main(["chequear", str(segment), "--grabadas", clean]) == 2
    assert sc.main(["chequear", str(EXAMPLE), "--grabadas", clean]) == 2  # sin instancia
