"""El arnes F-055 sobre un borrador del conocimiento vivo (5.5 parte 2, ``knowledge_harness``).

Nunca llama a un modelo: ``ask`` es un doble que contesta segun el conocimiento que recibe (el
segundo ``system``). Los casos los arma el test con la forma de ``casos.json`` del arnes de las
instancias (``conocimiento/prueba/casos.json`` de Maribel, E-194: ``reglas`` y ``casos`` con
``turnos``, ``decision``, ``debe_decir``, ``no_debe_decir``), con texto ficticio y sin datos de
personas. Las rutas y la pagina usan los dobles de ``test_live_knowledge_routes``.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_agent_campaign_routes import Chatwoot, _field, _visible
from test_flow_approval_page import DAN, ORIGIN, SlackEmulator, _Clock, _settings, _sign_in
from test_live_knowledge_routes import (
    AE_KEY,
    CLIENT_ID,
    CLIENT_SECRET,
    DB_KEY,
    ENABLED,
    KNOWLEDGE,
    KNOWLEDGE_ID,
    PAGE,
    Base,
    Db,
    _ae,
    _as_ae,
    _done,
    _post,
    _Processor,
    _propose,
    _publish,
)

from bridge import agent_rehearsal, flow_approval, knowledge_harness, live_knowledge, live_knowledge_api
from bridge.app import create_app

RULES = {
    "garantia_dias": "7",
    "cupon": "cupon10|10\\s?%",
    "presentacion": "soy del equipo",
    "promesa_de_resultado": "vas a (bajar|rejuvenecer)",
    "precio_calculado": "1[.,]?234",
    "plazos_confirmados": ["^7 d[íi]as$"],
    "numeros_confirmados": ["^8 m[óo]dulos$"],
    "mensaje_de_recuperacion": "retomar tu compra",
}
CASES = {
    "schema": "prueba-conocimiento-v1",
    "reglas": RULES,
    "casos": [
        {"id": "01-garantia", "espera": "dice la garantia de 7 dias", "turnos": [["prospect", "¿Tiene garantia?"]],
         "decision": ["ask_question"], "debe_decir": ["7 d[íi]as"], "no_debe_decir": []},
        {"id": "02-precio", "espera": "no calcula el precio", "turnos": [["prospect", "¿Cuanto sale?"]],
         "decision": ["ask_question", "send_payment_link"]},
        {"id": "03-observar", "espera": "solo se lee", "solo_observar": True,
         "turnos": [["prospect", "Hola"]]},
    ],
}
BASE_TEXT = "# Conocimiento vigente"
DRAFT_TEXT = "# Conocimiento borrador"


def _proposal(reply: str, decision: str = "ask_question") -> str:
    status = {"ask_question": "in_progress", "send_payment_link": "in_progress", "handoff": "needs_human"}[decision]
    return json.dumps({"decision": decision, "qualification_status": status, "reason_code": "commercial_response",
                       "reply": reply, "captured_fields": dict.fromkeys(
                           ("person_name", "location", "role", "company_name", "company_size", "business_model",
                            "company_operational", "can_invest_in_education")),
                       "missing_fields": []}, ensure_ascii=False)


GOOD = {"¿Tiene garantia?": _proposal("Sí, tiene 7 días de garantía. ¿Te cuento más?"),
        "¿Cuanto sale?": _proposal("El precio está en la página. ¿Te paso el enlace?"),
        "Hola": _proposal("Hola, gracias por escribir. ¿En qué te ayudo?")}


class FakeAsk:
    """Contesta por variante (el texto del conocimiento) y por el ultimo mensaje del lead."""

    def __init__(self, draft: dict[str, str] | None = None, *, base: dict[str, str] | None = None,
                 fail_every: int = 0) -> None:
        self.draft = {**GOOD, **(draft or {})}
        self.base = {**GOOD, **(base or {})}
        self.calls: list[list[dict[str, str]]] = []
        self.fail_every = fail_every
        self.lock = threading.Lock()

    def __call__(self, messages: list[dict[str, str]]) -> agent_rehearsal.Answer:
        with self.lock:
            self.calls.append(messages)
            count = len(self.calls)
        if self.fail_every and count % self.fail_every == 0:
            raise agent_rehearsal.RehearsalError("openrouter: ConnectError")
        knowledge = messages[1]["content"]
        last = json.loads(messages[-1]["content"])["messages"][-1]["text"] if messages[-1]["content"].startswith("{") \
            else json.loads(messages[-2]["content"])["messages"][-1]["text"]
        table = self.draft if knowledge == DRAFT_TEXT else self.base
        return agent_rehearsal.Answer(model="modelo-de-prueba", content=table[last])


def _cases(tmp_path: Path, payload: dict[str, Any] | None = None) -> knowledge_harness.HarnessCases:
    path = tmp_path / "casos.json"
    path.write_text(json.dumps(payload or CASES, ensure_ascii=False), encoding="utf-8")
    return knowledge_harness.load_cases(path)


def _run(tmp_path: Path, ask: FakeAsk) -> tuple[str, dict[str, Any], str | None]:
    return knowledge_harness.run_harness(ask, soul="SOUL", base_rendered=BASE_TEXT, draft_rendered=DRAFT_TEXT,
                                         cases=_cases(tmp_path), reps=2, max_workers=2)


# --------------------------------------------------------------- las reglas


def test_the_rules_are_those_of_the_instance_harness() -> None:
    rules = knowledge_harness._compiled_rules(RULES)  # noqa: SLF001
    case = knowledge_harness._case(CASES["casos"][0], 0)  # noqa: SLF001
    ok = {"valid": True, "decision": "ask_question", "reply": "Tiene 7 días de garantía."}
    assert knowledge_harness.check(case, ok, rules) == []
    bad = {"valid": True, "decision": "ask_question", "reply": "Tiene 30 días de garantía, mirá https://x.com/a"}
    fails = knowledge_harness.check(case, bad, rules)
    assert "voseo:mirá" in fails and any(f.startswith("url:") for f in fails) and "falta:7 d[íi]as" in fails
    assert knowledge_harness.check(case, {"valid": False, "rechazo": "too_many_questions"}, rules) == [
        "bridge_rechaza:too_many_questions"]


def test_the_cases_file_is_validated_without_showing_its_text(tmp_path: Path) -> None:
    broken = {**CASES, "casos": [{"id": "x", "turnos": [["lead", "hola"]]}]}
    with pytest.raises(knowledge_harness.CasesError, match=r"casos\[0\]\.turnos"):
        _cases(tmp_path, broken)
    with pytest.raises(knowledge_harness.CasesError, match="reglas"):
        _cases(tmp_path, {"casos": CASES["casos"]})
    repeated = {**CASES, "casos": [CASES["casos"][0], CASES["casos"][0]]}
    with pytest.raises(knowledge_harness.CasesError, match="repite"):
        _cases(tmp_path, repeated)


# --------------------------------------------------------------- el veredicto


def test_green_when_the_draft_answers_like_what_is_in_force(tmp_path: Path) -> None:
    ask = FakeAsk()
    status, result, code = _run(tmp_path, ask)
    assert (status, code) == ("verde", None)
    assert result["llamadas"] == 12 and result["sin_respuesta"] == 0 and result["regresiones"] == []
    assert [item["id"] for item in result["para_leer"]] == ["03-observar"]
    # El SOUL primero, el conocimiento despues y el contexto con la forma del arnes.
    first = ask.calls[0]
    assert first[0] == {"role": "system", "content": "SOUL"} and first[1]["content"] in (BASE_TEXT, DRAFT_TEXT)
    context = json.loads(first[2]["content"])
    assert context["payment_link_action"]["bridge_injects_exact_url"] is True and context["known_fields"] == {}


def test_red_when_the_draft_fails_a_case_every_time_and_what_is_in_force_does_not(tmp_path: Path) -> None:
    ask = FakeAsk({"¿Tiene garantia?": _proposal("Tiene 30 días de garantía. ¿Seguimos?")})
    status, result, code = _run(tmp_path, ask)
    assert (status, code) == ("rojo", None)
    [regression] = result["regresiones"]
    assert regression["id"] == "01-garantia" and regression["borrador_falla"] == "2/2"
    assert regression["vigente_falla"] == "0/2" and "garantia_distinta:30 días de garant" in regression["fallas"]
    assert "30 días" in regression["respuesta"]


def test_a_reply_the_bridge_would_not_send_counts_and_the_retry_is_the_bridge_one(tmp_path: Path) -> None:
    ask = FakeAsk({"¿Cuanto sale?": "no es json"})
    status, result, _ = _run(tmp_path, ask)
    assert status == "rojo"
    assert result["regresiones"][0]["fallas"] == ["bridge_rechaza:not_json"]
    retried = [call for call in ask.calls if len(call) == 4]
    assert retried and "no era un objeto JSON" in retried[0][-1]["content"]


def test_a_case_that_fails_in_both_is_a_notice_not_a_brake(tmp_path: Path) -> None:
    both = _proposal("Mirá, el precio está en la página. ¿Te paso el enlace?")
    status, result, _ = _run(tmp_path, FakeAsk({"¿Cuanto sale?": both}, base={"¿Cuanto sale?": both}))
    assert status == "verde" and result["avisos_total"] == 1 and result["avisos"][0]["id"] == "02-precio"
    assert result["avisos"][0]["vigente_falla"] == "2/2"


def test_without_answers_from_the_model_there_is_no_verdict(tmp_path: Path) -> None:
    status, result, code = _run(tmp_path, FakeAsk(fail_every=2))
    assert (status, code) == ("error", "knowledge_harness_model_unavailable") and result["sin_respuesta"] >= 4


# --------------------------------------------------------------- donde queda


class _Run:
    def __init__(self, outcome: tuple[str, dict[str, Any], str | None] = ("verde", {"regresiones_total": 0}, None)):
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []
        self.done = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def __call__(self, ask: Any, **kwargs: Any) -> tuple[str, dict[str, Any], str | None]:
        self.release.wait(5)
        self.calls.append(kwargs)
        self.done.set()
        return self.outcome


def _runner(tmp_path: Path, run: _Run, clock: Any = None) -> knowledge_harness.HarnessRunner:
    soul = tmp_path / "SOUL.md"
    soul.write_text("SOUL", encoding="utf-8")
    cases = tmp_path / "casos.json"
    cases.write_text(json.dumps(CASES, ensure_ascii=False), encoding="utf-8")
    job = knowledge_harness.HarnessJob(soul_path=soul, cases_path=cases, ask=FakeAsk())
    kwargs = {"clock": clock} if clock is not None else {}
    return knowledge_harness.HarnessRunner(job=job, results=knowledge_harness.HarnessResults(tmp_path / "res",
                                                                                             **kwargs),
                                           run=run, **kwargs)


def test_a_run_that_never_finished_is_abandoned_after_thirty_minutes(tmp_path: Path) -> None:
    now = [datetime(2026, 11, 27, 12, 0, tzinfo=UTC)]
    results = knowledge_harness.HarnessResults(tmp_path, clock=lambda: now[0])
    results.write(KNOWLEDGE_ID, {"status": "corriendo", "rendered_sha256": "a" * 64,
                                 "started_at": now[0].isoformat()})
    assert results.read(KNOWLEDGE_ID)["status"] == "corriendo"  # type: ignore[index]
    now[0] += timedelta(minutes=31)
    assert results.read(KNOWLEDGE_ID)["error_code"] == "knowledge_harness_abandoned"  # type: ignore[index]


@pytest.mark.anyio
async def test_one_at_a_time_a_daily_limit_and_no_fishing_for_a_green(tmp_path: Path) -> None:
    run = _Run(("rojo", {"regresiones_total": 1}, None))
    run.release.clear()
    runner = _runner(tmp_path, run)
    sha = "b" * 64
    assert runner.refusal(KNOWLEDGE_ID, sha) == "knowledge_harness_not_run"
    assert runner.start(KNOWLEDGE_ID, base_rendered=BASE_TEXT, draft_rendered=DRAFT_TEXT, rendered_sha256=sha) \
        == "corriendo"
    assert runner.refusal(KNOWLEDGE_ID, sha) == "knowledge_harness_running"
    other = "6f1c1e9a-55aa-4f6e-9d55-000000000056"
    assert runner.start(other, base_rendered=BASE_TEXT, draft_rendered=DRAFT_TEXT, rendered_sha256=sha) \
        == "knowledge_harness_busy"
    run.release.set()
    for task in list(runner.tasks):
        await task
    assert runner.refusal(KNOWLEDGE_ID, sha) == "knowledge_harness_red"
    # Un rojo del mismo texto no se vuelve a correr; otro texto no cuenta.
    assert runner.start(KNOWLEDGE_ID, base_rendered=BASE_TEXT, draft_rendered=DRAFT_TEXT, rendered_sha256=sha,
                        rerun=True) == "knowledge_harness_already_ran"
    assert runner.view(KNOWLEDGE_ID, "c" * 64) is None
    assert run.calls[0]["base_rendered"] == BASE_TEXT and run.calls[0]["draft_rendered"] == DRAFT_TEXT
    runner._daily_limit = 1  # noqa: SLF001 - el tope, chico para el test
    assert runner.start(other, base_rendered=BASE_TEXT, draft_rendered=DRAFT_TEXT, rendered_sha256=sha) \
        == "knowledge_harness_daily_limit"


# --------------------------------------------------------------- las rutas y la pagina


@pytest.fixture(autouse=True)
def _image_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETTER_VERSION", "v1.43.0")
    monkeypatch.setenv("GIT_SHA", "abcdef0")
    monkeypatch.setenv(agent_rehearsal.MODEL_ENV, "modelo-de-prueba")


def _harness_settings(tmp_path: Path) -> dict[str, Any]:
    soul = tmp_path / "SOUL.md"
    soul.write_text("SOUL", encoding="utf-8")
    cases = tmp_path / "casos.json"
    cases.write_text(json.dumps(CASES, ensure_ascii=False), encoding="utf-8")
    return {**ENABLED, "agent_live_knowledge_harness_enabled": True, "openrouter_api_key": "sk-test-no-red",
            "agent_live_knowledge_harness_soul_path": soul, "agent_live_knowledge_harness_cases_path": cases}


def _app(tmp_path: Path, run: _Run | None = None, **overrides: Any) -> tuple[TestClient, Db, Any, _Run]:
    db = Db()
    slack = SlackEmulator()
    clock = _Clock()
    processor = _Processor()
    run = run or _Run()
    approval = flow_approval.ApprovalStore(base_url="https://att1-rest.internal", db_key=DB_KEY,
                                           transport=httpx.MockTransport(db.handler))
    oidc = flow_approval.SlackIdTokenClient(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                            transport=httpx.MockTransport(slack.handler), clock=lambda: clock.now)
    store = live_knowledge_api.KnowledgeStore(base_url="https://att1-rest.internal", db_key=AE_KEY,
                                              transport=httpx.MockTransport(_as_ae(db)))
    settings = {**_harness_settings(tmp_path), **overrides}
    runner = _runner(tmp_path, run) if settings.get("agent_live_knowledge_harness_enabled") else None
    app = create_app(_settings(**settings), shadow_processor=processor,  # type: ignore[arg-type]
                     supabase_client=Base(db), chatwoot_client=Chatwoot(),  # type: ignore[arg-type]
                     flow_approval_store=approval, flow_approval_oidc=oidc, live_knowledge_store=store,
                     knowledge_harness_runner=runner)
    client = TestClient(app, base_url=ORIGIN)
    client.__enter__()
    _sign_in(client, slack, clock, user=DAN)
    return client, db, runner, run


def _wait(runner: knowledge_harness.HarnessRunner, run: _Run) -> None:
    assert run.done.wait(5)
    for _ in range(200):
        if not runner.tasks:
            return
        threading.Event().wait(0.01)


def test_with_the_harness_flag_off_the_page_and_the_warning_are_those_of_before(tmp_path: Path) -> None:
    client, db, runner, _ = _app(tmp_path, agent_live_knowledge_harness_enabled=False)
    response = _propose(client)
    assert response.json()["data"]["warnings"][0]["code"] == "knowledge_harness_not_run"
    assert "harness" not in response.json()["data"] and runner is None
    page = client.get(PAGE).text
    assert "Aprobar y usar" in page and "Arnés de conversaciones" not in page
    assert "knowledge_harness" not in client.get("/ready").json()


def test_the_flag_needs_the_soul_the_cases_the_model_and_the_key(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        _app(tmp_path, openrouter_api_key=None)
    with pytest.raises(ValueError, match=knowledge_harness.SOUL_ENV):
        _app(tmp_path, agent_live_knowledge_harness_soul_path=tmp_path / "no-existe.md")
    with pytest.raises(ValueError, match="harness cases"):
        _app(tmp_path, agent_live_knowledge_harness_cases_path=None)
    with pytest.raises(ValueError, match="AGENT_LIVE_KNOWLEDGE_ENABLED"):
        _app(tmp_path, agent_live_knowledge_enabled=False)
    monkeypatch.delenv(agent_rehearsal.MODEL_ENV)
    with pytest.raises(ValueError, match=agent_rehearsal.MODEL_ENV):
        _app(tmp_path)


def test_saving_starts_the_harness_and_approving_waits_for_green(tmp_path: Path) -> None:
    run = _Run()
    run.release.clear()
    client, db, runner, _ = _app(tmp_path, run)
    response = _propose(client)
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["harness"] == {"status": "corriendo"} and data["warnings"] == []
    assert client.get("/ready").json()["knowledge_harness"] == "enabled"
    # Corriendo: no hay boton y apretar igual no publica.
    page = client.get(PAGE).text
    assert "Aprobar y usar" not in page and "Corriendo" in _visible(page)
    refused = _post(client, "publish", csrf_token=_field(page, "csrf_token"), fingerprint="f" * 64)
    assert _done(refused) == "knowledge_harness_running"
    assert "approver_publish_agent_knowledge_v1" not in db.calls
    run.release.set()
    _wait(runner, run)
    # Lo que corrio es lo vigente contra el borrador guardado.
    rebuilt = live_knowledge.build_knowledge(db.rows[KNOWLEDGE_ID]["knowledge"], file_version=KNOWLEDGE.version,
                                             ally_ref="att1")
    assert run.calls[0]["base_rendered"] == KNOWLEDGE.render()
    assert run.calls[0]["draft_rendered"] == rebuilt.render()
    read = _ae(client, "GET", f"/versions/{KNOWLEDGE_ID}").json()["data"]
    assert read["harness"]["status"] == "verde" and read["harness"]["rendered_sha256"] == rebuilt.rendered_sha256
    assert "Verde" in _visible(client.get(PAGE).text)
    assert _done(_publish(client)) == "publicada"


def test_red_blocks_approving_and_shows_what_gets_worse(tmp_path: Path) -> None:
    result = {"casos": 3, "repeticiones": 2, "llamadas": 12, "sin_respuesta": 0, "regresiones_total": 1,
              "regresiones": [{"id": "01-garantia", "espera": "dice la garantia de 7 dias", "borrador_falla": "2/2",
                               "vigente_falla": "0/2", "fallas": ["garantia_distinta:30 días de garant"],
                               "respuesta": "Tiene 30 días de garantía.", "decision": "ask_question"}],
              "avisos": [], "avisos_total": 0, "para_leer": []}
    run = _Run(("rojo", result, None))
    client, db, runner, _ = _app(tmp_path, run)
    _propose(client)
    _wait(runner, run)
    page = client.get(PAGE).text
    visible = _visible(page)
    assert "Aprobar y usar" not in page and "Lo que empeora" in visible and "Tiene 30 días de garantía." in visible
    assert "Volver a correr el arnés" not in page
    refused = _post(client, "publish", csrf_token=_field(page, "csrf_token"), fingerprint="f" * 64)
    assert _done(refused) == "knowledge_harness_red"
    assert _done(_post(client, "harness", csrf_token=_field(page, "csrf_token"))) == "knowledge_harness_already_ran"
    assert "approver_publish_agent_knowledge_v1" not in db.calls


def test_an_error_can_be_run_again_from_the_page(tmp_path: Path) -> None:
    run = _Run(("error", {"sin_respuesta": 9}, "knowledge_harness_model_unavailable"))
    client, db, runner, _ = _app(tmp_path, run)
    _propose(client)
    _wait(runner, run)
    page = client.get(PAGE).text
    assert "Volver a correr el arnés" in page and "Aprobar y usar" not in page
    run.outcome = ("verde", {"regresiones_total": 0}, None)
    run.done.clear()
    assert _done(_post(client, "harness", csrf_token=_field(page, "csrf_token"))) == "knowledge_harness_started"
    _wait(runner, run)
    assert len(run.calls) == 2 and "Aprobar y usar" in client.get(PAGE).text
