"""Los vetos de la lista de un masivo (encargo 4 del mapa «El masivo manejado por el AE»).

Modulo: ``bridge.bulk_send_vetoes``; la base, 20261125000200 (``apply_bulk_list_vetoes_v1``).
Lo que la base hace de verdad con las exclusiones lo prueba PGlite:
``tests/sql/followup_engine/validate_bulk_send_vetoes.mjs`` aplica el mismo
``tests/fixtures/bulk_send_vetoes/apply_payload_20261125.json`` que este archivo arma y compara.

Chatwoot es el ``ChatwootClient`` real contra una API emulada con items capturados:

* los items del listado salen de ``chatwoot_contact_conversations_inbox_13_contact_767_20261029.json``
  (GET de produccion de Maribel, inbox 13, 29/10; la forma de un item del listado: ``status``,
  ``labels``, ``last_activity_at``, ``meta.sender`` con ``phone_number`` y ``blocked``; sin
  ``meta.assignee`` cuando no hay asignacion). A cada copia se le cambian id, telefono,
  etiquetas, estado, actividad, ``blocked`` y, para «con equipo», se le agrega ``meta.assignee``
  (su contenido no se lee, solo su presencia, como ``ChatwootClient._conversation_has_assignee``);
* la pagina del listado, ``{"data": {"meta": {"all_count"}, "payload"}}``, con la forma de
  ``chatwoot_daily_feedback_conversations_20260926.json``;
* los mensajes salen de ``chatwoot_conversation_messages_inbox_13_conv_6_20261029.json``
  (15 mensajes reales de la 6, el ultimo un entrante), corridos en el tiempo.

La base del setter (bajas y compras) es un doble del lector propuesto
``read_bulk_list_base_vetoes_v1``: la funcion no existe todavia (ver el PR).
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from bridge import bulk_send_vetoes as bv
from bridge.chatwoot import ChatwootClient

FIXTURES = Path(__file__).parent / "fixtures"
LISTING = json.loads((FIXTURES / "chatwoot_contact_conversations_inbox_13_contact_767_20261029.json")
                     .read_text(encoding="utf-8"))
MESSAGES_6 = json.loads((FIXTURES / "chatwoot_conversation_messages_inbox_13_conv_6_20261029.json")
                        .read_text(encoding="utf-8"))
GOLDEN = FIXTURES / "bulk_send_vetoes" / "apply_payload_20261125.json"

ACCOUNT = 3
INBOX = 13
NOW = datetime(2026, 11, 29, 22, 0, tzinfo=timezone.utc)
NOW_EPOCH = int(NOW.timestamp())
LIST_ID = "0000000b-0000-4000-8000-0000000b0004"
INSTANCE = {"tenant_ref": "lancemos", "ally_ref": "maribel", "funnel_ref": "maribel"}
PRODUCTS = ("maribel",)

TEMPLATE_ITEM = next(c for c in LISTING["payload"] if c["id"] == 6)  # abierta, sin asignar, ultimo un entrante
assert "assignee" not in TEMPLATE_ITEM["meta"]


def _phone(n: int) -> str:
    """Un movil de Mexico sintetico en forma de envio (521 + 10)."""
    return f"52155{n:08d}"


def conversation(cid: int, n: int | None, *, status: str = "resolved", labels: Sequence[str] = (),
                 hours_ago: float = 48.0, assignee: bool = False, blocked: bool = False,
                 cw_phone: str | None = None) -> dict[str, Any]:
    item = copy.deepcopy(TEMPLATE_ITEM)
    item["id"] = cid
    item["status"] = status
    item["labels"] = list(labels)
    item["last_activity_at"] = NOW_EPOCH - int(hours_ago * 3600)
    item["timestamp"] = item["last_activity_at"]
    sender = item["meta"]["sender"]
    sender["blocked"] = blocked
    sender["phone_number"] = cw_phone if cw_phone is not None else (None if n is None else "+" + _phone(n))
    if assignee:
        item["meta"]["assignee"] = {"id": 7, "type": "user"}
    for message in item["messages"]:
        message["conversation_id"] = cid
    return item


def messages_shifted(*, last_hours_ago: float, incoming_last: bool = True) -> list[dict[str, Any]]:
    """Los 15 mensajes reales de la 6, corridos para que el ultimo quede ``last_hours_ago`` atras."""
    payload = copy.deepcopy(MESSAGES_6["payload"])
    last = max(m["created_at"] for m in payload)
    shift = NOW_EPOCH - int(last_hours_ago * 3600) - last
    for m in payload:
        m["created_at"] += shift
    if not incoming_last:
        payload = [m for m in payload if m["created_at"] < NOW_EPOCH - 7 * 3600 or m["message_type"] != 0]
    return payload


class FakeChatwoot:
    """La API de Chatwoot que usa el lector: el listado por estado y los mensajes de una conversacion."""

    def __init__(self, items: Sequence[Mapping[str, Any]], messages: Mapping[int, list[dict[str, Any]]],
                 *, fail_status: int | None = None, page_size: int = 25) -> None:
        self.items = list(items)
        self.messages = dict(messages)
        self.fail_status = fail_status
        self.page_size = page_size
        self.paths: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        self.paths.append(url.path)
        assert "/contacts/search" not in url.path, "los vetos nunca buscan contactos"
        assert request.method == "GET", "los vetos solo leen"
        if self.fail_status is not None:
            return httpx.Response(self.fail_status, json={"error": "x"})
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        base = f"/api/v1/accounts/{ACCOUNT}/conversations"
        if url.path == base:
            assert query["inbox_id"] == str(INBOX) and query["sort_by"] == "last_activity_at_desc"
            rows = sorted((i for i in self.items if i["status"] == query["status"]),
                          key=lambda i: -i["last_activity_at"])
            page = int(query["page"])
            chunk = rows[(page - 1) * self.page_size: page * self.page_size]
            return httpx.Response(200, json={"data": {"meta": {"all_count": len(rows)}, "payload": chunk}})
        if url.path.startswith(base + "/") and url.path.endswith("/messages"):
            cid = int(url.path.split("/")[-2])
            if "before" in query:
                return httpx.Response(200, json={"meta": {}, "payload": []})
            return httpx.Response(200, json={"meta": {}, "payload": self.messages.get(cid, [])})
        raise AssertionError(f"ruta inesperada {url.path}")

    def client(self) -> ChatwootClient:
        return ChatwootClient(base_url="https://chatwoot.test", account_id=ACCOUNT, access_token="t",
                              transport=httpx.MockTransport(self.handler))


class FakeBase:
    def __init__(self, rows: Mapping[int, str] | Exception) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    async def base_vetoes(self, *, list_id: str, product_refs: Sequence[str]) -> Mapping[int, str]:
        self.calls.append({"list_id": list_id, "product_refs": list(product_refs)})
        if isinstance(self.rows, Exception):
            raise self.rows
        return dict(self.rows)


def reader(fake: FakeChatwoot, **kwargs: Any) -> bv.ChatwootVetoReader:
    return bv.ChatwootVetoReader(chatwoot=fake.client(), inbox_id=INBOX, clock=lambda: NOW, **kwargs)


# La lista del escenario principal: 16 personas, en el orden del archivo.
MEMBERS = [bv.VetoMember(position=i, phone=_phone(i)) for i in range(16)]
MEMBERS[9] = bv.VetoMember(position=9, phone=_phone(9), ghl_opted_out=True)   # columna de baja de GHL
GHL_FILE = ["+52 1 55 0000 0010"]                                               # segundo archivo: la 10
BASE_ROWS = {11: "baja", 12: "compro", 13: "compro"}


def main_scenario() -> FakeChatwoot:
    items = [
        conversation(100, 0, labels=["cliente"]),                                  # clienta
        conversation(101, 1, labels=["automation_opted_out", "automation_paused"]),  # baja por etiqueta
        conversation(102, 2, status="open", assignee=True),                         # con el equipo (asignada)
        conversation(103, 3, status="pending", labels=["automation_paused"]),       # con el equipo (pausada)
        conversation(104, 4, status="resolved", labels=["automation_paused"]),      # resuelta: no veta
        conversation(105, 5, blocked=True),                                         # bloqueado
        conversation(106, 6, status="open", hours_ago=1),                           # escribio hace 1 h
        conversation(107, 7, status="open", hours_ago=0.5),                         # solo salientes en la ventana
        # la 8 escribio hace 2 h, en Chatwoot con la forma sin el 1 (52 + 10): es la misma persona
        conversation(108, None, status="open", hours_ago=2, cw_phone="+52" + _phone(8)[3:]),
        conversation(113, 13, labels=["cliente"]),                                  # compro y clienta: gana compro
        conversation(115, 15, status="snoozed", hours_ago=30),                      # nada
        conversation(200, 900, status="open", labels=["cliente"], assignee=True),   # no esta en la lista
        conversation(201, None, status="resolved", cw_phone=""),                    # sin telefono y sin marca
    ]
    messages = {
        106: messages_shifted(last_hours_ago=1),
        107: messages_shifted(last_hours_ago=0.5, incoming_last=False)[-5:],
        108: messages_shifted(last_hours_ago=2),
    }
    # La 107: solo los salientes del final, todos dentro de la ventana, menos de una pagina.
    for m in messages[107]:
        m["message_type"] = 1
    return FakeChatwoot(items, messages)


EXPECTED = {0: "clienta", 1: "baja", 2: "con_equipo", 3: "con_equipo", 5: "bloqueado",
            6: "escribio_hace_poco", 8: "escribio_hace_poco", 9: "baja_ghl", 10: "baja_ghl",
            11: "baja", 12: "compro", 13: "compro"}


def test_the_list_vetoes_combine_the_base_chatwoot_and_ghl() -> None:
    fake = main_scenario()
    base = FakeBase(BASE_ROWS)
    result = asyncio.run(bv.build_list_vetoes(
        list_id=LIST_ID, members=MEMBERS, product_refs=PRODUCTS, base=base,
        chatwoot=reader(fake), ghl_opt_out_phones=GHL_FILE))
    assert {e["position"]: e["reason"] for e in result.exclusions} == EXPECTED
    assert [e["position"] for e in result.exclusions] == sorted(EXPECTED)
    assert result.eligible == 16 - len(EXPECTED)
    assert result.excluded_by_reason == {"baja": 2, "baja_ghl": 2, "bloqueado": 1, "clienta": 1,
                                         "compro": 2, "con_equipo": 2, "escribio_hace_poco": 2}
    assert base.calls == [{"list_id": LIST_ID, "product_refs": ["maribel"]}]
    # Solo se leyeron mensajes de las tres de la lista con actividad en la ventana; nada de contactos.
    message_paths = sorted({p for p in fake.paths if p.endswith("/messages")})
    assert message_paths == [f"/api/v1/accounts/{ACCOUNT}/conversations/{c}/messages" for c in (106, 107, 108)]
    assert result.sources["chatwoot_recientes_leidas"] == 3
    # El armado es el que PGlite le aplica a la base (validate_bulk_send_vetoes.mjs).
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert golden["members"] == len(MEMBERS)
    assert golden["p_exclusions"] == [dict(e) for e in result.exclusions]
    assert golden["excluded_by_reason"] == dict(result.excluded_by_reason)
    assert golden["eligible"] == result.eligible


def test_a_resolved_paused_conversation_and_old_activity_do_not_veto() -> None:
    fake = FakeChatwoot([conversation(104, 4, status="resolved", labels=["automation_paused"]),
                         conversation(115, 15, status="snoozed", hours_ago=7)], {})
    vetoes = asyncio.run(reader(fake).vetoes_by_phone([_phone(4), _phone(15)]))
    assert vetoes == {}
    assert not any(p.endswith("/messages") for p in fake.paths)


def test_a_full_page_inside_the_window_vetoes_without_proof() -> None:
    page = []
    for i in range(20):  # 20 salientes en la ultima hora: un entrante anterior no se puede descartar
        m = copy.deepcopy(MESSAGES_6["payload"][1])
        m["id"] = 9000 + i
        m["created_at"] = NOW_EPOCH - 3000 + i
        page.append(m)
    fake = FakeChatwoot([conversation(106, 6, status="open", hours_ago=0.2)], {106: page})
    assert asyncio.run(reader(fake).vetoes_by_phone([_phone(6)])) == {
        bv.canonical_whatsapp_phone(_phone(6)): "escribio_hace_poco"}


def test_a_private_note_is_not_someone_writing() -> None:
    note = copy.deepcopy(MESSAGES_6["payload"][-1])
    note["created_at"] = NOW_EPOCH - 600
    note["private"] = True
    fake = FakeChatwoot([conversation(106, 6, status="open", hours_ago=0.2)], {106: [note]})
    assert asyncio.run(reader(fake).vetoes_by_phone([_phone(6)])) == {}


@pytest.mark.parametrize("case", ["listado_incompleto", "http_500", "sin_telefono", "demasiadas", "forma"])
def test_chatwoot_fails_closed(case: str) -> None:
    kwargs: dict[str, Any] = {}
    if case == "listado_incompleto":
        # 30 resueltas y techo de 1 pagina de 25: el listado no cierra.
        fake = FakeChatwoot([conversation(300 + i, 300 + i) for i in range(30)], {})
        kwargs["max_pages"] = 1
        phones = [_phone(300)]
        expected = "chatwoot_listado:ChatwootProtocolError"
    elif case == "http_500":
        fake = FakeChatwoot([], {}, fail_status=500)
        phones = [_phone(1)]
        expected = "chatwoot_listado:HTTPStatusError"
    elif case == "sin_telefono":
        fake = FakeChatwoot([conversation(201, None, cw_phone="", labels=["automation_opted_out"])], {})
        phones = [_phone(1)]
        expected = "chatwoot_sin_telefono"
    elif case == "demasiadas":
        fake = FakeChatwoot([conversation(400 + i, 400 + i, status="open", hours_ago=1) for i in range(3)], {})
        kwargs["max_recent_conversations"] = 2
        phones = [_phone(400 + i) for i in range(3)]
        expected = "chatwoot_demasiadas_recientes"
    else:
        broken = conversation(500, 500)
        del broken["meta"]["sender"]["blocked"]
        fake = FakeChatwoot([broken], {})
        phones = [_phone(500)]
        expected = "chatwoot_bloqueado_ilegible"
    with pytest.raises(bv.BulkVetoesUnavailable) as error:
        asyncio.run(reader(fake, **kwargs).vetoes_by_phone(phones))
    assert error.value.reason == expected


# ---------------------------------------------------------------------------
# La base: el adaptador y aplicar.
# ---------------------------------------------------------------------------

class FakePostgrest:
    """PostgREST con la forma de 20261125000200 para apply_bulk_list_vetoes_v1; la funcion
    propuesta read_bulk_list_base_vetoes_v1 contesta lo que se le de (404 si no existe)."""

    def __init__(self, *, base_rows: Any = None, apply_body: Any = None, apply_status: int = 200) -> None:
        self.base_rows = base_rows
        self.apply_body = apply_body
        self.apply_status = apply_status
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        function = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        self.calls.append((function, body))
        if function == "read_bulk_list_base_vetoes_v1":
            if self.base_rows is None:  # la funcion no existe: lo que contesta PostgREST
                return httpx.Response(404, json={"code": "PGRST202", "message": "Could not find the function"})
            return httpx.Response(200, json=self.base_rows)
        if function == "apply_bulk_list_vetoes_v1":
            if self.apply_body is not None:
                return httpx.Response(self.apply_status, json=self.apply_body)
            excluded = len(body["p_exclusions"])
            return httpx.Response(200, json={"ok": True, "excluded": excluded, "eligible": 16 - excluded})
        raise AssertionError(function)

    def store(self) -> bv.SupabaseBulkVetoStore:
        return bv.SupabaseBulkVetoStore(base_url="https://supabase.test", service_role_key="k", instance=INSTANCE,
                                        transport=httpx.MockTransport(self.handler))


def test_apply_sends_the_exclusions_with_the_instance() -> None:
    rest = FakePostgrest(base_rows=[{"position": p, "reason": r} for p, r in BASE_ROWS.items()])
    out = asyncio.run(bv.apply_list_vetoes(store=rest.store(), list_id=LIST_ID, members=MEMBERS,
                                           product_refs=PRODUCTS, chatwoot=reader(main_scenario()),
                                           ghl_opt_out_phones=GHL_FILE))
    assert out["ok"] is True and out["excluded"] == len(EXPECTED) and out["eligible"] == 16 - len(EXPECTED)
    assert [c[0] for c in rest.calls] == ["read_bulk_list_base_vetoes_v1", "apply_bulk_list_vetoes_v1"]
    assert rest.calls[0][1] == {"p_instance": INSTANCE, "p_list_id": LIST_ID, "p_product_refs": ["maribel"]}
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert rest.calls[1][1] == {"p_instance": INSTANCE, "p_list_id": LIST_ID, "p_exclusions": golden["p_exclusions"]}


def test_without_product_refs_the_base_reads_the_products_of_the_send() -> None:
    """El producto lo fija el AE en el borrador (``bulk_sends.product_refs``, 20261125000300): el bridge no lo
    repite, la base recibe una lista vacia y usa la columna (``nullif(p_product_refs, '{}')``)."""
    rest = FakePostgrest(base_rows=[{"position": p, "reason": r} for p, r in BASE_ROWS.items()])
    out = asyncio.run(bv.apply_list_vetoes(store=rest.store(), list_id=LIST_ID, members=MEMBERS,
                                           product_refs=(), chatwoot=reader(main_scenario()),
                                           ghl_opt_out_phones=GHL_FILE))
    assert out["ok"] is True and out["excluded"] == len(EXPECTED)
    assert rest.calls[0][1] == {"p_instance": INSTANCE, "p_list_id": LIST_ID, "p_product_refs": []}


def test_without_the_base_function_nothing_is_written() -> None:
    rest = FakePostgrest(base_rows=None)
    fake = main_scenario()
    with pytest.raises(bv.BulkVetoesUnavailable) as error:
        asyncio.run(bv.apply_list_vetoes(store=rest.store(), list_id=LIST_ID, members=MEMBERS,
                                         product_refs=PRODUCTS, chatwoot=reader(fake)))
    assert error.value.reason == "read_bulk_list_base_vetoes_v1_http_404"
    assert [c[0] for c in rest.calls] == ["read_bulk_list_base_vetoes_v1"]
    assert fake.paths == []  # ni Chatwoot se llego a leer


def test_a_chatwoot_failure_writes_nothing() -> None:
    rest = FakePostgrest(base_rows=[])
    with pytest.raises(bv.BulkVetoesUnavailable):
        asyncio.run(bv.apply_list_vetoes(store=rest.store(), list_id=LIST_ID, members=MEMBERS,
                                         product_refs=PRODUCTS, chatwoot=reader(FakeChatwoot([], {}, fail_status=503))))
    assert [c[0] for c in rest.calls] == ["read_bulk_list_base_vetoes_v1"]


@pytest.mark.parametrize("rows", [{"x": 1}, [{"position": -1, "reason": "baja"}],
                                  [{"position": 1, "reason": "clienta"}], [{"position": True, "reason": "baja"}]])
def test_a_base_row_out_of_contract_fails_closed(rows: Any) -> None:
    with pytest.raises(bv.BulkVetoesUnavailable):
        asyncio.run(FakePostgrest(base_rows=rows).store().base_vetoes(list_id=LIST_ID, product_refs=PRODUCTS))


def test_the_base_rejections_and_state_answers() -> None:
    rejected = FakePostgrest(apply_body={"code": "22023", "message": "bulk_list_vetoes_unknown_position"},
                             apply_status=400)
    with pytest.raises(bv.BulkVetoesRejected) as error:
        asyncio.run(rejected.store().apply(LIST_ID, [{"position": 99, "reason": "baja"}]))
    assert error.value.reason == "bulk_list_vetoes_unknown_position"
    not_draft = FakePostgrest(apply_body={"ok": False, "reason_code": "not_a_draft", "state": "confirmado"})
    assert asyncio.run(not_draft.store().apply(LIST_ID, [])) == {
        "ok": False, "reason_code": "not_a_draft", "state": "confirmado"}
    mismatch = FakePostgrest(base_rows=[], apply_body={"ok": True, "excluded": 0, "eligible": 16})
    with pytest.raises(bv.BulkVetoesUnavailable) as error:
        asyncio.run(bv.apply_list_vetoes(store=mismatch.store(), list_id=LIST_ID, members=MEMBERS,
                                         product_refs=PRODUCTS, chatwoot=reader(main_scenario())))
    assert error.value.reason == "apply_bulk_list_vetoes_v1_count_mismatch"


def test_the_inputs_that_cannot_be_read_stop_the_review() -> None:
    fake = FakeChatwoot([], {})
    with pytest.raises(bv.BulkVetoesUnavailable) as error:
        asyncio.run(bv.build_list_vetoes(list_id=LIST_ID, members=MEMBERS, product_refs=PRODUCTS,
                                         base=FakeBase({}), chatwoot=reader(fake),
                                         ghl_opt_out_phones=["sin numero"]))
    assert error.value.reason == "baja_ghl_ilegible"
    for members, products in (([bv.VetoMember(0, _phone(0)), bv.VetoMember(0, _phone(1))], PRODUCTS),
                              ([bv.VetoMember(0, "abc")], PRODUCTS), (MEMBERS, ("",)), (MEMBERS, (" ",))):
        with pytest.raises(ValueError):
            asyncio.run(bv.build_list_vetoes(list_id=LIST_ID, members=members, product_refs=products,
                                             base=FakeBase({}), chatwoot=reader(fake)))


def test_a_batch_only_gets_its_own_base_rows() -> None:
    """En el envio (encargo 9) se revisa una tanda: las filas de la base de otras posiciones no cuentan."""
    batch = MEMBERS[10:14]
    result = asyncio.run(bv.build_list_vetoes(
        list_id=LIST_ID, members=batch, product_refs=PRODUCTS, base=FakeBase(BASE_ROWS),
        chatwoot=reader(main_scenario())))
    assert {e["position"]: e["reason"] for e in result.exclusions} == {11: "baja", 12: "compro", 13: "compro"}
    assert result.eligible == 1
