"""template-request-v1 1.3.0 (F-146 parte 4, E-361): el sondeo de Meta y el aviso en el hilo de Slack.

Meta y Slack van por los clientes reales con transporte simulado (formas en
fixtures/meta_template_status_borde_20261109.json, documentadas, sin captura:
deuda anotada ahi). La base va en un doble: la semantica real de sus RPC la prueba
tests/sql/followup_engine/validate_meta_template_request_notices.mjs.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge import template_status
from bridge.app import create_app
from bridge.flow_change_api import StoreUnavailable
from bridge.template_request_api import TemplateRequestStore
from bridge.meta_templates import MetaTemplatesClient
from bridge.template_request import number_body
from slack_correlation.client import SlackClient
from test_flow_change_api_app import FakeChatwoot, SharedSupabaseSpy
from test_flow_change_api_app import _settings as _change_settings
from test_template_request_api import APPROVAL

BORDE = json.loads((Path(__file__).parent / "fixtures" / "meta_template_status_borde_20261109.json").read_text("utf-8"))
TOKEN = "EAAtoken-solo-de-plantillas-" + "y" * 40
WABA = "1234567890123"
INSTANCE = {"tenant_ref": "lancemos", "ally_ref": "att1"}
ID = "00000000-0000-4000-8000-000000000001"
META_ID = "4596630157247420"
DM = "D0123456789"
THREAD = "1762700000.123456"
NAME = "att1_pago_efectivo_confirmado_02"
DRAFT = {"nombre": NAME, "idioma": "es_MX", "categoria": "UTILITY", "cuerpo": "Hola {{nombre}}, se confirmó tu pago."}


def _row(**overrides: object) -> dict:
    return {"request_id": ID, "status": "en_revision", "meta_template_id": META_ID, "template_name": NAME,
            "language": "es_MX", "draft": DRAFT, "meta_components": None, **overrides}


def _notice(kind: str = "aprobada", **overrides: object) -> dict:
    return {"request_id": ID, "kind": kind, "channel": DM, "thread_ts": THREAD, "template_name": NAME,
            "status": "en_catalogo", "category_requested": "UTILITY", "meta_category": "UTILITY",
            "meta_rejected_reason": None, **overrides}


class Store:
    def __init__(self, rows: list[dict] | None = None, notices: list[dict] | None = None) -> None:
        self.rows, self.notices = rows or [], notices or []
        self.recorded: list[dict] = []
        self.finished: list[dict] = []
        self.counts: dict[str, int] = {"sent": 2, "no_thread": 1}
        self.down: set[str] = set()
        self.claims = self.lists = 0

    async def list_to_poll(self, instance: dict, *, limit: int) -> dict:
        assert instance == INSTANCE and limit >= 1
        self.lists += 1
        if "list" in self.down:
            raise StoreUnavailable()
        return {"ok": True, "requests": self.rows}

    async def record_status(self, instance: dict, **fields: object) -> dict:
        assert instance == INSTANCE
        if "record" in self.down:
            raise StoreUnavailable()
        self.recorded.append(fields)
        return {"ok": True}

    async def claim_notices(self, instance: dict, *, limit: int) -> dict:
        self.claims += 1
        if "claim" in self.down:
            raise StoreUnavailable()
        notices, self.notices = self.notices, []  # la base no devuelve dos veces lo reclamado
        return {"ok": True, "notices": notices, "counts": self.counts}

    async def finish_notice(self, instance: dict, **fields: object) -> dict:
        self.finished.append(fields)
        if "finish" in self.down:
            raise StoreUnavailable()
        return {"ok": True, "state": fields["outcome"]}


class Graph:
    def __init__(self, answer: dict | None = None, templates: list[dict] | None = None) -> None:
        self.answer, self.templates = answer, templates or []
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.error: dict = {"error": {"code": 190, "message": "token"}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/message_templates"):
            return httpx.Response(200, json={"data": self.templates, "paging": {"cursors": {"after": "fin"}}})
        if self.status != 200:
            return httpx.Response(self.status, json=self.error)
        return httpx.Response(200, json=self.answer)


class Slack:
    def __init__(self, answer: dict | None = None, boom: bool = False) -> None:
        self.answer, self.boom = answer or BORDE["slack_post_ok"], boom
        self.posted: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.boom:
            raise httpx.ReadTimeout("sin respuesta", request=request)
        self.posted.append(json.loads(request.content))
        return httpx.Response(200, json=self.answer)


class Chatwoot:
    def __init__(self, templates: list[dict] | None = None, boom: str = "") -> None:
        self.templates, self.synced, self.boom = templates or [], 0, boom

    async def get_inbox(self, *, inbox_id: int) -> dict:
        assert inbox_id == 11
        if self.boom == "catalog":
            raise httpx.ConnectError("sin chatwoot")
        return {"id": 11, "message_templates": self.templates}

    async def sync_inbox_templates(self, *, inbox_id: int) -> None:
        self.synced += 1
        if self.boom == "sync":
            raise httpx.ConnectError("sin chatwoot")


def _catalog_row() -> dict:
    numbered = number_body(DRAFT["cuerpo"])[0]
    return {"name": NAME, "language": "es_MX", "status": "APPROVED", "category": "UTILITY",
            "components": [{"type": "BODY", "text": numbered}]}


def _worker(store: Store, graph: Graph | None = None, slack: Slack | None = None, chatwoot: Chatwoot | None = None,
            token: bool = True, clock=None) -> template_status.MetaTemplateStatusWorker:
    graph = graph or Graph(BORDE["read_pending"])
    slack = slack or Slack()
    client = SlackClient(bot_token="xoxb-prueba", transport=httpx.MockTransport(slack.handler)) if token else None
    return template_status.MetaTemplateStatusWorker(
        store=store,  # type: ignore[arg-type]
        meta=MetaTemplatesClient(access_token=TOKEN, waba_id=WABA, transport=httpx.MockTransport(graph.handler)),
        chatwoot=chatwoot or Chatwoot(), inbox_id=11, instance=INSTANCE, slack=client,
        **({"clock": clock} if clock is not None else {}))


def _run(worker: template_status.MetaTemplateStatusWorker) -> None:
    asyncio.run(worker.run_once())


# --------------------------------------------------------------------------- el sondeo


def test_a_pending_template_is_recorded_and_says_nothing() -> None:
    store = Store([_row()])
    worker = _worker(store)
    _run(worker)
    assert store.recorded == [{"request_id": ID, "meta_template_id": META_ID, "meta_status": "PENDING",
                               "meta_category": "UTILITY", "rejected_reason": None, "in_catalog": False}]
    assert worker.state == "ok" and worker.last_summary == "polled=1 updated=1 errors=0"


def test_approved_and_in_the_catalog_goes_straight_to_in_catalog_without_sync() -> None:
    store, chatwoot = Store([_row()]), Chatwoot([_catalog_row()])
    _run(_worker(store, Graph(BORDE["read_approved"]), chatwoot=chatwoot))
    assert store.recorded[0]["in_catalog"] is True and chatwoot.synced == 0


def test_approved_and_not_in_the_catalog_asks_for_one_sync_per_cycle() -> None:
    store, chatwoot = Store([_row(), _row(request_id="00000000-0000-4000-8000-000000000002")]), Chatwoot()
    _run(_worker(store, Graph(BORDE["read_approved"]), chatwoot=chatwoot))
    assert [fields["in_catalog"] for fields in store.recorded] == [False, False] and chatwoot.synced == 1


def test_same_name_with_another_body_is_not_in_the_catalog() -> None:
    other = {**_catalog_row(), "components": [{"type": "BODY", "text": "Otro texto"}]}
    store = Store([_row()])
    _run(_worker(store, Graph(BORDE["read_approved"]), chatwoot=Chatwoot([other])))
    assert store.recorded[0]["in_catalog"] is False


def test_rejected_carries_the_reason_and_only_rejected_does() -> None:
    store = Store([_row()])
    _run(_worker(store, Graph(BORDE["read_rejected"])))
    assert store.recorded[0]["meta_status"] == "REJECTED" and store.recorded[0]["rejected_reason"] == "PROMOTIONAL"
    pending_with_reason = {**BORDE["read_pending"], "rejected_reason": "NONE"}
    store = Store([_row()])
    _run(_worker(store, Graph(pending_with_reason)))
    assert store.recorded[0]["rejected_reason"] is None


def test_a_reason_that_is_not_a_code_is_dropped_and_does_not_break_the_row() -> None:
    store = Store([_row()])
    _run(_worker(store, Graph({**BORDE["read_rejected"], "rejected_reason": "texto libre"})))
    assert store.recorded[0]["rejected_reason"] is None


def _sent_without_id(components: list[dict]) -> dict:
    return _row(status="enviada", meta_template_id=None, meta_components=components)


def _found(components: list[dict]) -> dict:
    return {"id": META_ID, "name": NAME, "language": "es_MX", "status": "PENDING", "category": "UTILITY",
            "components": components}


def test_a_send_left_halfway_is_adopted_only_with_the_same_content() -> None:
    components = [{"type": "BODY", "text": "Hola {{1}}, se confirmó tu pago."}]
    store = Store([_sent_without_id(components)])
    _run(_worker(store, Graph(templates=[_found(components)])))
    assert store.recorded[0]["meta_template_id"] == META_ID
    store = Store([_sent_without_id(components)])
    _run(_worker(store, Graph(templates=[_found([{"type": "BODY", "text": "Otra"}])])))
    assert store.recorded == []


def test_a_meta_failure_is_published_and_the_notices_still_go() -> None:
    store, graph, slack = Store([_row()], [_notice()]), Graph(BORDE["read_pending"]), Slack()
    graph.status = 500
    worker = _worker(store, graph, slack)
    _run(worker)
    assert store.recorded == [] and len(slack.posted) == 1
    assert worker.state == "degraded" and worker.last_error == "meta:190"


# --------------------------------------------------------------------------- el aviso


def test_the_notice_goes_to_the_thread_where_it_was_asked() -> None:
    store, slack = Store(notices=[_notice()]), Slack()
    _run(_worker(store, slack=slack))
    assert slack.posted[0]["channel"] == DM and slack.posted[0]["thread_ts"] == THREAD
    assert store.finished == [{"request_id": ID, "kind": "aprobada", "outcome": "sent", "error_code": None}]


def test_a_dm_without_thread_posts_in_the_dm() -> None:
    slack = Slack()
    _run(_worker(Store(notices=[_notice(thread_ts=None)]), slack=slack))
    assert "thread_ts" not in slack.posted[0]


def test_the_worker_posts_only_what_the_base_hands_over() -> None:
    # Que un aviso salga una sola vez lo garantiza la base: seccion 3 del .mjs, con las RPC reales.
    store, slack = Store(notices=[_notice()]), Slack()
    _run(_worker(store, slack=slack))
    _run(_worker(store, slack=slack))
    assert len(slack.posted) == 1 and store.claims == 2


def test_slack_saying_no_is_retried_and_a_lost_answer_is_not() -> None:
    store = Store(notices=[_notice()])
    _run(_worker(store, slack=Slack({**BORDE["slack_post_rejected"], "error": "msg_too_long"})))
    assert store.finished[0]["outcome"] == "retry" and store.finished[0]["error_code"] == "slack_rejected"
    store = Store(notices=[_notice()])
    _run(_worker(store, slack=Slack(boom=True)))
    assert store.finished[0]["outcome"] == "unknown"


def test_without_the_slack_token_nothing_is_claimed_and_ready_says_so() -> None:
    store = Store([_row()], [_notice()])
    worker = _worker(store, token=False)
    _run(worker)
    assert store.claims == 0 and len(store.recorded) == 1
    assert worker.notices_state == "sin_token"


def test_counts_for_ready_come_from_the_base() -> None:
    store = Store(notices=[])
    worker = _worker(store)
    _run(worker)
    assert worker.notices_state == "sent=2 no_thread=1 failed=0 unknown=0 waiting=0"


def test_a_store_failure_on_finish_is_published_and_does_not_crash() -> None:
    # No repetir ese aviso lo hace la base: un reclamo sin cerrar pasa a 'unknown' (seccion 3 del .mjs).
    store, slack = Store(notices=[_notice()]), Slack()
    store.down.add("finish")
    worker = _worker(store, slack=slack)
    _run(worker)
    assert len(slack.posted) == 1 and worker.state == "degraded" and worker.last_error == "store_unavailable"


@pytest.mark.parametrize(("reason", "piece"), [
    ("INCORRECT_CATEGORY", "tipo pedido"), ("PROMOTIONAL", "vende"), ("INVALID_FORMAT", "forma"),
    ("TAG_CONTENT_MISMATCH", "tipo pedido"), ("ABUSIVE_CONTENT", "abusivo"), ("SCAM", "engañoso"),
    ("OTRA_COSA", "no dio un motivo claro"), ("NONE", "no dio un motivo claro"), (None, "cuenta de WhatsApp")])
def test_the_rejection_reason_is_in_business_language(reason: str | None, piece: str) -> None:
    text = template_status.notice_text(_notice("rechazada", status="rechazada", meta_rejected_reason=reason))
    assert piece in text and f"`{NAME}`" in text
    for jargon in ("INCORRECT", "PROMOTIONAL", "UTILITY", "categoría", "solicitud"):
        assert jargon not in text


def test_recategorized_says_both_types_in_words() -> None:
    text = template_status.notice_text(_notice("recategorizada", meta_category="MARKETING"))
    assert "de utilidad" in text and "de marketing" in text and "UTILITY" not in text


def test_approved_says_whether_it_is_usable_yet() -> None:
    assert "Ya se puede usar" in template_status.notice_text(_notice())
    assert "3 horas" in template_status.notice_text(_notice(status="aprobada_meta"))


def test_a_notice_with_an_unknown_kind_is_closed_as_failed_without_posting() -> None:
    store, slack = Store(notices=[_notice("otra")]), Slack()
    _run(_worker(store, slack=slack))
    assert slack.posted == [] and store.finished[0]["outcome"] == "failed"


def _rejected_at_creation(**overrides: object) -> dict:
    return _notice("rechazada", status="rechazada", meta_template_id=META_ID, **overrides)


def test_a_rejection_without_a_stored_reason_is_read_from_meta_before_notifying() -> None:
    store, slack = Store(notices=[_rejected_at_creation()]), Slack()
    _run(_worker(store, Graph(BORDE["read_rejected"]), slack))
    assert "vende" in slack.posted[0]["text"]


def test_a_rejection_whose_reason_cannot_be_read_does_not_claim_meta_gave_none() -> None:
    store, slack, graph = Store(notices=[_rejected_at_creation()]), Slack(), Graph()
    graph.status = 500
    _run(_worker(store, graph, slack))
    assert "cuenta de WhatsApp" in slack.posted[0]["text"] and "no dio un motivo" not in slack.posted[0]["text"]
    assert store.finished[0]["outcome"] == "sent"


@pytest.mark.parametrize("slack_error", ["invalid_auth", "token_revoked", "account_inactive", "channel_not_found",
                                         "not_in_channel"])
def test_a_bad_token_or_channel_waits_without_spending_attempts(slack_error: str) -> None:
    store = Store(notices=[_notice()])
    worker = _worker(store, slack=Slack({"ok": False, "error": slack_error}))
    _run(worker)
    assert store.finished[0]["outcome"] == "waiting" and store.finished[0]["error_code"] == f"slack_blocked:{slack_error}"
    assert worker.state == "degraded" and worker.last_error == f"slack_blocked:{slack_error}"


def test_a_notice_waiting_keeps_ready_degraded_until_the_base_has_none() -> None:
    store = Store(notices=[_notice()])
    worker = _worker(store, slack=Slack({"ok": False, "error": "token_revoked"}))
    _run(worker)
    store.counts = {"waiting": 1}
    _run(worker)  # una vuelta sin nada que postear: sigue roto
    assert worker.state == "degraded" and worker.last_error == "slack_blocked:token_revoked"
    assert "waiting=1" in worker.notices_state
    store.counts = {"sent": 1}
    _run(worker)
    assert worker.state == "ok" and worker.last_error is None


# --------------------------------------------------------------------------- lo que no debe esconderse ni cortar


def test_a_revoked_meta_token_stays_on_ready_through_empty_rounds() -> None:
    store, graph = Store([_row()]), Graph(BORDE["read_pending"])
    graph.status = 400
    worker = _worker(store, graph)
    _run(worker)
    store.rows = []
    _run(worker)  # casi todas las vueltas no tienen nada que leer
    assert worker.state == "degraded" and worker.last_error == "meta:190"
    store.rows, graph.status = [_row()], 200
    _run(worker)
    assert worker.state == "ok" and worker.last_error is None


def test_a_store_failure_clears_once_the_store_answers() -> None:
    store = Store()
    store.down.add("list")
    worker = _worker(store)
    _run(worker)
    assert worker.state == "degraded" and worker.last_error == "store_unavailable"
    store.down.clear()
    _run(worker)
    assert worker.state == "ok" and worker.last_error is None


@pytest.mark.parametrize("step", ["list", "record", "claim"])
def test_the_store_down_at_any_step_is_published_and_does_not_raise(step: str) -> None:
    store = Store([_row()], [_notice()])
    store.down.add(step)
    worker = _worker(store)
    _run(worker)
    assert worker.state == "degraded" and worker.last_error == "store_unavailable"
    assert store.lists == 1 and store.claims == 1


def test_a_429_from_meta_does_not_record_and_the_notices_still_go() -> None:
    store, graph, slack = Store([_row()], [_notice()]), Graph(), Slack()
    graph.status, graph.error = 429, {"algo": "sin la forma de un error de Graph"}
    worker = _worker(store, graph, slack)
    _run(worker)
    assert store.recorded == [] and len(slack.posted) == 1
    assert worker.state == "degraded" and worker.last_error == "meta_http:429"


def test_approved_without_a_known_category_is_not_recorded() -> None:
    store = Store([_row()])
    worker = _worker(store, Graph({**BORDE["read_approved"], "category": "OTRA"}))
    _run(worker)
    assert store.recorded == [] and worker.state == "degraded" and worker.last_error == "meta_unexpected_answer"


@pytest.mark.parametrize("boom", ["catalog", "sync"])
def test_a_chatwoot_failure_does_not_cut_the_record_or_the_notices(boom: str) -> None:
    store, slack = Store([_row()], [_notice()]), Slack()
    worker = _worker(store, Graph(BORDE["read_approved"]), slack, Chatwoot(boom=boom))
    _run(worker)
    assert store.recorded[0]["in_catalog"] is False and len(slack.posted) == 1 and worker.state == "ok"


def test_the_catalog_sync_is_spaced() -> None:
    now = [datetime(2026, 11, 9, 12, 0, tzinfo=UTC)]
    store, chatwoot = Store([_row()]), Chatwoot()
    worker = _worker(store, Graph(BORDE["read_approved"]), chatwoot=chatwoot, clock=lambda: now[0])
    _run(worker)
    _run(worker)
    assert chatwoot.synced == 1
    now[0] += timedelta(minutes=31)
    _run(worker)
    assert chatwoot.synced == 2


def test_a_template_deleted_in_whatsapp_manager_is_recorded_as_gone() -> None:
    store, graph = Store([_row()]), Graph()
    graph.status, graph.error = 400, {"error": {"code": 100, "error_subcode": 33, "message": "no existe"}}
    worker = _worker(store, graph)
    _run(worker)
    assert store.recorded == [{"request_id": ID, "meta_template_id": META_ID, "meta_status": "DELETED",
                               "meta_category": None, "rejected_reason": None, "in_catalog": False}]
    assert worker.state == "ok"


def test_not_found_with_the_template_still_listed_is_an_error_not_a_deletion() -> None:
    # Graph da el mismo 100/33 cuando el token perdio acceso al id: lo que decide es la lista.
    store, graph = Store([_row()]), Graph(templates=[_found([{"type": "BODY", "text": "x"}])])
    graph.status, graph.error = 400, {"error": {"code": 100, "error_subcode": 33, "message": "no existe"}}
    worker = _worker(store, graph)
    _run(worker)
    assert store.recorded == [] and worker.last_error == "meta:100:33"


def test_stop_cancels_the_round_in_flight() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        store = Store()

        async def hang(instance: dict, *, limit: int) -> dict:
            started.set()
            await asyncio.sleep(3600)
            return {"ok": True, "requests": []}

        store.list_to_poll = hang  # type: ignore[method-assign]
        worker = _worker(store)
        worker._interval = 0.001
        await worker.start()
        await asyncio.wait_for(started.wait(), 5)
        began = time.monotonic()
        await asyncio.wait_for(worker.stop(), 3)
        assert time.monotonic() - began < 2 and worker._task is None

    asyncio.run(scenario())


# --------------------------------------------------------------------------- las RPC contra su firma real

MIGRATIONS = Path(__file__).parents[1] / "supabase" / "migrations"
SIGNATURE = re.compile(r"create (?:or replace )?function public\.(\w+)\(\s*(.*?)\)\s*returns", re.S | re.I)


def _signatures() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        for name, params in SIGNATURE.findall(path.read_text("utf-8")):
            found[name] = [piece.split()[0] for piece in params.split(",") if piece.strip()]
    return found


def test_every_rpc_of_the_stores_matches_its_signature_in_the_migrations() -> None:
    calls: list[tuple[str, set[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path.rsplit("/", 1)[-1], set(json.loads(request.content))))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    status = template_status.TemplateStatusStore(base_url="http://db", db_key="k", transport=transport)
    requests = TemplateRequestStore(base_url="http://db", db_key="k", transport=transport)

    async def scenario() -> None:
        await status.list_to_poll(INSTANCE, limit=1)
        await status.record_status(INSTANCE, request_id=ID, meta_template_id=META_ID, meta_status="PENDING",
                                   meta_category=None, rejected_reason=None, in_catalog=False)
        await status.claim_notices(INSTANCE, limit=1)
        await status.finish_notice(INSTANCE, request_id=ID, kind="aprobada", outcome="sent", error_code=None)
        for thread in (None, (DM, THREAD)):
            await requests.save_request(INSTANCE, request_id=None, revision=None, draft=DRAFT, purpose="x",
                                        proposal_id=None, step_pointer=None, requested_by="U0123456789",
                                        slack_thread=thread)

    asyncio.run(scenario())
    signatures = _signatures()
    assert [name for name, _ in calls] == [
        "list_meta_template_requests_to_poll_v1", "record_meta_template_status_v1", "claim_meta_template_notices_v1",
        "finish_meta_template_notice_v1", "save_meta_template_request_draft_v1", "save_meta_template_request_draft_v2"]
    for name, params in calls:
        assert params == set(signatures[name]), name


# --------------------------------------------------------------------------- el arranque y /ready


def _settings(**overrides: object):
    return _change_settings(**{**APPROVAL, "meta_templates_enabled": True, **overrides})


def _app(store: Store | None = None, **overrides: object):
    meta = MetaTemplatesClient(access_token=TOKEN, waba_id=WABA, transport=httpx.MockTransport(Graph(BORDE["read_pending"]).handler))
    return create_app(_settings(**overrides), supabase_client=SharedSupabaseSpy(),  # type: ignore[arg-type]
                      chatwoot_client=FakeChatwoot(),  # type: ignore[arg-type]
                      meta_templates_client=meta, meta_template_status_store=store)  # type: ignore[arg-type]


def test_flag_off_there_is_no_worker_and_ready_does_not_change() -> None:
    app = _app(meta_templates_enabled=False)
    assert getattr(app.state, "meta_template_status_worker", None) is None
    body = TestClient(app).get("/ready").json()
    assert not any(key.startswith("meta_template_") for key in body)


def test_flag_on_ready_reports_the_poll_and_without_token_the_notices_wait() -> None:
    app = _app(Store())
    body = TestClient(app).get("/ready").json()
    assert body["meta_template_status"] == "not_started" and body["meta_template_notices"] == "sin_token"
    assert body["meta_template_status_last_run_at"] == "never" and body["meta_template_status_last_error"] == "none"


def test_flag_on_with_token_ready_counts_notices() -> None:
    body = TestClient(_app(Store(), meta_templates_slack_bot_token="xoxb-prueba")).get("/ready").json()
    assert body["meta_template_notices"] == "sent=0 no_thread=0 failed=0 unknown=0 waiting=0"


def test_a_token_that_is_not_a_bot_token_does_not_take_the_bridge_down() -> None:
    store = Store([_row()], [_notice()])
    app = _app(store, meta_templates_slack_bot_token="xoxp-usuario")
    body = TestClient(app).get("/ready").json()
    assert body["meta_template_notices"] == "token_invalido"
    asyncio.run(app.state.meta_template_status_worker.run_once())
    assert store.claims == 0 and len(store.recorded) == 1


def test_flag_on_without_service_role_ready_says_so() -> None:
    app = _app(supabase_service_role_key=None)
    assert getattr(app.state, "meta_template_status_worker", None) is None
    assert TestClient(app).get("/ready").json()["meta_template_status"] == "sin_service_role"


def test_the_lifespan_runs_the_worker_for_real_and_stops_it() -> None:
    store, slack = Store([_row()], [_notice()]), Slack()
    app = _app(store, meta_templates_slack_bot_token="xoxb-prueba")
    worker = app.state.meta_template_status_worker
    worker._interval = 0.01
    worker._slack = SlackClient(bot_token="xoxb-prueba", transport=httpx.MockTransport(slack.handler))
    seen: dict = {}

    def scenario() -> None:
        with TestClient(app) as client:
            deadline = time.monotonic() + 5
            while not slack.posted and time.monotonic() < deadline:
                time.sleep(0.01)
            seen["ready"] = client.get("/ready").json()

    # Sin pytest-timeout en el repo: si stop() no corta la vuelta, esto falla en vez de colgar la corrida.
    failure: list[BaseException] = []

    def guarded() -> None:
        try:
            scenario()
        except BaseException as error:  # noqa: BLE001 - se relanza en el hilo del test
            failure.append(error)

    thread = threading.Thread(target=guarded, daemon=True)
    thread.start()
    thread.join(20)
    hung = thread.is_alive()
    if hung and worker._task is not None:
        worker._task.get_loop().call_soon_threadsafe(worker._task.cancel)  # libera el hilo: el fallo es el assert
        thread.join(5)
    assert not hung, "el lifespan no se apago: stop() no corto el worker"
    if failure:
        raise failure[0]
    assert seen["ready"]["meta_template_status_last_run_at"] != "never" and store.lists >= 1 and len(slack.posted) == 1
    assert worker._task is None
    lists = store.lists
    time.sleep(0.05)
    assert store.lists == lists
