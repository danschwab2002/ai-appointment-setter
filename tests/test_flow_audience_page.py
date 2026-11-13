"""La pagina de los envios a una audiencia (E-484, P20, D7): ver cuantos van a salir y confirmar.

Los dobles de tests/test_flow_approval_page.py (la sesion de «Entrar con Slack», PostgREST
y Slack emulados) con las tres funciones de la pagina de 20261112220000 y sus reglas: la
confirmacion exige el conteo exacto y el segundo paso (lo que hace la base de verdad lo
prueba PGlite: validate_flow_audience_triggers.mjs). La lista la arma el barredor (un doble).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_flow_approval_page import (
    CLIENT_ID,
    CLIENT_SECRET,
    DAN,
    DB_KEY,
    ORIGIN,
    FakeChatwoot,
    OUTSIDER,
    PostgrestEmulator,
    SlackEmulator,
    _Clock,
    _NoSupabase,
    _settings,
    _sign_in,
)

from bridge import flow_approval, flow_audience_page
from bridge import flow_audience_sweeper as fs
from bridge.app import create_app

FLOW = "0000000b-0000-4000-8000-00000000d1f0"
OCCURRENCE = "0000000c-0000-4000-8000-00000000a0c1"
PATH = "/flow-approval/envios"
FORM = {"Content-Type": "application/x-www-form-urlencoded", "Origin": ORIGIN}
PHONE = "5215550000001"


class AudienceDb(PostgrestEmulator):
    def __init__(self) -> None:
        super().__init__()
        self.occurrence: dict[str, Any] = {
            "occurrence_id": OCCURRENCE, "flow_id": FLOW, "flow_key": "black_manual", "form": "manual",
            "state": "materializada", "member_count": 137, "omitted_count": 9, "per_hour": 200,
            "omitted_by_reason": {"baja": 4, "sin_permiso": 5}, "released_count": 0, "started_count": 0,
            "materialized_at": "2026-12-01T15:00:00Z", "brake_reason": None}
        self.audience_calls: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[1]
        if "flow_audience" not in name:
            return super().handler(request)
        import json
        body = json.loads(request.content)
        self.audience_calls.append((name, body))
        session = self._session(body)
        if session is None:
            return self._raise("28000", "flow_approval_session_invalid", 403)
        row = self.occurrence
        if name == "approver_read_flow_audience_v1":
            return httpx.Response(200, json={
                "flows": [{"flow_id": FLOW, "flow_key": "black_manual", "nombre": "Aviso de Black", "form": "manual",
                           "state": "prendido", "version": 1}],
                "occurrences": [dict(row)]})
        if body["p_occurrence_id"] != OCCURRENCE:
            return self._raise("P0002", "flow_audience_occurrence_not_found", 500)
        if name == "approver_confirm_flow_audience_v1":
            if row["state"] != "materializada":
                return httpx.Response(200, json={"ok": False, "reason_code": "not_waiting_for_confirmation"})
            if body["p_second_step"] is not True:
                return httpx.Response(200, json={"ok": False, "reason_code": "second_step_missing"})
            if body["p_expected_count"] != row["member_count"]:
                return httpx.Response(200, json={"ok": False, "reason_code": "count_mismatch"})
            row.update(state="confirmada", confirmed_by=f"slack:{session['user']}")
            return httpx.Response(200, json={"ok": True, "member_count": row["member_count"]})
        if name == "approver_brake_flow_audience_v1":
            if row["state"] not in ("materializada", "confirmada", "enviando"):
                return httpx.Response(200, json={"ok": False, "reason_code": "not_brakeable"})
            row.update(state="frenada", brake_reason="a_mano")
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(name)


class FakeSweeper:
    def __init__(self) -> None:
        self.built: list[tuple[str, str]] = []

    async def materialize_manual(self, flow_id: str, *, slack_user_id: str) -> fs.ManualResult:
        self.built.append((flow_id, slack_user_id))
        return fs.ManualResult(True, None, OCCURRENCE, 137)


def _client(*, user: str = DAN) -> tuple[TestClient, AudienceDb, FakeSweeper]:
    db, slack, clock, sweeper = AudienceDb(), SlackEmulator(), _Clock(), FakeSweeper()
    settings = _settings()
    config = flow_approval.check_settings(settings)
    assert config is not None
    app = FastAPI()
    flow_approval.register_flow_approval_routes(
        app, config=config,
        store=flow_approval.ApprovalStore(base_url="https://att1-rest.internal", db_key=DB_KEY,
                                          transport=httpx.MockTransport(db.handler)),
        oidc=flow_approval.SlackIdTokenClient(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                              transport=httpx.MockTransport(slack.handler), clock=lambda: clock.now),
        time_zone="America/Mexico_City", clock=None,
        extra_routes=flow_audience_page.routes(sweeper=sweeper))  # type: ignore[arg-type]
    client = TestClient(app, base_url=ORIGIN)
    _sign_in(client, slack, clock, user=user)
    return client, db, sweeper


def _page(client: TestClient) -> str:
    response = client.get(PATH)
    assert response.status_code == 200, response.text
    return response.text


def _csrf(page: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match is not None
    return match.group(1)


def _post(client: TestClient, path: str, fields: dict[str, str], *, origin: str = ORIGIN) -> httpx.Response:
    return client.post(path, content=urlencode(fields), headers={**FORM, "Origin": origin}, follow_redirects=False)


def _done(response: httpx.Response) -> str:
    assert response.status_code == 303, response.text
    return parse_qs(urlsplit(response.headers["location"]).query)["hecho"][0]


def test_the_page_shows_how_many_will_go_out_and_why_some_stay_out_without_phones() -> None:
    client, db, _ = _client()
    page = _page(client)
    visible = re.sub(r"<[^>]+>", " ", re.sub(r"<style.*?</style>", " ", page, flags=re.S))
    assert "Van a salir:" in visible and "137" in visible and "200 por hora" in visible
    assert "4 pidieron no recibir más" in visible and "5 sin permiso vigente" in visible
    assert "Confirmar el envío" in visible and "Armar la lista" in visible
    assert PHONE not in page and DAN not in visible


def test_without_a_session_there_is_no_page_and_no_confirmation() -> None:
    client, db, _ = _client(user=OUTSIDER)  # no esta en la lista de aprobadores
    assert "Entrar con Slack" in client.get(PATH).text
    response = _post(client, f"{PATH}/{OCCURRENCE}/confirmar", {"csrf_token": "0" * 64, "conteo": "137",
                                                                 "segundo_paso": "si"})
    assert response.status_code in (200, 401, 403)
    assert not [call for call in db.audience_calls if call[0] == "approver_confirm_flow_audience_v1"]


def test_build_the_list_asks_the_sweeper_with_who_asked() -> None:
    client, _, sweeper = _client()
    assert _done(_post(client, f"{PATH}/armar", {"csrf_token": _csrf(_page(client)), "flujo": FLOW})) == "armada"
    assert sweeper.built == [(FLOW, DAN)]


@pytest.mark.parametrize(("fields", "code"), [
    ({"conteo": "136", "segundo_paso": "si"}, "count_mismatch"),
    ({"conteo": "137"}, "second_step_missing"),
    ({"conteo": "ciento"}, "count_mismatch"),
])
def test_a_wrong_count_or_no_second_step_does_not_confirm(fields: dict[str, str], code: str) -> None:
    client, db, _ = _client()
    assert _done(_post(client, f"{PATH}/{OCCURRENCE}/confirmar", {"csrf_token": _csrf(_page(client)), **fields})) \
        == code
    assert db.occurrence["state"] == "materializada"


def test_the_exact_count_and_the_second_step_confirm_once() -> None:
    client, db, _ = _client()
    fields = {"csrf_token": _csrf(_page(client)), "conteo": "137", "segundo_paso": "si"}
    assert _done(_post(client, f"{PATH}/{OCCURRENCE}/confirmar", fields)) == "confirmada"
    assert db.occurrence["confirmed_by"] == f"slack:{DAN}"
    (call,) = [body for name, body in db.audience_calls if name == "approver_confirm_flow_audience_v1"]
    assert call["p_expected_count"] == 137 and call["p_second_step"] is True
    assert _done(_post(client, f"{PATH}/{OCCURRENCE}/confirmar", fields)) == "not_waiting_for_confirmation"


def test_another_origin_or_a_bad_csrf_do_not_confirm() -> None:
    client, db, _ = _client()
    csrf = _csrf(_page(client))
    fields = {"conteo": "137", "segundo_paso": "si"}
    assert _post(client, f"{PATH}/{OCCURRENCE}/confirmar", {"csrf_token": csrf, **fields},
                 origin="https://otro.example").status_code == 403
    assert _post(client, f"{PATH}/{OCCURRENCE}/confirmar", {"csrf_token": "0" * 64, **fields}).status_code == 403
    assert db.occurrence["state"] == "materializada"


def test_brake_by_hand_and_an_unknown_occurrence_is_404() -> None:
    client, db, _ = _client()
    csrf = _csrf(_page(client))
    assert _done(_post(client, f"{PATH}/{OCCURRENCE}/frenar", {"csrf_token": csrf})) == "frenada"
    assert db.occurrence["state"] == "frenada"
    assert "lo frenó una persona" in _page(client)
    assert _post(client, f"{PATH}/{FLOW}/frenar", {"csrf_token": csrf}).status_code == 404
    assert _post(client, f"{PATH}/no-es-un-id/frenar", {"csrf_token": csrf}).status_code == 404


def test_with_the_flag_off_the_page_does_not_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(fs.FLAG, raising=False)
    app = create_app(_settings(), supabase_client=_NoSupabase(),  # type: ignore[arg-type]
                     chatwoot_client=FakeChatwoot())  # type: ignore[arg-type]
    client = TestClient(app, base_url=ORIGIN)
    assert client.get(PATH).status_code == 404
    assert getattr(app.state, "flow_audience", "falta") is None


def test_with_the_flag_on_and_no_health_reading_the_bridge_does_not_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(fs.FLAG, "true")
    monkeypatch.delenv(fs.HEALTH_PATH_VARIABLE, raising=False)
    with pytest.raises(ValueError, match=fs.HEALTH_PATH_VARIABLE):
        create_app(_settings(), supabase_client=_NoSupabase(),  # type: ignore[arg-type]
                     chatwoot_client=FakeChatwoot())  # type: ignore[arg-type]


def test_with_the_flag_on_the_page_exists(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv(fs.FLAG, "true")
    monkeypatch.setenv(fs.HEALTH_PATH_VARIABLE, str(tmp_path / "estado.json"))
    app = create_app(_settings(), supabase_client=_NoSupabase(),  # type: ignore[arg-type]
                     chatwoot_client=FakeChatwoot())  # type: ignore[arg-type]
    client = TestClient(app, base_url=ORIGIN)
    assert "Entrar con Slack" in client.get(PATH).text
    assert app.state.flow_audience.readiness()["flow_audience"] == fs.STATE_NO_ENGINE


def test_a_brake_by_a_low_limit_reads_in_plain_words_e547() -> None:
    row = {"occurrence_id": OCCURRENCE, "flow_key": "black_manual", "form": "manual", "state": "frenada",
           "member_count": 137, "per_hour": 200, "omitted_by_reason": {}, "materialized_at": "2026-12-01T15:00:00Z",
           "brake_reason": fs.BRAKE_LOW_LIMIT}
    html = flow_audience_page._occurrence_html(row, csrf="x", time_zone=None)
    assert "límite de mensajes del número es bajo" in html and "meta_salud:" not in html
    # Las frenadas viejas por LIMITED (antes de E-547) siguen leyéndose igual.
    old = flow_audience_page._occurrence_html({**row, "brake_reason": "meta_salud:limited"}, csrf="x", time_zone=None)
    assert "Meta limitó los envíos del número" in old
