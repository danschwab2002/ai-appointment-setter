"""El comando del juez de una campana (agent-campaigns-v1, E-355), con el juez grabado y una base de mentira.

El juez es ``segment_check.check_files``, el mismo del alta de un segmento por
archivo: aca se prueba que el comando lea la fila, arme el TOML con la clave y
la version que pide el cargador, y anote el resultado en la fila.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_agent_campaigns import CAMPAIGN, _context, _draft

from bridge import campaign_check
from bridge.agent_campaigns import expected_check_sha, parse_campaign
from bridge.campaign_check import report_for_row, run_check
from bridge.segment_check import JudgeAnswer, SegmentCheckError


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
