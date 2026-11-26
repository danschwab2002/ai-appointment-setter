"""Pausar al agente para todas, por etiqueta o por producto (agent-global-pause-v1, item 5.4).

D4 y D10 del mapa Z1 del 2026-11-26; decisiones D-54.1 a D-54.9 del PR.

Datos:

* El turno usa la captura del webhook de la conversacion 200 del inbox 9
  (``chatwoot_message_created_audio_inbox_9_conv_200_20260928.json``, via
  ``_text_webhook`` de ``test_whatsapp_inbound_equivalence``) con ``content``
  reemplazado, y la etiqueta va en ``conversation.labels``, donde la manda
  Chatwoot (captura ``chatwoot_message_created_inbox_9_conv_158_20260923.json``).
* Las rutas usan el manifiesto de prueba de ATT1 y la lista de aprobadores de
  ``test_flow_approval_page`` (IDs de Slack inventados).
* La forma de la lectura viva es la de ``get_live_agent_global_pauses_v1``
  (20261126100000); lo que la base hace de verdad lo prueba PGlite
  (``validate_agent_global_pause.mjs``).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_flow_approval_page import (
    BRAYAN,
    CLIENT_ID,
    CLIENT_SECRET,
    DAN,
    DB_KEY,
    ORIGIN,
    OUTSIDER,
    SESSION_SECRET,
    TEAM,
    FakeChatwoot,
    _NoSupabase,
)
from test_flow_approval_page import _settings as _approval_settings
from test_webhook import StubChatwootClient, StubShadowProcessor, _post, _signed_headers
from test_whatsapp_inbound_equivalence import FORM_ID, SECRET, _manifest_settings, _Supabase, _text_webhook

from bridge import agent_global_pause
from bridge import app as app_module
from bridge.agent_global_pause import LivePause, LivePauses, parse_live_pauses
from bridge.app import Settings, create_app
from bridge.instance_manifest import Product
from bridge.supabase import SupabaseClient

PREFIX = "/internal/automation-expert/v1"
TOKEN = "c" * 48
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PAUSE_ALL = "0000000a-0000-4000-8000-000000005401"
PAUSE_LABEL = "0000000a-0000-4000-8000-000000005402"
PAUSE_PRODUCT = "0000000a-0000-4000-8000-000000005403"
NOW = datetime(2026, 11, 26, 15, 0, tzinfo=UTC)
SERVICE_KEY = "service-role-key-de-prueba"


def _jwt(role: str) -> str:
    import base64

    part = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{part({'alg': 'RS256', 'kid': 'k'})}.{part({'role': role})}.firma"


AE_KEY = _jwt("automation_expert_change")
ENABLED: dict[str, Any] = {
    "agent_global_pause_enabled": True,
    "automation_expert_change_enabled": True,
    "automation_expert_change_token": TOKEN,
    "automation_expert_change_db_key": AE_KEY,
    "automation_expert_read_enabled": True, "automation_expert_read_token": "r" * 48,
    "chatwoot_account_id": 2, "chatwoot_inbox_id": 11,
}
APPROVAL: dict[str, Any] = {
    "flow_approval_enabled": True, "flow_approval_base_url": ORIGIN, "flow_approval_slack_team_id": TEAM,
    "flow_approval_slack_approvers": f"{DAN},{BRAYAN}", "flow_approval_slack_client_id": CLIENT_ID,
    "flow_approval_slack_client_secret": CLIENT_SECRET, "flow_approval_session_secret": SESSION_SECRET,
    "flow_change_approver_db_key": DB_KEY,
}


def _live_row(pause_id: str, scope: str, value: str | None = None, label: str | None = None,
              ends_at: str | None = None) -> dict[str, Any]:
    return {"pause_id": pause_id, "scope": scope, "scope_value": value, "match_label": label,
            "created_at": "2026-11-26T14:00:00Z", "ends_at": ends_at}


# ------------------------------------------------------------------ la lectura viva


def test_the_message_time_comes_from_the_captured_webhook() -> None:
    webhook, _ = _text_webhook("x")
    assert agent_global_pause.message_at(webhook) == datetime(2026, 9, 28, 18, 39, 45, 415000, tzinfo=UTC)
    assert agent_global_pause.message_at({"created_at": 1790620785}) == datetime.fromtimestamp(1790620785, tz=UTC)
    for value in (None, "ayer", "2026-09-28T18:39:45", True, -1):
        assert agent_global_pause.message_at({"created_at": value}) is None


def test_the_live_reading_is_strict() -> None:
    [pause] = parse_live_pauses({"pauses": [_live_row(PAUSE_LABEL, "etiqueta", "black", "black",
                                                      "2026-11-27T00:00:00+00:00")]})
    assert pause == LivePause(pause_id=PAUSE_LABEL, scope="etiqueta", scope_value="black", match_label="black",
                              created_at=datetime(2026, 11, 26, 14, 0, tzinfo=UTC),
                              ends_at=datetime(2026, 11, 27, tzinfo=UTC))
    for broken in (
        {"pauses": [_live_row(PAUSE_ALL, "todas", "x", "x")]},
        {"pauses": [_live_row(PAUSE_ALL, "otra")]},
        {"pauses": [_live_row("no-es-uuid", "todas")]},
        {"pauses": [_live_row(PAUSE_LABEL, "etiqueta", "Black", "Black")]},
        {"pauses": [{**_live_row(PAUSE_ALL, "todas"), "ends_at": "2026-11-27T00:00:00"}]},
        {"pauses": None},
    ):
        with pytest.raises(ValueError):
            parse_live_pauses(broken)


def _reader(rows: list[dict[str, Any]] | Exception, now: datetime = NOW) -> tuple[LivePauses, dict[str, Any]]:
    state: dict[str, Any] = {"rows": rows}

    async def fetch() -> dict[str, Any]:
        if isinstance(state["rows"], Exception):
            raise state["rows"]
        return {"pauses": state["rows"]}

    return LivePauses(fetch=fetch, clock=lambda: now), state


def test_each_scope_reaches_its_conversations() -> None:
    reader, _ = _reader([_live_row(PAUSE_LABEL, "etiqueta", "black", "black"),
                         _live_row(PAUSE_PRODUCT, "producto", "protocolo", "producto-el-protocolo")])
    assert asyncio.run(reader.refresh()) is True

    assert reader.holding(labels=("otra",)) is None
    assert reader.holding(labels=("black",)).pause_id == PAUSE_LABEL  # type: ignore[union-attr]
    # Por la etiqueta del producto que ya tiene, o por el producto que el bridge le asigno en el turno.
    assert reader.holding(labels=("producto-el-protocolo",)).pause_id == PAUSE_PRODUCT  # type: ignore[union-attr]
    assert reader.holding(labels=(), product_keys=("protocolo",)).pause_id == PAUSE_PRODUCT  # type: ignore[union-attr]
    assert reader.holding(labels=(), product_keys=("forever",)) is None


def test_todas_reaches_everyone_and_wins() -> None:
    reader, _ = _reader([_live_row(PAUSE_LABEL, "etiqueta", "black", "black"), _live_row(PAUSE_ALL, "todas")])
    asyncio.run(reader.refresh())
    assert reader.holding(labels=()).pause_id == PAUSE_ALL  # type: ignore[union-attr]
    assert reader.holding(labels=("black",)).pause_id == PAUSE_ALL  # type: ignore[union-attr]
    assert reader.status_line() == "paused:2:todas,etiqueta,refresh=ok"


def test_until_is_read_with_the_bridge_clock() -> None:
    reader, _ = _reader([_live_row(PAUSE_ALL, "todas", ends_at="2026-11-26T16:00:00Z")])
    asyncio.run(reader.refresh())
    assert reader.holding(labels=(), now=NOW) is not None
    assert reader.holding(labels=(), now=datetime(2026, 11, 26, 16, 0, tzinfo=UTC)) is None
    assert reader.status_line(datetime(2026, 11, 26, 16, 0, 1, tzinfo=UTC)) == "none,refresh=ok"


def test_a_message_written_during_a_pause_is_not_answered_after_it() -> None:
    """D-54.4: el monitor de trabadas vuelve a admitir el mensaje despues de reanudar."""
    reader, _ = _reader([_live_row(PAUSE_LABEL, "etiqueta", "black", "black", ends_at="2026-11-26T15:00:00Z")])
    asyncio.run(reader.refresh())
    later = datetime(2026, 11, 26, 18, 0, tzinfo=UTC)
    written_during = datetime(2026, 11, 26, 14, 30, tzinfo=UTC)
    assert reader.holding(labels=("black",), now=later, message_at=written_during).pause_id == PAUSE_LABEL  # type: ignore[union-attr]
    # Escrito antes o despues, o en otra conversacion: el agente contesta.
    assert reader.holding(labels=("black",), now=later, message_at=datetime(2026, 11, 26, 13, 0, tzinfo=UTC)) is None
    assert reader.holding(labels=("black",), now=later, message_at=later) is None
    assert reader.holding(labels=("otra",), now=later, message_at=written_during) is None
    # Una que termino no cuenta como viva en /ready.
    assert reader.status_line(later) == "none,refresh=ok"


def test_the_last_good_reading_stays_when_the_base_does_not_answer(caplog: pytest.LogCaptureFixture) -> None:
    reader, state = _reader([_live_row(PAUSE_ALL, "todas")])
    asyncio.run(reader.refresh())
    state["rows"] = OSError("caida")
    assert asyncio.run(reader.refresh()) is False
    assert reader.holding(labels=()) is not None
    assert reader.status_line() == "paused:1:todas,refresh=failing:1"
    # Una fila sin la forma tampoco pisa lo ultimo bueno.
    state["rows"] = [_live_row(PAUSE_ALL, "otra")]
    assert asyncio.run(reader.refresh()) is False
    assert reader.holding(labels=()) is not None
    # Y cuando vuelve, lo nuevo manda: ya no hay pausa.
    state["rows"] = []
    assert asyncio.run(reader.refresh()) is True
    assert reader.holding(labels=()) is None and reader.status_line() == "none,refresh=ok"
    assert "agent_global_pause_refresh_failed failures=2 error=ValueError" in caplog.text


def test_without_any_good_reading_there_is_no_pause() -> None:
    reader, _ = _reader(OSError("caida"))
    assert asyncio.run(reader.refresh()) is False
    assert reader.holding(labels=()) is None
    assert reader.status_line() == "none,refresh=never_read"


# ------------------------------------------------------------------ el cliente de la base


def test_the_service_role_client_reads_and_records() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[1]
        calls.append((name, json.loads(request.content)))
        if name == "get_live_agent_global_pauses_v1":
            return httpx.Response(200, json={"pauses": [_live_row(PAUSE_ALL, "todas")]})
        return httpx.Response(200, json={"ok": True, "outcome": "recorded"})

    client = SupabaseClient(base_url="https://att1-rest.internal", service_role_key=SERVICE_KEY,
                            transport=httpx.MockTransport(handler))
    instance = {"tenant_ref": "lancemos", "ally_ref": "att1"}
    assert asyncio.run(client.get_live_agent_global_pauses(instance))["pauses"][0]["pause_id"] == PAUSE_ALL
    assert asyncio.run(client.record_agent_global_pause_hold(instance, pause_id=PAUSE_ALL,
                                                             conversation_id=200)) == "recorded"
    assert calls == [
        ("get_live_agent_global_pauses_v1", {"p_instance": instance}),
        ("record_agent_global_pause_hold_v1", {"p_instance": instance, "p_pause_id": PAUSE_ALL,
                                               "p_conversation_id": 200}),
    ]


# ------------------------------------------------------------------ las rutas del AE


class FakePauseStore(agent_global_pause.PauseStore):
    """Las tres funciones del agente, con las reglas de 20261126100000 que le importan a las rutas."""

    def __init__(self) -> None:
        super().__init__(base_url="https://att1-rest.internal", db_key=AE_KEY)
        self.rows: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def live(self) -> list[dict[str, Any]]:
        return [_live_row(row["pause_id"], row["scope"], row["scope_value"], row["match_label"],
                          row["until"] if row["status"] == "activa" else "2026-11-26T14:30:00Z")
                for row in self.rows.values()]

    def _row(self, pause_id: str, **fields: Any) -> dict[str, Any]:
        return {"pause_id": pause_id, "created_at": "2026-11-26T14:00:00Z", "status": "activa", "lifted_at": None,
                "lifted_by": None, "lift_reason": None, "held_conversations": 0, "held_messages": 0, **fields}

    async def pause(self, instance: Any, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        self.calls.append(("pause", {"instance": instance, **kwargs}))
        for row in self.rows.values():
            if (row["status"] == "activa" and row["scope"] == kwargs["scope"]
                    and row["scope_value"] == kwargs["scope_value"]):
                return {"ok": True, "replayed": True, "pause": copy.deepcopy(row)}
        pause_id = {"todas": PAUSE_ALL, "etiqueta": PAUSE_LABEL, "producto": PAUSE_PRODUCT}[kwargs["scope"]]
        self.rows[pause_id] = self._row(pause_id, scope=kwargs["scope"], scope_value=kwargs["scope_value"],
                                        match_label=kwargs["match_label"], until=kwargs["until"],
                                        reason=kwargs["reason"], requested_by=kwargs["requested_by"])
        return {"ok": True, "replayed": False, "pause": copy.deepcopy(self.rows[pause_id])}

    async def resume(self, instance: Any, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        self.calls.append(("resume", {"instance": instance, **kwargs}))
        row = self.rows.get(kwargs["pause_id"])
        if row is None:
            raise agent_global_pause.StoreRejected("agent_pause_not_found", 404)
        replayed = row["status"] != "activa"
        row.update(status="levantada", lifted_by=row["lifted_by"] or kwargs["requested_by"],
                   lift_reason=row["lift_reason"] or kwargs["reason"])
        return {"ok": True, "replayed": replayed, "pause": copy.deepcopy(row)}

    async def list_pauses(self, instance: Any, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        self.calls.append(("list", {"instance": instance, **kwargs}))
        return {"pauses": [copy.deepcopy(row) for row in self.rows.values()]}


class RouteBase(_NoSupabase):
    """El cliente de service_role: con el flag, solo la lectura viva (y lo de /ready)."""

    def __init__(self, store: FakePauseStore) -> None:
        self.__dict__["store"] = store

    async def resolve_commercial_ally_runtime_binding(self, config: object) -> object:
        return config

    async def get_human_handoff_projection_status(self) -> object:
        return SimpleNamespace(pending_count=0, retryable_count=0, delivery_unknown_count=0,
                               conflict_count=0, dead_letter_count=0)

    async def get_live_agent_global_pauses(self, instance: dict[str, str]) -> dict[str, Any]:
        assert instance == {"tenant_ref": "lancemos", "ally_ref": "att1"}
        return {"pauses": self.store.live()}


class LabelsChatwoot:
    """La lista de etiquetas de la cuenta, como la da la herramienta de Chatwoot del AE."""

    def __init__(self, titles: list[str], *, down: bool = False) -> None:
        self.titles, self.down = titles, down

    async def list_labels(self) -> list[dict[str, Any]]:
        if self.down:
            raise OSError("caida")
        return [{"id": index, "title": title} for index, title in enumerate(self.titles, 1)]


def _route_app(store: FakePauseStore | None = None, *, products: tuple[Product, ...] | None = None,
               chatwoot_labels: LabelsChatwoot | None = None, **overrides: Any) -> tuple[TestClient, FakePauseStore]:
    store = store or FakePauseStore()
    settings = _approval_settings(**{**ENABLED, **overrides})
    if products is not None:
        assert settings.instance_manifest is not None
        settings = replace(settings, instance_manifest=replace(settings.instance_manifest, products=products))
    if chatwoot_labels is not None:
        settings = replace(settings, automation_expert_chatwoot_enabled=True,
                           chatwoot_control_api_access_token="control-token-de-prueba")
    app = create_app(settings, supabase_client=RouteBase(store),  # type: ignore[arg-type]
                     chatwoot_client=FakeChatwoot(),  # type: ignore[arg-type]
                     agent_pause_store=store,
                     automation_expert_chatwoot_client=chatwoot_labels)  # type: ignore[arg-type]
    client = TestClient(app, base_url=ORIGIN)
    client.__enter__()
    return client, store


def _pause(client: TestClient, body: dict[str, Any], *, user: str = DAN) -> httpx.Response:
    return client.post(f"{PREFIX}/agent-pause", headers=AUTH,
                       json={"motivo": "Vivo de las 19", **body, "requested_by": {"slack_user": user}})


def test_an_approver_pauses_everyone_and_the_bridge_applies_it_at_once() -> None:
    client, store = _route_app()
    response = _pause(client, {"alcance": "todas"})

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert response.json()["contract"] == "agent-global-pause-v1"
    assert data["replayed"] is False and data["aplicada_en_este_bridge"] is True
    assert data["pause"]["alcance"] == "todas" and data["pause"]["pedida_por"] == DAN
    assert data["pause"]["motivo"] == "Vivo de las 19" and data["pause"]["hasta"] is None
    [(_, call)] = store.calls
    assert call == {"instance": {"tenant_ref": "lancemos", "ally_ref": "att1"}, "scope": "todas",
                    "scope_value": None, "match_label": None, "until": None, "reason": "Vivo de las 19",
                    "requested_by": DAN}
    # La misma pausa otra vez: la que ya esta.
    again = _pause(client, {"alcance": "todas"}, user=BRAYAN)
    assert again.status_code == 201 and again.json()["data"]["replayed"] is True
    # /ready la dice.
    assert client.get("/ready").json()["agent_global_pause"] == "paused:1:todas,refresh=ok"
    client.__exit__(None, None, None)


def test_someone_outside_the_approvers_list_cannot_pause_or_resume() -> None:
    client, store = _route_app()
    response = _pause(client, {"alcance": "todas"}, user=OUTSIDER)
    assert response.status_code == 403 and response.json()["detail"] == "not_an_approver"
    resumed = client.post(f"{PREFIX}/agent-pause/{PAUSE_ALL}/resume", headers=AUTH,
                          json={"requested_by": {"slack_user": OUTSIDER}})
    assert resumed.status_code == 403 and resumed.json()["detail"] == "not_an_approver"
    assert store.calls == []
    client.__exit__(None, None, None)


def test_the_request_is_checked_before_reaching_the_base() -> None:
    client, store = _route_app()
    no_token = client.post(f"{PREFIX}/agent-pause", json={"alcance": "todas", "motivo": "x",
                                                          "requested_by": {"slack_user": DAN}})
    assert no_token.status_code == 401
    cases = [
        ({"alcance": "algunas"}, "invalid_body"),
        ({"alcance": "todas", "valor": "black"}, "invalid_body"),
        ({"alcance": "etiqueta"}, "invalid_body"),
        ({"alcance": "etiqueta", "valor": "automation_paused"}, "chatwoot_label_reserved"),
        ({"alcance": "todas", "motivo": "llamame al 11 5555 1234"}, "identifier_not_allowed"),
        ({"alcance": "todas", "hasta": "2026-11-27T00:00:00"}, "invalid_body"),
        ({"alcance": "todas", "hasta": "2000-01-01T00:00:00Z"}, "agent_pause_until_invalid"),
        ({"alcance": "todas", "hasta": (datetime.now(UTC) + timedelta(days=8)).isoformat()},
         "agent_pause_until_invalid"),
        ({"alcance": "producto", "valor": "otro"}, "agent_pause_product_unknown"),
    ]
    for body, code in cases:
        response = _pause(client, body)
        assert response.status_code == 422, (body, response.text)
        assert response.json()["detail"] == code, body
    assert store.calls == []
    client.__exit__(None, None, None)


def test_a_label_is_checked_against_chatwoot_when_the_bridge_can_read_it() -> None:
    client, store = _route_app(chatwoot_labels=LabelsChatwoot(["cliente", "black"]))
    missing = _pause(client, {"alcance": "etiqueta", "valor": "blak"})
    assert missing.status_code == 422 and missing.json()["detail"] == "chatwoot_label_not_found"
    until = (datetime.now(UTC) + timedelta(hours=3)).replace(microsecond=0)
    response = _pause(client, {"alcance": "etiqueta", "valor": "black", "hasta": until.isoformat()})
    assert response.status_code == 201, response.text
    assert response.json()["data"]["warnings"] == []
    [(_, call)] = store.calls
    assert call["match_label"] == "black" and call["until"] == until.isoformat().replace("+00:00", "Z")
    client.__exit__(None, None, None)

    down, _ = _route_app(chatwoot_labels=LabelsChatwoot([], down=True))
    response = _pause(down, {"alcance": "etiqueta", "valor": "black"})
    assert response.status_code == 503 and response.json()["detail"] == "chatwoot_tool_unavailable"
    down.__exit__(None, None, None)


def test_without_the_chatwoot_tool_a_label_goes_by_its_shape_and_says_so() -> None:
    client, _ = _route_app()
    response = _pause(client, {"alcance": "etiqueta", "valor": "black"})
    assert response.status_code == 201
    assert response.json()["data"]["warnings"] == [{"code": "agent_pause_label_not_verified"}]
    client.__exit__(None, None, None)


def test_a_product_pauses_by_its_label_and_one_without_a_label_cannot() -> None:
    unlabeled, unlabeled_store = _route_app(products=(Product(key="forever"),))
    without = _pause(unlabeled, {"alcance": "producto", "valor": "forever"})
    assert without.status_code == 422 and without.json()["detail"] == "agent_pause_product_without_label"
    assert unlabeled_store.calls == []
    unlabeled.__exit__(None, None, None)

    products = (Product(key="protocolo", chatwoot_label="producto-el-protocolo", chatwoot_label_macro_id=7),)
    client, store = _route_app(products=products)
    response = _pause(client, {"alcance": "producto", "valor": "protocolo"})
    assert response.status_code == 201
    [(_, call)] = store.calls
    assert call["scope_value"] == "protocolo" and call["match_label"] == "producto-el-protocolo"
    state = client.get(f"{PREFIX}/agent-pause", headers=AUTH).json()["data"]
    assert state["productos"] == ["protocolo"]
    client.__exit__(None, None, None)


def test_resume_and_the_state_reading() -> None:
    client, store = _route_app()
    assert _pause(client, {"alcance": "todas"}).status_code == 201
    state = client.get(f"{PREFIX}/agent-pause", headers=AUTH).json()["data"]
    assert state["pausado"] is True and [view["pause_id"] for view in state["vivas"]] == [PAUSE_ALL]
    assert state["este_bridge"] == "paused:1:todas,refresh=ok"

    response = client.post(f"{PREFIX}/agent-pause/{PAUSE_ALL}/resume", headers=AUTH,
                           json={"motivo": "Termino el vivo", "requested_by": {"slack_user": BRAYAN}})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["pause"]["estado"] == "levantada" and data["pause"]["levantada_por"] == BRAYAN
    assert data["pause"]["motivo_al_levantar"] == "Termino el vivo"
    assert client.get("/ready").json()["agent_global_pause"] == "none,refresh=ok"
    state = client.get(f"{PREFIX}/agent-pause", headers=AUTH).json()["data"]
    assert state["pausado"] is False and [view["pause_id"] for view in state["recientes"]] == [PAUSE_ALL]

    unknown = client.post(f"{PREFIX}/agent-pause/{PAUSE_LABEL}/resume", headers=AUTH,
                          json={"requested_by": {"slack_user": DAN}})
    assert unknown.status_code == 404 and unknown.json()["detail"] == "agent_pause_not_found"
    bad = client.post(f"{PREFIX}/agent-pause/no-es-uuid/resume", headers=AUTH,
                      json={"requested_by": {"slack_user": DAN}})
    assert bad.status_code == 422 and bad.json()["detail"] == "invalid_pause_id"
    client.__exit__(None, None, None)


# ------------------------------------------------------------------ el flag


def test_without_the_flag_the_routes_do_not_exist_and_ready_does_not_change() -> None:
    client, store = _route_app(agent_global_pause_enabled=False)
    assert _pause(client, {"alcance": "todas"}).status_code == 404
    assert client.get(f"{PREFIX}/agent-pause", headers=AUTH).status_code == 404
    assert "agent_global_pause" not in client.get("/ready").json()
    assert store.calls == []
    client.__exit__(None, None, None)


def test_the_flag_is_off_by_default_portable_and_demands_its_pieces() -> None:
    assert Settings.__dataclass_fields__["agent_global_pause_enabled"].default is False
    assert "agent_global_pause_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    on = _approval_settings(**ENABLED)
    agent_global_pause.check_settings(on)
    for overrides, message in (
        ({"instance_manifest": None}, "requires an instance manifest"),
        ({"supabase_service_role_key": None}, "requires SUPABASE_BASE_URL and SUPABASE_SERVICE_ROLE_KEY"),
        ({"automation_expert_change_enabled": False}, "requires AUTOMATION_EXPERT_CHANGE_ENABLED"),
        ({"flow_approval_enabled": False}, "requires FLOW_APPROVAL_ENABLED"),
    ):
        with pytest.raises(ValueError, match=message):
            agent_global_pause.check_settings(replace(on, **overrides))


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
    monkeypatch.delenv("AGENT_GLOBAL_PAUSE_ENABLED", raising=False)
    assert Settings.from_env().agent_global_pause_enabled is False
    monkeypatch.setenv("AGENT_GLOBAL_PAUSE_ENABLED", "yes")
    assert Settings.from_env().agent_global_pause_enabled is False
    monkeypatch.setenv("AGENT_GLOBAL_PAUSE_ENABLED", " TRUE ")
    assert Settings.from_env().agent_global_pause_enabled is True


# ------------------------------------------------------------------ el turno


class TurnBase(_Supabase):
    """La base falsa de la equivalencia, con la lectura viva y el registro de lo retenido."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, *, hold_error: bool = False) -> None:
        super().__init__(identities=(FORM_ID,))
        self.rows = rows or []
        self.hold_error = hold_error
        self.live_reads = 0
        self.holds: list[tuple[str, int]] = []

    async def resolve_commercial_ally_runtime_binding(self, config: object) -> object:
        return config

    async def get_human_handoff_projection_status(self) -> object:
        return SimpleNamespace(pending_count=0, retryable_count=0, delivery_unknown_count=0,
                               conflict_count=0, dead_letter_count=0)

    async def get_live_agent_global_pauses(self, instance: dict[str, str]) -> dict[str, Any]:
        self.live_reads += 1
        return {"pauses": self.rows}

    async def record_agent_global_pause_hold(self, instance: dict[str, str], *, pause_id: str,
                                             conversation_id: int) -> str:
        if self.hold_error:
            raise OSError("caida")
        self.holds.append((pause_id, conversation_id))
        return "recorded"


def _turn_settings(tmp_path: Path, *, enabled: bool = True, product: bool = False, **extra: Any) -> Settings:
    base = _manifest_settings(tmp_path)
    settings = replace(base, supabase_base_url="https://att1-rest.internal", supabase_service_role_key=SERVICE_KEY,
                       **{**{key: value for key, value in ENABLED.items()
                             if key not in ("chatwoot_account_id", "chatwoot_inbox_id")},
                          "agent_global_pause_enabled": enabled}, **APPROVAL, **extra)
    if product:
        assert settings.instance_manifest is not None
        settings = replace(settings, instance_manifest=replace(
            settings.instance_manifest,
            products=(Product(key="att1", chatwoot_label="producto-att1", chatwoot_label_macro_id=7),)))
    return settings


def _turn(tmp_path: Path, base: TurnBase, *, labels: list[str] | None = None, enabled: bool = True,
          product: bool = False, text: str = "¿Qué precio tiene ahora?",
          **extra: Any) -> tuple[StubShadowProcessor, StubChatwootClient, Any]:
    webhook, history = _text_webhook(text)
    if labels is not None:
        webhook["conversation"]["labels"] = labels
    shadow = StubShadowProcessor({"decision": "reply", "reply": "Te leemos."})
    chatwoot = StubChatwootClient(messages=history)
    app = create_app(_turn_settings(tmp_path, enabled=enabled, product=product, **extra), chatwoot_client=chatwoot,
                     shadow_processor=shadow, supabase_client=base)  # type: ignore[arg-type]
    live = getattr(app.state, "agent_global_pause", None)
    if live is not None:
        assert asyncio.run(live.refresh()) is True
    raw_body = json.dumps(webhook, separators=(",", ":")).encode("utf-8")
    response = _post(app, raw_body, _signed_headers(raw_body, secret=SECRET, delivery="inbound-200"))
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())
    return shadow, chatwoot, app


def test_with_everyone_paused_the_agent_does_not_run_and_nothing_goes_out(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="bridge")
    base = TurnBase([_live_row(PAUSE_ALL, "todas")])

    shadow, chatwoot, _ = _turn(tmp_path, base)

    assert shadow.calls == []
    assert chatwoot.reply_calls == []
    # Sin la etiqueta automation_paused: la conversacion queda como estaba, para el equipo.
    assert chatwoot.calls == []
    assert base.holds == [(PAUSE_ALL, 200)]
    assert f"agent_global_pause_held pause={PAUSE_ALL} scope=todas conversation=200" in caplog.text


def test_a_label_pause_holds_only_the_conversations_with_the_label(tmp_path: Path) -> None:
    rows = [_live_row(PAUSE_LABEL, "etiqueta", "black", "black")]
    shadow, chatwoot, _ = _turn(tmp_path / "con", TurnBase(rows), labels=["black", "cliente"])
    assert shadow.calls == [] and chatwoot.reply_calls == []

    base = TurnBase(rows)
    shadow, chatwoot, _ = _turn(tmp_path / "sin", base, labels=["cliente"])
    assert len(shadow.calls) == 1 and len(chatwoot.reply_calls) == 1
    assert base.holds == []


def test_a_product_pause_holds_the_conversations_of_the_product(tmp_path: Path) -> None:
    # Con un producto, el bridge le asigna la etiqueta del producto en este mismo turno.
    rows = [_live_row(PAUSE_PRODUCT, "producto", "att1", "producto-att1")]
    base = TurnBase(rows)
    shadow, chatwoot, _ = _turn(tmp_path, base, labels=[], product=True)
    assert shadow.calls == [] and chatwoot.reply_calls == []
    assert base.holds == [(PAUSE_PRODUCT, 200)]


def test_after_resuming_the_next_message_is_answered(tmp_path: Path) -> None:
    base = TurnBase([])
    shadow, chatwoot, _ = _turn(tmp_path, base)
    assert len(shadow.calls) == 1 and len(chatwoot.reply_calls) == 1
    assert base.live_reads == 1 and base.holds == []


def test_a_stalled_replay_of_a_message_written_during_the_pause_is_not_answered(tmp_path: Path) -> None:
    webhook, _ = _text_webhook("x")
    written = agent_global_pause.message_at(webhook)
    assert written is not None
    ended = (written + timedelta(minutes=5)).isoformat()
    row = {**_live_row(PAUSE_ALL, "todas", ends_at=ended), "created_at": (written - timedelta(minutes=5)).isoformat()}
    base = TurnBase([row])
    shadow, chatwoot, _ = _turn(tmp_path, base)
    assert shadow.calls == [] and chatwoot.reply_calls == []


def test_a_hold_that_is_not_recorded_still_holds(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    shadow, chatwoot, _ = _turn(tmp_path, TurnBase([_live_row(PAUSE_ALL, "todas")], hold_error=True))
    assert shadow.calls == [] and chatwoot.reply_calls == []
    assert "agent_global_pause_hold_not_recorded" in caplog.text


def test_an_opt_out_during_the_pause_is_recorded_without_the_confirmation(tmp_path: Path) -> None:
    # Sin pausa, la misma baja sale confirmada con una linea (E-107): el control.
    base = TurnBase([])
    _, chatwoot, _ = _turn(tmp_path / "sin", base, text="No más mensajes", opt_out_confirmation_enabled=True)
    assert len(base.opt_outs) == 1 and len(chatwoot.reply_calls) == 1

    base = TurnBase([_live_row(PAUSE_ALL, "todas")])
    shadow, chatwoot, _ = _turn(tmp_path / "con", base, text="No más mensajes", opt_out_confirmation_enabled=True)
    assert shadow.calls == []
    assert len(base.opt_outs) == 1
    assert chatwoot.reply_calls == []


def test_without_the_flag_the_turn_never_reads_a_pause(tmp_path: Path) -> None:
    base = TurnBase([_live_row(PAUSE_ALL, "todas")])
    shadow, chatwoot, app = _turn(tmp_path, base, enabled=False)
    assert len(shadow.calls) == 1 and len(chatwoot.reply_calls) == 1
    assert base.live_reads == 0 and base.holds == []
    assert getattr(app.state, "agent_global_pause", None) is None
