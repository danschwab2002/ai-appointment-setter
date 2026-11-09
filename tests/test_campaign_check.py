"""El comando del juez de una campana (agent-campaigns-v1, E-355), con el juez grabado y una base de mentira.

El juez es ``segment_check.check_files``, el mismo del alta de un segmento por
archivo: aca se prueba que el comando lea la fila, arme el TOML con la clave y
la version que pide el cargador, y anote el resultado en la fila.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_agent_campaigns import CAMPAIGN, LABEL, _context, _draft

from bridge import campaign_check
from bridge.agent_campaigns import expected_check_sha, parse_campaign
from bridge.campaign_check import report_for_row, run_check
from bridge.segment_check import Citation, Item, JudgeAnswer, SegmentCheckError


class _Store:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, function: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((function, payload))
        if function == "operator_get_agent_campaign_v1":
            return self.row
        return {"ok": True, "status": "validada", "clean": payload["p_check_sha"] is not None}


def _row(status: str = "validada") -> dict[str, Any]:
    return {"status": status, "fingerprint": "f" * 64, "segment_version": 1, "draft": parse_campaign(_draft())}


def _instance(tmp_path: Path) -> Path:
    # La carpeta de la instancia que deja _manifest_settings (instancia.toml y su conocimiento).
    return tmp_path / "instancia"


def _clean_judge(_: Any) -> JudgeAnswer:
    return JudgeAnswer(content=json.dumps({"items": []}), model="grabado")


def _soul(tmp_path: Path) -> Path:
    soul = tmp_path / "soul.md"
    soul.write_text("Sos el agente comercial.", encoding="utf-8")
    return soul


def test_a_clean_judge_records_the_check_sha_the_bridge_expects(tmp_path: Path) -> None:
    context = _context(tmp_path)
    store = _Store(_row())
    report, saved = run_check(store, manifest=context.manifest, instance_dir=_instance(tmp_path), campaign_id=CAMPAIGN,
                              judge=_clean_judge, soul_path=_soul(tmp_path), runs=3)
    assert report.clean is True and saved["ok"] is True
    function, payload = store.calls[-1]
    assert function == "record_agent_campaign_check_v1"
    assert payload["p_fingerprint"] == "f" * 64
    assert payload["p_report"]["veredicto"] == "limpio"
    assert payload["p_check_sha"] == expected_check_sha(parse_campaign(_draft()), version=1, context=context)


def test_open_questions_are_recorded_without_a_check_sha(tmp_path: Path) -> None:
    context = _context(tmp_path)
    store = _Store(_row())
    item = {"tipo": "hueco", "clave": "x", "titulo": "Falta decir",
            "citas": [{"fuente": "segmento", "texto": "x"}], "pregunta": "¿Hasta cuándo?", "campo": "z"}

    def judge(_: Any) -> JudgeAnswer:
        return JudgeAnswer(content=json.dumps({"items": [item]}), model="grabado")

    report, _ = run_check(store, manifest=context.manifest, instance_dir=_instance(tmp_path), campaign_id=CAMPAIGN,
                          judge=judge, soul_path=_soul(tmp_path), runs=3)
    payload = store.calls[-1][1]
    assert report.clean is False and payload["p_check_sha"] is None
    assert payload["p_report"]["veredicto"] == "abiertos"
    assert "¿Hasta cuándo?" in payload["p_report"]["items"][0]["pregunta"]


def test_rechecking_a_published_one_with_open_questions_shows_them_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # La base no pisa el control limpio de una publicada (agent_campaign_check_not_clean): no es un error.
    context = _context(tmp_path)
    item = {"tipo": "hueco", "clave": "x", "titulo": "Falta decir",
            "citas": [{"fuente": "segmento", "texto": "x"}], "pregunta": "¿Hasta cuándo?", "campo": "z"}

    class Refusing(_Store):
        def __init__(self, *_: Any) -> None:
            super().__init__(_row("publicada"))

        def call(self, function: str, payload: dict[str, Any]) -> dict[str, Any]:
            if function == "record_agent_campaign_check_v1":
                seen.update(payload)
                return {"ok": False, "reason_code": "agent_campaign_check_not_clean", "status": "publicada"}
            return super().call(function, payload)

    seen: dict[str, Any] = {}
    monkeypatch.setenv("SUPABASE_BASE_URL", "https://att1-rest.internal")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "x" * 40)
    monkeypatch.setattr(campaign_check, "_Store", Refusing)
    monkeypatch.setattr(campaign_check, "read_recorded",
                        lambda _: lambda __: JudgeAnswer(content=json.dumps({"items": [item]}), model="grabado"))
    code = campaign_check.main([CAMPAIGN, "--instancia", str(_instance(tmp_path)), "--soul", str(_soul(tmp_path)),
                                "--grabadas", "x.json"])
    out = capsys.readouterr()
    assert code == 1
    assert context.manifest is not None and seen["p_check_sha"] is None
    assert "VEREDICTO abiertos" in out.out and "¿Hasta cuándo?" in out.out and "sigue con su control limpio" in out.out
    assert "ERROR" not in out.err


@pytest.mark.parametrize("reason", ["agent_campaign_not_validated", "agent_campaign_check_of_other_content"])
def test_another_refusal_of_the_base_is_still_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], reason: str
) -> None:
    _context(tmp_path)

    class Refusing(_Store):
        def __init__(self, *_: Any) -> None:
            super().__init__(_row())

        def call(self, function: str, payload: dict[str, Any]) -> dict[str, Any]:
            if function == "record_agent_campaign_check_v1":
                return {"ok": False, "reason_code": reason}
            return super().call(function, payload)

    monkeypatch.setenv("SUPABASE_BASE_URL", "https://att1-rest.internal")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "x" * 40)
    monkeypatch.setattr(campaign_check, "_Store", Refusing)
    monkeypatch.setattr(campaign_check, "read_recorded", lambda _: _clean_judge)
    assert campaign_check.main([CAMPAIGN, "--instancia", str(_instance(tmp_path)), "--soul", str(_soul(tmp_path)),
                                "--grabadas", "x.json"]) == 2
    assert reason in capsys.readouterr().err


def test_a_draft_is_not_checked(tmp_path: Path) -> None:
    context = _context(tmp_path)
    store = _Store({**_row("borrador"), "fingerprint": None})
    try:
        run_check(store, manifest=context.manifest, instance_dir=_instance(tmp_path), campaign_id=CAMPAIGN,
                  judge=_clean_judge, soul_path=_soul(tmp_path), runs=3)
    except SegmentCheckError as error:
        assert "borrador" in str(error)
    else:
        raise AssertionError("un borrador no se chequea")
    assert [call[0] for call in store.calls] == ["operator_get_agent_campaign_v1"]


def test_report_for_row_with_no_valid_runs_is_no_verdict() -> None:
    class Report:
        clean, valid_runs, requested_runs, items = False, 0, 3, ()

    assert report_for_row(Report())["veredicto"] == "sin_veredicto"  # type: ignore[arg-type]


# ------------------------------------------------------------- el criterio (E-364a)

_PRICE_ITEM = {"tipo": "contradiccion", "clave": "otro-precio", "titulo": "Dos precios",
               "citas": [{"fuente": "segmento", "texto": "precio = \"37\""},
                         {"fuente": "segmento", "texto": "En Colombia cuesta USD 40."}],
               "pregunta": "¿Cuál va?", "campo": "oferta.precio"}
_WORDING_ITEM = {"tipo": "contradiccion", "clave": "tuteo", "titulo": "Escriba o escribe",
                 "citas": [{"fuente": "segmento", "texto": "Si pregunta por México: USD 37 con el link de la campaña."}],
                 "pregunta": "¿Tuteo?", "campo": "instruccion.texto"}


def _row_with_second_price() -> dict[str, Any]:
    return {**_row(), "draft": parse_campaign(_draft(se_puede_decir=["En Colombia cuesta USD 40."]))}


def _answer(*items: dict[str, Any]):
    return lambda _: JudgeAnswer(content=json.dumps({"items": list(items)}), model="grabado")


def _run(context: Any, tmp_path: Path, store: Any, judge: Any, **more: Any):
    return run_check(store, manifest=context.manifest, instance_dir=_instance(tmp_path), campaign_id=CAMPAIGN,
                     judge=judge, soul_path=_soul(tmp_path), runs=3, **more)


def test_with_the_criterio_a_wording_item_is_a_notice_and_the_row_is_clean(tmp_path: Path) -> None:
    context = _context(tmp_path)
    store = _Store(_row_with_second_price())
    report, _ = _run(context, tmp_path, store, _answer(_WORDING_ITEM), criterio="frenos")
    payload = store.calls[-1][1]
    row = payload["p_report"]
    assert report.clean is True and row["veredicto"] == "limpio" and row["abiertos"] == 0 and row["items"] == []
    assert row["criterio"] == "frenos-v1" and row["avisos_total"] == 1
    assert row["avisos"] == [{"numero": "C1", "tipo": "C", "pregunta": "Escriba o escribe: ¿Tuteo?", "campo": "instruccion.texto"}]
    # el sha es el de siempre: una campana publicada se sigue sirviendo
    assert payload["p_check_sha"] == expected_check_sha(parse_campaign(_draft(se_puede_decir=["En Colombia cuesta USD 40."])),
                                                        version=1, context=context)


def test_with_the_criterio_a_price_contradiction_stops_with_its_reason(tmp_path: Path) -> None:
    store = _Store(_row_with_second_price())
    report, _ = _run(_context(tmp_path), tmp_path, store, _answer(_PRICE_ITEM), criterio="frenos")
    payload = store.calls[-1][1]
    row = payload["p_report"]
    assert report.clean is False and payload["p_check_sha"] is None and row["veredicto"] == "abiertos"
    item = row["items"][0]
    assert item["razon"] == "precio" and item["campo"] == "oferta.precio"
    assert item["citas"] == [{"fuente": "segmento", "texto": 'precio = "37"'}, {"fuente": "segmento", "texto": "En Colombia cuesta USD 40."}]


def test_without_the_criterio_the_row_has_todays_shape(tmp_path: Path) -> None:
    store = _Store(_row_with_second_price())
    _run(_context(tmp_path), tmp_path, store, _answer(_WORDING_ITEM))
    row = store.calls[-1][1]["p_report"]
    assert row["veredicto"] == "abiertos" and set(row) == {"veredicto", "abiertos", "corridas_validas", "corridas_pedidas", "items"}
    assert set(row["items"][0]) == {"numero", "tipo", "pregunta"}


def test_the_judge_looks_for_the_flows_with_the_real_label_and_the_sha_does_not_change(tmp_path: Path) -> None:
    # Bug de E-355: buscaba con la clave. La etiqueta de una campana de la base es otra (black-november vs black-november-2026).
    draft = parse_campaign(_draft())
    assert draft["etiqueta"] == LABEL and draft["clave"] != LABEL
    context = _context(tmp_path)
    flows = _instance(tmp_path) / "flujos" / "publicados"
    flows.mkdir(parents=True)
    (flows / "promo.json").write_text(json.dumps({"pasos": [{"etiquetar": LABEL, "plantilla": "promo_01"}]}), encoding="utf-8")
    seen: list[str] = []

    def judge(messages: Any) -> JudgeAnswer:
        seen.append(messages[1]["content"])
        return JudgeAnswer(content=json.dumps({"items": []}), model="grabado")

    store = _Store(_row())
    report, _ = _run(context, tmp_path, store, judge, criterio="frenos")
    assert "pone la etiqueta del segmento y manda las plantillas: promo_01" in seen[0]
    assert "Ningun flujo publicado" not in seen[0]
    assert report.sources["flujos"]["etiqueta"] == LABEL
    assert store.calls[-1][1]["p_check_sha"] == expected_check_sha(draft, version=1, context=context)


@pytest.mark.parametrize("criterio", [None, "frenos-v1"])
@pytest.mark.parametrize(
    ("clean", "valid", "items", "expected"),
    [
        (True, 3, 0, "limpio"),
        (False, 2, 0, "sin_veredicto"),  # Bug: daba abiertos con la lista vacia
        (False, 2, 1, "abiertos"),  # lo que frena ya es real aunque falte una corrida
        (False, 3, 1, "abiertos"),
        (False, 0, 0, "sin_veredicto"),
    ],
)
def test_the_verdict_table(criterio: str | None, clean: bool, valid: int, items: int, expected: str) -> None:
    item = Item("C", "x", "t", [Citation("segmento", "x")], "q", "f", [1], number="C1", stop_reason="precio")
    report = SimpleNamespace(clean=clean, valid_runs=valid, requested_runs=3, items=[item] * items, notices=[])
    row = report_for_row(report, criterio=criterio)  # type: ignore[arg-type]
    assert row["veredicto"] == expected and row["abiertos"] == items


def _worst_item(number: int) -> Item:
    # 3 bytes por caracter y al tope de todos los recortes.
    cites = [Citation(f"plantilla:{'n' * 200}", "€" * 500) for _ in range(9)]
    return Item("C", "x", "€" * 900, cites, "€" * 900, "€" * 500, [1], number=f"C{number}", stop_reason="precio")


def test_the_row_fits_in_the_column_even_in_the_worst_case() -> None:
    report = SimpleNamespace(clean=False, valid_runs=3, requested_runs=3, items=[_worst_item(n) for n in range(50)],
                             notices=[_worst_item(n) for n in range(50)])
    row = report_for_row(report, criterio="frenos-v1")  # type: ignore[arg-type]
    assert len(json.dumps(row, ensure_ascii=False).encode("utf-8")) <= 131072
    assert row["abiertos"] == 50 and row["avisos_total"] == 50  # el total no se recorta
    assert 1 <= len(row["items"]) <= 40 and len(row["avisos"]) <= 40  # en el peor caso las citas y los avisos ceden primero


def test_a_normal_row_keeps_its_quotes_and_notices() -> None:
    item = Item("C", "x", "titulo", [Citation("segmento", "a"), Citation("base", "b")], "q", "f", [1], number="C1", stop_reason="cupon")
    notice = Item("H", "y", "falta", [Citation("base", "c")], "q2", "g", [1], number="H1")
    row = report_for_row(SimpleNamespace(clean=False, valid_runs=3, requested_runs=3, items=[item], notices=[notice]), criterio="frenos-v1")  # type: ignore[arg-type]
    assert len(row["items"][0]["citas"]) == 2 and len(row["avisos"]) == 1


def test_grabar_keeps_the_raw_answers_and_the_quotes_without_the_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _context(tmp_path)
    secret = "x" * 40
    monkeypatch.setenv("SUPABASE_BASE_URL", "https://att1-rest.internal")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", secret)
    monkeypatch.setattr(campaign_check, "_Store", lambda *_: _Store(_row_with_second_price()))
    monkeypatch.setattr(campaign_check, "read_recorded", lambda _: _answer(_PRICE_ITEM, _WORDING_ITEM))
    out_dir = tmp_path / "captura"

    code = campaign_check.main([CAMPAIGN, "--instancia", str(_instance(tmp_path)), "--soul", str(_soul(tmp_path)),
                                "--grabadas", "x.json", "--criterio", "frenos", "--grabar", str(out_dir)])

    assert code == 1
    printed = capsys.readouterr().out
    assert "VEREDICTO abiertos abiertos=1 corridas=3/3 avisos=1" in printed
    assert "(precio) Dos precios: ¿Cuál va?" in printed and "aviso C2. [C] Escriba o escribe: ¿Tuteo?" in printed
    names = sorted(path.name for path in out_dir.iterdir())
    assert names == ["reporte.json", "respuesta-1.json", "respuesta-2.json", "respuesta-3.json", "segmento.toml"]
    for path in out_dir.iterdir():
        assert secret not in path.read_text(encoding="utf-8")
    assert "otro-precio" in json.loads((out_dir / "respuesta-1.json").read_text(encoding="utf-8"))["content"]
    assert 'precio = "37"' in (out_dir / "segmento.toml").read_text(encoding="utf-8")
    reporte = json.loads((out_dir / "reporte.json").read_text(encoding="utf-8"))
    assert reporte["criterio"] == "frenos-v1" and reporte["veredicto"] == "abiertos"
    assert reporte["items"][0]["razon"] == "precio" and reporte["items"][0]["citas"][1]["texto"] == "En Colombia cuesta USD 40."
    assert reporte["no_frenan"][0]["clave"].startswith("c-tuteo-") and reporte["no_frenan"][0]["citas"]


def test_grabar_without_the_criterio_keeps_todays_exit_and_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _context(tmp_path)
    monkeypatch.setenv("SUPABASE_BASE_URL", "https://att1-rest.internal")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "x" * 40)
    monkeypatch.setattr(campaign_check, "_Store", lambda *_: _Store(_row_with_second_price()))
    monkeypatch.setattr(campaign_check, "read_recorded", lambda _: _answer(_WORDING_ITEM))

    code = campaign_check.main([CAMPAIGN, "--instancia", str(_instance(tmp_path)), "--soul", str(_soul(tmp_path)),
                                "--grabadas", "x.json", "--grabar", str(tmp_path / "c")])

    assert code == 1
    out = capsys.readouterr().out
    assert "VEREDICTO abiertos abiertos=1 corridas=3/3\n" in out and "aviso" not in out
    assert json.loads((tmp_path / "c" / "reporte.json").read_text(encoding="utf-8"))["criterio"] is None


def test_sin_anotar_runs_and_shows_without_writing_to_the_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _context(tmp_path)
    store = _Store(_row_with_second_price())
    monkeypatch.setenv("SUPABASE_BASE_URL", "https://att1-rest.internal")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "x" * 40)
    monkeypatch.setattr(campaign_check, "_Store", lambda *_: store)
    monkeypatch.setattr(campaign_check, "read_recorded", lambda _: _answer(_PRICE_ITEM))
    out_dir = tmp_path / "captura"

    code = campaign_check.main([CAMPAIGN, "--instancia", str(_instance(tmp_path)), "--soul", str(_soul(tmp_path)),
                                "--grabadas", "x.json", "--criterio", "frenos", "--grabar", str(out_dir), "--sin-anotar"])

    assert code == 1
    assert [call[0] for call in store.calls] == ["operator_get_agent_campaign_v1"]
    out = capsys.readouterr().out
    assert "VEREDICTO abiertos abiertos=1" in out and "Sin anotar" in out
    assert (out_dir / "reporte.json").exists()


@pytest.mark.parametrize("break_it", ["the_folder", "one_answer"])
def test_grabar_that_cannot_write_warns_and_the_paid_run_still_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], break_it: str) -> None:
    _context(tmp_path)
    store = _Store(_row_with_second_price())
    monkeypatch.setenv("SUPABASE_BASE_URL", "https://att1-rest.internal")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "x" * 40)
    monkeypatch.setattr(campaign_check, "_Store", lambda *_: store)
    monkeypatch.setattr(campaign_check, "read_recorded", lambda _: _answer(_PRICE_ITEM))
    if break_it == "the_folder":
        (tmp_path / "archivo").write_text("x", encoding="utf-8")
        out_dir = tmp_path / "archivo" / "captura"
    else:
        out_dir = tmp_path / "captura"
        (out_dir / "respuesta-1.json").mkdir(parents=True)

    code = campaign_check.main([CAMPAIGN, "--instancia", str(_instance(tmp_path)), "--soul", str(_soul(tmp_path)),
                                "--grabadas", "x.json", "--criterio", "frenos", "--grabar", str(out_dir)])

    assert code == 1  # hubo una contradiccion: el contrato, no un 2 por el disco
    assert store.calls[-1][0] == "record_agent_campaign_check_v1"
    captured = capsys.readouterr()
    assert "AVISO: no se pudo grabar" in captured.err and "VEREDICTO abiertos" in captured.out


def _published_event_bytes(row: dict[str, Any]) -> int:
    # Lo que arma la migracion 20261108005000 para una publicada: diez items y la pregunta a 300 caracteres.
    items = [item | {"pregunta": item["pregunta"][:300]} for item in row["items"][:10]]
    return len(json.dumps({"veredicto": row["veredicto"], "abiertos": row["abiertos"], "items": items}, ensure_ascii=False).encode("utf-8"))


@pytest.mark.parametrize("char", ["€", "😀", "\x01"])
def test_the_event_of_a_published_campaign_fits_even_in_the_worst_case(char: str) -> None:
    def worst(number: int) -> Item:
        cites = [Citation(f"plantilla:{char * 200}", char * 500) for _ in range(9)]
        return Item("C", "x", char * 900, cites, char * 900, char * 500, [1], number=f"C{number}", stop_reason="precio")

    report = SimpleNamespace(clean=False, valid_runs=3, requested_runs=3, items=[worst(n) for n in range(12)], notices=[])
    row = report_for_row(report, criterio="frenos-v1")  # type: ignore[arg-type]
    size = len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
    assert size <= 131072 and _published_event_bytes(row) <= 15_000
    assert row["abiertos"] == 12 and len(row["items"]) == 12


def test_a_typical_stop_keeps_its_quotes_under_the_event_limit() -> None:
    cites = [Citation("segmento", "a" * 280), Citation("base", "b" * 280)]
    items = [Item("C", "x", "titulo", cites, "q", "campo", [1], number=f"C{n}", stop_reason="precio") for n in range(4)]
    row = report_for_row(SimpleNamespace(clean=False, valid_runs=3, requested_runs=3, items=items, notices=[]), criterio="frenos-v1")  # type: ignore[arg-type]
    assert all(len(item["citas"]) == 2 for item in row["items"])
