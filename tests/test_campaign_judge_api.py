"""El juez de una campana pedido por el Automation Expert (agent-campaign-judge-v1, 4.9, F-163).

La base se emula con las reglas de 20261126500000 que le importan al bridge (lo que la base hace de
verdad lo prueba PGlite, ``validate_agent_campaign_judge_runs.mjs``). El juez no llama a ningun modelo:
la corrida es un doble, o ``run_judge`` con el juez grabado y la base de mentira de ``test_campaign_check``.
La forma del resultado sale de la fila real de Maribel ``forever-young-2026`` v4 (leida en solo lectura
el 2026-11-26: ``abiertos`` con 2 items de ``razon`` = ``promesa``, ``campo`` y 6 ``citas``).
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from test_agent_campaign_routes import ENABLED, Base, Chatwoot, Db, _Processor
from test_agent_campaigns import CAMPAIGN, _context
from test_campaign_check import _clean_judge, _instance, _row, _soul
from test_campaign_check import _Store as CheckStore
from test_flow_approval_page import ORIGIN, _settings

from bridge import campaign_check, campaign_judge_api, segment_check
from bridge.app import create_app
from bridge.flow_change_api import EntryRefused
from bridge.instance_manifest import InstanceManifest
from bridge.segment_check import JudgeAnswer

PREFIX = "/internal/automation-expert/v1"
TOKEN = "c" * 48
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ASKER = {"requested_by": {"slack_user": "U01ASKER00"}}
RUN = "6f0c1c9e-3c1a-4d55-9d0e-2f6a2b8b4a11"
# ATT1 esta en America/Mexico_City (UTC-6): 16:00Z son las 10:00 y 04:00Z las 22:00 del dia anterior.
INSIDE = datetime(2026, 11, 26, 16, 0, tzinfo=UTC)
OUTSIDE = datetime(2026, 11, 27, 4, 0, tzinfo=UTC)
ATT1 = Path(__file__).parent / "fixtures" / "instances" / "att1"
MANIFEST = InstanceManifest.from_toml_file(ATT1 / "instancia.toml")
# Lo que dejaria el comando sobre la v4 de forever-young-2026 (mismas claves y tipos, texto ficticio).
QUESTIONS = {"veredicto": "abiertos", "abiertos": 2, "corridas_validas": 3, "corridas_pedidas": 3,
             "criterio": "frenos-v1", "avisos_total": 16,
             "preguntas": [{"numero": "C1", "tipo": "C", "razon": "promesa", "campo": "oferta.precio",
                            "pregunta": "Precio y moneda cambian: ¿van en reemplaza?"},
                           {"numero": "C2", "tipo": "C", "razon": "promesa", "campo": "se_puede_decir",
                            "pregunta": "¿La garantia es de 7 o de 30 dias?"}],
             "avisos": []}


class FakeStore:
    """Las tres funciones de la migracion, con lo que contestan."""

    def __init__(self, start: dict[str, Any] | None = None, read: dict[str, Any] | None = None) -> None:
        self.start = start or {"ok": True, "run_id": RUN, "runs_today": 1, "daily_limit": 3}
        self.read = read or {"last_run": None, "runs_today": 0}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.finished = threading.Event()

    async def start_run(self, instance: dict[str, str], **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("start", {"instance": instance, **kwargs}))
        return self.start

    async def finish_run(self, instance: dict[str, str], **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("finish", {"instance": instance, **kwargs}))
        self.finished.set()
        return {"ok": True}

    async def get_run(self, instance: dict[str, str], **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get", {"instance": instance, **kwargs}))
        return self.read


class Runner:
    def __init__(self, result: tuple[str, dict[str, Any] | None, str | None] = ("con_preguntas", QUESTIONS, None),
                 *, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []

    def __call__(self, campaign_id: str) -> tuple[str, dict[str, Any] | None, str | None]:
        self.calls.append(campaign_id)
        if self.error is not None:
            raise self.error
        return self.result


def _client(store: FakeStore, runner: Runner, now: datetime = INSIDE) -> TestClient:
    app = FastAPI()

    @app.exception_handler(EntryRefused)
    async def refused(_: Any, error: EntryRefused) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": error.code, "reason": error.reason})

    campaign_judge_api.register_campaign_judge_routes(app, token=TOKEN, store=store, manifest=MANIFEST,  # type: ignore[arg-type]
                                                      run=runner, clock=lambda: now, git_sha="abcdef0")
    return TestClient(app)


def _start(client: TestClient, body: dict[str, Any] | None = None) -> httpx.Response:
    return client.post(f"{PREFIX}/campaigns/{CAMPAIGN}/judge", json=ASKER if body is None else body, headers=AUTH)


def test_inside_the_window_it_opens_the_run_answers_at_once_and_records_the_result_later() -> None:
    store, runner = FakeStore(), Runner()
    client = _client(store, runner)
    response = _start(client)
    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert data["run_id"] == RUN and data["resultado"] == "CORRIENDO" and data["daily_limit"] == 3
    assert data["window"] == {"desde": "09:00", "hasta": "21:00", "zona": "America/Mexico_City"}
    assert store.finished.wait(5)
    start = store.calls[0][1]
    assert start["campaign_id"] == CAMPAIGN and start["requested_by"] == "U01ASKER00" and start["daily_limit"] == 3
    # El dia es el de la instancia: el 26/11 de CDMX empieza a las 06:00Z.
    assert start["day_start"] == datetime(2026, 11, 26, 6, 0, tzinfo=UTC)
    assert start["instance"] == {"tenant_ref": MANIFEST.tenant_ref, "ally_ref": MANIFEST.ally_ref}
    assert runner.calls == [CAMPAIGN]
    finish = store.calls[-1][1]
    assert finish == {"instance": start["instance"], "run_id": RUN, "status": "con_preguntas", "result": QUESTIONS,
                      "error_code": None}


def test_outside_the_window_nothing_reaches_the_base_nor_the_model() -> None:
    store, runner = FakeStore(), Runner()
    response = _start(_client(store, runner, now=OUTSIDE))
    assert response.status_code == 409 and response.json()["detail"] == "agent_campaign_judge_outside_hours"
    assert store.calls == [] and runner.calls == []


@pytest.mark.parametrize("hour, inside", [(14, False), (15, True), (2, True), (3, False)])
def test_the_window_is_nine_to_twenty_one_of_the_instance(hour: int, inside: bool) -> None:
    # 15:00Z = 09:00 CDMX (entra); 03:00Z = 21:00 CDMX (ya no).
    zone = campaign_judge_api.ZoneInfo("America/Mexico_City")
    assert campaign_judge_api.in_window(datetime(2026, 11, 26, hour, 0, tzinfo=UTC), zone) is inside


@pytest.mark.parametrize("code", ["agent_campaign_judge_daily_limit", "agent_campaign_judge_running",
                                  "agent_campaign_not_validated", "agent_campaign_judge_expires_soon",
                                  "agent_campaign_expired"])
def test_what_the_base_refuses_comes_back_as_409_and_nothing_runs(code: str) -> None:
    store, runner = FakeStore(start={"ok": False, "reason_code": code}), Runner()
    response = _start(_client(store, runner))
    assert response.status_code == 409 and response.json()["detail"] == code
    assert runner.calls == [] and [name for name, _ in store.calls] == ["start"]


def test_an_unknown_answer_of_the_base_is_unavailable_and_nothing_runs() -> None:
    store, runner = FakeStore(start={"ok": False, "reason_code": "otra_cosa"}), Runner()
    response = _start(_client(store, runner))
    assert response.status_code == 503 and runner.calls == []


def test_it_needs_the_change_token_and_who_asks() -> None:
    store, runner = FakeStore(), Runner()
    client = _client(store, runner)
    assert client.post(f"{PREFIX}/campaigns/{CAMPAIGN}/judge", json=ASKER).status_code == 401
    assert _start(client, {}).status_code == 422
    assert _start(client, {"requested_by": {"slack_user": "5215512345678"}}).status_code == 422
    assert client.post(f"{PREFIX}/campaigns/no-es-un-id/judge", json=ASKER, headers=AUTH).status_code == 422
    assert store.calls == [] and runner.calls == []


def test_a_run_that_breaks_is_recorded_as_an_error() -> None:
    store, runner = FakeStore(), Runner(error=RuntimeError("se rompio"))
    assert _start(_client(store, runner)).status_code == 202
    assert store.finished.wait(5)
    finish = store.calls[-1][1]
    assert finish["status"] == "error" and finish["error_code"] == "agent_campaign_judge_failed"
    assert finish["result"] is None


@pytest.mark.parametrize("state, word", [("corriendo", "CORRIENDO"), ("limpio", "LIMPIO"),
                                         ("con_preguntas", "CON-PREGUNTAS"), ("error", "ERROR")])
def test_the_read_says_the_last_run_in_one_word(state: str, word: str) -> None:
    last = {"run_id": RUN, "status": state, "error_code": "agent_campaign_judge_abandoned" if state == "error" else None,
            "result": QUESTIONS if state == "con_preguntas" else None, "started_at": "2026-11-26T16:00:00Z",
            "finished_at": None}
    store = FakeStore(read={"last_run": last, "runs_today": 1})
    response = _client(store, Runner()).get(f"{PREFIX}/campaigns/{CAMPAIGN}/judge", headers=AUTH)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["last_run"]["resultado"] == word and data["runs_today"] == 1
    assert data["can_start_now"] is (state != "corriendo")
    if state == "con_preguntas":
        assert [item["pregunta"] for item in data["last_run"]["result"]["preguntas"]] == [
            item["pregunta"] for item in QUESTIONS["preguntas"]]
    assert store.calls[0][1]["day_start"] == datetime(2026, 11, 26, 6, 0, tzinfo=UTC)


def test_the_read_without_runs_and_with_the_limit_reached() -> None:
    client = _client(FakeStore(read={"last_run": None, "runs_today": 3}), Runner())
    data = client.get(f"{PREFIX}/campaigns/{CAMPAIGN}/judge", headers=AUTH).json()["data"]
    assert data["last_run"] is None and data["can_start_now"] is False
    late = _client(FakeStore(), Runner(), now=OUTSIDE)
    assert late.get(f"{PREFIX}/campaigns/{CAMPAIGN}/judge", headers=AUTH).json()["data"]["can_start_now"] is False


# ---------------------------------------------------------------- la corrida, con la funcion del comando


def _job(tmp_path: Path) -> campaign_judge_api.JudgeJob:
    context = _context(tmp_path)
    return campaign_judge_api.JudgeJob(base_url="https://att1-rest.internal", service_role_key="s" * 40,
                                       manifest=context.manifest, instance_dir=_instance(tmp_path),
                                       soul_path=_soul(tmp_path), openrouter_api_key="k" * 40)


def test_run_judge_is_the_same_function_as_the_command_with_the_brakes_criterion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _job(tmp_path)
    store = CheckStore(_row())
    keys: list[str | None] = []

    def fake_openrouter(*, model: str, api_key: str | None = None, **_: Any) -> Any:
        keys.append(api_key)
        return _clean_judge

    monkeypatch.setattr(campaign_check, "_Store", lambda base_url, key: store)
    monkeypatch.setattr(segment_check, "openrouter_judge", fake_openrouter)
    state, result, error = campaign_judge_api.run_judge(job, CAMPAIGN)
    assert (state, error) == ("limpio", None) and keys == ["k" * 40]
    assert result is not None and result["veredicto"] == "limpio" and result["criterio"] == "frenos-v1"
    function, payload = store.calls[-1]
    assert function == "record_agent_campaign_check_v1" and payload["p_report"]["criterio"] == "frenos-v1"
    assert payload["p_check_sha"] is not None


def test_run_judge_without_the_model_is_an_error_without_a_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Como el comando: las tres corridas fallan, la fila queda con sin_veredicto. Para el AE es un ERROR.
    job = _job(tmp_path)
    store = CheckStore(_row())

    def down(_: Any) -> JudgeAnswer:
        raise segment_check.JudgeError("OpenRouter respondio HTTP 503")

    monkeypatch.setattr(campaign_check, "_Store", lambda base_url, key: store)
    monkeypatch.setattr(segment_check, "openrouter_judge", lambda **_: down)
    state, result, error = campaign_judge_api.run_judge(job, CAMPAIGN)
    assert (state, error) == ("error", "agent_campaign_judge_no_verdict")
    assert result is not None and result["veredicto"] == "sin_veredicto" and result["corridas_validas"] == 0
    assert store.calls[-1][1]["p_report"]["veredicto"] == "sin_veredicto"


def test_run_judge_without_the_key_is_an_error_and_reads_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = CheckStore(_row())
    monkeypatch.setattr(campaign_check, "_Store", lambda base_url, key: store)
    job = replace(_job(tmp_path), openrouter_api_key="")
    assert campaign_judge_api.run_judge(job, CAMPAIGN) == ("error", None, "agent_campaign_judge_model_unavailable")
    assert store.calls == []


def test_run_judge_on_a_draft_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = CheckStore(_row("borrador"))
    monkeypatch.setattr(campaign_check, "_Store", lambda base_url, key: store)
    monkeypatch.setattr(segment_check, "openrouter_judge", lambda **_: _clean_judge)
    assert campaign_judge_api.run_judge(_job(tmp_path), CAMPAIGN) == ("error", None, "agent_campaign_judge_check_failed")
    assert [name for name, _ in store.calls] == ["operator_get_agent_campaign_v1"]


def test_open_questions_become_the_questions_the_agent_tells(tmp_path: Path) -> None:
    context = _context(tmp_path)
    item = {"tipo": "hueco", "clave": "x", "titulo": "Falta decir",
            "citas": [{"fuente": "segmento", "texto": "x"}], "pregunta": "¿Hasta cuándo?" + "x" * 400, "campo": "z"}

    def judge(_: Any) -> JudgeAnswer:
        return JudgeAnswer(content=json.dumps({"items": [item]}), model="grabado")

    report, saved = campaign_check.run_check(CheckStore(_row()), manifest=context.manifest,
                                             instance_dir=_instance(tmp_path), campaign_id=CAMPAIGN, judge=judge,
                                             soul_path=_soul(tmp_path), runs=3)
    state, result, error = campaign_judge_api.outcome(report, saved)
    assert state == "con_preguntas" and error is None and result is not None
    assert result["veredicto"] == "abiertos" and result["abiertos"] == 1
    assert result["preguntas"][0]["pregunta"].startswith("Falta decir: ¿Hasta cuándo?")
    assert len(result["preguntas"][0]["pregunta"]) == 300
    assert len(json.dumps(result).encode("utf-8")) <= 16384


def test_a_refusal_of_the_base_when_recording_is_an_error(tmp_path: Path) -> None:
    context = _context(tmp_path)
    report, _ = campaign_check.run_check(CheckStore(_row()), manifest=context.manifest,
                                         instance_dir=_instance(tmp_path), campaign_id=CAMPAIGN, judge=_clean_judge,
                                         soul_path=_soul(tmp_path), runs=3)
    state, result, error = campaign_judge_api.outcome(report, {"ok": False, "reason_code": "agent_campaign_expired"})
    assert (state, result, error) == ("error", None, "agent_campaign_judge_not_recorded:agent_campaign_expired")
    assert campaign_judge_api.outcome(report, {"ok": False, "reason_code": "Raro!"})[2] == (
        "agent_campaign_judge_not_recorded:unknown")


# ---------------------------------------------------------------- el arranque y el flag


def _judge_settings(tmp_path: Path, **overrides: Any) -> Any:
    soul = tmp_path / "SOUL.md"
    soul.write_text("Sos el agente comercial.", encoding="utf-8")
    base = {**ENABLED, "automation_expert_judge_enabled": True, "automation_expert_judge_soul_path": soul,
            "openrouter_api_key": "k" * 40}
    return _settings(**{**base, **overrides})


def test_the_flag_requires_what_the_run_needs(tmp_path: Path) -> None:
    campaign_judge_api.check_settings(_judge_settings(tmp_path))
    for override, needed in (({"agent_campaigns_enabled": False}, "AGENT_CAMPAIGNS_ENABLED"),
                             ({"openrouter_api_key": None}, "OPENROUTER_API_KEY"),
                             ({"supabase_service_role_key": None}, "SUPABASE_SERVICE_ROLE_KEY"),
                             ({"automation_expert_judge_soul_path": None}, "AUTOMATION_EXPERT_JUDGE_SOUL_PATH"),
                             ({"automation_expert_judge_soul_path": tmp_path / "no-esta.md"},
                              "AUTOMATION_EXPERT_JUDGE_SOUL_PATH")):
        with pytest.raises(ValueError, match=needed):
            campaign_judge_api.check_settings(_judge_settings(tmp_path, **override))
    # Apagado no exige nada.
    campaign_judge_api.check_settings(_settings(**{**ENABLED, "openrouter_api_key": None}))


def _bridge(settings: Any, store: FakeStore | None = None, runner: Runner | None = None) -> TestClient:
    db = Db()
    app = create_app(settings, shadow_processor=_Processor(), supabase_client=Base(db),  # type: ignore[arg-type]
                     chatwoot_client=Chatwoot(),  # type: ignore[arg-type]
                     campaign_judge_store=store, campaign_judge_runner=runner)  # type: ignore[arg-type]
    client = TestClient(app, base_url=ORIGIN)
    client.__enter__()
    return client


@pytest.fixture(autouse=True)
def _image_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETTER_VERSION", "v1.42.0")
    monkeypatch.setenv("GIT_SHA", "abcdef0")


def test_with_the_flag_off_the_routes_do_not_exist_and_nothing_calls_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(segment_check, "openrouter_judge", lambda **_: called.append("modelo"))
    monkeypatch.setattr(campaign_judge_api, "run_judge", lambda *_: called.append("corrida"))
    client = _bridge(_settings(**ENABLED))
    headers = {"Authorization": f"Bearer {ENABLED['automation_expert_change_token']}"}
    assert client.post(f"{PREFIX}/campaigns/{CAMPAIGN}/judge", json=ASKER, headers=headers).status_code == 404
    assert client.get(f"{PREFIX}/campaigns/{CAMPAIGN}/judge", headers=headers).status_code == 404
    ready = client.get("/ready")
    assert ready.status_code == 200, ready.text
    assert "automation_expert_judge" not in json.dumps(ready.json())
    assert called == []
    assert not hasattr(client.app.state, "campaign_judge_tasks")


def test_with_the_flag_on_the_bridge_has_the_routes_with_the_change_token(tmp_path: Path) -> None:
    store, runner = FakeStore(read={"last_run": None, "runs_today": 0}), Runner()
    client = _bridge(_judge_settings(tmp_path), store, runner)
    headers = {"Authorization": f"Bearer {ENABLED['automation_expert_change_token']}"}
    response = client.get(f"{PREFIX}/campaigns/{CAMPAIGN}/judge", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["contract"] == "agent-campaign-judge-v1"
    ready = client.get("/ready")
    assert ready.status_code == 200, ready.text
    assert "automation_expert_judge" in json.dumps(ready.json())
    assert client.get(f"{PREFIX}/campaigns/{CAMPAIGN}/judge", headers=AUTH | {"Authorization": "Bearer x"}).status_code == 401
    assert runner.calls == []


def test_the_flag_is_portable() -> None:
    from bridge import app as app_module

    assert "automation_expert_judge_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
