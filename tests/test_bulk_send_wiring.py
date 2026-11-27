"""La conexion del masivo manejado por el AE (``bulk_send_wiring``): la base, la pagina y el motor.

Mapa: ``OS/productos/soporte-infoproductores/documentacion/2026-11-25-mapa-masivo-por-el-ae.md``. Lo que se
prueba aca es el cableado, no el criterio de cada pieza (eso lo prueban sus tests y PGlite):

* que apagado no exista nada y prendido ``create_app`` registre la pagina y arme el motor;
* que prendido sin lo que lo hace seguro (el candado montado, los numeros del portfolio, la salud, Meta, el
  AgentBot) el bridge no arranque;
* que los adaptadores de la base llamen a las funciones de las dos migraciones con sus parametros
  (``20261125000200`` y ``20261125000300``; la forma de lo que devuelven es la de esas funciones, y lo que
  la base hace de verdad lo prueba PGlite: ``validate_bulk_send_store.mjs`` y
  ``validate_bulk_send_prepared_and_base_vetoes.mjs``);
* que el producto de los vetos salga de la columna del envio;
* que una persona sin preparar reciba la etiqueta de la campana antes de la plantilla, sin pisar las que
  ya tiene. La respuesta del POST de etiquetas es la de
  ``chatwoot_v4_13_bulk_prepare_documentacion_20261125.json`` (fixture de documentacion de Chatwoot 4.13:
  ``labels/create.json.jbuilder``; el index usa la misma forma ``{"payload": [titulos]}``);
* que el motor real, con la base real por HTTP, frene y avise un envio con una plantilla de reintento;
* la purga;
* el reporte (D10 del #511): el registro de ``read_bulk_send_recipients_v1`` (20261125000400) por cursor,
  la conversacion capturada leida con el ``ChatwootClient`` real, la cache y que falle cerrado.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_agent_product_routes import CHANGE
from test_bulk_send_ae_routes import BULK_WIRING, Chatwoot, bulk_environment
from test_flow_approval_page import ORIGIN, SERVICE_KEY, _NoSupabase, _settings

from bridge import bulk_send_wiring as wiring
from bridge.app import create_app
from bridge.bulk_send_page import ListMember
from bridge.bulk_send_prepare import BulkMember, PreparedConversation
from bridge.bulk_send_report import ChatwootBulkSendReader
from bridge.bulk_send_vetoes import SupabaseBulkVetoStore
from bridge.bulk_send_worker import BulkSendConfigError, BulkSendWorker
from bridge.chatwoot import ChatwootClient
from bridge.bulk_send_prepare import BulkPrepareChatwoot
from bridge.messaging import FirstTouchResult
from bridge.meta_templates import MetaTemplatesClient

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PREPARE_DOC = json.loads((FIXTURES / "chatwoot_v4_13_bulk_prepare_documentacion_20261125.json").read_text("utf-8"))
INSTANCE = {"tenant_ref": "lancemos", "ally_ref": "att1"}
SEND_ID = "4f1c2b8e-0d7a-4c55-9a0e-2b7d1e6f9a10"
LIST_ID = "9b2f3c4d-1e5f-4a6b-8c7d-0e1f2a3b4c5d"
# 14:00 de CDMX del sabado 29/11: adentro del horario.
CENTER = datetime(2026, 11, 29, 20, 0, tzinfo=UTC)
PAGE = "/flow-approval/masivos"


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def view(**changes: Any) -> dict[str, Any]:
    """La forma de ``_bulk_send_view`` (20261125000300), con lo que lee el motor."""
    out: dict[str, Any] = {
        "send_id": SEND_ID, "state": "confirmado", "template_name": "att1_carrito_abandonado_01",
        "template_language": "es_MX", "template_variables": [{"de": "nombre"}, "el Protocolo"],
        "header_image_url": None, "campaign_label": "vivo-29-11", "product_refs": ["att1"],
        "scheduled_at": CENTER.isoformat(), "window_mode": "centrada", "rate_millis": 2500, "recipient_cap": 6000,
        "created_by": "automation_expert", "created_at": "2026-11-25T15:00:00Z", "confirmed_by": "slack:U0DAN0000",
        "confirmed_at": "2026-11-28T15:00:00Z", "confirmed_count": 3, "confirmed_health": "verde",
        "started_at": None, "finished_at": None, "brake_reason": None, "braked_by": None, "braked_at": None,
        "list": {"list_id": LIST_ID, "state": "revisada", "eligible_count": 3, "member_count": 3},
        "recipients_by_state": {},
    }
    out.update(changes)
    return out


class Rest:
    """PostgREST con las funciones del masivo: guarda cada llamada y contesta lo que se le diga."""

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.answers: dict[str, Any] = answers or {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {SERVICE_KEY}"
        name = request.url.path.rsplit("/", 1)[1]
        body = json.loads(request.content)
        self.calls.append((name, body))
        answer = self.answers.get(name)
        if callable(answer):
            answer = answer(body)
        if isinstance(answer, httpx.Response):
            return answer
        if answer is None:
            raise AssertionError(f"funcion inesperada {name}")
        return httpx.Response(200, json=answer)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def store(self) -> wiring.SupabaseBulkSendStore:
        return wiring.SupabaseBulkSendStore(base_url="https://att1-rest.internal", service_role_key=SERVICE_KEY,
                                            instance=INSTANCE, transport=self.transport())


# ------------------------------------------------------------------ el flag


def _create_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, enabled: bool) -> TestClient:
    if enabled:
        bulk_environment(monkeypatch, tmp_path)
    settings = _settings(flow_approval_enabled=True, **CHANGE, **BULK_WIRING, bulk_send_enabled=enabled)
    app = create_app(settings, supabase_client=_NoSupabase(),  # type: ignore[arg-type]
                     chatwoot_client=Chatwoot())  # type: ignore[arg-type]
    return TestClient(app, base_url=ORIGIN)


def _page_paths(client: TestClient) -> set[str]:
    return {getattr(route, "path", "") for route in client.app.routes
            if getattr(route, "path", "").startswith(PAGE)}


def test_off_there_is_no_page_no_worker_and_build_returns_none(monkeypatch, tmp_path) -> None:
    client = _create_app(monkeypatch, tmp_path, enabled=False)
    assert client.app.state.bulk_send is None
    assert _page_paths(client) == set()
    assert client.get(f"{PAGE}/{SEND_ID}").status_code == 404
    assert wiring.build_bulk_send(
        enabled=False, environ={}, supabase_base_url=None, supabase_service_role_key=None, tenant_ref=None,
        ally_ref=None, time_zone=None, chatwoot_base_url=None, chatwoot_account_id=None, chatwoot_inbox_id=None,
        chatwoot_control_token=None, agent_bot_ready=False, control_client=None, meta_access_token=None,
        meta_waba_id=None, meta_graph_version="v23.0", producer=None, campaigns=None) is None


def test_on_create_app_registers_the_page_and_builds_the_worker(monkeypatch, tmp_path) -> None:
    client = _create_app(monkeypatch, tmp_path, enabled=True)
    bulk = client.app.state.bulk_send
    assert isinstance(bulk, wiring.BulkSend) and isinstance(bulk.worker, BulkSendWorker)
    assert bulk.config.instance_key == "att1" and bulk.config.lock_dir == tmp_path / "portfolio"
    assert _page_paths(client) == {f"{PAGE}/{{send_id}}", f"{PAGE}/{{send_id}}/subir",
                                   f"{PAGE}/{{send_id}}/no-validos.csv", f"{PAGE}/{{send_id}}/confirmar",
                                   f"{PAGE}/{{send_id}}/frenar", f"{PAGE}/{{send_id}}/no-entregados.csv"}
    # Sin sesion, la pagina pide entrar con Slack: existe y no habla con la base.
    page = client.get(f"{PAGE}/{SEND_ID}")
    assert page.status_code in (200, 401) and "Slack" in page.text
    assert bulk.readiness() == {"bulk_send_worker": "ok", "bulk_send_last_outcome": "none",
                                "bulk_send_last_send": "none", "bulk_send_instance_key": "att1",
                                "bulk_send_purge": "never", "bulk_send_warmup": "disabled"}


def _build(tmp_path: Path, **changes: Any) -> wiring.BulkSend | None:
    lock = tmp_path / "portfolio"
    lock.mkdir(exist_ok=True)
    health = tmp_path / "estado.json"
    health.write_text("{}", encoding="utf-8")
    env = {"BULK_SEND_INSTANCE_KEY": "att1", "BULK_SEND_PORTFOLIO_LOCK_DIR": str(lock),
           "BULK_SEND_PORTFOLIO_NUMBERS": "att1=100000000002,maribel=100000000001,johanna=100000000003",
           "BULK_SEND_META_HEALTH_PATH": str(health)}
    env.update(changes.pop("env", {}))
    kwargs: dict[str, Any] = dict(
        enabled=True, environ={k: v for k, v in env.items() if v is not None},
        supabase_base_url="https://att1-rest.internal", supabase_service_role_key=SERVICE_KEY,
        tenant_ref="lancemos", ally_ref="att1", time_zone="America/Mexico_City",
        chatwoot_base_url="https://chatwoot.test", chatwoot_account_id=2, chatwoot_inbox_id=11,
        chatwoot_control_token="control", agent_bot_ready=True, control_client=Chatwoot(),
        meta_access_token="EAA" + "x" * 40, meta_waba_id="1234567890123", meta_graph_version="v23.0",
        producer=None, campaigns=None)
    kwargs.update(changes)
    return wiring.build_bulk_send(**kwargs)


def test_the_full_configuration_builds(tmp_path) -> None:
    bulk = _build(tmp_path)
    assert bulk is not None and bulk.config.send_hours == (9, 21) and bulk.config.timezone_name == "America/Mexico_City"


@pytest.mark.parametrize(("changes", "message"), [
    ({"env": {"BULK_SEND_PORTFOLIO_LOCK_DIR": "/no/existe/el/montaje"}}, "must be an existing writable directory"),
    ({"env": {"BULK_SEND_PORTFOLIO_LOCK_DIR": None}}, "requires BULK_SEND_PORTFOLIO_LOCK_DIR"),
    ({"env": {"BULK_SEND_INSTANCE_KEY": None}}, "requires BULK_SEND_INSTANCE_KEY"),
    ({"env": {"BULK_SEND_PORTFOLIO_NUMBERS": None}}, "requires BULK_SEND_PORTFOLIO_NUMBERS"),
    ({"env": {"BULK_SEND_PORTFOLIO_NUMBERS": "maribel=100000000001"}}, "must include att1"),
    ({"env": {"BULK_SEND_PORTFOLIO_NUMBERS": "att1=12"}}, "must be key=phone_number_id"),
    ({"env": {"BULK_SEND_META_HEALTH_PATH": None}}, "BULK_SEND_META_HEALTH_PATH"),
    ({"agent_bot_ready": False}, "CHATWOOT_AGENT_BOT_ACCESS_TOKEN"),
    ({"meta_access_token": None}, "META_TEMPLATES_ACCESS_TOKEN"),
    ({"meta_waba_id": None}, "META_WABA_ID"),
    ({"chatwoot_control_token": None}, "CHATWOOT_CONTROL_API_ACCESS_TOKEN"),
    ({"control_client": None}, "el cliente de control de Chatwoot"),
    ({"supabase_service_role_key": None}, "SUPABASE_SERVICE_ROLE_KEY"),
    ({"env": {"BULK_SEND_TEMPLATES": "att1_masivo_01", "META_FIRST_TOUCH_RETRY_TEMPLATES": "att1_masivo_01"}},
     "cannot include bulk send templates"),
    ({"env": {"BULK_SEND_SEND_HOURS": "21-9"}}, "BULK_SEND_SEND_HOURS"),
])
def test_on_without_what_makes_it_safe_the_bridge_does_not_start(tmp_path, changes, message) -> None:
    with pytest.raises(BulkSendConfigError, match=message):
        _build(tmp_path, **changes)


def test_the_audience_health_file_serves_when_the_bulk_one_is_not_declared(tmp_path) -> None:
    health = tmp_path / "salud.json"
    health.write_text("{}", encoding="utf-8")
    assert _build(tmp_path, env={"BULK_SEND_META_HEALTH_PATH": None,
                                 "FLOW_AUDIENCE_META_HEALTH_PATH": str(health)}) is not None


def test_the_lock_dir_is_never_created_by_the_bridge(tmp_path) -> None:
    missing = tmp_path / "sin-montar"
    with pytest.raises(BulkSendConfigError):
        _build(tmp_path, env={"BULK_SEND_PORTFOLIO_LOCK_DIR": str(missing)})
    assert not missing.exists()


# ------------------------------------------------------------------ la base


def test_the_engine_store_calls_the_functions_of_the_migrations() -> None:
    prepared = {"position": 1, "contact_id": 1001, "conversation_id": 501, "source_id": "573001234567",
                "label": "vivo-29-11", "how": "nuevo", "prepared_at": "2026-11-28T20:00:00Z"}
    rest = Rest({
        "read_bulk_sends_v1": [view()], "read_bulk_send_v1": view(),
        "transition_bulk_send_v1": {"ok": True, "state": "frenado"},
        "reserve_bulk_send_recipient_v1": {"ok": True, "phone": "573001234567", "first_name": "Ana", "attempt": 1},
        "close_bulk_send_recipient_v1": {"ok": True, "state": "enviado"},
        "expire_bulk_send_reservations_v1": {"ok": True, "dudosos": 0},
        "read_bulk_send_batch_v1": [{"position": 0, "phone": "573001234560", "first_name": None}],
        "read_bulk_send_prepared_v1": [prepared],
        "read_bulk_send_members_v1": [{"position": 2, "phone": "573001234562", "first_name": "Eva"}],
        "record_bulk_send_prepared_v1": {"ok": True, "position": 1},
        "purge_bulk_list_phones_v1": {"ok": True, "lists": 0, "sends_braked": 0, "prepared": 0},
    })
    store = rest.store()
    assert run(store.read_sends()) == [view()]
    assert run(store.read_send(SEND_ID))["send_id"] == SEND_ID
    run(store.transition(SEND_ID, "frenado", "a_mano"))
    run(store.reserve(SEND_ID, 3))
    run(store.close(SEND_ID, 3, {"state": "enviado", "chatwoot_conversation_id": 9, "chatwoot_message_id": 10}))
    run(store.expire(SEND_ID, 1))
    assert run(store.read_batch(SEND_ID, 25)) == [{"position": 0, "phone": "573001234560", "first_name": None}]
    got = run(store.read_prepared(SEND_ID, [1, 2]))
    assert got == {1: PreparedConversation(position=1, contact_id=1001, conversation_id=501,
                                           source_id="573001234567", label="vivo-29-11", how="nuevo")}
    assert run(store.read_prepared(SEND_ID, [])) == {}
    run(store.read_members(SEND_ID, after_position=-1, limit=25))
    run(store.record_prepared(SEND_ID, got[1]))
    run(store.purge(CENTER))
    assert rest.calls == [
        ("read_bulk_sends_v1", {"p_instance": INSTANCE, "p_limit": 50}),
        ("read_bulk_send_v1", {"p_instance": INSTANCE, "p_send_id": SEND_ID}),
        ("transition_bulk_send_v1", {"p_instance": INSTANCE, "p_send_id": SEND_ID, "p_to": "frenado",
                                     "p_reason": "a_mano"}),
        ("reserve_bulk_send_recipient_v1", {"p_send_id": SEND_ID, "p_position": 3, "p_retry_failed": False}),
        ("close_bulk_send_recipient_v1", {"p_send_id": SEND_ID, "p_position": 3, "p_outcome": {
            "state": "enviado", "chatwoot_conversation_id": 9, "chatwoot_message_id": 10}}),
        ("expire_bulk_send_reservations_v1", {"p_send_id": SEND_ID, "p_minutes": 1}),
        ("read_bulk_send_batch_v1", {"p_send_id": SEND_ID, "p_limit": 25}),
        ("read_bulk_send_prepared_v1", {"p_send_id": SEND_ID, "p_positions": [1, 2]}),
        ("read_bulk_send_members_v1", {"p_send_id": SEND_ID, "p_after_position": -1, "p_limit": 25}),
        ("record_bulk_send_prepared_v1", {"p_send_id": SEND_ID, "p_position": 1, "p_prepared": {
            "contact_id": 1001, "conversation_id": 501, "source_id": "573001234567", "label": "vivo-29-11",
            "how": "nuevo"}}),
        ("purge_bulk_list_phones_v1", {"p_now": "2026-11-29T20:00:00+00:00"}),
    ]


@pytest.mark.parametrize(("response", "error", "reason"), [
    (httpx.Response(400, json={"code": "P0002", "message": "bulk_send_not_found"}), wiring.BulkSendStoreRejected,
     "bulk_send_not_found"),
    (httpx.Response(400, json={"code": "22023", "message": "bulk_send_close_invalid"}), wiring.BulkSendStoreRejected,
     "bulk_send_close_invalid"),
    (httpx.Response(500, json={"code": "XX000", "message": "algo con texto de postgres"}),
     wiring.BulkSendStoreUnavailable, "read_bulk_send_v1_http_500"),
    (httpx.Response(404, json={"code": "PGRST202", "message": "Could not find the function"}),
     wiring.BulkSendStoreUnavailable, "read_bulk_send_v1_http_404"),
    (httpx.Response(200, json=[]), wiring.BulkSendStoreUnavailable, "read_bulk_send_v1_invalid_shape"),
    (httpx.Response(200, content=b"<html>"), wiring.BulkSendStoreUnavailable, "read_bulk_send_v1_not_json"),
])
def test_the_store_fails_closed(response, error, reason) -> None:
    store = Rest({"read_bulk_send_v1": response}).store()
    with pytest.raises(error) as raised:
        run(store.read_send(SEND_ID))
    assert raised.value.reason == reason


@pytest.mark.parametrize("row", [
    {"position": 1, "contact_id": 0, "conversation_id": 501, "source_id": "57", "label": None, "how": "nuevo"},
    {"position": 1, "contact_id": 1, "conversation_id": 501, "source_id": "", "label": None, "how": "nuevo"},
    {"position": 1, "contact_id": 1, "conversation_id": 501, "source_id": "57", "label": None, "how": "otro"},
    {"position": True, "contact_id": 1, "conversation_id": 501, "source_id": "57", "label": None, "how": "nuevo"},
])
def test_a_prepared_row_without_the_shape_is_not_used(row) -> None:
    store = Rest({"read_bulk_send_prepared_v1": [row]}).store()
    with pytest.raises(wiring.BulkSendStoreUnavailable):
        run(store.read_prepared(SEND_ID, [1]))


def test_a_batch_row_without_a_phone_is_not_sent() -> None:
    store = Rest({"read_bulk_send_batch_v1": [{"position": 0, "phone": None, "first_name": None}]}).store()
    with pytest.raises(wiring.BulkSendStoreUnavailable):
        run(store.read_batch(SEND_ID, 25))


def test_the_prepare_store_tolerates_a_send_that_moved_on() -> None:
    rest = Rest({"read_bulk_send_v1": view(state="confirmado"),
                 "transition_bulk_send_v1": {"ok": False, "reason_code": "transition_not_allowed", "state": "frenado"},
                 "read_bulk_send_members_v1": [{"position": 2, "phone": "573001234562", "first_name": "Eva"}],
                 "record_bulk_send_prepared_v1": lambda body: {"ok": False, "reason_code": "already_prepared",
                                                               "conversation_id": 501}})
    prepare = wiring.SupabasePrepareStore(rest.store())
    snapshot = run(prepare.read_send(SEND_ID))
    assert (snapshot.state, snapshot.campaign_label) == ("confirmado", "vivo-29-11")
    run(prepare.start_preparing(SEND_ID))  # frenado entre medio: no levanta, la vuelta siguiente lo ve
    assert run(prepare.read_members(SEND_ID, after_position=-1, limit=25)) == [
        BulkMember(position=2, phone="573001234562", first_name="Eva")]
    run(prepare.record_prepared(SEND_ID, PreparedConversation(
        position=2, contact_id=1002, conversation_id=502, source_id="573001234562", label="vivo-29-11", how="nuevo")))
    assert rest.calls[1] == ("transition_bulk_send_v1", {"p_instance": INSTANCE, "p_send_id": SEND_ID,
                                                         "p_to": "preparando", "p_reason": None})


# ------------------------------------------------------------------ los vetos


def test_the_page_reviewer_takes_the_products_from_the_send(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_apply(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(wiring, "apply_list_vetoes", fake_apply)
    store = SupabaseBulkVetoStore(base_url="https://att1-rest.internal", service_role_key=SERVICE_KEY,
                                  instance=INSTANCE)
    reviewer = wiring.BulkListVetoReviewer(store=store, chatwoot=object(), inbox_id=11)
    out = run(reviewer.review(send_id=SEND_ID, list_id=LIST_ID,
                              members=[ListMember(0, "573001234560"), ListMember(1, "5215512345678")],
                              ghl_opt_out_phones=["573009999999"]))
    assert out == {"ok": True}
    assert seen["product_refs"] == () and seen["list_id"] == LIST_ID and seen["store"] is store
    assert [(m.position, m.phone) for m in seen["members"]] == [(0, "573001234560"), (1, "5215512345678")]
    assert seen["ghl_opt_out_phones"] == ["573009999999"]
    assert seen["chatwoot"].__class__.__name__ == "ChatwootVetoReader"


def test_the_last_minute_vetoes_of_prepare_come_from_the_base_for_those_people() -> None:
    rest = Rest({"read_bulk_send_v1": view(),
                 "read_bulk_list_base_vetoes_v1": [{"position": 0, "reason": "compro"},
                                                   {"position": 7, "reason": "baja"}]})
    vetoes = SupabaseBulkVetoStore(base_url="https://att1-rest.internal", service_role_key=SERVICE_KEY,
                                   instance=INSTANCE, transport=rest.transport())
    check = wiring.base_vetoes_for_prepare(store=rest.store(), vetoes=vetoes, send_id=SEND_ID)
    got = run(check([BulkMember(0, "573001234560"), BulkMember(1, "573001234561")]))
    assert got == {0: "compro"}
    assert rest.calls[1] == ("read_bulk_list_base_vetoes_v1", {"p_instance": INSTANCE, "p_list_id": LIST_ID,
                                                               "p_product_refs": []})


# ------------------------------------------------------------------ la etiqueta de la campana


class InnerSender:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_first_touch(self, **kwargs: Any) -> FirstTouchResult:
        self.calls.append(kwargs)
        if kwargs.get("before_send") is not None:
            await kwargs["before_send"](77)
        return FirstTouchResult("sent", 77, 7001, None)

    async def send_first_touch_to_conversation(self, **kwargs: Any) -> FirstTouchResult:
        self.calls.append(kwargs)
        return FirstTouchResult("sent", kwargs["conversation_id"], 7002, None)


def _chatwoot_labels(current: list[str]) -> tuple[httpx.MockTransport, list[tuple[str, str, Any]]]:
    seen: list[tuple[str, str, Any]] = []
    posted = PREPARE_DOC["conversation_labels_post"]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        assert request.url.path == "/api/v1/accounts/2/conversations/77/labels"
        if request.method == "GET":
            return httpx.Response(200, json={"payload": current})
        assert request.method == "POST"
        return httpx.Response(posted["status"], json={"payload": sorted(body["labels"])})

    return httpx.MockTransport(handler), seen


def _labeled_sender(transport: httpx.MockTransport, rest: Rest) -> tuple[wiring.CampaignLabelSender, InnerSender]:
    control = ChatwootClient(base_url="https://chatwoot.test", account_id=2, access_token="control",
                             transport=transport)
    prepare = BulkPrepareChatwoot(base_url="https://chatwoot.test", account_id=2, access_token="control",
                                  inbox_id=11, transport=transport)
    inner = InnerSender()
    sender = wiring.CampaignLabelSender(sender=inner, labels=wiring.campaign_labels(rest.store()),  # type: ignore[arg-type]
                                        label_conversation=wiring.conversation_labeler(control=control,
                                                                                       prepare=prepare))
    return sender, inner


def test_an_unprepared_person_gets_the_campaign_label_before_the_template_without_losing_others() -> None:
    transport, seen = _chatwoot_labels(["cliente_potencial"])
    rest = Rest({"read_bulk_send_v1": view()})
    sender, inner = _labeled_sender(transport, rest)
    for position in (3, 4):
        result = run(sender.send_first_touch(phone="573001234563", buyer_name="Ana", buyer_email=None,
                                             content="Hola", delivery_id=f"bulk:{SEND_ID}:{position}",
                                             template_params={"name": "x"}))
        assert result.status == "sent"
    assert seen[:2] == [("GET", "/api/v1/accounts/2/conversations/77/labels", None),
                        ("POST", "/api/v1/accounts/2/conversations/77/labels",
                         {"labels": ["cliente_potencial", "vivo-29-11"]})]
    assert inner.calls[0]["template_params"] == {"name": "x"} and inner.calls[0]["before_send"] is not None
    # La etiqueta del envio se lee una vez.
    assert [c[0] for c in rest.calls] == ["read_bulk_send_v1"]
    # Una preparada ya la tiene: va directo, sin tocar etiquetas.
    run(sender.send_first_touch_to_conversation(conversation_id=501, phone="573001234567", buyer_name="Eva",
                                                content="Hola", delivery_id=f"bulk:{SEND_ID}:1", template_params=None))
    assert "before_send" not in inner.calls[-1] and len(seen) == 4


def test_the_label_already_there_is_not_posted_again() -> None:
    transport, seen = _chatwoot_labels(["vivo-29-11"])
    sender, _ = _labeled_sender(transport, Rest({"read_bulk_send_v1": view()}))
    run(sender.send_first_touch(phone="573001234563", buyer_name=None, buyer_email=None, content="Hola",
                                delivery_id=f"bulk:{SEND_ID}:3"))
    assert [method for method, _, _ in seen] == ["GET"]


def test_without_a_campaign_or_with_the_base_down_the_template_goes_without_label() -> None:
    transport, seen = _chatwoot_labels([])
    for rest in (Rest({"read_bulk_send_v1": view(campaign_label=None)}),
                 Rest({"read_bulk_send_v1": httpx.Response(503, json={})})):
        sender, inner = _labeled_sender(transport, rest)
        result = run(sender.send_first_touch(phone="573001234563", buyer_name=None, buyer_email=None,
                                             content="Hola", delivery_id=f"bulk:{SEND_ID}:3"))
        assert result.status == "sent" and inner.calls[0]["before_send"] is None
    assert seen == []


# ------------------------------------------------------------------ el motor real sobre la base real


class Producer:
    def __init__(self) -> None:
        self.commands: list[Any] = []

    async def admit(self, command: Any) -> object:
        self.commands.append(command)
        return object()


def test_the_real_engine_over_the_real_store_brakes_a_retried_template_and_warns(tmp_path) -> None:
    state = {"send": view()}

    def transition(body: dict[str, Any]) -> dict[str, Any]:
        assert body["p_to"] == "frenado"
        state["send"] = view(state="frenado", brake_reason=body["p_reason"], braked_by="motor")
        return {"ok": True, "state": "frenado"}

    rest = Rest({"read_bulk_sends_v1": lambda _: [state["send"]], "transition_bulk_send_v1": transition})
    producer = Producer()
    bulk = _build(tmp_path, env={"META_FIRST_TOUCH_RETRY_TEMPLATES": "att1_carrito_abandonado_01"},
                  supabase_transport=rest.transport(), producer=producer, clock=lambda: CENTER)
    assert bulk is not None
    report = run(bulk.worker.tick())
    assert report.outcome == "frenado:plantilla_con_reintento" and report.send_id == SEND_ID
    assert [c[0] for c in rest.calls] == ["read_bulk_sends_v1", "transition_bulk_send_v1"]
    assert rest.calls[1][1] == {"p_instance": INSTANCE, "p_send_id": SEND_ID, "p_to": "frenado",
                                "p_reason": "plantilla_con_reintento"}
    assert [(c.event_code, c.reason_code) for c in producer.commands] == [("SYS-007", "plantilla_con_reintento")]
    # La pasada siguiente ve el envio frenado y no hace nada.
    assert run(bulk.worker.tick()).outcome == "esperando"


def test_the_engine_reads_the_portfolio_health_of_every_number_with_the_instance_token(tmp_path) -> None:
    raw = json.loads((FIXTURES / "bulk_send_worker" / "meta_phone_health_att1_maribel_20261125.json")
                     .read_text("utf-8"))["crudas"]
    numbers = {"100000000001": "maribel", "100000000002": "att1"}
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        number = request.url.path.rsplit("/", 1)[1]
        asked.append(number)
        assert request.headers["Authorization"] == "Bearer EAA" + "x" * 40
        return httpx.Response(200, json=copy.deepcopy(raw[numbers[number]]))

    meta = MetaTemplatesClient(access_token="EAA" + "x" * 40, waba_id="1234567890123",
                               transport=httpx.MockTransport(handler))
    bulk = _build(tmp_path, env={"BULK_SEND_PORTFOLIO_NUMBERS": "att1=100000000002,maribel=100000000001"},
                  meta_client=meta)
    assert bulk is not None
    readings = run(bulk.worker._health.read())  # noqa: SLF001 - el lector que arma el cableado
    assert set(readings) == {"att1", "maribel"} and all("error" not in r for r in readings.values())
    assert sorted(asked) == ["100000000001", "100000000002"]


# ------------------------------------------------------------------ la purga


def test_the_purge_runs_and_a_failure_is_only_a_readiness_mark(tmp_path) -> None:
    rest = Rest({"purge_bulk_list_phones_v1": {"ok": True, "lists": 2, "sends_braked": 0, "prepared": 40}})
    bulk = _build(tmp_path, supabase_transport=rest.transport(), clock=lambda: CENTER)
    assert bulk is not None
    assert run(bulk.purge_once()) == "ok" and bulk.readiness()["bulk_send_purge"] == "ok"
    rest.answers["purge_bulk_list_phones_v1"] = httpx.Response(503, json={})
    assert run(bulk.purge_once()) == "failed" and bulk.readiness()["bulk_send_purge"] == "failed"


def test_start_and_stop_run_the_worker_and_the_purge(tmp_path) -> None:
    rest = Rest({"purge_bulk_list_phones_v1": {"ok": True}, "read_bulk_sends_v1": []})
    bulk = _build(tmp_path, supabase_transport=rest.transport(), clock=lambda: CENTER)
    assert bulk is not None

    async def cycle() -> None:
        await bulk.start()
        for _ in range(50):
            if {"purge_bulk_list_phones_v1", "read_bulk_sends_v1"} <= {c[0] for c in rest.calls}:
                break
            await asyncio.sleep(0.01)
        await bulk.stop()

    run(cycle())
    assert {"purge_bulk_list_phones_v1", "read_bulk_sends_v1"} <= {c[0] for c in rest.calls}
    assert not (tmp_path / "portfolio" / "masivo.lock").exists() or not bulk.worker._lock.held  # noqa: SLF001


def test_nothing_here_writes_a_phone_to_the_log(tmp_path, caplog) -> None:
    caplog.set_level("INFO")
    rest = Rest({"read_bulk_send_v1": view(), "read_bulk_list_base_vetoes_v1": [{"position": 0, "reason": "baja"}]})
    vetoes = SupabaseBulkVetoStore(base_url="https://att1-rest.internal", service_role_key=SERVICE_KEY,
                                   instance=INSTANCE, transport=rest.transport())
    run(wiring.base_vetoes_for_prepare(store=rest.store(), vetoes=vetoes, send_id=SEND_ID)(
        [BulkMember(0, "573001234560")]))
    _build(tmp_path)
    assert "573001234560" not in caplog.text and "100000000002" not in caplog.text
    assert "bulk_send_enabled instance=att1 numbers=att1,johanna,maribel" in caplog.text


# ------------------------------------------------------------------ el reporte (D10)
#
# La fila de ``read_bulk_send_recipients_v1`` (20261125000400) tiene la forma de la funcion; lo que la base
# devuelve de verdad lo prueba ``validate_bulk_send_recipients_read.mjs``. La conversacion que se lee es la
# capturada de ATT1 (``chatwoot_conversation_inbox_11_conv_50_opt_out_paused_20261017.json``: cuenta 2,
# bandeja 11, 2026-10-17, anonimizada), servida por el ``ChatwootClient`` real como en
# ``test_bulk_send_report_chatwoot.py``.

CONV_50 = json.loads((FIXTURES / "chatwoot_conversation_inbox_11_conv_50_opt_out_paused_20261017.json")
                     .read_text("utf-8"))["conversation"]
TEMPLATE_50 = next(m for m in CONV_50["messages"] if m["id"] == 2820)


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def recipient_row(position: int, state: str, **changes: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"position": position, "phone": f"52155000000{position:02d}", "state": state,
                           "chatwoot_conversation_id": None, "chatwoot_message_id": None, "meta_error_code": None,
                           "closed_at": None, "purchased_at": None}
    out.update(changes)
    return out


REGISTER = [
    # La plantilla de la conversacion 50: leida, contesto, pidio la baja y esta con el equipo; despues compro.
    recipient_row(0, "enviado", chatwoot_conversation_id=50, chatwoot_message_id=2820,
                  closed_at=_iso(TEMPLATE_50["created_at"]), purchased_at=_iso(TEMPLATE_50["created_at"] + 3600)),
    recipient_row(1, "fallido", closed_at=_iso(TEMPLATE_50["created_at"])),
    recipient_row(2, "pendiente"),
    # Un enviado sin conversacion no se puede leer: dudoso, nunca entregado.
    recipient_row(3, "enviado", chatwoot_message_id=2821, closed_at=_iso(TEMPLATE_50["created_at"]),
                  purchased_at=_iso(TEMPLATE_50["created_at"] + 60)),
    recipient_row(4, "reservado"),
]


class _Conversation50:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        base = "/api/v1/accounts/2/conversations/50"
        if request.url.path == base:
            return httpx.Response(200, json=CONV_50["conversation"])
        if request.url.path == base + "/messages":
            before = request.url.params.get("before")
            older = [m for m in sorted(CONV_50["messages"], key=lambda m: m["id"])
                     if before is None or m["id"] < int(before)]
            return httpx.Response(200, json={"meta": {}, "payload": older[-20:]})
        return httpx.Response(404, json={"error": "not found"})


def _paged(register: list[dict[str, Any]], size: int):
    def answer(body: dict[str, Any]) -> list[dict[str, Any]]:
        assert body["p_send_id"] == SEND_ID and body["p_limit"] == size
        return [r for r in register if r["position"] > body["p_after_position"]][:size]
    return answer


def _report_source(rest: Rest, chatwoot: _Conversation50, clock: Any = lambda: CENTER) -> wiring.BulkSendRecipientsReport:
    client = ChatwootClient(base_url="https://chatwoot.invalid", account_id=2, access_token="token-de-prueba",
                            transport=httpx.MockTransport(chatwoot.handler))
    return wiring.BulkSendRecipientsReport(store=rest.store(), chatwoot=ChatwootBulkSendReader(client, inbox_id=11),
                                           clock=clock)


def test_the_report_reads_the_whole_register_by_cursor_and_the_captured_conversation(monkeypatch) -> None:
    monkeypatch.setattr(wiring, "RECIPIENTS_PAGE", 2)
    rest = Rest({"read_bulk_send_recipients_v1": _paged(REGISTER, 2)})
    chatwoot = _Conversation50()

    report = run(_report_source(rest, chatwoot).report(SEND_ID))

    assert [c[1]["p_after_position"] for c in rest.calls] == [-1, 1, 3]
    assert (report.sent, report.delivered, report.read, report.failed) == (1, 1, 1, 0)
    assert (report.replies, report.opt_outs, report.handoffs, report.purchases) == (1, 1, 1, 1)
    assert report.not_sent == {"dubious": 1, "pending": 2, "send_failed": 1}
    # Solo se leyo la conversacion mandada, por su id: nunca se buscan contactos.
    assert {r.url.path for r in chatwoot.requests} == {"/api/v1/accounts/2/conversations/50",
                                                      "/api/v1/accounts/2/conversations/50/messages"}


def test_the_report_is_kept_for_a_while_and_then_read_again() -> None:
    now = [CENTER]
    rest = Rest({"read_bulk_send_recipients_v1": _paged(REGISTER, wiring.RECIPIENTS_PAGE)})
    source = _report_source(rest, _Conversation50(), clock=lambda: now[0])

    first = run(source.report(SEND_ID))
    again = run(source.report(SEND_ID))
    assert again is first and len(rest.calls) == 1
    now[0] = CENTER + wiring.REPORT_TTL
    run(source.report(SEND_ID))
    assert len(rest.calls) == 2


@pytest.mark.parametrize("row", [
    recipient_row(0, "entregado"),
    recipient_row(-1, "pendiente"),
    recipient_row(0, "enviado", chatwoot_conversation_id=0, chatwoot_message_id=1, closed_at=_iso(1)),
    recipient_row(0, "enviado", chatwoot_conversation_id=5, chatwoot_message_id=1, closed_at="2026-11-29T20:00:00"),
    recipient_row(0, "pendiente", phone=5215500000000),
    "no es una fila",
])
def test_a_register_row_without_the_shape_fails_closed(row) -> None:
    rest = Rest({"read_bulk_send_recipients_v1": [row]})
    with pytest.raises(wiring.BulkSendStoreUnavailable, match="read_bulk_send_recipients_v1_invalid_row"):
        run(_report_source(rest, _Conversation50()).report(SEND_ID))


def test_a_cursor_that_does_not_advance_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(wiring, "RECIPIENTS_PAGE", 2)
    rest = Rest({"read_bulk_send_recipients_v1": [recipient_row(0, "pendiente"), recipient_row(1, "pendiente")]})
    with pytest.raises(wiring.BulkSendStoreUnavailable, match="cursor_did_not_advance"):
        run(_report_source(rest, _Conversation50()).report(SEND_ID))


def test_a_purged_list_still_reports_without_phones() -> None:
    recipient, purchase = wiring.recipient_of(recipient_row(7, "vetado", phone=None, closed_at=_iso(1)))
    assert (recipient.recipient_id, recipient.phone, recipient.state, purchase) == ("7", "", "vetoed", None)
    # Una compra de quien no salio no se ata.
    _, purchase = wiring.recipient_of(recipient_row(8, "fallido", closed_at=_iso(1), purchased_at=_iso(2)))
    assert purchase is None


def test_the_page_gets_the_report(tmp_path, monkeypatch) -> None:
    seen: dict[str, Any] = {}
    real = wiring.page_routes

    def capture(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(wiring, "page_routes", capture)
    assert _build(tmp_path) is not None
    assert isinstance(seen["report"], wiring.BulkSendRecipientsReport)
