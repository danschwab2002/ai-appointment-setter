"""Los frenos ante Meta (meta-enforcement-brakes-v1, F1 del mapa del 2026-11-27).

Decisiones D2 a D6 del mapa y D-F1.1 a D-F1.11 del modulo.

Datos:

* Los webhooks son los de la documentacion oficial de Meta, **documentados y no
  capturados** (H2: hoy no llega ninguno): ``tests/fixtures/meta_webhooks/
  template_category_update_documentado_meta_20261127.json`` y
  ``account_update_documentado_meta_20261127.json`` (las URLs van en su ``_que_es``).
  La cuenta de WhatsApp de las pruebas es el id de esos ejemplos.
* El catalogo de la bandeja es el capturado de ATT1 (``FakeChatwoot`` de
  ``test_flow_approval_page``) y el de Maribel reconstruido del carrito nativo
  (``test_flow_engine_native_cart``), que es el caso H6.
* La forma del estado es la de ``_meta_enforcement_state`` (migracion
  20261127100000); lo que la base hace de verdad lo prueba PGlite
  (``validate_meta_enforcement_brakes.mjs``).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_flow_approval_page import BRAYAN, DAN, ORIGIN, OUTSIDER, FakeChatwoot, _NoSupabase
from test_agent_global_pause import AE_KEY
from test_flow_approval_page import _settings as _approval_settings

from bridge import app as app_module
from bridge import meta_enforcement
from bridge.approved_templates import BRAKE_KEY, BRAKED_STATUS, ApprovedTemplateError, parse_approved_template
from bridge.app import Settings, create_app
from bridge.chatwoot import ChatwootClient, ChatwootProtocolError
from bridge.meta_enforcement import (
    REASON_ACCOUNT_RESTRICTED,
    REASON_RECATEGORIZED,
    REASON_STREAK,
    EnforcementStore,
    LiveEnforcement,
    MetaEnforcementWorker,
    notice_text,
    parse_state,
    parse_webhook,
    signature_ok,
)
from bridge.meta_templates import MetaTemplatesClient, MetaTemplatesError
from slack_correlation.client import SlackProtocolError, SlackRejectedError

FIXTURES = Path(__file__).parent / "fixtures" / "meta_webhooks"
CATEGORY = json.loads((FIXTURES / "template_category_update_documentado_meta_20261127.json").read_text("utf-8"))
ACCOUNT = json.loads((FIXTURES / "account_update_documentado_meta_20261127.json").read_text("utf-8"))
WABA = "102290129340398"
NOW = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
INSTANCE = {"tenant_ref": "lancemos", "ally_ref": "att1"}
PREFIX = "/internal/automation-expert/v1"
TOKEN = "c" * 48
AUTH = {"Authorization": f"Bearer {TOKEN}"}
VERIFY = "verify-token-de-prueba"
SECRET = "app-secret-de-prueba"


def _state(*, retired: list[tuple[str, str]] = (), frozen: bool = False, streak: int = 0) -> dict[str, Any]:
    return {"frozen": {"freeze_id": "f", "source": "webhook", "violation_type": "UTILITY_TEMPLATE_ABUSE",
                       "frozen_at": "2026-11-27T15:00:00Z"} if frozen else None,
            "retired": [{"template_name": n, "language": lang, "from": "UTILITY", "to": "MARKETING",
                         "retired_at": "2026-11-27T15:00:00Z"} for n, lang in retired],
            "streak": streak, "last_seen_at": "2026-11-27T14:45:00Z"}


def _live(state: dict[str, Any] | None = None) -> LiveEnforcement:
    live = LiveEnforcement(fetch=lambda: asyncio.sleep(0, result=state or _state()))
    if state is not None:
        assert live.apply(state)
    return live


def _signed(body: bytes) -> dict[str, str]:
    return {"X-Hub-Signature-256": "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(),
            "Content-Type": "application/json"}


# ------------------------------------------------------------------ el webhook leido


def test_the_documented_category_payloads_are_read_with_the_catalog_language() -> None:
    previous = parse_webhook(CATEGORY["aviso_previo"], waba_id=WABA, now=NOW)
    done = parse_webhook(CATEGORY["hecho"], waba_id=WABA, now=NOW)
    [warning] = previous.categories
    [change] = done.categories
    # D-F1.8: el idioma viene como en-US y el catalogo lo guarda como en_US.
    assert warning == {"meta_template_id": "278077987957091", "name": "welcome_template", "language": "en_US",
                       "new_category": "UTILITY", "occurred_at": "2025-05-01T07:00:00Z", "kind": "aviso_previo",
                       "correct_category": "MARKETING", "effective_at": "2025-05-02T07:00:00Z"}
    assert change["kind"] == "hecho" and change["previous_category"] == "UTILITY" and "correct_category" not in change


@pytest.mark.parametrize("name,violation,types", [
    ("advertencia", "UTILITY_TEMPLATE_ABUSE", []),
    ("suspension_utilidad", "UTILITY_TEMPLATE_ABUSE", ["RESTRICTED_UTILITY_TEMPLATES"]),
    ("unban", "UTILITY_TEMPLATE_ABUSE_UNBAN", []),
    ("limite_utilidad", "UTILITY_TEMPLATE_ABUSE_RATE_LIMIT", ["RATE_LIMITED_UTILITY_TEMPLATE_MESSAGING"]),
    ("recuperacion_limite", "UTILITY_TEMPLATE_ABUSE_RATE_LIMIT_RECOVERY", []),
    ("restriccion_generica", None, ["RESTRICTED_ADD_PHONE_NUMBER_ACTION", "RESTRICTED_BIZ_INITIATED_MESSAGING"]),
])
def test_the_documented_account_payloads_are_read(name: str, violation: str | None, types: list[str]) -> None:
    [event] = parse_webhook(ACCOUNT[name], waba_id=WABA, now=NOW).accounts
    assert event["event"] == "ACCOUNT_RESTRICTION" and event["violation_type"] == violation
    assert sorted(event["restriction_types"]) == sorted(types)


def test_another_account_and_other_fields_are_not_read() -> None:
    other = parse_webhook(CATEGORY["hecho"], waba_id="999999999999", now=NOW)
    assert other.categories == [] and other.other_account == 1
    messages = {"object": "whatsapp_business_account", "entry": [{"id": WABA, "changes": [
        {"field": "messages", "value": {"messages": []}},
        {"field": "account_update", "value": {"event": "PARTNER_ADDED"}}]}]}
    read = parse_webhook(messages, waba_id=WABA, now=NOW)
    assert read.categories == [] and read.accounts == [] and read.ignored == 2
    assert parse_webhook({"object": "page"}, waba_id=WABA, now=NOW).invalid == 1


def test_the_signature_is_the_hmac_of_the_raw_body() -> None:
    body = json.dumps(CATEGORY["hecho"]).encode()
    header = _signed(body)["X-Hub-Signature-256"]
    assert signature_ok(SECRET, body, header)
    assert not signature_ok(SECRET, body + b" ", header)
    assert not signature_ok("otro", body, header)
    assert not signature_ok(SECRET, body, None)
    assert not signature_ok(SECRET, body, header.replace("sha256=", "sha1="))
    assert not signature_ok("", body, header)


# ------------------------------------------------------------------ el freno en el catalogo


def _catalog() -> dict[str, Any]:
    return asyncio.run(FakeChatwoot().get_inbox(inbox_id=11))


def _first_approved(catalog: dict[str, Any]) -> dict[str, Any]:
    return next(t for t in catalog["message_templates"] if t.get("status") == "APPROVED")


def test_a_retired_template_reads_as_not_approved_with_its_own_reason() -> None:
    catalog = _catalog()
    target = _first_approved(catalog)
    live = _live(_state(retired=[(target["name"], target["language"])]))
    braked = live.braked_catalog(catalog)
    [marked] = [t for t in braked["message_templates"] if t["name"] == target["name"]
                and t["language"] == target["language"]]
    assert marked["status"] == BRAKED_STATUS and marked[BRAKE_KEY] == REASON_RECATEGORIZED
    # Las demas quedan iguales, y el catalogo original no se toca.
    others = [t for t in braked["message_templates"] if t is not marked]
    assert all(BRAKE_KEY not in t for t in others)
    assert _first_approved(catalog)["status"] == "APPROVED" and BRAKE_KEY not in target
    with pytest.raises(ApprovedTemplateError) as error:
        parse_approved_template(braked, template_name=target["name"], expected_language=target["language"],
                                expected_category=target["category"], parameter_count=1)
    assert error.value.reason == REASON_RECATEGORIZED


def test_a_frozen_instance_brakes_every_template_only_for_what_the_bridge_starts() -> None:
    catalog = _catalog()
    live = _live(_state(frozen=True))
    braked = live.braked_catalog(catalog)
    assert all(t.get(BRAKE_KEY) == REASON_ACCOUNT_RESTRICTED for t in braked["message_templates"])
    # El link del agente en una conversacion abierta no lo frena la restriccion.
    assert live.retired_catalog(catalog) == catalog
    assert live.initiated_brake("cualquiera", "es_MX") == REASON_ACCOUNT_RESTRICTED


def test_without_brakes_the_catalog_is_the_same_object() -> None:
    catalog = _catalog()
    assert _live(_state()).braked_catalog(catalog) is catalog
    assert _live().braked_catalog(catalog) is catalog


def test_the_streak_brakes_utility_requests_from_two() -> None:
    assert _live(_state(streak=1)).utility_request_brake() is None
    assert _live(_state(streak=2)).utility_request_brake() == REASON_STREAK


def test_the_state_reading_is_strict_and_keeps_the_last_good_one() -> None:
    live = _live(_state(retired=[("a_b", "es_MX")]))
    assert not live.apply({"retired": "x", "streak": 0})
    assert live.initiated_brake("a_b", "es_MX") == REASON_RECATEGORIZED
    with pytest.raises(ValueError):
        parse_state({"retired": [], "frozen": None, "streak": -1})


def test_without_any_good_reading_nothing_is_braked_and_ready_says_so() -> None:
    async def down() -> dict[str, Any]:
        raise OSError("caida")

    live = LiveEnforcement(fetch=down)
    assert asyncio.run(live.refresh()) is False
    assert live.initiated_brake("x_y", "es_MX") is None
    assert "refresh=never_read" in live.readiness()


def test_the_chatwoot_copy_filters_the_inbox_and_stops_a_braked_template() -> None:
    catalog = _catalog()
    target = _first_approved(catalog)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.method == "GET":
            return httpx.Response(200, json=catalog)
        return httpx.Response(200, json={"id": 1})

    client = ChatwootClient(base_url="https://chatwoot.example.org", account_id=2, access_token="t",
                            agent_bot_access_token="b", agent_bot_id=2, transport=httpx.MockTransport(handler))
    live = _live(_state(retired=[(target["name"], target["language"])]))
    braked = client.with_catalog_filter(live.braked_catalog, template_brake=live.initiated_brake)
    inbox = asyncio.run(braked.get_inbox(inbox_id=11))
    assert any(t.get(BRAKE_KEY) for t in inbox["message_templates"])
    assert BRAKE_KEY not in json.dumps(asyncio.run(client.get_inbox(inbox_id=11)))
    with pytest.raises(ChatwootProtocolError, match="meta_enforcement:template_recategorized"):
        asyncio.run(braked.send_first_message(conversation_id=7, content="x", delivery_id="d",
                                              template_params={"name": target["name"],
                                                               "language": target["language"]}))
    assert all(path.endswith("/inboxes/11") for path in calls)


# ------------------------------------------------------------------ el motor nativo (H6)


def test_a_retired_marketing_template_of_a_native_formulario_step_does_not_go(tmp_path: Path) -> None:
    import test_flow_engine_native_cart as cart

    retired = _live(_state(retired=[(cart.NAME, "es_MX")]))

    class _Braked(cart._MaribelChatwoot):
        # El mismo cliente que arma app.py con el flag: el despachador y el que manda.
        def client(self) -> ChatwootClient:
            return super().client().with_catalog_filter(retired.braked_catalog, template_brake=retired.initiated_brake)

    authority = cart._CartAuthority("formulario")
    chatwoot = _Braked(cart.MARIBEL)
    asyncio.run(cart._dispatcher(authority, chatwoot, tmp_path).dispatch_due(now=cart.NOW))
    assert cart._templates(chatwoot) == []
    assert authority.finalizations[0]["reason_code"] == REASON_RECATEGORIZED


def test_a_frozen_instance_stops_the_native_step_with_its_reason(tmp_path: Path) -> None:
    import test_flow_engine_native_cart as cart

    frozen = _live(_state(frozen=True))

    class _Frozen(cart._MaribelChatwoot):
        # El mismo cliente que arma app.py con el flag: el despachador y el que manda.
        def client(self) -> ChatwootClient:
            return super().client().with_catalog_filter(frozen.braked_catalog, template_brake=frozen.initiated_brake)

    authority = cart._CartAuthority("formulario")
    chatwoot = _Frozen(cart.MARIBEL)
    asyncio.run(cart._dispatcher(authority, chatwoot, tmp_path).dispatch_due(now=cart.NOW))
    assert cart._templates(chatwoot) == []
    assert authority.finalizations[0]["reason_code"] == REASON_ACCOUNT_RESTRICTED


# ------------------------------------------------------------------ los avisos


@pytest.mark.parametrize("kind,data,words", [
    ("recategorizacion_previa", {"template_name": "att1_pago_01", "language": "es_MX", "from": "UTILITY",
                                 "to": "MARKETING"}, ["va a cambiar", "de utilidad", "de marketing"]),
    ("recategorizacion_hecha", {"template_name": "att1_pago_01", "language": "es_MX", "from": "UTILITY",
                                "to": "MARKETING"}, ["dejó de mandarla", "masivo", "60 días"]),
    ("contador_dos", {"streak": 2}, ["2 plantillas", "frena los pedidos nuevos"]),
    ("congelamiento", {"source": "webhook", "violation_type": "UTILITY_TEMPLATE_ABUSE"},
     ["restringió", "sigue contestando", "No se levanta solo"]),
    ("congelamiento", {"source": "manual"}, ["a mano"]),
    ("restriccion_levantada_por_meta", {"violation_type": "UTILITY_TEMPLATE_ABUSE_UNBAN"},
     ["no se descongela solo"]),
])
def test_each_notice_has_a_fixed_text(kind: str, data: dict[str, Any], words: list[str]) -> None:
    text = notice_text(kind, data)
    assert all(word in text for word in words), text


def test_a_notice_that_does_not_close_is_refused() -> None:
    with pytest.raises(ValueError):
        notice_text("recategorizacion_hecha", {"template_name": "Mal Nombre", "from": "UTILITY", "to": "MARKETING"})
    with pytest.raises(ValueError):
        notice_text("contador_dos", {"streak": 1})
    with pytest.raises(ValueError):
        notice_text("otro", {})


# ------------------------------------------------------------------ el sondeo y los avisos


class FakeStore(EnforcementStore):
    def __init__(self, *, notices: list[dict[str, Any]] | None = None, down: bool = False) -> None:
        super().__init__(base_url="https://x", db_key="k")
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.state = _state()
        self.notices = notices or []
        self.down = down

    async def _rpc(self, function: str, payload: Any, *, list_ok: bool = False) -> Any:
        self.calls.append((function, dict(payload)))
        if self.down:
            raise meta_enforcement.StoreUnavailable()
        if function == "record_meta_template_categories_v1":
            retired = [(t["name"], t["language"]) for t in payload["p_templates"] if t["category"] == "MARKETING"]
            self.state = _state(retired=retired)
            return {"ok": True, "baseline": False, "counts": {"retirada": len(retired)}, "state": self.state}
        if function in ("record_meta_template_category_webhook_v1", "record_meta_account_event_v1"):
            event = payload["p_event"]
            if function == "record_meta_account_event_v1" and event["violation_type"] not in \
                    meta_enforcement.NOTICE_ONLY_VIOLATIONS:
                self.state = {**self.state, "frozen": _state(frozen=True)["frozen"]}
            elif function == "record_meta_template_category_webhook_v1" and event["kind"] == "hecho":
                self.state = _state(retired=[(event["name"], event["language"])])
            return {"ok": True, "outcome": "registrado", "state": self.state}
        if function == "get_meta_enforcement_state_v1":
            return self.state
        if function == "act_on_meta_enforcement_v1":
            if payload["p_action"] == "congelar":
                self.state = {**self.state, "frozen": _state(frozen=True)["frozen"]}
            elif payload["p_action"] == "descongelar":
                self.state = {**self.state, "frozen": None}
            return {"ok": True, "replayed": False, "state": self.state}
        if function == "claim_meta_enforcement_notices_v1":
            claimed, self.notices = self.notices, []
            return {"notices": claimed, "counts": {"reclamado": len(claimed)}}
        if function == "finish_meta_enforcement_notice_v1":
            return {"ok": True}
        raise AssertionError(function)


class FakeMeta:
    def __init__(self, rows: list[dict[str, Any]] | Exception) -> None:
        self.rows = rows

    async def list_categories(self) -> list[dict[str, Any]]:
        if isinstance(self.rows, Exception):
            raise self.rows
        return list(self.rows)


class FakeSlack:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def post_message(self, *, channel_id: str, message: dict[str, Any]) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        self.posts.append((channel_id, message))
        return {"ok": True}


def _worker(store: FakeStore, meta: FakeMeta, slack: FakeSlack | None, *, channel: str | None = "C0ALIADA01",
            live: LiveEnforcement | None = None) -> tuple[MetaEnforcementWorker, LiveEnforcement]:
    live = live or _live(_state())
    worker = MetaEnforcementWorker(store=store, meta=meta, live=live, instance=INSTANCE, waba_id=WABA,  # type: ignore[arg-type]
                                   slack=slack, channel=channel, clock=lambda: NOW)  # type: ignore[arg-type]
    return worker, live


def test_the_poll_sends_every_template_and_applies_the_state_at_once() -> None:
    store = FakeStore()
    rows = [{"id": "1", "name": "att1_pago_01", "language": "es-MX", "category": "MARKETING",
             "correct_category": None},
            {"id": "2", "name": "att1_bienvenida_02", "language": "es_MX", "category": "UTILITY",
             "correct_category": "MARKETING"}]
    worker, live = _worker(store, FakeMeta(rows), FakeSlack())
    asyncio.run(worker.run_once())
    [(function, payload)] = [call for call in store.calls if call[0] == "record_meta_template_categories_v1"]
    assert payload["p_waba_id"] == WABA and payload["p_instance"] == INSTANCE
    assert [t["language"] for t in payload["p_templates"]] == ["es_MX", "es_MX"]
    assert live.initiated_brake("att1_pago_01", "es_MX") == REASON_RECATEGORIZED
    assert worker.state == "ok" and "polled=2" in worker.last_summary


def test_the_poll_does_not_compare_a_partial_list_and_says_why() -> None:
    store = FakeStore()
    worker, _ = _worker(store, FakeMeta(MetaTemplatesError("meta:190")), FakeSlack())
    asyncio.run(worker.run_once())
    assert not any(call[0] == "record_meta_template_categories_v1" for call in store.calls)
    assert worker.state == "degraded" and worker.last_error == "meta:190"


def test_the_poll_waits_fifteen_minutes() -> None:
    store = FakeStore()
    clock = [NOW]
    worker = MetaEnforcementWorker(store=store, meta=FakeMeta([]), live=_live(_state()), instance=INSTANCE,  # type: ignore[arg-type]
                                   waba_id=WABA, slack=None, channel=None, clock=lambda: clock[0])
    asyncio.run(worker.run_once())
    clock[0] = NOW + timedelta(minutes=14)
    asyncio.run(worker.run_once())
    clock[0] = NOW + timedelta(minutes=15)
    asyncio.run(worker.run_once())
    assert [c[0] for c in store.calls].count("record_meta_template_categories_v1") == 2


def test_notices_go_to_the_ally_channel_and_each_outcome_is_closed() -> None:
    notice = {"notice_id": "0000000a-0000-4000-8000-0000000f1001", "kind": "contador_dos", "data": {"streak": 2}}
    store = FakeStore(notices=[notice])
    slack = FakeSlack()
    worker, _ = _worker(store, FakeMeta([]), slack)
    asyncio.run(worker.notify())
    [(channel, message)] = slack.posts
    assert channel == "C0ALIADA01" and "2 plantillas" in message["text"]
    assert store.calls[-1] == ("finish_meta_enforcement_notice_v1", {
        "p_instance": INSTANCE, "p_notice_id": notice["notice_id"], "p_outcome": "sent", "p_error_code": None})


@pytest.mark.parametrize("error,outcome", [
    (SlackRejectedError("x", slack_error="not_in_channel"), "waiting"),
    (SlackRejectedError("x", slack_error="ratelimited"), "retry"),
    (SlackProtocolError("timeout"), "unknown"),
])
def test_a_slack_failure_closes_the_notice_with_its_outcome(error: Exception, outcome: str) -> None:
    store = FakeStore(notices=[{"notice_id": "n", "kind": "contador_dos", "data": {"streak": 2}}])
    worker, _ = _worker(store, FakeMeta([]), FakeSlack(error))
    asyncio.run(worker.notify())
    assert store.calls[-1][1]["p_outcome"] == outcome


def test_without_a_channel_or_a_token_the_notices_wait_in_the_base() -> None:
    store = FakeStore(notices=[{"notice_id": "n", "kind": "contador_dos", "data": {"streak": 2}}])
    worker, _ = _worker(store, FakeMeta([]), FakeSlack(), channel=None)
    asyncio.run(worker.notify())
    assert store.calls == [] and worker.notices_state == "sin_canal"
    worker, _ = _worker(store, FakeMeta([]), None)
    assert worker.notices_state == "sin_token"


# ------------------------------------------------------------------ la app


class _Base(_NoSupabase):
    async def resolve_commercial_ally_runtime_binding(self, config: object) -> object:
        return config

    async def get_human_handoff_projection_status(self) -> object:
        return SimpleNamespace(pending_count=0, retryable_count=0, delivery_unknown_count=0,
                               conflict_count=0, dead_letter_count=0)


ON: dict[str, Any] = {
    "meta_enforcement_brakes_enabled": True,
    "meta_templates_access_token": "EAAG-token-de-prueba",
    "meta_waba_id": WABA,
    "meta_webhook_verify_token": VERIFY,
    "meta_app_secret": SECRET,
    "automation_expert_change_enabled": True,
    "automation_expert_change_token": TOKEN,
    "automation_expert_change_db_key": AE_KEY,
    "automation_expert_read_enabled": True, "automation_expert_read_token": "r" * 48,
    "chatwoot_account_id": 2, "chatwoot_inbox_id": 11,
}


def _app(store: FakeStore | None = None, **overrides: Any) -> tuple[TestClient, FakeStore]:
    store = store or FakeStore()
    settings = _approval_settings(**{**ON, **overrides})
    original = meta_enforcement.EnforcementStore

    class _Store(EnforcementStore):
        async def _rpc(self, function: str, payload: Any, *, list_ok: bool = False) -> Any:
            return await store._rpc(function, payload)

    meta_enforcement.EnforcementStore = _Store  # type: ignore[misc]
    try:
        app = create_app(settings, supabase_client=_Base(),  # type: ignore[arg-type]
                         chatwoot_client=FakeChatwoot(),  # type: ignore[arg-type]
                         meta_templates_client=FakeMeta([]))  # type: ignore[arg-type]
    finally:
        meta_enforcement.EnforcementStore = original  # type: ignore[misc]
    client = TestClient(app, base_url=ORIGIN)
    client.__enter__()
    return client, store


def test_the_webhook_verification_answers_only_the_right_token() -> None:
    client, _ = _app()
    ok = client.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY,
                                              "hub.challenge": "1158201444"})
    assert ok.status_code == 200 and ok.text == "1158201444"
    for params in ({"hub.mode": "subscribe", "hub.verify_token": "otro", "hub.challenge": "1"},
                   {"hub.mode": "unsubscribe", "hub.verify_token": VERIFY, "hub.challenge": "1"},
                   {"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "<script>"}):
        assert client.get("/webhooks/meta", params=params).status_code == 403
    client.__exit__(None, None, None)


def test_a_post_without_a_valid_signature_reads_nothing() -> None:
    client, store = _app()
    body = json.dumps(ACCOUNT["suspension_utilidad"]).encode()
    assert client.post("/webhooks/meta", content=body, headers={"Content-Type": "application/json"}).status_code == 401
    bad = {**_signed(body), "X-Hub-Signature-256": "sha256=" + "0" * 64}
    assert client.post("/webhooks/meta", content=body, headers=bad).status_code == 401
    assert not any(c[0] == "record_meta_account_event_v1" for c in store.calls)
    client.__exit__(None, None, None)


def test_a_signed_restriction_freezes_the_bridge_at_once_and_ready_says_it() -> None:
    client, store = _app()
    assert "frozen=no" in client.get("/ready").json()["meta_enforcement"]
    body = json.dumps(ACCOUNT["suspension_utilidad"]).encode()
    response = client.post("/webhooks/meta", content=body, headers=_signed(body))
    assert response.status_code == 200 and response.json()["accounts"] == 1
    [(_, payload)] = [c for c in store.calls if c[0] == "record_meta_account_event_v1"]
    assert payload["p_waba_id"] == WABA and payload["p_event"]["violation_type"] == "UTILITY_TEMPLATE_ABUSE"
    ready = client.get("/ready").json()["meta_enforcement"]
    assert "frozen=yes" in ready and "webhook=on" in ready and "manual=on" in ready
    client.__exit__(None, None, None)


def test_a_category_change_of_another_account_is_dropped() -> None:
    client, store = _app()
    payload = json.loads(json.dumps(CATEGORY["hecho"]))
    payload["entry"][0]["id"] = "999999999999"
    body = json.dumps(payload).encode()
    response = client.post("/webhooks/meta", content=body, headers=_signed(body))
    assert response.status_code == 200 and response.json()["other_account"] == 1
    assert not any(c[0] == "record_meta_template_category_webhook_v1" for c in store.calls)
    client.__exit__(None, None, None)


def test_when_the_base_does_not_answer_meta_retries() -> None:
    client, store = _app()
    store.down = True
    body = json.dumps(CATEGORY["hecho"]).encode()
    assert client.post("/webhooks/meta", content=body, headers=_signed(body)).status_code == 503
    client.__exit__(None, None, None)


def _act(client: TestClient, body: dict[str, Any], *, user: str = DAN) -> httpx.Response:
    return client.post(f"{PREFIX}/meta-enforcement", headers=AUTH,
                       json={"motivo": "Banner de restriccion en WhatsApp Manager", **body,
                             "requested_by": {"slack_user": user}})


def test_an_approver_freezes_and_unfreezes_by_hand() -> None:
    client, store = _app()
    frozen = _act(client, {"accion": "congelar"})
    assert frozen.status_code == 200 and frozen.json()["data"]["congelada"] is True
    assert client.app.state.meta_enforcement.initiated_brake("x_y", "es_MX") == REASON_ACCOUNT_RESTRICTED  # type: ignore[attr-defined]
    [(_, payload)] = [c for c in store.calls if c[0] == "act_on_meta_enforcement_v1"]
    assert payload["p_actor_ref"] == DAN and payload["p_target"] is None
    lifted = _act(client, {"accion": "descongelar"}, user=BRAYAN)
    assert lifted.json()["data"]["congelada"] is False
    state = client.get(f"{PREFIX}/meta-enforcement", headers=AUTH)
    assert state.status_code == 200 and state.json()["contract"] == "meta-enforcement-brakes-v1"
    client.__exit__(None, None, None)


def test_the_manual_routes_check_the_person_and_the_body() -> None:
    client, store = _app()
    assert _act(client, {"accion": "congelar"}, user=OUTSIDER).status_code == 403
    assert client.post(f"{PREFIX}/meta-enforcement", json={}).status_code == 401
    assert _act(client, {"accion": "borrar"}).status_code == 422
    assert _act(client, {"accion": "liberar_plantilla"}).status_code == 422
    assert _act(client, {"accion": "congelar", "plantilla": {"nombre": "a_b", "idioma": "es_MX"}}).status_code == 422
    released = _act(client, {"accion": "liberar_plantilla", "plantilla": {"nombre": "att1_pago_01", "idioma": "es_MX"}})
    assert released.status_code == 200
    assert store.calls[-1][1]["p_target"] == {"template_name": "att1_pago_01", "language": "es_MX"}
    client.__exit__(None, None, None)


def test_without_the_flag_nothing_exists_and_ready_says_disabled() -> None:
    client, store = _app(meta_enforcement_brakes_enabled=False)
    body = json.dumps(CATEGORY["hecho"]).encode()
    assert client.post("/webhooks/meta", content=body, headers=_signed(body)).status_code in (404, 405)
    assert client.get("/webhooks/meta", params={"hub.mode": "subscribe"}).status_code in (404, 405)
    assert _act(client, {"accion": "congelar"}).status_code == 404
    ready = client.get("/ready").json()
    assert ready["meta_enforcement"] == "disabled" and "meta_enforcement_worker" not in ready
    assert store.calls == []
    client.__exit__(None, None, None)


def test_without_the_secrets_the_webhook_does_not_exist() -> None:
    client, _ = _app(meta_webhook_verify_token=None, meta_app_secret=None)
    body = json.dumps(CATEGORY["hecho"]).encode()
    assert client.post("/webhooks/meta", content=body, headers=_signed(body)).status_code in (404, 405)
    assert "webhook=sin_secreto" in client.get("/ready").json()["meta_enforcement"]
    client.__exit__(None, None, None)


def test_the_flag_is_off_by_default_portable_and_demands_its_pieces() -> None:
    assert Settings.__dataclass_fields__["meta_enforcement_brakes_enabled"].default is False
    assert "meta_enforcement_brakes_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    on = _approval_settings(**ON)
    meta_enforcement.check_settings(on)
    meta_enforcement.check_settings(replace(on, meta_webhook_verify_token=None, meta_app_secret=None))
    for overrides, message in (
        ({"instance_manifest": None}, "requires an instance manifest"),
        ({"supabase_service_role_key": None}, "SUPABASE_SERVICE_ROLE_KEY"),
        ({"meta_waba_id": None}, "META_WABA_ID"),
        ({"meta_templates_access_token": None}, "META_TEMPLATES_ACCESS_TOKEN"),
        ({"meta_app_secret": None}, "go together"),
    ):
        with pytest.raises(ValueError, match=message):
            meta_enforcement.check_settings(replace(on, **overrides))


def test_the_flag_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "CHATWOOT_INBOX_ID": "9",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(meta_enforcement.ENV, raising=False)
    assert Settings.from_env().meta_enforcement_brakes_enabled is False
    monkeypatch.setenv(meta_enforcement.ENV, " TRUE ")
    monkeypatch.setenv(meta_enforcement.VERIFY_TOKEN_ENV, VERIFY)
    settings = Settings.from_env()
    assert settings.meta_enforcement_brakes_enabled is True and settings.meta_webhook_verify_token == VERIFY
    assert settings.meta_app_secret is None


# ------------------------------------------------------------------ el masivo y el pedido de utilidad


def test_the_bulk_send_preflight_brakes_a_retired_template_or_a_frozen_instance() -> None:
    from bridge.bulk_send_worker import BulkSendWorker

    live = _live(_state(retired=[("masivo_01", "es_MX")]))
    worker = BulkSendWorker.__new__(BulkSendWorker)
    worker._config = SimpleNamespace(retry_templates=frozenset())  # type: ignore[attr-defined]
    worker._meta_brake = live.initiated_brake  # type: ignore[attr-defined]

    class _Templates:
        async def resolve(self, name: str, language: str) -> None:
            raise AssertionError("no llega a Meta")

    worker._templates = _Templates()  # type: ignore[attr-defined]
    send = SimpleNamespace(template_name="masivo_01", template_language="es_MX")
    assert asyncio.run(worker._preflight(send)) == REASON_RECATEGORIZED  # type: ignore[arg-type]
    worker._meta_brake = _live(_state(frozen=True)).initiated_brake  # type: ignore[attr-defined]
    send = SimpleNamespace(template_name="otro_01", template_language="es_MX")
    assert asyncio.run(worker._preflight(send)) == REASON_ACCOUNT_RESTRICTED  # type: ignore[arg-type]


def test_the_meta_brake_reasons_fit_the_bulk_send_brake_column() -> None:
    from bridge.bulk_send_worker import machine_reason

    for reason in (REASON_RECATEGORIZED, REASON_ACCOUNT_RESTRICTED):
        assert machine_reason(reason) == reason


def test_the_delivery_retry_does_not_resend_a_braked_template() -> None:
    from bridge.meta_delivery import MetaDeliveryRejectionSweeper, RetryPlan

    live = _live(_state(retired=[("att1_primer_contacto_01", "es_MX")]))
    sweeper = MetaDeliveryRejectionSweeper(chatwoot=object(), supabase=object(), account_id=2, inbox_id=11,
                                           meta_brake=live.initiated_brake)
    plan = RetryPlan(conversation_id=1, message_id=2, content="x", template_params={},
                     template_name="att1_primer_contacto_01", template_language="es_MX", target_phone="+10",
                     contact_email=None)
    assert sweeper._braked(plan) == REASON_RECATEGORIZED
    assert MetaDeliveryRejectionSweeper(chatwoot=object(), supabase=object(), account_id=2,
                                        inbox_id=11)._braked(plan) is None


def test_the_store_calls_the_service_role_functions() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "state": _state()})

    store = EnforcementStore(base_url="https://att1-rest.internal", db_key="k", transport=httpx.MockTransport(handler))
    asyncio.run(store.get_state(INSTANCE, waba_id=WABA))
    asyncio.run(store.act(INSTANCE, waba_id=WABA, action="congelar", target=None, reason="r", actor=DAN))
    assert [path for path, _ in seen] == ["/rest/v1/rpc/get_meta_enforcement_state_v1",
                                          "/rest/v1/rpc/act_on_meta_enforcement_v1"]
    assert seen[1][1] == {"p_instance": INSTANCE, "p_waba_id": WABA, "p_action": "congelar", "p_target": None,
                          "p_reason": "r", "p_actor_ref": DAN}


def test_meta_list_categories_reads_every_page_with_correct_category() -> None:
    pages = [
        {"data": [{"id": "1", "name": "a_b", "language": "es_MX", "status": "APPROVED", "category": "UTILITY",
                   "correct_category": "MARKETING"}],
         "paging": {"cursors": {"after": "c2"}, "next": "https://graph.facebook.com/x"}},
        {"data": [{"id": "2", "name": "c_d", "language": "es_MX", "status": "APPROVED", "category": "marketing"}]},
    ]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=pages[len(requests) - 1])

    client = MetaTemplatesClient(access_token="t", waba_id=WABA, transport=httpx.MockTransport(handler))
    rows = asyncio.run(client.list_categories())
    assert rows == [{"id": "1", "name": "a_b", "language": "es_MX", "category": "UTILITY",
                     "correct_category": "MARKETING"},
                    {"id": "2", "name": "c_d", "language": "es_MX", "category": "MARKETING",
                     "correct_category": None}]
    assert "correct_category" in requests[0].url.params["fields"] and requests[1].url.params["after"] == "c2"
    assert "access_token" not in str(requests[0].url)


def test_from_the_second_recategorization_a_new_utility_request_is_braked(monkeypatch: pytest.MonkeyPatch) -> None:
    import test_template_request_api as requests_api

    store = FakeStore()
    store.state = _state(streak=2)

    class _Store(EnforcementStore):
        async def _rpc(self, function: str, payload: Any, *, list_ok: bool = False) -> Any:
            return await store._rpc(function, payload)

    monkeypatch.setattr(meta_enforcement, "EnforcementStore", _Store)
    client, emulator = requests_api._client(meta_enforcement_brakes_enabled=True,
                                            meta_templates_access_token="EAAG" + "x" * 60, meta_waba_id=WABA)
    with client:
        body = requests_api._body()
        assert body["template"]["categoria"] == "UTILITY"
        braked = client.post(f"{requests_api.PREFIX}/template-requests", headers=requests_api.AUTH, json=body)
        assert (braked.status_code, braked.json()["detail"]) == (409, REASON_STREAK)
        assert requests_api._calls(emulator, "save_meta_template_request_draft_v1") == []
        marketing = requests_api._body(template={**requests_api._template(), "categoria": "MARKETING"})
        assert client.post(f"{requests_api.PREFIX}/template-requests", headers=requests_api.AUTH,
                           json=marketing).json().get("detail") != REASON_STREAK
        # Con una sola, el pedido de utilidad sigue.
        store.state = _state(streak=1)
        assert asyncio.run(client.app.state.meta_enforcement.refresh())  # type: ignore[attr-defined]
        assert client.post(f"{requests_api.PREFIX}/template-requests", headers=requests_api.AUTH,
                           json=requests_api._body()).status_code == 201
