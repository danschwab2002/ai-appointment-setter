"""Preparar un masivo (src/bridge/bulk_send_prepare.py, encargo 7 del masivo por el AE).

Chatwoot se emula con ``httpx.MockTransport`` y el cliente real (``ChatwootClient``
dentro de ``BulkPrepareChatwoot``), con las reglas de v4.13.0 que importan aca: el
telefono unico por cuenta (422), el filtro exacto por telefono, la conversacion
nueva que nace ``pending``, el POST de etiquetas que REEMPLAZA la lista.

Fixtures:

* las conversaciones del contacto 767 (bandeja 13 de Maribel) y los mensajes de su
  conversacion 6, **capturados** el 2026-10-29
  (``chatwoot_contact_conversations_inbox_13_contact_767_20261029.json`` y
  ``chatwoot_conversation_messages_inbox_13_conv_6_20261029.json``): de ahi sale la
  forma de todo listado de conversaciones y de todo mensaje que el emulador devuelve;
* el filtro de contactos, crear contacto (y su 422), crear conversacion y el POST de
  etiquetas: **fixture de documentacion, falta captura real**
  (``chatwoot_v4_13_bulk_prepare_documentacion_20261125.json``);
* el index de etiquetas: **fixture de documentacion**
  (``automation_expert_chatwoot/chatwoot_v4_13_labels_macros_documentacion_20261101.json``).
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
import pytest

from bridge import bulk_send_prepare as prep
from bridge.bulk_send_prepare import (
    BulkMember,
    BulkPrepareChatwoot,
    PreparedConversation,
    SendSnapshot,
    prepare_bulk_send,
    revalidate_prepared,
)

FIXTURES = Path(__file__).parent / "fixtures"
DOC = json.loads((FIXTURES / "chatwoot_v4_13_bulk_prepare_documentacion_20261125.json").read_text())
LABELS_DOC = json.loads((FIXTURES / "automation_expert_chatwoot" /
                         "chatwoot_v4_13_labels_macros_documentacion_20261101.json").read_text())
CONTACT_767 = json.loads((FIXTURES / "chatwoot_contact_conversations_inbox_13_contact_767_20261029.json")
                         .read_text())
MESSAGES_CONV_6 = json.loads((FIXTURES / "chatwoot_conversation_messages_inbox_13_conv_6_20261029.json")
                             .read_text())
ACCOUNT = 3
INBOX = 13
LABEL = "vivo-29-11"
SEND = "00000000-0000-4000-8000-000000000001"
PREFIX = f"/api/v1/accounts/{ACCOUNT}"
# El ultimo entrante real de la 6 (una imagen sin texto, 28/10 02:38:06Z).
LAST_INBOUND_6 = max(m["created_at"] for m in MESSAGES_CONV_6["payload"]
                     if m["message_type"] == 0 and m["sender"]["type"] == "contact")
LONG_AFTER = LAST_INBOUND_6 + 30 * 86400


class ChatwootEmulator:
    """Lo que preparar le pide a Chatwoot v4.13.0, con estado. Nunca acepta mandar ni buscar."""

    def __init__(self) -> None:
        self.contacts: dict[int, dict] = {}
        self.conversations: dict[int, dict] = {}
        self.messages: dict[int, list[dict]] = {}
        self.labels = [*LABELS_DOC["labels_index_account_2"]["body"]["payload"],
                       {"id": 20, "title": LABEL, "description": None, "color": "#1f93ff",
                        "show_on_sidebar": True}]
        self.requests: list[tuple[str, str]] = []
        self.fail: dict[tuple[str, str], int] = {}  # (metodo, sufijo) -> status
        self.next_contact = 900
        self.next_conversation = 4000

    # --- siembra -----------------------------------------------------------------------------
    def seed_767(self, phone: str = "+520000000767", source_id: str = "520000000767") -> None:
        """El contacto 767 con sus dos conversaciones reales (9 y 6, abiertas, sin etiquetas)."""
        self.contacts[767] = {"id": 767, "phone_number": phone, "blocked": False,
                              "contact_inboxes": [{"source_id": source_id, "inbox": {"id": INBOX}}]}
        for item in CONTACT_767["payload"]:
            self.conversations[item["id"]] = copy.deepcopy(item)
        self.messages[6] = copy.deepcopy(MESSAGES_CONV_6["payload"])
        self.messages[9] = copy.deepcopy(self.conversations[9]["messages"])

    def seed_contact(self, phone: str, *, blocked: bool = False, inbox: int | None = INBOX,
                     source_id: str | None = None) -> int:
        self.next_contact += 1
        cid = self.next_contact
        inboxes = [] if inbox is None else [{"source_id": source_id or phone.lstrip("+"), "inbox": {"id": inbox}}]
        self.contacts[cid] = {"id": cid, "phone_number": phone, "blocked": blocked, "contact_inboxes": inboxes}
        return cid

    def seed_conversation(self, contact_id: int, *, status: str = "open", labels: Sequence[str] = (),
                          assignee: bool = False, activity: int = LAST_INBOUND_6) -> int:
        self.next_conversation += 1
        conv = copy.deepcopy(CONTACT_767["payload"][0])
        conv.update(id=self.next_conversation, inbox_id=INBOX, status=status, labels=list(labels),
                    last_activity_at=activity, messages=[])
        conv["meta"]["sender"]["id"] = contact_id
        if assignee:
            conv["meta"]["assignee"] = {"id": 5, "name": "Persona del equipo"}
        conv["_contact"] = contact_id
        self.conversations[conv["id"]] = conv
        self.messages[conv["id"]] = []
        return conv["id"]

    # --- vistas ------------------------------------------------------------------------------
    def _contact_view(self, contact: dict) -> dict:
        view = copy.deepcopy(DOC["contact_filter_one"]["body"]["payload"][0])
        view.update(id=contact["id"], phone_number=contact["phone_number"], blocked=contact["blocked"])
        template_inbox = view["contact_inboxes"][0]["inbox"]
        view["contact_inboxes"] = [{"source_id": ci["source_id"], "inbox": {**template_inbox, "id": ci["inbox"]["id"]}}
                                   for ci in contact["contact_inboxes"]]
        return view

    def _contact_of(self, conv: dict) -> int:
        return conv.get("_contact", conv["meta"]["sender"]["id"])

    def labels_of(self, conv_id: int) -> list[str]:
        return list(self.conversations[conv_id]["labels"])

    def count(self, method: str, suffix_start: str) -> int:
        return sum(1 for m, p in self.requests if m == method and p.startswith(suffix_start))

    # --- el transporte -----------------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        assert path.startswith(PREFIX), path
        suffix = path[len(PREFIX):]
        self.requests.append((method, suffix))
        assert "/contacts/search" not in suffix, "preparar no puede usar /contacts/search"
        assert not suffix.endswith("/messages") or method == "GET", "preparar no manda nunca"
        for (fm, fs), status in self.fail.items():
            if fm == method and suffix.startswith(fs):
                return httpx.Response(status, json={"error": "falla de prueba"})
        body = json.loads(request.content) if request.content else None
        if method == "GET" and suffix == "/labels":
            return httpx.Response(200, json={"payload": self.labels})
        if method == "POST" and suffix == "/contacts/filter":
            rule = body["payload"][0]
            assert rule["attribute_key"] == "phone_number" and rule["filter_operator"] == "equal_to"
            wanted = "+" + rule["values"][0].lstrip("+")
            found = [self._contact_view(c) for c in self.contacts.values() if c["phone_number"] == wanted]
            return httpx.Response(200, json={"meta": {"count": len(found), "current_page": 1}, "payload": found})
        if method == "POST" and suffix == "/contacts":
            if any(c["phone_number"] == body["phone_number"] for c in self.contacts.values()):
                return httpx.Response(422, json=DOC["contact_create_phone_taken"]["body"])
            self.next_contact += 1
            cid = self.next_contact
            source = body["phone_number"].lstrip("+")
            self.contacts[cid] = {"id": cid, "phone_number": body["phone_number"], "blocked": False,
                                  "contact_inboxes": [{"source_id": source, "inbox": {"id": body["inbox_id"]}}]}
            out = copy.deepcopy(DOC["contact_create"]["body"])
            out["payload"]["contact"] = self._contact_view(self.contacts[cid])
            out["payload"]["contact"]["name"] = body.get("name")
            out["payload"]["contact_inbox"]["source_id"] = source
            return httpx.Response(200, json=out)
        if method == "POST" and suffix == "/conversations":
            contact = self.contacts[body["contact_id"]]
            assert body["source_id"] in {ci["source_id"] for ci in contact["contact_inboxes"]}
            self.next_conversation += 1
            conv = copy.deepcopy(CONTACT_767["payload"][0])
            conv.update(id=self.next_conversation, inbox_id=body["inbox_id"], status="pending", labels=[],
                        last_activity_at=LAST_INBOUND_6, messages=[])
            conv["meta"]["sender"]["id"] = contact["id"]
            conv["_contact"] = contact["id"]
            self.conversations[conv["id"]] = conv
            self.messages[conv["id"]] = []
            out = copy.deepcopy(DOC["conversation_create"]["body"])
            out.update(id=conv["id"], inbox_id=conv["inbox_id"])
            out["meta"]["sender"]["id"] = contact["id"]
            return httpx.Response(200, json=out)
        parts = suffix.strip("/").split("/")
        if method == "GET" and len(parts) == 3 and parts[0] == "contacts" and parts[2] == "conversations":
            cid = int(parts[1])
            items = [{k: v for k, v in c.items() if k != "_contact"} for c in self.conversations.values()
                     if self._contact_of(c) == cid]
            return httpx.Response(200, json={"payload": items})
        if method == "GET" and len(parts) == 3 and parts[0] == "conversations" and parts[2] == "messages":
            conv_id = int(parts[1])
            if request.url.params.get("before"):
                return httpx.Response(200, json={"payload": []})
            return httpx.Response(200, json={"meta": MESSAGES_CONV_6["meta"], "payload": self.messages[conv_id]})
        if method == "POST" and len(parts) == 3 and parts[0] == "conversations" and parts[2] == "labels":
            conv_id = int(parts[1])
            self.conversations[conv_id]["labels"] = list(body["labels"])
            return httpx.Response(200, json={"payload": self.conversations[conv_id]["labels"]})
        raise AssertionError(f"pedido no esperado: {method} {suffix}")

    def client(self) -> BulkPrepareChatwoot:
        return BulkPrepareChatwoot(base_url="https://chatwoot.example.test", account_id=ACCOUNT,
                                   access_token="k" * 24, inbox_id=INBOX,
                                   transport=httpx.MockTransport(self.handler))


class MemoryStore:
    """La interfaz ``BulkPrepareStore`` en memoria (la de Supabase llega con la migracion propuesta)."""

    def __init__(self, members: Sequence[BulkMember], *, state: str = "confirmado",
                 label: str | None = LABEL) -> None:
        self.members = list(members)
        self.state = state
        self.label = label
        self.prepared: dict[int, PreparedConversation] = {}
        self.transitions: list[str] = []
        self.on_read_send = None

    async def read_send(self, send_id: str) -> SendSnapshot:
        assert send_id == SEND
        if self.on_read_send is not None:
            self.on_read_send(self)
        return SendSnapshot(self.state, self.label)

    async def start_preparing(self, send_id: str) -> None:
        assert self.state == "confirmado"
        self.state = "preparando"
        self.transitions.append("preparando")

    async def read_members(self, send_id: str, *, after_position: int, limit: int) -> Sequence[BulkMember]:
        return [m for m in self.members if m.position > after_position][:limit]

    async def read_prepared(self, send_id: str, positions: Sequence[int]) -> Mapping[int, PreparedConversation]:
        return {p: self.prepared[p] for p in positions if p in self.prepared}

    async def record_prepared(self, send_id: str, prepared: PreparedConversation) -> None:
        assert prepared.position not in self.prepared
        self.prepared[prepared.position] = prepared


class FakeTime:
    def __init__(self, now: float = LONG_AFTER) -> None:
        self.now = now
        self.mono = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.mono

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.mono += seconds


async def no_vetoes(members: Sequence[BulkMember]) -> Mapping[int, str]:
    return {}


def run(store, emulator, *, vetoes=no_vetoes, fake=None, client=None, **kwargs):
    fake = fake or FakeTime()
    client = client or emulator.client()
    report = asyncio.run(prepare_bulk_send(send_id=SEND, store=store, chatwoot=client, vetoes=vetoes,
                                           clock=fake.clock, monotonic=fake.monotonic, sleep=fake.sleep,
                                           **kwargs))
    return report, fake


CO_1 = "573000000002"
CO_2 = "573000000003"


def test_new_person_gets_contact_conversation_and_label_without_sending():
    cw = ChatwootEmulator()
    store = MemoryStore([BulkMember(0, CO_1, "Ana")])
    report, _ = run(store, cw)
    assert report.finished and report.counts == {"preparado": 1}
    assert store.transitions == ["preparando"]
    prepared = store.prepared[0]
    assert prepared.how == "nuevo" and prepared.source_id == CO_1 and prepared.label == LABEL
    assert cw.contacts[prepared.contact_id]["phone_number"] == "+" + CO_1
    assert cw.labels_of(prepared.conversation_id) == [LABEL]
    assert cw.conversations[prepared.conversation_id]["status"] == "pending"
    # GET etiquetas, filtro (Colombia: una forma), contacto, conversacion, etiqueta. Ningun mensaje.
    assert [m for m, _ in cw.requests] == ["GET", "POST", "POST", "POST", "POST"]
    assert cw.count("POST", "/conversations/") == 1 and cw.count("GET", "/conversations/") == 0


def test_repeating_touches_nothing_already_prepared():
    cw = ChatwootEmulator()
    store = MemoryStore([BulkMember(0, CO_1, "Ana"), BulkMember(1, CO_2, None)])
    run(store, cw)
    before = len(cw.requests)
    report, fake = run(store, cw)
    assert report.finished and report.counts == {"ya_preparado": 2}
    assert len(cw.requests) == before + 1  # solo el GET de etiquetas del arranque
    assert fake.sleeps == []


def test_repeating_after_a_cut_before_recording_does_not_duplicate():
    """El bridge se cayo despues de etiquetar y antes de anotar: la proxima corrida no duplica nada."""
    cw = ChatwootEmulator()
    first = MemoryStore([BulkMember(0, CO_1, "Ana")])
    run(first, cw)
    conv = first.prepared[0].conversation_id
    lost = MemoryStore([BulkMember(0, CO_1, "Ana")], state="preparando")
    report, _ = run(lost, cw)
    assert report.counts == {"preparado": 1}
    assert lost.prepared[0].how == "reusa" and lost.prepared[0].conversation_id == conv
    assert len(cw.contacts) == 1 and len(cw.conversations) == 1
    assert cw.count("POST", "/contacts") - cw.count("POST", "/contacts/filter") == 1
    assert cw.count("POST", f"/conversations/{conv}/labels") == 1
    assert cw.labels_of(conv) == [LABEL]


def test_existing_contact_reuses_the_most_active_live_conversation_and_keeps_its_labels():
    cw = ChatwootEmulator()
    cw.seed_767()
    cw.conversations[9]["labels"] = ["producto_maribel"]
    store = MemoryStore([BulkMember(0, "520000000767", "Contacto")])
    report, _ = run(store, cw)
    assert report.counts == {"preparado": 1}
    prepared = store.prepared[0]
    assert (prepared.contact_id, prepared.conversation_id, prepared.how) == (767, 9, "reusa")
    assert cw.labels_of(9) == sorted(["producto_maribel", LABEL])
    assert cw.labels_of(6) == []
    assert cw.count("POST", "/contacts") == 2  # los dos filtros (52 y 521), ninguna creacion
    assert cw.count("POST", "/conversations") == 1  # solo la etiqueta


def test_existing_contact_without_live_conversation_gets_a_new_one():
    cw = ChatwootEmulator()
    cid = cw.seed_contact("+" + CO_1)
    old = cw.seed_conversation(cid, status="resolved", labels=["producto_maribel"])
    store = MemoryStore([BulkMember(0, CO_1, "Ana")])
    run(store, cw)
    prepared = store.prepared[0]
    assert prepared.how == "conversacion_nueva" and prepared.conversation_id != old
    assert cw.labels_of(prepared.conversation_id) == [LABEL] and cw.labels_of(old) == ["producto_maribel"]


def test_mexico_prefers_the_contact_in_the_delivery_form():
    cw = ChatwootEmulator()
    cw.seed_contact("+520000000111")
    in_521 = cw.seed_contact("+5210000000111")
    store = MemoryStore([BulkMember(0, "5210000000111", None)])
    report, _ = run(store, cw)
    assert report.counts == {"preparado": 1} and store.prepared[0].contact_id == in_521
    assert store.prepared[0].source_id == "5210000000111"


def test_mexico_new_contact_is_created_in_the_delivery_form():
    cw = ChatwootEmulator()
    store = MemoryStore([BulkMember(0, "520000000222", None)])
    run(store, cw)
    assert [c["phone_number"] for c in cw.contacts.values()] == ["+5210000000222"]


@pytest.mark.parametrize("setup,reason", [
    (lambda cw: cw.seed_contact("+" + CO_1, blocked=True), "bloqueado"),
    (lambda cw: cw.seed_conversation(cw.seed_contact("+" + CO_1), status="resolved", labels=["cliente"]),
     "clienta"),
    (lambda cw: cw.seed_conversation(cw.seed_contact("+" + CO_1), labels=["automation_opted_out"]), "baja"),
    (lambda cw: cw.seed_conversation(cw.seed_contact("+" + CO_1), assignee=True), "con_equipo"),
    (lambda cw: cw.seed_conversation(cw.seed_contact("+" + CO_1), labels=["automation_paused"]), "con_equipo"),
])
def test_chatwoot_vetoes_leave_the_person_untouched(setup, reason):
    cw = ChatwootEmulator()
    setup(cw)
    conversations_before = copy.deepcopy(cw.conversations)
    store = MemoryStore([BulkMember(0, CO_1, "Ana")])
    report, _ = run(store, cw)
    assert report.finished and report.counts == {f"vetado_{reason}": 1}
    assert store.prepared == {} and cw.conversations == conversations_before
    assert cw.count("POST", "/conversations") == 0


def test_someone_who_wrote_in_the_last_six_hours_is_vetoed():
    cw = ChatwootEmulator()
    cw.seed_767()
    store = MemoryStore([BulkMember(0, "520000000767", None)])
    report, _ = run(store, cw, fake=FakeTime(now=LAST_INBOUND_6 + 3600))
    assert report.counts == {"vetado_escribio_hace_poco": 1}
    assert cw.labels_of(9) == [] and cw.labels_of(6) == []


def test_last_minute_vetoes_skip_chatwoot_and_unreadable_vetoes_stop():
    cw = ChatwootEmulator()
    store = MemoryStore([BulkMember(0, CO_1, "Ana"), BulkMember(1, CO_2, None)])

    async def vetoes(members):
        assert [m.position for m in members] == [0, 1]
        return {0: "compro"}

    report, fake = run(store, cw, vetoes=vetoes)
    assert report.counts == {"vetado_compro": 1, "preparado": 1}
    assert list(store.prepared) == [1] and len(fake.sleeps) == 1

    async def broken(members):
        raise RuntimeError("base caida")

    cw2 = ChatwootEmulator()
    report, _ = run(MemoryStore([BulkMember(0, CO_1, "Ana")]), cw2, vetoes=broken)
    assert report.stop_reason == "sin_vetos"
    assert cw2.count("POST", "/") == 0


def test_more_than_twenty_percent_api_failures_brakes():
    cw = ChatwootEmulator()
    cw.fail[("POST", "/contacts/filter")] = 503
    members = [BulkMember(i, f"5730000001{i:02d}", None) for i in range(40)]
    store = MemoryStore(members)
    report, _ = run(store, cw)
    assert report.stop_reason.startswith("chatwoot_api_")
    # Frena al llegar a la muestra minima de 20 pedidos: el GET de etiquetas del arranque y 19 filtros.
    assert report.stop_reason == "chatwoot_api_19_de_20"
    assert report.counts == {"fallido_chatwoot_http_503": 19}
    assert store.prepared == {} and cw.count("POST", "/contacts/filter") == 19


def test_occasional_failures_do_not_brake_and_are_retried_next_run():
    cw = ChatwootEmulator()
    members = [BulkMember(i, f"5730000002{i:02d}", None) for i in range(10)]
    store = MemoryStore(members)
    calls = {"n": 0}
    original = cw.handler

    def flaky(request):
        if request.url.path.endswith("/conversations") and request.method == "POST":
            calls["n"] += 1
            if calls["n"] == 3:
                return httpx.Response(500)
        return original(request)

    client = BulkPrepareChatwoot(base_url="https://chatwoot.example.test", account_id=ACCOUNT,
                                 access_token="k" * 24, inbox_id=INBOX, transport=httpx.MockTransport(flaky))
    report, _ = run(store, cw, client=client)
    assert report.finished and report.counts == {"preparado": 9, "fallido_chatwoot_http_500": 1}
    assert report.api_failures == 1
    report, _ = run(store, cw)
    assert report.counts == {"ya_preparado": 9, "preparado": 1}
    assert len(cw.contacts) == 10 and len(cw.conversations) == 10


def test_phone_taken_between_filter_and_create_is_a_retryable_failure_not_an_api_failure():
    cw = ChatwootEmulator()
    original = cw.handler

    def race(request):
        if request.method == "POST" and request.url.path.endswith("/contacts"):
            cw.seed_contact("+" + CO_1)
        return original(request)

    client = BulkPrepareChatwoot(base_url="https://chatwoot.example.test", account_id=ACCOUNT,
                                 access_token="k" * 24, inbox_id=INBOX, transport=httpx.MockTransport(race))
    report, _ = run(MemoryStore([BulkMember(0, CO_1, "Ana")]), cw, client=client)
    assert report.counts == {"fallido_contacto_aparecio": 1} and report.api_failures == 0


def test_contact_without_the_inbox_or_with_another_phone_is_skipped():
    cw = ChatwootEmulator()
    cw.seed_contact("+" + CO_1, inbox=None)
    cw.seed_contact("+" + CO_2, source_id="573999999999")
    store = MemoryStore([BulkMember(0, CO_1, None), BulkMember(1, CO_2, None)])
    report, _ = run(store, cw)
    assert report.counts == {"omitido_contacto_sin_bandeja": 1, "omitido_contacto_otro_telefono": 1}
    assert cw.count("POST", "/conversations") == 0


@pytest.mark.parametrize("state", ["borrador", "enviando", "terminado", "frenado"])
def test_only_a_confirmed_or_preparing_send_is_prepared(state):
    cw = ChatwootEmulator()
    report, _ = run(MemoryStore([BulkMember(0, CO_1, None)], state=state), cw)
    assert report.stop_reason == f"estado_{state}" and cw.requests == []


def test_a_brake_from_the_page_stops_between_batches():
    cw = ChatwootEmulator()
    members = [BulkMember(i, f"5730000003{i:02d}", None) for i in range(5)]
    store = MemoryStore(members)
    reads = {"n": 0}

    def brake_on_third_read(s):
        reads["n"] += 1
        if reads["n"] == 3:
            s.state = "frenado"

    store.on_read_send = brake_on_third_read
    report, _ = run(store, cw, batch_size=2)
    assert report.stop_reason == "estado_frenado" and len(store.prepared) == 2


def test_missing_campaign_label_in_chatwoot_stops_before_anyone():
    cw = ChatwootEmulator()
    cw.labels = [label for label in cw.labels if label["title"] != LABEL]
    store = MemoryStore([BulkMember(0, CO_1, None)])
    report, _ = run(store, cw)
    assert report.stop_reason == "etiqueta_no_existe" and store.transitions == []
    assert cw.requests == [("GET", "/labels")]


def test_send_without_campaign_label_prepares_contact_and_conversation_only():
    cw = ChatwootEmulator()
    store = MemoryStore([BulkMember(0, CO_1, None)], label=None)
    report, _ = run(store, cw)
    assert report.counts == {"preparado": 1} and store.prepared[0].label is None
    assert cw.labels_of(store.prepared[0].conversation_id) == []
    assert cw.count("GET", "/labels") == 0


def test_rate_is_one_person_per_second_by_default():
    cw = ChatwootEmulator()
    store = MemoryStore([BulkMember(i, f"5730000004{i:02d}", None) for i in range(3)])
    _, fake = run(store, cw)
    assert fake.sleeps == [1.0, 1.0, 1.0]
    _, fake = run(MemoryStore([BulkMember(0, CO_1, None)]), ChatwootEmulator(), rate_per_second=2.5)
    assert fake.sleeps == [0.4]
    with pytest.raises(ValueError):
        run(MemoryStore([]), ChatwootEmulator(), rate_per_second=6)


def test_should_stop_cuts_the_run():
    cw = ChatwootEmulator()
    store = MemoryStore([BulkMember(i, f"5730000005{i:02d}", None) for i in range(3)])
    report, _ = run(store, cw, should_stop=lambda: len(store.prepared) >= 1)
    assert report.stop_reason == "parar" and len(store.prepared) == 1


# --- revalidar al mandar (lo usa el motor, encargo 9) -------------------------------------------

def _prepared_767(cw: ChatwootEmulator) -> PreparedConversation:
    cw.seed_767()
    store = MemoryStore([BulkMember(0, "520000000767", None)])
    run(store, cw)
    return store.prepared[0]


def revalidate(cw, prepared, phone="520000000767", now=LONG_AFTER):
    return asyncio.run(revalidate_prepared(cw.client(), prepared, phone=phone, now=now))


def test_revalidate_ok_with_one_read_and_no_send():
    cw = ChatwootEmulator()
    prepared = _prepared_767(cw)
    before = len(cw.requests)
    assert revalidate(cw, prepared) == (True, "preparada")
    assert cw.requests[before:] == [("GET", "/contacts/767/conversations")]


@pytest.mark.parametrize("change,expected", [
    (lambda cw: cw.conversations[9].update(labels=[]), (False, "prep_sin_etiqueta")),
    (lambda cw: cw.conversations[9].update(status="resolved"), (False, "prep_no_abierta")),
    (lambda cw: cw.conversations[9]["meta"].update(assignee={"id": 5}), (False, "veto_con_equipo")),
    (lambda cw: cw.conversations[6].update(labels=["automation_opted_out"]), (False, "veto_baja")),
    (lambda cw: cw.conversations.pop(9), (False, "prep_no_existe")),
])
def test_revalidate_rejects_what_changed_since_preparing(change, expected):
    cw = ChatwootEmulator()
    prepared = _prepared_767(cw)
    change(cw)
    assert revalidate(cw, prepared) == expected


def test_revalidate_rejects_another_phone_and_a_recent_inbound():
    cw = ChatwootEmulator()
    prepared = _prepared_767(cw)
    assert revalidate(cw, prepared, phone=CO_1) == (False, "prep_otro_telefono")
    assert revalidate(cw, prepared, now=LAST_INBOUND_6 + 600) == (False, "veto_escribio_hace_poco")


def test_module_never_sends_nor_searches():
    source = Path(prep.__file__).read_text()
    assert "/messages" not in source.replace("``/messages``", "")
    assert "contacts/search\"" not in source and "find_contact_inbox_by_phone" not in source
