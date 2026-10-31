"""La pagina de aprobacion (src/bridge/flow_approval.py, A′ PR 3a) sobre el manifiesto de ATT1.

La app usa los clientes reales (``ApprovalStore`` y ``SlackIdTokenClient``) y los
dos bordes se emulan con ``httpx.MockTransport``:

* **PostgREST**, con las reglas de las funciones de 20261028000100 que importan
  aca (el state de un uso, la sesion por hash, el 28000). Lo que la base hace de
  verdad lo prueba PGlite (validate_flow_change_slack_approver.mjs). La forma
  del error 28000 (``{"code", "message"}``) es la de PostgREST para los demas
  SQLSTATE capturados el 2026-10-21 (tests/fixtures/automation_expert_change/);
  el 28000 en si no tiene captura.
* **Slack** (``openid.connect.token``): **no hay captura real** del token
  endpoint ni del ``id_token`` de la app del Automation Expert (la app todavia
  no tiene el permiso ``openid``; es el paso 6 del plan de A′). El cuerpo y los
  claims de abajo salen de la documentacion de Slack («Sign in with Slack»,
  OpenID Connect): ``ok``, ``id_token`` y los claims ``iss``, ``aud``, ``exp``,
  ``iat``, ``nonce``, ``sub``, ``https://slack.com/user_id`` y
  ``https://slack.com/team_id``. La captura queda para el E2E del paso 6 (regla 2).

La propuesta que se muestra es la del puente del bridge de la conversacion de
prueba (tests/fixtures/flow_change_new_flow/ae_bienvenida_20261024.json). El
almacen es el de ATT1 leido en solo lectura el 2026-10-22
(tests/fixtures/flow_change/att1_read_flow_store_v1_20261022.json), y el
catalogo de plantillas, el de la bandeja 11 de ATT1 del 2026-10-21: con ellos
corre de verdad la revalidacion al publicar (A′ PR 3b,
tests/test_flow_approval_writes.py). Las escrituras del emulador siguen las
reglas de las funciones de 20261016000100 y 20261028000100 que importan a la
pagina; lo que la base hace de verdad lo prueba PGlite.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge import flow_approval
from bridge.app import Settings, create_app
from bridge.instance_manifest import InstanceManifest

FIXTURES = Path(__file__).parent / "fixtures"
ATT1 = FIXTURES / "instances" / "att1"
BRIDGE = json.loads((FIXTURES / "flow_change_new_flow/ae_bienvenida_20261024.json").read_text())
ATT1_STORE = json.loads((FIXTURES / "flow_change" / "att1_read_flow_store_v1_20261022.json").read_text())["response"]
CATALOG = json.loads((FIXTURES / "flow_change_operator/chatwoot_db_message_templates_inbox_11_20261021.json")
                     .read_text())
INSTANCE = {"tenant_ref": "lancemos", "ally_ref": "att1"}
WELCOME_ID = "0000000b-0000-4000-8000-0000000a7701"
ORIGIN = "https://setter-att1.example.test"
TEAM = "T0LANCEMOS01"
DAN = "U0DANSCHWAB1"
BRAYAN = "U0BRAYANGHL2"
OUTSIDER = "U0NOAPRUEBA3"
CLIENT_ID = "1234567890.987654321"
PROPOSAL = "11111111-2222-4333-8444-555555555555"
RIVAL = "99999999-2222-4333-8444-555555555555"


def _jwt(claims: dict) -> str:
    part = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{part({'alg': 'RS256', 'kid': 'k'})}.{part(claims)}.firma"


DB_KEY = _jwt({"role": "flow_change_approver"})
SERVICE_KEY = _jwt({"role": "service_role"})
SESSION_SECRET = "s" * 48
CLIENT_SECRET = "c" * 32


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class PostgrestEmulator:
    """Las funciones del rol del aprobador, con las reglas que importan a la pagina."""

    def __init__(self) -> None:
        self.states: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        preview = copy.deepcopy(BRIDGE["preview"])
        self.proposal = {
            "proposal_id": PROPOSAL, "status": "validada", "effective_status": "validada", "revision": 1,
            "items": BRIDGE["items"], "purpose": BRIDGE["purpose"], "requested_by_kind": "automation_expert",
            "requested_by_ref": "U0123456789", "validation": {"preview": preview, "result": BRIDGE["result"]},
            "fingerprint": "f" * 64, "validated_at": "2026-10-28T02:00:00Z", "expires_at": "2026-10-29T02:00:00Z",
        }
        self.proposal["draft_base"] = {}
        self.others = [{"proposal_id": RIVAL, "status": "validada", "validated_at": "2026-10-28T01:00:00Z",
                        "expires_at": "2026-10-29T01:00:00Z", "requested_by_kind": "automation_expert",
                        "flows": [{"producto": "att1", "flujo": "bienvenida", "tipo": "nuevo"}]}]
        # El almacen de ATT1 del 22/10: el carrito prendido (generacion 2), los otros dos apagados.
        self.flows: list[dict] = copy.deepcopy(ATT1_STORE["flows"])
        # Lo que contestan las escrituras cuando la prueba quiere otra cosa ({"ok": false, ...}).
        self.approve_result: dict | None = None
        self.publish_result: dict | None = None
        self.cohort_result: dict | None = None
        self.cohort_members = 2
        self.writes: list[tuple[str, str]] = []

    def add_welcome_in_cohort(self) -> None:
        """La bienvenida ya publicada en la cohorte (un flujo nativo), como la deja publicar el nuevo."""
        definition = copy.deepcopy(BRIDGE["items"][0]["definicion"])
        definition.update(version=1, estado="publicada")
        self.flows.append({"flow_id": WELCOME_ID, "product_ref": "att1", "flow_key": "bienvenida", "state": "cohorte",
                           "active_version": 1, "generation": 1, "native": True, "changed_by_kind": "slack",
                           "versions": {"1": definition}})

    @staticmethod
    def _raise(code: str, message: str, status: int = 400) -> httpx.Response:
        return httpx.Response(status, json={"code": code, "details": None, "hint": None, "message": message})

    def _session(self, body: dict) -> dict | None:
        session = self.sessions.get(body.get("p_session_hash"))
        if session is None or session["revoked"]:
            return None
        return session

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {DB_KEY}"
        name = request.url.path.rsplit("/", 1)[1]
        body = json.loads(request.content)
        self.calls.append((name, body))
        if name == "approver_publish_flow_change_v1":
            # La zona de la instancia viaja en p_instance (operator_publish_flow_change_v1).
            assert body["p_instance"] == {**INSTANCE, "time_zone": "America/Mexico_City"}
        else:
            assert body["p_instance"] == INSTANCE
        if name == "begin_flow_approval_oidc_v1":
            self.states[body["p_state_hash"]] = {"nonce_hash": body["p_nonce_hash"],
                                                 "return_path": body["p_return_path"], "consumed": False}
            return httpx.Response(200, json={"ok": True})
        if name == "consume_flow_approval_oidc_v1":
            state = self.states.get(body["p_state_hash"])
            if state is None or state["consumed"]:
                return self._raise("28000", "flow_approval_oidc_state_invalid", 403)
            state["consumed"] = True
            return httpx.Response(200, json={"ok": True, "nonce_hash": state["nonce_hash"],
                                             "return_path": state["return_path"]})
        if name == "create_flow_approval_session_v1":
            state = self.states.get(body["p_state_hash"])
            if state is None or not state["consumed"] or state.get("session"):
                return self._raise("28000", "flow_approval_oidc_state_invalid", 403)
            state["session"] = body["p_session_hash"]
            self.sessions[body["p_session_hash"]] = {"user": body["p_slack_user_id"], "team": body["p_slack_team_id"],
                                                     "revoked": False}
            return httpx.Response(200, json={"ok": True, "return_path": state["return_path"]})
        if name == "revoke_flow_approval_session_v1":
            if body["p_session_hash"] in self.sessions:
                self.sessions[body["p_session_hash"]]["revoked"] = True
            return httpx.Response(200, json={"ok": True})
        session = self._session(body)
        if session is None:
            return self._raise("28000", "flow_approval_session_invalid", 403)
        if name == "validate_flow_approval_session_v1":
            return httpx.Response(200, json={"ok": True, "slack_user_id": session["user"],
                                             "slack_team_id": session["team"]})
        if name == "approver_get_flow_change_proposal_v1":
            if body["p_proposal_id"] != PROPOSAL:
                return self._raise("P0002", "flow_change_not_found", 500)
            return httpx.Response(200, json=self.proposal)
        if name == "approver_read_open_flow_changes_v1":
            rows = self.others if body["p_proposal_id"] else [
                {**self.others[0], "proposal_id": PROPOSAL}, self.others[0]]
            return httpx.Response(200, json={"proposals": rows})
        if name == "approver_read_flow_store_v1":
            return httpx.Response(200, json={"database_at": ATT1_STORE["database_at"], "products": ["att1"],
                                             "flows": copy.deepcopy(self.flows)})
        return self._write(name, body, session["user"])

    def _write(self, name: str, body: dict, user: str) -> httpx.Response:
        """Las escrituras del aprobador, con el actor de la sesion (lo pone la base, nunca el pedido)."""
        proposal = self.proposal
        if name in ("approver_approve_flow_change_v1", "approver_publish_flow_change_v1"):
            if body["p_proposal_id"] != PROPOSAL:
                return self._raise("P0002", "flow_change_not_found", 500)
            if body["p_fingerprint"] != proposal["fingerprint"]:
                return self._raise("22023", "flow_change_fingerprint_mismatch")
        if name == "approver_approve_flow_change_v1":
            if self.approve_result is not None:
                return httpx.Response(200, json=self.approve_result)
            if proposal["status"] == "aprobada":
                return httpx.Response(200, json={"ok": True, "replayed": True})
            if proposal["status"] != "validada":
                return httpx.Response(200, json={"ok": False, "reason_code": "flow_change_not_validated"})
            proposal.update(status="aprobada", effective_status="aprobada", approved_by_kind="slack",
                            approved_by_ref=user)
            self.writes.append((name, user))
            return httpx.Response(200, json={"ok": True, "status": "aprobada"})
        if name == "approver_publish_flow_change_v1":
            if proposal["status"] == "publicada":
                return httpx.Response(200, json={"ok": True, "replayed": True})
            if self.publish_result is not None:
                return httpx.Response(200, json=self.publish_result)
            if proposal["status"] != "aprobada":
                return httpx.Response(200, json={"ok": False, "reason_code": "flow_change_not_approved"})
            revalidation = body["p_revalidation"]
            # Lo que _flow_change_publish exige a la revalidacion (20261016000100).
            if not (sorted(revalidation) == ["catalog_sha256", "fingerprint", "result", "validated_at", "validator"]
                    and revalidation["fingerprint"] == proposal["fingerprint"]
                    and revalidation["validator"]["phase"] == "publish"
                    and revalidation["validator"]["actor"] == proposal["requested_by_kind"]
                    and revalidation["result"]["blocks"] == []
                    and re.fullmatch(r"[0-9a-f]{64}", revalidation["catalog_sha256"])):
                return httpx.Response(200, json={"ok": False, "reason_code": "flow_change_revalidation_blocked"})
            proposal.update(status="publicada", effective_status="publicada")
            self.writes.append((name, user))
            return httpx.Response(200, json={"ok": True, "status": "publicada"})
        if name == "approver_pause_flow_v1":
            flow = next((flow for flow in self.flows if (flow["product_ref"], flow["flow_key"])
                         == (body["p_product_ref"], body["p_flow_key"])), None)
            if flow is None:
                return self._raise("P0002", "flow_not_found", 500)
            if flow["generation"] != body["p_expected_generation"]:
                return httpx.Response(200, json={"ok": False, "reason_code": "flow_control_generation_mismatch",
                                                 "generation": flow["generation"]})
            if flow["state"] == "pausado":
                return httpx.Response(200, json={"ok": True, "changed": False, "generation": flow["generation"]})
            if flow["state"] == "apagado":
                return httpx.Response(200, json={"ok": False, "reason_code": "flow_pause_requires_on"})
            flow.update(state="pausado", generation=flow["generation"] + 1)
            self.writes.append((name, user))
            return httpx.Response(200, json={"ok": True, "changed": True, "generation": flow["generation"]})
        if name == "approver_discard_flow_change_v1":
            if body["p_proposal_id"] != PROPOSAL:
                return self._raise("P0002", "flow_change_not_found", 500)
            if proposal["status"] in ("publicada", "vencida"):
                return httpx.Response(200, json={"ok": False, "reason_code": "flow_change_final"})
            proposal.update(status="descartada", effective_status="descartada", close_reason=body["p_reason"])
            self.writes.append((name, user))
            return httpx.Response(200, json={"ok": True})
        if name == "approver_flow_cohort_v1":
            if self.cohort_result is not None:
                return httpx.Response(200, json=self.cohort_result)
            self.cohort_members += 1
            self.writes.append((name, user))
            return httpx.Response(200, json={"ok": True, "action": "agregar", "changed": True,
                                             "members": self.cohort_members, "max_members": 5})
        raise AssertionError(f"la pagina llamo a una funcion que no conoce: {name}")


class SlackEmulator:
    """``openid.connect.token`` con un ``id_token`` armado de la documentacion (sin captura real)."""

    def __init__(self) -> None:
        self.claims: dict = {}
        self.calls: list[dict] = []
        self.ok = True

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/openid.connect.token"
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        self.calls.append(form)
        if not self.ok:
            return httpx.Response(200, json={"ok": False, "error": "invalid_code"})
        return httpx.Response(200, json={"ok": True, "access_token": "xoxp-no-se-usa", "token_type": "Bearer",
                                         "id_token": _jwt(self.claims)})


def _settings(**overrides: object) -> Settings:
    manifest = InstanceManifest.from_toml_file(ATT1 / "instancia.toml")
    settings = Settings(
        webhook_secret="test-secret", allowed_jid=None,
        capture_dir=Path("/tmp/flow-approval-captures"), max_age_seconds=300,
        commercial_ally_config=manifest.to_commercial_ally_config(),
        commercial_ally_manifest_path=Path("/instancia/instancia.toml"),
        instance_manifest=manifest, hermes_model_name=manifest.agent_model_name, agent_bot_id=2,
        chatwoot_base_url="https://chatwoot.example.org", agent_provenance_scope_ref="att1-agent-bot",
        pilot_boundary_enabled=False, pilot_scope_key="att1-recuperacion", pilot_scope_version=5,
        pilot_precheckout_scope_key="att1-primer-contacto", pilot_precheckout_scope_version=2,
        supabase_base_url="https://att1-rest.internal", supabase_service_role_key=SERVICE_KEY,
        flow_approval_enabled=True, flow_approval_base_url=ORIGIN, flow_approval_slack_team_id=TEAM,
        flow_approval_slack_approvers=f"{DAN},{BRAYAN}", flow_approval_slack_client_id=CLIENT_ID,
        flow_approval_slack_client_secret=CLIENT_SECRET, flow_approval_session_secret=SESSION_SECRET,
        flow_change_approver_db_key=DB_KEY,
    )
    return replace(settings, **overrides)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_761_616_800.0  # 2025-10-28T02:00:00Z


class FakeChatwoot:
    """El catalogo de la bandeja 11 de ATT1 (capturado el 2026-10-21)."""

    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.inboxes: list[int] = []

    async def get_inbox(self, *, inbox_id: int) -> dict:
        self.inboxes.append(inbox_id)
        if self.failure is not None:
            raise self.failure
        return {"id": inbox_id, "channel_type": "Channel::Whatsapp",
                "message_templates": copy.deepcopy(CATALOG["message_templates"])}


class FakeProducer:
    """El productor del conector de Slack: guarda los avisos que admite (o falla, si se le pide)."""

    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.commands: list = []

    async def admit(self, command: object) -> object:
        if self.failure is not None:
            raise self.failure
        self.commands.append(command)
        return object()


class _SlackRuntime:
    def __init__(self, producer: FakeProducer) -> None:
        self.producer = producer

    async def aclose(self) -> None:
        return None


def _client(db: PostgrestEmulator | None = None, *, chatwoot: FakeChatwoot | None = None,
            producer: FakeProducer | None = None,
            **overrides: object) -> tuple[TestClient, PostgrestEmulator, SlackEmulator, _Clock]:
    db = db or PostgrestEmulator()
    slack = SlackEmulator()
    clock = _Clock()
    store = flow_approval.ApprovalStore(base_url="https://att1-rest.internal", db_key=DB_KEY,
                                        transport=httpx.MockTransport(db.handler))
    oidc = flow_approval.SlackIdTokenClient(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                            transport=httpx.MockTransport(slack.handler), clock=lambda: clock.now)
    app = create_app(_settings(**overrides), supabase_client=_NoSupabase(),  # type: ignore[arg-type]
                     chatwoot_client=chatwoot or FakeChatwoot(),  # type: ignore[arg-type]
                     slack_runtime=_SlackRuntime(producer) if producer is not None else None,  # type: ignore[arg-type]
                     flow_approval_store=store, flow_approval_oidc=oidc)
    return TestClient(app, base_url=ORIGIN), db, slack, clock


class _NoSupabase:
    """La pagina nunca usa el cliente de service_role: cualquier llamada falla la prueba."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"la pagina uso el cliente de service_role ({name})")


def _claims(slack: SlackEmulator, clock: _Clock, nonce: str, **overrides: object) -> None:
    slack.claims = {"iss": "https://slack.com", "aud": CLIENT_ID, "exp": int(clock.now) + 3600,
                    "iat": int(clock.now), "auth_time": int(clock.now), "nonce": nonce, "sub": DAN,
                    "https://slack.com/user_id": DAN, "https://slack.com/team_id": TEAM, **overrides}


def _start(client: TestClient, proposal: str | None = PROPOSAL) -> tuple[str, str]:
    response = client.get("/flow-approval/auth/slack/start", params={"p": proposal} if proposal else None,
                          follow_redirects=False)
    assert response.status_code == 303
    target = urlsplit(response.headers["location"])
    assert (target.scheme, target.netloc, target.path) == ("https", "slack.com", "/openid/connect/authorize")
    query = {key: values[0] for key, values in parse_qs(target.query).items()}
    assert query["scope"] == "openid"
    assert query["redirect_uri"] == f"{ORIGIN}/flow-approval/auth/slack/callback"
    assert query["client_id"] == CLIENT_ID
    return query["state"], query["nonce"]


def _sign_in(client: TestClient, slack: SlackEmulator, clock: _Clock, user: str = DAN,
             proposal: str | None = PROPOSAL) -> httpx.Response:
    state, nonce = _start(client, proposal)
    _claims(slack, clock, nonce, sub=user, **{"https://slack.com/user_id": user})
    return client.get("/flow-approval/auth/slack/callback", params={"code": "codigo-de-slack", "state": state},
                      follow_redirects=False)


# ----------------------------------------------------------------- el flag y la configuracion


def test_with_the_flag_off_the_routes_do_not_exist() -> None:
    app = create_app(_settings(flow_approval_enabled=False), supabase_client=_NoSupabase())  # type: ignore[arg-type]
    client = TestClient(app, base_url=ORIGIN)
    for path in ("/flow-approval/", f"/flow-approval/p/{PROPOSAL}", "/flow-approval/auth/slack/start"):
        assert client.get(path, follow_redirects=False).status_code == 404


@pytest.mark.parametrize(("overrides", "message"), [
    ({"instance_manifest": None}, "instance manifest"),
    ({"flow_approval_base_url": "https://setter-att1.example.test/otra"}, "https origin"),
    ({"flow_approval_base_url": "http://setter-att1.example.test"}, "https origin"),
    ({"flow_approval_slack_team_id": "lancemos"}, "FLOW_APPROVAL_SLACK_TEAM_ID"),
    ({"flow_approval_slack_approvers": ""}, "at least one"),
    ({"flow_approval_slack_approvers": f"{DAN},brayan"}, "not a Slack user ID"),
    ({"flow_approval_slack_client_id": ""}, "FLOW_APPROVAL_SLACK_CLIENT_ID"),
    ({"flow_approval_session_secret": "corto"}, "FLOW_APPROVAL_SESSION_SECRET"),
    ({"flow_change_approver_db_key": _jwt({"role": "automation_expert_change"})}, "flow_change_approver"),
    ({"flow_change_approver_db_key": DB_KEY, "supabase_service_role_key": DB_KEY}, "must differ"),
    ({"flow_approval_session_secret": CLIENT_SECRET + "x" * 16,
      "flow_approval_slack_client_secret": CLIENT_SECRET + "x" * 16}, "must differ"),
])
def test_the_flag_refuses_to_start_without_its_configuration(overrides: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        flow_approval.check_settings(_settings(**overrides))


def test_the_configuration_reads_the_instance_from_the_manifest_and_the_approvers_from_the_list() -> None:
    config = flow_approval.check_settings(_settings())
    assert config is not None
    assert config.instance == {"tenant_ref": "lancemos", "ally_ref": "att1"}
    assert config.approvers == frozenset({DAN, BRAYAN})
    assert config.redirect_uri == f"{ORIGIN}/flow-approval/auth/slack/callback"


# ----------------------------------------------------------------- entrar


def test_signing_in_stores_only_hashes_and_lands_on_the_proposal() -> None:
    client, db, slack, clock = _client()
    response = _sign_in(client, slack, clock)
    assert response.status_code == 303
    assert response.headers["location"] == f"/flow-approval/p/{PROPOSAL}"
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(cookie for cookie in cookies if cookie.startswith("__Host-flow_approval_session="))
    for attribute in ("Secure", "HttpOnly", "SameSite=lax", "Path=/", "Max-Age=28800"):
        assert attribute.lower() in session_cookie.lower(), attribute
    secret = session_cookie.split(";", 1)[0].split("=", 1)[1]
    # La base guarda el sha256 de la cookie y del state, nunca los valores.
    begun = next(body for name, body in db.calls if name == "begin_flow_approval_oidc_v1")
    created = next(body for name, body in db.calls if name == "create_flow_approval_session_v1")
    assert created["p_session_hash"] == _sha(secret)
    assert created["p_slack_user_id"] == DAN and created["p_slack_team_id"] == TEAM
    assert begun["p_state_hash"] == created["p_state_hash"]
    assert begun["p_return_path"] == f"/flow-approval/p/{PROPOSAL}"
    assert secret not in json.dumps(db.calls)
    # El code va al endpoint de tokens con el secreto de la app y la URL de vuelta fija.
    assert slack.calls[0]["code"] == "codigo-de-slack"
    assert slack.calls[0]["client_secret"] == CLIENT_SECRET
    assert slack.calls[0]["redirect_uri"] == f"{ORIGIN}/flow-approval/auth/slack/callback"


def test_without_a_proposal_the_sign_in_returns_to_the_list() -> None:
    client, _, slack, clock = _client()
    response = _sign_in(client, slack, clock, proposal=None)
    assert response.headers["location"] == "/flow-approval/"


@pytest.mark.parametrize(("overrides", "reference"), [
    ({"iss": "https://otro.example"}, "OIDC-ISSUER"),
    ({"aud": "otra-app"}, "OIDC-AUDIENCE"),
    ({"exp": 1}, "OIDC-EXPIRED"),
    ({"sub": BRAYAN}, "OIDC-IDENTITY-PAYLOAD"),
    ({"https://slack.com/team_id": "otro"}, "OIDC-IDENTITY-PAYLOAD"),
])
def test_an_id_token_that_does_not_check_out_creates_no_session(overrides: dict, reference: str) -> None:
    client, db, slack, clock = _client()
    state, nonce = _start(client)
    _claims(slack, clock, nonce, **overrides)
    response = client.get("/flow-approval/auth/slack/callback", params={"code": "c", "state": state},
                          follow_redirects=False)
    assert response.status_code == 401
    assert reference in response.text
    assert not any(name == "create_flow_approval_session_v1" for name, _ in db.calls)


def test_a_nonce_that_is_not_the_one_of_the_state_creates_no_session() -> None:
    client, db, slack, clock = _client()
    state, _ = _start(client)
    _claims(slack, clock, "otro-nonce")
    response = client.get("/flow-approval/auth/slack/callback", params={"code": "c", "state": state},
                          follow_redirects=False)
    assert response.status_code == 401 and "OIDC-NONCE" in response.text
    assert not any(name == "create_flow_approval_session_v1" for name, _ in db.calls)


def test_another_workspace_and_someone_outside_the_list_create_no_session() -> None:
    client, db, slack, clock = _client()
    state, nonce = _start(client)
    _claims(slack, clock, nonce, **{"https://slack.com/team_id": "T0OTROWORK99"})
    other = client.get("/flow-approval/auth/slack/callback", params={"code": "c", "state": state},
                       follow_redirects=False)
    assert other.status_code == 403
    outsider = _sign_in(client, slack, clock, user=OUTSIDER)
    assert outsider.status_code == 403
    assert "No estás en la lista de aprobadores de esta instancia" in outsider.text
    assert not any(name == "create_flow_approval_session_v1" for name, _ in db.calls)


def test_the_state_needs_its_cookie_and_is_used_once() -> None:
    client, _, slack, clock = _client()
    state, nonce = _start(client)
    _claims(slack, clock, nonce)
    stolen = TestClient(client.app, base_url=ORIGIN)
    assert stolen.get("/flow-approval/auth/slack/callback", params={"code": "c", "state": state},
                      follow_redirects=False).status_code == 401
    assert client.get("/flow-approval/auth/slack/callback", params={"code": "c", "state": state},
                      follow_redirects=False).status_code == 303
    client.cookies.set("__Host-flow_approval_oidc", state)
    again = client.get("/flow-approval/auth/slack/callback", params={"code": "c", "state": state},
                       follow_redirects=False)
    assert again.status_code == 401


def test_slack_refusing_the_code_shows_a_closed_reference() -> None:
    client, _, slack, clock = _client()
    state, nonce = _start(client)
    slack.ok = False
    response = client.get("/flow-approval/auth/slack/callback", params={"code": "c", "state": state},
                          follow_redirects=False)
    assert response.status_code == 401 and "OIDC-TOKEN-REJECTED" in response.text


# ----------------------------------------------------------------- la propuesta


def test_without_a_session_the_proposal_asks_to_sign_in_with_slack() -> None:
    client, db, _, _ = _client()
    response = client.get(f"/flow-approval/p/{PROPOSAL}")
    assert response.status_code == 401
    assert f"/flow-approval/auth/slack/start?p={PROPOSAL}" in response.text
    assert not any(name.startswith("approver_") for name, _ in db.calls)


def test_the_proposal_shows_its_preview_the_other_open_one_and_the_buttons_of_its_state() -> None:
    client, db, slack, clock = _client()
    _sign_in(client, slack, clock)
    db.proposal["validation"]["preview"]["para_que"] = "<script>alert(1)</script> bienvenida"
    response = client.get(f"/flow-approval/p/{PROPOSAL}")
    assert response.status_code == 200
    text = response.text
    # El para que, escapado y marcado como texto sin verificar.
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt; bienvenida" in text
    assert "sin verificar" in text
    # A quien llega, el diff, la simulacion, como se vuelve atras.
    assert "solo la cohorte de prueba" in text
    assert "att1_pago_efectivo_confirmado_01" in text
    assert "Estimación con los eventos reales" in text
    assert "se pausa o se apaga" in text
    # El aviso de la otra propuesta abierta del mismo flujo.
    assert "Hay otra propuesta abierta de este flujo" in text
    assert "la otra va a tener que validarse de nuevo" in text
    # Validada, con simulacion: aprobar y publicar, y descartar (A′ PR 3b). Mirar no escribe nada.
    forms = re.findall(r'<form[^>]*action="([^"]+)"', text)
    assert forms == ["/flow-approval/logout", f"/flow-approval/p/{PROPOSAL}/publish",
                     f"/flow-approval/p/{PROPOSAL}/discard"]
    assert re.findall(r"<button[^>]*>([^<]*)</button>", text) == ["Salir", "Aprobar y publicar", "Descartar"]
    assert f'name="fingerprint" value="{"f" * 64}"' in text
    # Cada pedido valida la sesion en la base, y nunca llama a una funcion que escribe.
    names = [name for name, _ in db.calls]
    assert names.count("validate_flow_approval_session_v1") == 1
    assert not set(names) & {"approver_approve_flow_change_v1", "approver_publish_flow_change_v1",
                             "approver_pause_flow_v1", "approver_discard_flow_change_v1",
                             "approver_flow_cohort_v1"}


def test_the_page_headers_are_those_of_the_daily_review() -> None:
    client, _, slack, clock = _client()
    _sign_in(client, slack, clock)
    response = client.get(f"/flow-approval/p/{PROPOSAL}")
    assert response.headers["cache-control"].startswith("no-store")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "script-src" not in response.headers["content-security-policy"]
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_an_expired_proposal_says_so_and_how_to_continue() -> None:
    client, db, slack, clock = _client()
    _sign_in(client, slack, clock)
    db.proposal["effective_status"] = "vencida"
    assert "Esta propuesta venció" in client.get(f"/flow-approval/p/{PROPOSAL}").text


def test_a_proposal_of_another_instance_or_a_bad_id_is_not_found() -> None:
    client, _, slack, clock = _client()
    _sign_in(client, slack, clock)
    assert client.get(f"/flow-approval/p/{RIVAL}").status_code == 404
    assert client.get("/flow-approval/p/no-es-un-uuid").status_code == 404


def test_a_session_the_base_does_not_accept_goes_back_to_sign_in_and_drops_the_cookie() -> None:
    client, db, slack, clock = _client()
    _sign_in(client, slack, clock)
    for session in db.sessions.values():
        session["revoked"] = True
    response = client.get(f"/flow-approval/p/{PROPOSAL}")
    assert response.status_code == 401
    assert any(cookie.startswith("__Host-flow_approval_session=") and "max-age=0" in cookie.lower()
               for cookie in response.headers.get_list("set-cookie"))


def test_taking_someone_off_the_list_cuts_access_on_the_next_request() -> None:
    client, db, slack, clock = _client()
    _sign_in(client, slack, clock, user=BRAYAN)
    assert client.get(f"/flow-approval/p/{PROPOSAL}").status_code == 200
    # La misma base y la misma cookie, con la lista sin Brayan (otro deploy de la configuracion).
    shortened, _, _, _ = _client(db=db, flow_approval_slack_approvers=DAN)
    shortened.cookies = client.cookies
    assert shortened.get(f"/flow-approval/p/{PROPOSAL}").status_code == 401


def test_the_list_shows_the_flows_and_the_proposals_waiting() -> None:
    db = PostgrestEmulator()
    db.add_welcome_in_cohort()
    client, _, slack, clock = _client(db)
    _sign_in(client, slack, clock, proposal=None)
    text = client.get("/flow-approval/").text
    assert "att1/carrito" in text and "att1/bienvenida" in text and "cohorte" in text
    assert f'href="/flow-approval/p/{PROPOSAL}"' in text
    # Pausar lo que llega a alguien (el carrito prendido y la bienvenida en cohorte); la cohorte, solo
    # en la bienvenida. Lo apagado no se pausa.
    assert re.findall(r"<button[^>]*>([^<]*)</button>", text) == [
        "Salir", "Pausar att1/carrito", "Pausar att1/bienvenida", "Sumar a la cohorte"]


# ----------------------------------------------------------------- salir


def test_logging_out_needs_the_csrf_token_and_the_origin_and_revokes() -> None:
    client, db, slack, clock = _client()
    _sign_in(client, slack, clock)
    page = client.get(f"/flow-approval/p/{PROPOSAL}").text
    token = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', page).group(1)
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    assert client.post("/flow-approval/logout", content="csrf_token=" + "0" * 64,
                       headers={**form, "Origin": ORIGIN}).status_code == 403
    assert client.post("/flow-approval/logout", content=f"csrf_token={token}",
                       headers={**form, "Origin": "https://otro.example"}).status_code == 403
    response = client.post("/flow-approval/logout", content=f"csrf_token={token}",
                           headers={**form, "Origin": ORIGIN}, follow_redirects=False)
    assert response.status_code == 303
    assert any(name == "revoke_flow_approval_session_v1" for name, _ in db.calls)
    assert client.get(f"/flow-approval/p/{PROPOSAL}").status_code == 401


# ----------------------------------------------------------------- el cliente de la base


def test_the_store_maps_28000_to_an_invalid_session_and_unknown_errors_to_unavailable() -> None:
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[1]
        if name == "validate_flow_approval_session_v1":
            return httpx.Response(403, json={"code": "28000", "message": "flow_approval_session_invalid"})
        return httpx.Response(500, json={"code": "XX000", "message": "algo con texto de Postgres"})

    store = flow_approval.ApprovalStore(base_url="https://att1-rest.internal", db_key=DB_KEY,
                                        transport=httpx.MockTransport(handler))
    instance = {"tenant_ref": "lancemos", "ally_ref": "att1"}
    with pytest.raises(flow_approval.SessionInvalid):
        asyncio.run(store.validate_session(instance, session_hash="a" * 64))
    with pytest.raises(flow_approval.StoreUnavailable):
        asyncio.run(store.read_store(instance, session_hash="a" * 64))


def test_the_id_token_check_is_strict_about_time() -> None:
    clock = _Clock()
    client = flow_approval.SlackIdTokenClient(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                              clock=lambda: clock.now)
    claims = {"iss": "https://slack.com", "aud": CLIENT_ID, "exp": int(clock.now) + 10, "iat": int(clock.now),
              "nonce": "n", "sub": DAN, "https://slack.com/user_id": DAN, "https://slack.com/team_id": TEAM}
    assert client.identity_from_claims(claims).user_id == DAN
    with pytest.raises(flow_approval.SlackOidcError, match="OIDC-EXPIRED"):
        client.identity_from_claims({**claims, "iat": int(clock.now) + 3600})
    with pytest.raises(flow_approval.SlackOidcError, match="OIDC-NONCE"):
        client.identity_from_claims({**claims, "nonce": ""})
    with pytest.raises(flow_approval.SlackOidcError, match="OIDC-AUDIENCE"):
        client.identity_from_claims({**claims, "aud": [CLIENT_ID, "otra"], "azp": "otra"})
    assert datetime.fromtimestamp(clock.now, tz=UTC).year == 2025


def test_the_proposal_opens_with_a_business_summary_and_folds_the_technical_detail() -> None:
    """E-232: lo primero que lee el duenio es el resumen de negocio; el diff campo por campo va plegado."""
    client, _, slack, clock = _client()
    _sign_in(client, slack, clock)
    text = client.get(f"/flow-approval/p/{PROPOSAL}").text
    # El titulo y el resumen: que flujo, que cambia, cuando arranca, a quien llega.
    assert "<h1>Flujo nuevo: Bienvenida despues de la compra</h1>" in text
    assert "cuando alguien compra" in text and "nace en la cohorte de prueba" in text
    assert "pedida por el agente (Automation Expert)" in text
    # El mensaje como lo ve la persona, con la plantilla traducida.
    assert "Hola, [nombre]. Se confirmó tu pago de [producto]." in text
    assert "aprobada por Meta" in text and "Le pone la etiqueta «cliente»" in text
    assert "se habría disparado <strong>2</strong> veces, para <strong>2</strong> personas" in text
    # El diff tecnico existe, pero dentro de un <details> cerrado, despues del resumen.
    summary_at, details_at = text.index("Resumen"), text.index('<details class="tech">')
    assert summary_at < details_at
    assert '<details class="tech" open' not in text
    assert text.index("<code>/dispara</code>") > details_at
    # La estetica es la de la revision diaria: los mismos tokens y la misma barra.
    from bridge.review_theme import REVIEW_SHELL_CSS
    assert REVIEW_SHELL_CSS in text and 'class="app-shell"' in text
