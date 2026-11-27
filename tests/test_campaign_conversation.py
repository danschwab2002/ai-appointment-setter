"""«Probarla conversando» una campana validada (4.9 parte 2, D-49.9, ``campaign_conversation``).

Nunca llama a un modelo ni a Meta: el agente es un doble (``agent_rehearsal.Answer``) y la base de la
campana es la de ``test_campaign_check`` (la fila validada con el borrador de ``test_agent_campaigns``).
La corrida de la base es la del juez (20261126500000), emulada con ``FakeStore`` de
``test_campaign_judge_api``; lo que hace la base de verdad lo prueba PGlite
(``validate_agent_campaign_judge_runs.mjs``) y no cambia. Los mensajes del lead son inventados.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from test_agent_campaigns import CAMPAIGN, _context
from test_campaign_check import _row, _soul
from test_campaign_check import _Store as CheckStore
from test_campaign_judge_api import (
    ASKER,
    AUTH,
    INSIDE,
    MANIFEST,
    OUTSIDE,
    PREFIX,
    RUN,
    TOKEN,
    FakeStore,
    _bridge,
    _judge_settings,
)
from test_flow_approval_page import _settings

from bridge import agent_rehearsal, campaign_check, campaign_conversation, campaign_judge_api
from bridge.flow_change_api import EntryRefused

MESSAGES = ["Hola, vi la campaña, ¿de qué se trata?", "¿Y cuánto sale?", "Dale, lo quiero"]


def _proposal(reply: str, decision: str = "ask_question") -> str:
    status = {"ask_question": "in_progress", "send_payment_link": "in_progress", "handoff": "needs_human"}[decision]
    return json.dumps({"decision": decision, "qualification_status": status, "reason_code": "commercial_response",
                       "reply": reply, "captured_fields": dict.fromkeys(
                           ("person_name", "location", "role", "company_name", "company_size", "business_model",
                            "company_operational", "can_invest_in_education")),
                       "missing_fields": []}, ensure_ascii=False)


class Agent:
    """Contesta en orden; guarda lo que recibio."""

    def __init__(self, *answers: str | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[list[dict[str, str]]] = []

    def __call__(self, messages: list[dict[str, str]]) -> agent_rehearsal.Answer:
        self.calls.append(messages)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return agent_rehearsal.Answer(model="modelo-de-prueba", content=answer)


def _job(tmp_path: Path, agent: Agent) -> campaign_conversation.ConversationJob:
    return campaign_conversation.ConversationJob(base_url="https://att1-rest.internal", service_role_key="s" * 40,
                                                 context=_context(tmp_path), soul_path=_soul(tmp_path), ask=agent)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> CheckStore:
    found = CheckStore(_row())
    monkeypatch.setattr(campaign_check, "_Store", lambda base_url, key: found)
    return found


def test_each_message_goes_with_the_soul_the_base_and_the_campaign_block(tmp_path: Path, store: CheckStore) -> None:
    agent = Agent(_proposal("Es una campaña de noviembre. ¿Te cuento?"), _proposal("Sale lo que dice la oferta."),
                  _proposal("Te paso el enlace.", "send_payment_link"))
    job = _job(tmp_path, agent)
    state, result, error = campaign_conversation.run_conversation(job, CAMPAIGN, MESSAGES)
    assert (state, error) == ("limpio", None) and result is not None
    assert result["tipo"] == "conversacion" and result["veredicto"] == "limpio" and result["rechazados"] == 0
    assert [turn["estado"] for turn in result["turnos"]] == ["respondio"] * 3
    assert result["turnos"][2]["decision"] == "send_payment_link"
    context = job.context
    first = agent.calls[0]
    assert first[0]["content"] == "Sos el agente comercial."
    assert first[1]["content"] == context.base.render()
    assert first[2]["content"].startswith("#") and first[2]["content"] != first[1]["content"]
    # La historia crece: el tercer turno ve los dos mensajes del lead y las dos respuestas.
    third = json.loads(agent.calls[2][-1]["content"])
    assert [message["actor"] for message in third["messages"]] == ["prospect", "assistant"] * 2 + ["prospect"]
    assert third["payment_link_action"]["coupon_applied"] is False
    assert [name for name, _ in store.calls] == ["operator_get_agent_campaign_v1"]
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 16384


def test_a_reply_the_bridge_would_not_send_after_the_retry_stops_the_conversation(tmp_path: Path,
                                                                                   store: CheckStore) -> None:
    agent = Agent(_proposal("¿Cómo estás? ¿Qué buscás?"), _proposal("¿Uno? ¿Dos?"))
    state, result, error = campaign_conversation.run_conversation(_job(tmp_path, agent), CAMPAIGN, MESSAGES)
    assert (state, error) == ("con_preguntas", None) and result is not None
    first, second, third = result["turnos"]
    assert first["estado"] == "el_bridge_no_lo_mandaria" and first["rechazo"] == "too_many_questions"
    assert first["intentos"] == 2 and "una sola" in agent.calls[1][-1]["content"]
    assert second["estado"] == third["estado"] == "no_llego" and result["termino_en"] == "derivada_por_falla"


def test_a_handoff_ends_the_conversation_without_being_a_failure(tmp_path: Path, store: CheckStore) -> None:
    agent = Agent(_proposal("Te paso con el equipo.", "handoff"))
    state, result, _ = campaign_conversation.run_conversation(_job(tmp_path, agent), CAMPAIGN, MESSAGES[:2])
    assert state == "limpio" and result is not None and result["termino_en"] == "handoff"
    assert [turn["estado"] for turn in result["turnos"]] == ["respondio", "no_llego"]


def test_without_the_model_it_is_an_error_with_what_it_had(tmp_path: Path, store: CheckStore) -> None:
    agent = Agent(_proposal("Hola."), agent_rehearsal.RehearsalError("openrouter: ConnectError"))
    state, result, error = campaign_conversation.run_conversation(_job(tmp_path, agent), CAMPAIGN, MESSAGES)
    assert (state, error) == ("error", "agent_campaign_conversation_model_unavailable") and result is not None
    assert [turn["estado"] for turn in result["turnos"]] == ["respondio", "sin_respuesta_del_modelo", "no_llego"]


def test_a_campaign_that_is_not_validated_is_not_played(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    found = CheckStore(_row("borrador"))
    monkeypatch.setattr(campaign_check, "_Store", lambda base_url, key: found)
    agent = Agent()
    state, _, error = campaign_conversation.run_conversation(_job(tmp_path, agent), CAMPAIGN, MESSAGES)
    assert (state, error) == ("error", "agent_campaign_conversation_not_validated") and agent.calls == []


def test_long_replies_are_cut_to_fit_the_column(tmp_path: Path, store: CheckStore) -> None:
    long = "Texto largo de la respuesta. " * 34
    agent = Agent(*[_proposal(long[:995] + ".")] * 5)
    _, result, _ = campaign_conversation.run_conversation(_job(tmp_path, agent), CAMPAIGN, ["á" * 300] * 5)
    assert result is not None and len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 15_000


# --------------------------------------------------------------- las rutas


class Runner:
    def __init__(self, result: tuple[str, dict[str, Any] | None, str | None] | None = None) -> None:
        self.result = result or ("limpio", {"tipo": "conversacion", "veredicto": "limpio", "turnos": []}, None)
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, campaign_id: str, messages: list[str]) -> tuple[str, dict[str, Any] | None, str | None]:
        self.calls.append((campaign_id, list(messages)))
        return self.result


def _client(store: FakeStore, runner: Runner, now: Any = INSIDE) -> TestClient:
    app = FastAPI()

    @app.exception_handler(EntryRefused)
    async def refused(_: Any, error: EntryRefused) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": error.code, "reason": error.reason})

    campaign_conversation.register_campaign_conversation_routes(app, token=TOKEN, store=store,  # type: ignore[arg-type]
                                                                manifest=MANIFEST, run=runner, clock=lambda: now,
                                                                git_sha="abcdef0")
    return TestClient(app)


def _start(client: TestClient, messages: Any = None) -> Any:
    return client.post(f"{PREFIX}/campaigns/{CAMPAIGN}/conversation",
                       json={**ASKER, "mensajes": MESSAGES if messages is None else messages}, headers=AUTH)


def test_it_opens_the_same_run_as_the_judge_and_records_it_with_its_kind() -> None:
    store, runner = FakeStore(), Runner()
    response = _start(_client(store, runner))
    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert data["tipo"] == "conversacion" and data["resultado"] == "CORRIENDO" and data["daily_limit"] == 3
    assert store.finished.wait(5)
    assert store.calls[0][0] == "start" and store.calls[0][1]["daily_limit"] == 3
    assert runner.calls == [(CAMPAIGN, MESSAGES)]
    finish = store.calls[-1][1]
    assert finish["run_id"] == RUN and finish["status"] == "limpio" and finish["result"]["tipo"] == "conversacion"


def test_an_error_without_a_result_is_still_marked_as_a_conversation() -> None:
    store, runner = FakeStore(), Runner(("error", None, "agent_campaign_conversation_check_failed"))
    _start(_client(store, runner))
    assert store.finished.wait(5)
    assert store.calls[-1][1]["result"] == {"tipo": "conversacion"}


def test_outside_the_window_and_with_the_limit_nothing_runs() -> None:
    store, runner = FakeStore(), Runner()
    assert _start(_client(store, runner, now=OUTSIDE)).json()["detail"] == "agent_campaign_judge_outside_hours"
    assert store.calls == []
    limited = FakeStore(start={"ok": False, "reason_code": "agent_campaign_judge_daily_limit"})
    response = _start(_client(limited, runner))
    assert response.status_code == 409 and response.json()["detail"] == "agent_campaign_judge_daily_limit"
    assert runner.calls == []


@pytest.mark.parametrize("messages, problem", [
    ([], "list_of_1_to_5"), (["hola"] * 6, "list_of_1_to_5"), (["x" * 301], "text_1_to_300"), ([3], "text_1_to_300"),
])
def test_the_messages_are_one_to_five_short_texts(messages: Any, problem: str) -> None:
    store, runner = FakeStore(), Runner()
    response = _start(_client(store, runner), messages)
    assert response.status_code == 422 and response.json()["reason"]["problem"] == problem
    assert store.calls == []


def test_a_phone_or_a_mail_in_the_messages_is_refused() -> None:
    store, runner = FakeStore(), Runner()
    response = _start(_client(store, runner), ["Mi numero es 5512345678"])
    assert response.status_code == 422 and response.json()["detail"] == "identifier_not_allowed"
    assert store.calls == []


def test_the_read_shows_the_last_conversation_and_not_a_judge_run() -> None:
    conversation = {"run_id": RUN, "status": "con_preguntas", "error_code": None,
                    "result": {"tipo": "conversacion", "veredicto": "con_preguntas", "turnos": []},
                    "started_at": "2026-11-26T16:00:00Z", "finished_at": "2026-11-26T16:02:00Z"}
    data = _client(FakeStore(read={"last_run": conversation, "runs_today": 2}), Runner()).get(
        f"{PREFIX}/campaigns/{CAMPAIGN}/conversation", headers=AUTH).json()["data"]
    assert data["last_conversation"]["resultado"] == "CON-PREGUNTAS" and data["ultima_corrida"] == "conversacion"
    assert data["can_start_now"] is True
    judge = {**conversation, "status": "limpio", "result": {"veredicto": "limpio"}}
    data = _client(FakeStore(read={"last_run": judge, "runs_today": 3}), Runner()).get(
        f"{PREFIX}/campaigns/{CAMPAIGN}/conversation", headers=AUTH).json()["data"]
    assert data["last_conversation"] is None and data["ultima_corrida"] == "juez" and data["can_start_now"] is False


# --------------------------------------------------------------- el arranque y el flag


@pytest.fixture(autouse=True)
def _model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(agent_rehearsal.MODEL_ENV, "modelo-de-prueba")
    monkeypatch.setenv("SETTER_VERSION", "v1.42.0")
    monkeypatch.setenv("GIT_SHA", "abcdef0")


def test_the_flag_needs_the_judge_and_the_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="AUTOMATION_EXPERT_JUDGE_ENABLED"):
        campaign_conversation.check_settings(_settings(automation_expert_campaign_conversation_enabled=True))
    settings = _judge_settings(tmp_path, automation_expert_campaign_conversation_enabled=True)
    campaign_conversation.check_settings(settings)
    monkeypatch.delenv(agent_rehearsal.MODEL_ENV)
    with pytest.raises(ValueError, match=agent_rehearsal.MODEL_ENV):
        campaign_conversation.check_settings(settings)


def test_with_the_flag_off_the_route_does_not_exist_and_the_judge_read_does_not_change(tmp_path: Path) -> None:
    last = {"run_id": RUN, "status": "limpio", "error_code": None, "result": {"veredicto": "limpio"},
            "started_at": None, "finished_at": None}
    store = FakeStore(read={"last_run": last, "runs_today": 1})
    client = _bridge(_judge_settings(tmp_path), store)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert client.post(f"{PREFIX}/campaigns/{CAMPAIGN}/conversation",
                       json={**ASKER, "mensajes": ["hola"]}, headers=headers).status_code == 404
    assert "tipo" not in client.get(f"{PREFIX}/campaigns/{CAMPAIGN}/judge", headers=headers).json()["data"]["last_run"]
    assert "automation_expert_campaign_conversation" not in json.dumps(client.get("/ready").json())


def test_with_the_flag_on_the_bridge_has_the_route_and_the_judge_read_says_the_kind(tmp_path: Path) -> None:
    last = {"run_id": RUN, "status": "limpio", "error_code": None,
            "result": {"tipo": "conversacion", "veredicto": "limpio", "turnos": []},
            "started_at": None, "finished_at": None}
    store = FakeStore(read={"last_run": last, "runs_today": 1})
    client = _bridge(_judge_settings(tmp_path, automation_expert_campaign_conversation_enabled=True), store)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    read = client.get(f"{PREFIX}/campaigns/{CAMPAIGN}/conversation", headers=headers)
    assert read.status_code == 200 and read.json()["data"]["ultima_corrida"] == "conversacion"
    assert client.get(f"{PREFIX}/campaigns/{CAMPAIGN}/judge", headers=headers).json()["data"]["last_run"]["tipo"] \
        == "conversacion"
    assert "automation_expert_campaign_conversation" in json.dumps(client.get("/ready").json())


def test_the_flag_is_portable() -> None:
    from bridge import app as app_module

    assert "automation_expert_campaign_conversation_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES


def test_the_judge_module_has_no_new_route_without_the_flag() -> None:
    app = FastAPI()
    campaign_judge_api.register_campaign_judge_routes(app, token=TOKEN, store=FakeStore(),  # type: ignore[arg-type]
                                                      manifest=MANIFEST, run=lambda _: ("limpio", {}, None))
    assert not any(str(getattr(route, "path", "")).endswith("/conversation") for route in app.routes)
