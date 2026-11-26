"""Las rutas del AE, la pagina y /ready de la instruccion temporal (agent-temporary-instruction-v1, 5.3 de Z1).

Los dobles de ``test_flow_approval_page`` (la app con el cliente real de la base,
PostgREST y Slack emulados), como ``test_agent_campaign_routes``. El emulador
sigue las reglas de 20261126200000 que le importan a estas rutas; lo que la base
hace de verdad lo prueba PGlite (``validate_agent_temporary_instructions.mjs``).
La instruccion es ficticia.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from test_agent_campaign_routes import AE_KEY, TOKEN, Chatwoot
from test_flow_approval_page import (
    BRAYAN,
    CLIENT_ID,
    CLIENT_SECRET,
    DAN,
    DB_KEY,
    ORIGIN,
    OUTSIDER,
    PostgrestEmulator,
    SlackEmulator,
    _Clock,
    _claims,
    _NoSupabase,
    _settings,
    _sign_in,
)

from bridge import flow_approval, temporary_instruction, temporary_instruction_api
from bridge.app import Settings, create_app

PREFIX = "/internal/automation-expert/v1/temporary-instructions"
ID = "6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b"
PAGE = f"/flow-approval/i/{ID}"
FINGERPRINT = "a" * 64
FORM = {"Content-Type": "application/x-www-form-urlencoded", "Origin": ORIGIN}
ASKER = {"slack_user": "U01ASKER00"}
TEXT = "Hoy no ofrezcas cupón. Si preguntan por el vivo, avisá que se movió al jueves."
ENABLED = {
    "agent_temporary_instruction_enabled": True,
    "automation_expert_change_enabled": True, "automation_expert_change_token": TOKEN,
    "automation_expert_change_db_key": AE_KEY,
    "automation_expert_read_enabled": True, "automation_expert_read_token": "r" * 48,
    "chatwoot_account_id": 2, "chatwoot_inbox_id": 11,
}


def _until(days: float = 2) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).replace(microsecond=0).isoformat()


class Db(PostgrestEmulator):
    """Las funciones de la instruccion, con las reglas de la migracion que le importan a la pagina."""

    def __init__(self) -> None:
        super().__init__()
        self.row: dict[str, Any] | None = None
        self.live: dict[str, Any] | None = None
        self.other_live: dict[str, Any] | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def set_validated(self, *, valid_until: str | None = None) -> None:
        self.row = {"instruction_id": ID, "status": "validada", "effective_status": "validada", "version": 3,
                    "texto": TEXT, "purpose": "Black November sin cupon",
                    "valid_until": valid_until or _until(), "fingerprint": FINGERPRINT,
                    "expires_at": _until(1), "approved_at": None, "approved_by_ref": None, "closed_at": None,
                    "closed_by_kind": None, "closed_by_ref": None, "close_reason_code": None,
                    "created_at": "2026-11-26T12:00:00Z", "events": [], "other_live": self.other_live}

    def handler(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[1]
        if "temporary_instruction" not in name:
            return super().handler(request)
        body = json.loads(request.content)
        self.calls.append((name, body))
        if name.startswith("approver_"):
            session = self._session(body)
            if session is None:
                return self._raise("28000", "flow_approval_session_invalid", 403)
            if body["p_instruction_id"] != ID or self.row is None:
                return self._raise("P0002", "agent_temporary_instruction_not_found", 500)
            return getattr(self, "_" + name)(body, session["user"])
        return getattr(self, "_" + name)(body)

    def _live_row(self) -> dict[str, Any]:
        assert self.row is not None
        return {key: self.row[key] for key in ("instruction_id", "version", "texto", "valid_until", "fingerprint",
                                               "approved_at")}

    def _approver_get_agent_temporary_instruction_v1(self, _: dict, __: str) -> httpx.Response:
        return httpx.Response(200, json=copy.deepcopy(self.row))

    def _approver_publish_agent_temporary_instruction_v1(self, body: dict, user: str) -> httpx.Response:
        row = self.row
        assert row is not None
        if body["p_fingerprint"] != row["fingerprint"]:
            return self._raise("22023", "agent_temporary_instruction_fingerprint_mismatch")
        if row["status"] == "publicada":
            return httpx.Response(200, json={"ok": True, "replayed": True, "status": "publicada"})
        if row["status"] != "validada":
            return httpx.Response(200, json={"ok": False, "reason_code": "agent_temporary_instruction_not_validated"})
        row.update(status="publicada", effective_status="publicada", approved_at="2026-11-26T13:00:00Z",
                   approved_by_ref=user, other_live=None)
        self.live = self._live_row()
        return httpx.Response(200, json={"ok": True, "replayed": False, "status": "publicada", "replaced": False})

    def _approver_retire_agent_temporary_instruction_v1(self, body: dict, user: str) -> httpx.Response:
        row = self.row
        assert row is not None
        if row["status"] != "publicada":
            return httpx.Response(200, json={"ok": False, "reason_code": "agent_temporary_instruction_not_live"})
        row.update(status="retirada", effective_status="retirada", closed_by_kind="slack", closed_by_ref=user,
                   last_reason=body["p_reason"])
        self.live = None
        return httpx.Response(200, json={"ok": True, "replayed": False, "status": "retirada"})

    def _approver_discard_agent_temporary_instruction_v1(self, body: dict, __: str) -> httpx.Response:
        row = self.row
        assert row is not None
        if row["status"] != "validada":
            return httpx.Response(200, json={"ok": False, "reason_code": "agent_temporary_instruction_final"})
        row.update(status="descartada", effective_status="descartada", last_reason=body["p_reason"])
        return httpx.Response(200, json={"ok": True, "replayed": False})

    # El rol del agente.
    def _propose_agent_temporary_instruction_v1(self, body: dict) -> httpx.Response:
        assert body["p_instance"] == {"tenant_ref": "lancemos", "ally_ref": "att1"}
        self.set_validated(valid_until=body["p_valid_until"])
        assert self.row is not None
        self.row["texto"] = body["p_text"]
        return httpx.Response(200, json={"ok": True, "instruction_id": ID, "version": 3, "status": "validada",
                                         "fingerprint": FINGERPRINT, "expires_at": self.row["expires_at"],
                                         "valid_until": body["p_valid_until"]})

    def _get_agent_temporary_instruction_v1(self, _: dict) -> httpx.Response:
        if self.row is None:
            return self._raise("P0002", "agent_temporary_instruction_not_found", 500)
        return httpx.Response(200, json={key: value for key, value in copy.deepcopy(self.row).items()
                                         if key not in ("approved_by_ref", "closed_by_ref", "other_live")})

    def _list_agent_temporary_instructions_v1(self, _: dict) -> httpx.Response:
        rows = [] if self.row is None else [{key: self.row[key] for key in (
            "instruction_id", "status", "effective_status", "version", "texto", "valid_until")}]
        return httpx.Response(200, json={"instructions": rows,
                                         "live_instruction_id": ID if self.live else None,
                                         "database_at": "2026-11-26T13:00:00Z"})


class Base(_NoSupabase):
    """Solo lo que usa el bridge con el flag: la lectura de la viva."""

    def __init__(self, db: Db, *, down: bool = False) -> None:
        self.__dict__["db"] = db
        self.__dict__["down"] = down

    async def resolve_commercial_ally_runtime_binding(self, config: object) -> object:
        return config

    async def get_human_handoff_projection_status(self) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(pending_count=0, retryable_count=0, delivery_unknown_count=0,
                               conflict_count=0, dead_letter_count=0)

    async def get_live_agent_temporary_instruction(self, instance: dict[str, str]) -> dict[str, Any]:
        if self.down:
            raise OSError("caida")
        return {"instruction": self.db.live}


@pytest.fixture(autouse=True)
def _image_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETTER_VERSION", "v1.42.0")
    monkeypatch.setenv("GIT_SHA", "abcdef0")


def _app(db: Db | None = None, *, base_down: bool = False,
         **overrides: Any) -> tuple[TestClient, Db, SlackEmulator, _Clock]:
    db = db or Db()
    slack = SlackEmulator()
    clock = _Clock()
    approval = flow_approval.ApprovalStore(base_url="https://att1-rest.internal", db_key=DB_KEY,
                                           transport=httpx.MockTransport(db.handler))
    oidc = flow_approval.SlackIdTokenClient(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                            transport=httpx.MockTransport(slack.handler), clock=lambda: clock.now)

    def as_agent(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {AE_KEY}"
        return db.handler(request)

    store = temporary_instruction_api.TemporaryInstructionStore(base_url="https://att1-rest.internal", db_key=AE_KEY,
                                                                transport=httpx.MockTransport(as_agent))
    app = create_app(_settings(**{**ENABLED, **overrides}), supabase_client=Base(db, down=base_down),  # type: ignore[arg-type]
                     chatwoot_client=Chatwoot(),  # type: ignore[arg-type]
                     flow_approval_store=approval, flow_approval_oidc=oidc, temporary_instruction_store=store)
    client = TestClient(app, base_url=ORIGIN)
    client.__enter__()
    return client, db, slack, clock


def _signed(db: Db | None = None, *, user: str = DAN, **overrides: Any) -> tuple[TestClient, Db]:
    client, db, slack, clock = _app(db, **overrides)
    _sign_in(client, slack, clock, user=user)
    return client, db


def _ae(client: TestClient, method: str, path: str = "", body: Any = None, *, token: str = TOKEN) -> httpx.Response:
    return client.request(method, PREFIX + path, json=body, headers={"Authorization": f"Bearer {token}"})


def _visible(page: str) -> str:
    page = re.sub(r"<style.*?</style>", " ", page, flags=re.S)
    return re.sub(r"<[^>]+>", " ", page)


def _field(page: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]+)"', page)
    assert match is not None, name
    return match.group(1)


def _page(client: TestClient) -> str:
    response = client.get(PAGE)
    assert response.status_code == 200, response.text
    return response.text


def _post(client: TestClient, action: str, **fields: str) -> httpx.Response:
    return client.post(f"{PAGE}/{action}", content=urlencode(fields), headers=FORM, follow_redirects=False)


def _done(response: httpx.Response) -> str:
    assert response.status_code == 303, response.text
    query = {key: values[0] for key, values in parse_qs(urlsplit(response.headers["location"]).query).items()}
    return query["hecho"]


def _ready(client: TestClient) -> dict[str, Any]:
    return client.get("/ready").json()


# ------------------------------------------------------------------ el flag (D3)


def test_the_flag_is_off_by_default_and_portable() -> None:
    from bridge import app as app_module

    assert Settings.__dataclass_fields__["agent_temporary_instruction_enabled"].default is False
    assert "agent_temporary_instruction_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES


def test_with_the_flag_off_nothing_exists_and_ready_does_not_change() -> None:
    db = Db()
    db.set_validated()
    client, _, _, _ = _app(db, agent_temporary_instruction_enabled=False)
    assert _ae(client, "GET").status_code == 404
    assert _ae(client, "POST", "/preview", {"instruction": {"texto": TEXT, "hasta": _until()}}).status_code == 404
    assert client.get(PAGE, follow_redirects=False).status_code == 404
    assert "agent_temporary_instruction" not in _ready(client)
    assert db.calls == []


@pytest.mark.parametrize(("overrides", "message"), [
    ({"automation_expert_change_enabled": False}, "requires AUTOMATION_EXPERT_CHANGE_ENABLED"),
    ({"flow_approval_enabled": False}, "requires FLOW_APPROVAL_ENABLED"),
])
def test_the_flag_asks_for_the_agent_routes_and_the_page(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        create_app(_settings(**{**ENABLED, **overrides}), supabase_client=_NoSupabase())  # type: ignore[arg-type]


def test_without_a_manifest_it_does_not_start() -> None:
    with pytest.raises(ValueError):
        temporary_instruction_api.check_settings(_settings(**{**ENABLED, "instance_manifest": None}))


# ------------------------------------------------------------------ las rutas del AE


def test_the_ae_routes_need_its_key() -> None:
    client, _, _, _ = _app()
    assert _ae(client, "GET", token="x" * 48).status_code == 401


def test_the_preview_validates_and_renders_without_saving() -> None:
    client, db, _, _ = _app()
    response = _ae(client, "POST", "/preview", {"instruction": {"texto": TEXT, "hasta": _until()}})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ok"] is True and data["blocks"] == []
    assert data["preview"]["render"].startswith("# Instrucción temporal del equipo\n")
    assert data["preview"]["render"].endswith(TEXT) and data["preview"]["maximo"] == 600
    assert db.calls == []
    blocked = _ae(client, "POST", "/preview", {"instruction": {"texto": "Hoy sale USD 37 en lancemos.com",
                                                               "hasta": _until(9)}}).json()["data"]
    assert blocked["ok"] is False and [block["code"] for block in blocked["blocks"]] == [
        "temporary_instruction_link", "temporary_instruction_amount", "temporary_instruction_window_too_long"]


def test_what_does_not_parse_is_a_422_with_the_field_and_never_the_value() -> None:
    client, _, _, _ = _app()
    response = _ae(client, "POST", "/preview", {"instruction": {"texto": "a" * 601, "hasta": _until()}})
    assert response.status_code == 422
    assert response.json()["detail"] == "temporary_instruction_invalid"
    assert "a" * 50 not in response.text


def test_proposing_saves_it_frozen_and_returns_the_page_link() -> None:
    client, db, _, _ = _app()
    hasta = _until()
    response = _ae(client, "POST", "", {"instruction": {"texto": TEXT, "hasta": hasta}, "purpose": "Black sin cupon",
                                        "requested_by": ASKER})
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["instruction_id"] == ID and data["status"] == "validada"
    assert data["approval_url"] == f"{ORIGIN}/flow-approval/i/{ID}"
    [(name, body)] = db.calls
    assert name == "propose_agent_temporary_instruction_v1"
    assert body["p_text"] == TEXT and body["p_requested_by_ref"] == "U01ASKER00"
    assert body["p_validator"] == {"contract": "agent-temporary-instruction-v1", "rules_version": 1,
                                   "product_version": "v1.42.0", "git_sha": "abcdef0"}
    assert body["p_valid_until"] == datetime.fromisoformat(hasta).astimezone(UTC).isoformat().replace("+00:00", "Z")


def test_proposing_with_a_block_does_not_reach_the_base() -> None:
    client, db, _, _ = _app()
    response = _ae(client, "POST", "", {"instruction": {"texto": "Que escriban a soporte@lancemos.com",
                                                        "hasta": _until()}, "purpose": "x", "requested_by": ASKER})
    assert response.status_code == 422 and response.json()["detail"] == "temporary_instruction_blocked"
    assert db.calls == []


def test_proposing_without_the_image_identity_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_SHA")
    client, db, _, _ = _app()
    response = _ae(client, "POST", "", {"instruction": {"texto": TEXT, "hasta": _until()}, "purpose": "x",
                                        "requested_by": ASKER})
    assert response.status_code == 503 and db.calls == []


def test_a_purpose_with_a_phone_is_refused() -> None:
    client, db, _, _ = _app()
    response = _ae(client, "POST", "", {"instruction": {"texto": TEXT, "hasta": _until()},
                                        "purpose": "Llamar al 5512345678", "requested_by": ASKER})
    assert response.status_code == 422 and response.json()["detail"] == "identifier_not_allowed"
    assert db.calls == []


def test_the_agent_has_no_route_to_publish_or_retire() -> None:
    client, _, _, _ = _app()
    for action in ("publish", "retire", "discard"):
        assert _ae(client, "POST", f"/{ID}/{action}", {}).status_code in (404, 405)


def test_reading_and_listing_bring_the_page_link() -> None:
    db = Db()
    db.set_validated()
    client, _, _, _ = _app(db)
    read = _ae(client, "GET", f"/{ID}").json()["data"]
    assert read["status"] == "validada" and read["approval_url"] == f"{ORIGIN}/flow-approval/i/{ID}"
    assert "approved_by_ref" not in read
    listed = _ae(client, "GET").json()["data"]
    assert [row["instruction_id"] for row in listed["instructions"]] == [ID]
    assert listed["instructions"][0]["approval_url"] == f"{ORIGIN}/flow-approval/i/{ID}"


# ------------------------------------------------------------------ la pagina


def test_the_page_needs_a_sign_in_and_comes_back_to_the_instruction() -> None:
    db = Db()
    db.set_validated()
    client, _, slack, clock = _app(db)
    response = client.get(PAGE, follow_redirects=False)
    assert response.status_code == 401 and f"?i={ID}" in response.text
    start = client.get(f"/flow-approval/auth/slack/start?i={ID}", follow_redirects=False)
    assert start.status_code == 303
    query = {key: values[0] for key, values in parse_qs(urlsplit(start.headers["location"]).query).items()}
    assert query["state"].startswith(f"i{ID}")
    _claims(slack, clock, query["nonce"])
    back = client.get("/flow-approval/auth/slack/callback", params={"code": "codigo", "state": query["state"]},
                      follow_redirects=False)
    assert back.status_code == 303 and back.headers["location"] == PAGE
    # Un id que no es un uuid no abre el ingreso.
    assert client.get("/flow-approval/auth/slack/start?i=otra-cosa", follow_redirects=False).status_code == 404


def test_someone_out_of_the_list_does_not_get_in() -> None:
    db = Db()
    db.set_validated()
    client, _, slack, clock = _app(db)
    response = _sign_in(client, slack, clock, user=OUTSIDER)
    assert response.status_code == 403
    assert client.get(PAGE, follow_redirects=False).status_code == 401


def test_the_page_is_in_business_words_with_no_ids() -> None:
    db = Db()
    db.set_validated(valid_until="2026-11-29T05:59:00+00:00")
    client, _ = _signed(db)
    page = _page(client)
    visible = _visible(page)
    assert "Esperando el sí" in visible and "Aprobar y publicar" in visible
    assert "Hasta el 28/11/2026 a las 23:59" in visible
    assert "Hoy no ofrezcas cupón" in visible
    for hidden in (ID, FINGERPRINT, DAN):
        assert hidden not in visible


def test_approving_publishes_and_the_turn_gets_it_without_waiting() -> None:
    db = Db()
    db.set_validated()
    client, _ = _signed(db)
    assert _ready(client)["agent_temporary_instruction"] == "none,refresh=ok"
    page = _page(client)
    done = _done(_post(client, "publish", csrf_token=_field(page, "csrf_token"),
                       fingerprint=_field(page, "fingerprint")))
    assert done == "publicada"
    assert _ready(client)["agent_temporary_instruction"] == "served:v3,refresh=ok"
    assert db.row is not None and db.row["approved_by_ref"] == DAN


def test_with_another_live_one_the_page_says_it_is_replaced() -> None:
    db = Db()
    db.other_live = {"version": 2, "valid_until": "2026-11-27T05:59:00+00:00"}
    db.set_validated()
    client, _ = _signed(db)
    visible = _visible(_page(client))
    assert "Ahora hay otra instrucción andando (hasta el 26/11/2026 a las 23:59" in visible
    assert "aquella deja de usarse" in visible


def test_another_origin_or_a_bad_csrf_do_not_publish() -> None:
    db = Db()
    db.set_validated()
    client, _ = _signed(db)
    page = _page(client)
    bad = client.post(f"{PAGE}/publish", content=urlencode({"csrf_token": "x" * 64,
                                                           "fingerprint": _field(page, "fingerprint")}),
                      headers=FORM, follow_redirects=False)
    assert bad.status_code == 403
    other = client.post(f"{PAGE}/publish", content=urlencode({"csrf_token": _field(page, "csrf_token"),
                                                             "fingerprint": FINGERPRINT}),
                        headers={**FORM, "Origin": "https://otro.example"}, follow_redirects=False)
    assert other.status_code == 403
    assert db.row is not None and db.row["status"] == "validada"


def test_retiring_stops_it_right_away() -> None:
    db = Db()
    db.set_validated()
    client, _ = _signed(db)
    page = _page(client)
    _post(client, "publish", csrf_token=_field(page, "csrf_token"), fingerprint=_field(page, "fingerprint"))
    page = _page(client)
    assert "Retirar" in _visible(page)
    assert _done(_post(client, "retire", csrf_token=_field(page, "csrf_token"), motivo="ya pasó el vivo")) \
        == "retirada"
    assert _ready(client)["agent_temporary_instruction"] == "none,refresh=ok"
    assert db.row is not None and db.row["closed_by_ref"] == DAN and db.row["last_reason"] == "ya pasó el vivo"


def test_discard_only_what_was_not_published() -> None:
    db = Db()
    db.set_validated()
    client, _ = _signed(db)
    page = _page(client)
    assert _done(_post(client, "discard", csrf_token=_field(page, "csrf_token"))) == "descartada"
    assert db.row is not None and db.row["last_reason"] == "descartada desde la pagina de aprobacion"


def test_a_published_one_the_bridge_cannot_serve_says_so() -> None:
    db = Db()
    db.set_validated()
    assert db.row is not None
    db.row.update(status="publicada", effective_status="publicada", approved_at="2026-11-26T13:00:00Z",
                  texto="Hoy sale USD 37")
    db.live = db._live_row()
    client, _ = _signed(db)
    assert _ready(client)["agent_temporary_instruction"] == "not_served:contenido_invalido,refresh=ok"
    assert "el agente NO la está usando" in _visible(_page(client))


def test_with_the_base_down_nothing_goes_and_ready_says_so() -> None:
    db = Db()
    db.set_validated()
    client, _, _, _ = _app(db, base_down=True)
    assert _ready(client)["agent_temporary_instruction"] == "none,refresh=failing:1"


def test_every_text_of_the_model_is_escaped() -> None:
    db = Db()
    db.set_validated()
    assert db.row is not None
    db.row["texto"] = "<script>alert(1)</script> no ofrezcas cupón"
    client, _ = _signed(db)
    page = _page(client)
    assert "<script>alert" not in page and "&lt;script&gt;" in page


def test_the_refresh_stops_with_the_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[temporary_instruction.LiveTemporaryInstruction] = []

    class Recorded(temporary_instruction.LiveTemporaryInstruction):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            made.append(self)

    monkeypatch.setattr(temporary_instruction, "LiveTemporaryInstruction", Recorded)
    client, _, _, _ = _app()
    [live] = made
    task = live._task
    assert task is not None and not task.done()
    client.__exit__(None, None, None)
    assert live._task is None and task.done()


def test_brayan_is_an_approver_too() -> None:
    db = Db()
    db.set_validated()
    client, _ = _signed(db, user=BRAYAN)
    assert "Aprobar y publicar" in _visible(_page(client))
