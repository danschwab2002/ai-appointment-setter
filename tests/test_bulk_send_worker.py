"""El motor de envio del masivo (src/bridge/bulk_send_worker.py, encargo 9).

Contra una base falsa que copia las reglas de ``20261125000200_bulk_send_store_v1.sql`` (transiciones,
reservar, cerrar, vencer, la tanda) y ``20261125000300`` (lo preparado), un Chatwoot falso y un Meta
falso, con un reloj falso que avanza con cada espera. Las pruebas son las de ``cmd_enviar`` de masivo.py
(``scripts-2026-11-25/vivo-29-11/masivo.py``): ventana, piloto que frena, freno por codigo, por salud de
otro numero, freno manual, corte por hora, reservado sin cierre, reanudar sin reenviar, candado ocupado
y la regla de las 24 h.

**Regla 2.** Los bordes externos usan respuestas capturadas:

* Chatwoot: ``chatwoot_db_messages_inbox_13_maribel_131042_20261107.json`` (los dos 131042 de Maribel del
  07/11 y la primera plantilla leida despues, en la forma de la API de mensajes), leidos por el
  ``ChatwootClient`` real sobre ``httpx.MockTransport``.
* Meta: ``bulk_send_worker/meta_phone_health_att1_maribel_20261125.json`` (la lectura de produccion de
  ``meta_salud.py`` del OS para los dos numeros; la forma cruda se reconstruye campo a campo y el
  fixture lo declara), leida por ``MetaTemplatesClient.read_phone_health`` real.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from bridge.bulk_send_brakes import FinishedSend, PilotPolicy
from bridge.bulk_send_prepare import PreparedConversation
from bridge.bulk_send_worker import (
    BatchMember,
    BulkSendConfigError,
    BulkSendSnapshot,
    BulkSendWorker,
    BulkSendWorkerConfig,
    BulkTemplate,
    ChatwootBatchVetoSource,
    ChatwootDeliveryStatusReader,
    MetaPortfolioHealthReader,
    PortfolioLock,
    SlackBulkBrakeNotifier,
    assert_not_retried,
    batch_vetoes,
    machine_reason,
    parse_portfolio_numbers,
    send_window,
    template_params,
    template_values,
    within_send_hours,
)
from bridge.chatwoot import ChatwootClient, ContactConversation
from bridge.messaging import ChatwootMessageSender, FirstTouchResult
from bridge.meta_templates import MetaTemplatesClient

FIXTURES = Path(__file__).parent / "fixtures"
CW_131042 = json.loads((FIXTURES / "chatwoot_db_messages_inbox_13_maribel_131042_20261107.json").read_text("utf-8"))
META_HEALTH = json.loads((FIXTURES / "bulk_send_worker" / "meta_phone_health_att1_maribel_20261125.json")
                         .read_text("utf-8"))

# 14:00 de CDMX del sabado 29/11 (UTC-6): adentro del horario.
CENTER = datetime(2026, 11, 29, 20, 0, tzinfo=timezone.utc)
SEND_ID = "4f1c2b8e-0d7a-4c55-9a0e-2b7d1e6f9a10"
LIST_ID = "9b2f3c4d-1e5f-4a6b-8c7d-0e1f2a3b4c5d"
TEMPLATE = BulkTemplate(name="maribel_masivo_prueba_01", language="es_MX", category="MARKETING",
                        body="Hola {{1}}, {{2}}")


def run(coroutine):
    return asyncio.run(coroutine)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=max(0.0, seconds))


# ---------------------------------------------------------------------------
# La base falsa: las reglas de las dos migraciones.
# ---------------------------------------------------------------------------

class FakeStore:
    def __init__(self, clock: Clock, people: int, *, state: str = "confirmado", prepared: tuple[int, ...] = (),
                 confirmed_at: datetime | None = None, confirmed_health: str = "verde",
                 template_name: str = TEMPLATE.name, mode: str = "centrada", scheduled_at: datetime = CENTER,
                 rate_millis: int = 2500) -> None:
        self.clock = clock
        self.members = [{"position": i, "phone": f"57300{i:07d}", "first_name": "Ana" if i % 2 else None}
                        for i in range(people)]
        self.recipients: dict[int, dict] = {}
        self.prepared = {p: PreparedConversation(position=p, contact_id=1000 + p, conversation_id=500 + p,
                                                 source_id=f"57300{p:07d}", label="black_forever", how="nuevo")
                         for p in prepared}
        self.send = {"state": state, "template_name": template_name, "confirmed_health": confirmed_health,
                     "confirmed_at": (confirmed_at or CENTER - timedelta(days=1)), "window_mode": mode,
                     "scheduled_at": scheduled_at, "rate_millis": rate_millis, "brake_reason": None,
                     "braked_by": None}
        self.transitions: list[tuple[str, str | None]] = []
        self.batch_sizes: list[int] = []
        self.fail_close_at: int | None = None

    def view(self) -> dict:
        by_state: dict[str, int] = {}
        for r in self.recipients.values():
            by_state[r["state"]] = by_state.get(r["state"], 0) + 1
        s = self.send
        return {"send_id": SEND_ID, "state": s["state"], "template_name": s["template_name"],
                "template_language": "es_MX", "template_variables": [{"de": "nombre"}, "hoy a las 8"],
                "header_image_url": None, "campaign_label": "black_forever", "scheduled_at": s["scheduled_at"].isoformat(),
                "window_mode": s["window_mode"], "rate_millis": s["rate_millis"], "recipient_cap": 6000,
                "confirmed_at": s["confirmed_at"].isoformat(), "confirmed_by": "slack:U0123456789",
                "confirmed_count": len(self.members), "confirmed_health": s["confirmed_health"],
                "brake_reason": s["brake_reason"], "braked_by": s["braked_by"], "product_refs": ["maribel_forever"],
                "list": {"list_id": LIST_ID, "eligible_count": len(self.members)},
                "recipients_by_state": by_state}

    async def read_sends(self):
        return [self.view()]

    async def read_send(self, send_id):
        assert send_id == SEND_ID
        return self.view()

    async def transition(self, send_id, to, reason):
        allowed = {("confirmado", "preparando"), ("confirmado", "enviando"), ("preparando", "enviando"),
                   ("enviando", "terminado"), ("borrador", "frenado"), ("confirmado", "frenado"),
                   ("preparando", "frenado"), ("enviando", "frenado")}
        assert (to == "frenado") == (reason is not None)
        if reason is not None:
            assert machine_reason(reason) == reason
        if (self.send["state"], to) not in allowed:
            return {"ok": False, "reason_code": "transition_not_allowed", "state": self.send["state"]}
        if to == "terminado" and any(r["state"] == "reservado" for r in self.recipients.values()):
            return {"ok": False, "reason_code": "reservations_open"}
        self.send["state"] = to
        if to == "frenado":
            self.send["brake_reason"], self.send["braked_by"] = reason, "motor"
        self.transitions.append((to, reason))
        return {"ok": True, "state": to}

    def brake_by_hand(self) -> None:
        self.send.update(state="frenado", brake_reason="a_mano", braked_by="slack:U0123456789")

    async def reserve(self, send_id, position):
        if self.send["state"] != "enviando":
            return {"ok": False, "reason_code": "not_sending", "state": self.send["state"]}
        if position in self.recipients:
            return {"ok": False, "reason_code": "already_" + self.recipients[position]["state"]}
        self.recipients[position] = {"state": "reservado", "reserved_at": self.clock.now}
        member = self.members[position]
        return {"ok": True, "phone": member["phone"], "first_name": member["first_name"], "attempt": 1}

    async def close(self, send_id, position, outcome):
        if self.fail_close_at == position:
            raise RuntimeError("supabase_unavailable")
        row = self.recipients.get(position)
        if row is None:
            return {"ok": False, "reason_code": "not_reserved"}
        if row["state"] != "reservado":
            return {"ok": False, "reason_code": "already_" + row["state"]}
        assert outcome["state"] in ("enviado", "fallido", "dudoso", "vetado")
        assert outcome["state"] != "enviado" or outcome.get("chatwoot_message_id")
        assert outcome.get("reason") is None or machine_reason(outcome["reason"]) == outcome["reason"]
        row.update(outcome)
        return {"ok": True, "state": outcome["state"]}

    async def expire(self, send_id, minutes):
        n = 0
        for row in self.recipients.values():
            if row["state"] == "reservado" and row["reserved_at"] <= self.clock.now - timedelta(minutes=minutes):
                row.update(state="dudoso", reason="reservado_sin_cierre")
                n += 1
        return {"ok": True, "dudosos": n}

    async def read_batch(self, send_id, limit):
        self.batch_sizes.append(limit)
        return [m for m in self.members if m["position"] not in self.recipients][:limit]

    async def read_prepared(self, send_id, positions):
        return {p: self.prepared[p] for p in positions if p in self.prepared}

    def states(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.recipients.values():
            out[r["state"]] = out.get(r["state"], 0) + 1
        return out


class FakeSender:
    """Chatwoot falso del lado de mandar. Cada mensaje sale con un id nuevo."""

    def __init__(self, clock: Clock, *, seconds_per_call: float = 0.0, on_call=None) -> None:
        self.clock = clock
        self.seconds = seconds_per_call
        self.on_call = on_call
        self.calls: list[tuple[str, int, datetime, dict]] = []
        self.by_conversation: dict[int, int] = {}
        self._next = 9000

    def _sent(self, kind: str, delivery_id: str, conversation: int, params: dict) -> FirstTouchResult:
        position = int(delivery_id.rsplit(":", 1)[1])
        self.clock.now += timedelta(seconds=self.seconds)
        self.calls.append((kind, position, self.clock.now, params))
        if self.on_call is not None:
            self.on_call(len(self.calls))
        self._next += 1
        self.by_conversation[conversation] = self._next
        return FirstTouchResult("sent", conversation, self._next)

    async def send_first_touch(self, *, phone, buyer_name, buyer_email, content, delivery_id, template_params=None):
        assert template_params is not None and "Hola " in content
        return self._sent("first_touch", delivery_id, 700 + int(delivery_id.rsplit(":", 1)[1]), template_params)

    async def send_first_touch_to_conversation(self, *, conversation_id, phone, buyer_name, content, delivery_id,
                                               template_params=None):
        assert template_params is not None
        return self._sent("to_conversation", delivery_id, conversation_id, template_params)

    def positions(self) -> list[int]:
        return [c[1] for c in self.calls]


class FakeStatus:
    """El estado de cada mensaje: ``policy(message_id) -> (status, codigo)``; por defecto, entregado."""

    def __init__(self, policy=None) -> None:
        self.policy = policy or (lambda message_id: ("delivered", None))
        self.reads = 0

    async def read_status(self, *, conversation_id, message_id):
        self.reads += 1
        return self.policy(message_id)


class FakeHealth:
    def __init__(self, readings: dict) -> None:
        self.readings = readings
        self.reads = 0

    async def read(self):
        self.reads += 1
        return copy.deepcopy(self.readings)


class FakeTemplates:
    def __init__(self, template: BulkTemplate | None = TEMPLATE) -> None:
        self.template = template

    async def resolve(self, name, language):
        return None if self.template is None else BulkTemplate(**{**self.template.__dict__, "name": name})


class FakeProducer:
    def __init__(self) -> None:
        self.admitted: list = []

    async def admit(self, command) -> None:
        self.admitted.append(command)


def healthy() -> dict:
    return copy.deepcopy(META_HEALTH["normalizadas"])


def make(tmp_path: Path, clock: Clock, store: FakeStore, *, sender: FakeSender | None = None,
         status=None, health=None, vetoes=None, revalidate=None, pilot: PilotPolicy | None = None,
         lock_dir: Path | None = None, producer: FakeProducer | None = None, **config):
    calls: list[list[int]] = []

    async def no_vetoes(send, members, prepared):
        calls.append([m.position for m in members])
        return {} if vetoes is None else {m.position: vetoes[m.position] for m in members if m.position in vetoes}

    async def valid(prepared, phone, now):
        return (True, "preparada") if revalidate is None else revalidate(prepared)

    cfg = BulkSendWorkerConfig(instance_key="maribel", lock_dir=lock_dir or tmp_path / "candado",
                               pilot=pilot or PilotPolicy(), **config)
    worker = BulkSendWorker(
        store=store, sender=sender or FakeSender(clock), revalidate=valid, vetoes=no_vetoes,
        status_reader=status or FakeStatus(), health=health or FakeHealth(healthy()), lock=PortfolioLock(cfg.lock_dir),
        templates=FakeTemplates(), config=cfg,
        notifier=SlackBulkBrakeNotifier(producer=producer or FakeProducer(), clock=clock), clock=clock,
        sleep=clock.sleep)
    worker.veto_calls = calls  # type: ignore[attr-defined]
    return worker


# ---------------------------------------------------------------------------
# La ventana y el horario.
# ---------------------------------------------------------------------------

def test_the_window_is_centered_on_the_hour_and_waits_for_its_start(tmp_path):
    window = send_window(100, 2.5, CENTER, "centrada")
    assert (window.start, window.end) == (CENTER - timedelta(seconds=20), CENTER + timedelta(seconds=20))
    assert send_window(100, 2.5, CENTER, "desde_la_hora").start == CENTER
    clock = Clock(CENTER - timedelta(seconds=30))
    store = FakeStore(clock, 100)
    sender = FakeSender(clock)
    worker = make(tmp_path, clock, store, sender=sender)
    assert run(worker.tick()).outcome == "esperando" and not sender.calls
    clock.now = CENTER - timedelta(seconds=20)
    report = run(worker.tick())
    assert report.outcome == "terminado"
    # Arranco D/2 antes de la hora: la primera plantilla sale en el arranque, no a la hora.
    assert sender.calls[0][2] < CENTER


def test_the_window_is_frozen_ten_minutes_before_the_start(tmp_path):
    clock = Clock(CENTER - timedelta(minutes=9))
    store = FakeStore(clock, 100)
    worker = make(tmp_path, clock, store)
    first = worker.window_for(BulkSendSnapshotFrom(store), now=clock.now)
    store.members = store.members[:10]  # la lista cambio despues de congelar: la ventana no se mueve
    assert worker.window_for(BulkSendSnapshotFrom(store), now=clock.now) == first


def BulkSendSnapshotFrom(store: FakeStore) -> BulkSendSnapshot:
    from bridge.bulk_send_worker import snapshot_of

    return snapshot_of(store.view())


def test_a_window_outside_the_allowed_hours_brakes_without_sending(tmp_path):
    late = datetime(2026, 11, 30, 3, 0, tzinfo=timezone.utc)  # 21:00 de CDMX
    clock = Clock(late)
    store = FakeStore(clock, 50, scheduled_at=late)
    sender = FakeSender(clock)
    producer = FakeProducer()
    report = run(make(tmp_path, clock, store, sender=sender, producer=producer).tick())
    assert report.outcome == "frenado:fuera_de_horario" and not sender.calls
    assert producer.admitted[0].event_code == "SYS-007"
    zone = BulkSendWorkerConfig(instance_key="maribel", lock_dir=tmp_path).zone
    assert within_send_hours(CENTER - timedelta(minutes=30), CENTER + timedelta(minutes=30), zone, (9, 21))


# ---------------------------------------------------------------------------
# El envio entero, el piloto y los lotes.
# ---------------------------------------------------------------------------

def test_a_whole_send_pilot_then_lots_of_25_at_most_2_5_per_second(tmp_path):
    clock = Clock(CENTER)
    store = FakeStore(clock, 60, prepared=(1, 2, 3, 40))
    sender = FakeSender(clock)
    worker = make(tmp_path, clock, store, sender=sender)
    report = run(worker.tick())
    assert report.outcome == "terminado"
    assert store.send["state"] == "terminado" and store.states() == {"enviado": 60}
    assert sorted(sender.positions()) == list(range(60))
    # Las preparadas salen en su conversacion; el resto por el camino de siempre.
    kinds = {position: kind for kind, position, _, _ in sender.calls}
    assert {p for p, k in kinds.items() if k == "to_conversation"} == {1, 2, 3, 40}
    # El piloto (20), la espera de 5 minutos, y despues tandas de 25 como mucho.
    assert store.batch_sizes[0] == 20 and max(store.batch_sizes) == 25
    gap = sender.calls[20][2] - sender.calls[19][2]
    assert gap >= timedelta(minutes=5)
    times = [c[2] for c in sender.calls]
    assert all((b - a).total_seconds() >= 0.399 for a, b in zip(times, times[1:]))
    assert max(len(c) for c in worker.veto_calls) <= 25  # type: ignore[attr-defined]
    # El candado se solto.
    assert not PortfolioLock(tmp_path / "candado").busy_elsewhere()


def test_the_template_params_carry_the_first_name_or_the_neutral_greeting(tmp_path):
    clock = Clock(CENTER)
    store = FakeStore(clock, 4)
    sender = FakeSender(clock)
    run(make(tmp_path, clock, store, sender=sender).tick())
    bodies = {position: params["processed_params"]["body"] for _, position, _, params in sender.calls}
    assert bodies[1] == {"1": "Ana", "2": "hoy a las 8"}
    assert bodies[0] == {"1": "qué tal", "2": "hoy a las 8"}
    with pytest.raises(ValueError):
        template_params(BulkTemplate("x", "es", "MARKETING", "b", header_format="IMAGE"), {"1": "a"}, None)
    with pytest.raises(ValueError):
        template_values(["con\nsalto"], None)


def test_a_pilot_with_too_many_failures_brakes_and_does_not_continue(tmp_path):
    clock = Clock(CENTER)
    store = FakeStore(clock, 80)
    sender = FakeSender(clock)
    producer = FakeProducer()
    failed = set(range(9001, 9006))  # 5 de 20 fallan, sin codigo que corte
    status = FakeStatus(lambda mid: ("failed", None) if mid in failed else ("delivered", None))
    report = run(make(tmp_path, clock, store, sender=sender, status=status, producer=producer).tick())
    assert report.outcome.startswith("frenado:piloto:entrega:fallidos_5_de_20")
    assert len(sender.calls) == 20 and store.send["state"] == "frenado"
    assert producer.admitted and producer.admitted[0].component == "bulk_send:" + SEND_ID.replace("-", "")[:12]
    # Un freno de Meta queda para la regla de las 24 h, en el archivo del portfolio.
    brakes = PortfolioLock(tmp_path / "candado").read_brakes()
    assert [b.reason for b in brakes] == [store.send["brake_reason"]]


def test_a_131042_read_from_captured_chatwoot_brakes_at_the_first_one(tmp_path):
    clock = Clock(CENTER)
    store = FakeStore(clock, 80)
    sender = FakeSender(clock)
    rejected = CW_131042["rechazados_131042"][0]
    delivered = CW_131042["primera_plantilla_entregada_despues"]

    def handler(request: httpx.Request) -> httpx.Response:
        conversation = int(request.url.path.split("/")[-2])
        message_id = sender.by_conversation[conversation]
        captured = rejected if message_id == 9001 else delivered
        message = {**copy.deepcopy(captured), "id": message_id, "conversation_id": conversation}
        return httpx.Response(200, json={"payload": [message]})

    client = ChatwootClient(base_url="https://chatwoot.test", account_id=3, access_token="control",
                            transport=httpx.MockTransport(handler))
    report = run(make(tmp_path, clock, store, sender=sender, status=ChatwootDeliveryStatusReader(client)).tick())
    assert report.outcome == "frenado:piloto:meta_codigo:131042_x1"
    assert len(sender.calls) == 20


def _meta_client(raw: dict) -> MetaTemplatesClient:
    numbers = {"100000000001": "maribel", "100000000002": "att1"}

    def handler(request: httpx.Request) -> httpx.Response:
        key = numbers[request.url.path.rsplit("/", 1)[1]]
        assert request.headers["Authorization"].startswith("Bearer ") and "access_token" not in str(request.url)
        assert request.url.params["fields"].startswith("quality_rating,")
        return httpx.Response(200, json=raw[key])

    return MetaTemplatesClient(access_token="EAA" + "x" * 40, waba_id="1234567890123",
                               transport=httpx.MockTransport(handler))


def test_the_health_of_another_number_of_the_portfolio_brakes_the_send(tmp_path):
    clock = Clock(CENTER)
    store = FakeStore(clock, 80)
    raw = copy.deepcopy(META_HEALTH["crudas"])
    client = _meta_client(raw)
    health = MetaPortfolioHealthReader({"maribel": (client, "100000000001"), "att1": (client, "100000000002")})

    def degrade(calls: int) -> None:
        if calls == 12:
            raw["att1"]["quality_rating"] = "YELLOW"

    sender = FakeSender(clock, on_call=degrade)
    report = run(make(tmp_path, clock, store, sender=sender, health=health,
                      pilot=PilotPolicy(size=5, wait_seconds=60), health_every=10).tick())
    assert report.outcome == "frenado:salud:att1:calidad_green_a_yellow"
    assert len(sender.calls) == 30 and store.send["state"] == "frenado"


def test_a_red_number_does_not_start_and_an_unreadable_one_waits(tmp_path):
    clock = Clock(CENTER)
    readings = healthy()
    readings["maribel"]["calidad"] = "RED"
    store = FakeStore(clock, 30)
    sender = FakeSender(clock)
    assert run(make(tmp_path, clock, store, sender=sender, health=FakeHealth(readings)).tick()).outcome \
        == "frenado:salud:maribel:rojo_al_arrancar"
    readings = healthy()
    readings["att1"] = {"error": "meta:190"}
    store = FakeStore(clock, 30)
    # Otra carpeta: el rojo de arriba es un freno de Meta y la regla de las 24 h frenaria este envio.
    report = run(make(tmp_path, clock, store, sender=sender, health=FakeHealth(readings),
                      lock_dir=tmp_path / "otro").tick())
    assert report.outcome.startswith("salud_ilegible:") and store.send["state"] == "confirmado"
    assert not sender.calls


def test_yellow_only_starts_with_the_second_mark(tmp_path):
    clock = Clock(CENTER)
    readings = healthy()
    readings["maribel"]["calidad"] = "YELLOW"
    store = FakeStore(clock, 10, confirmed_health="amarillo")
    assert run(make(tmp_path, clock, store, health=FakeHealth(readings)).tick()).outcome == "terminado"


def test_the_manual_brake_from_the_page_stops_at_the_next_lot(tmp_path):
    clock = Clock(CENTER)
    store = FakeStore(clock, 80)
    producer = FakeProducer()
    sender = FakeSender(clock, on_call=lambda n: store.brake_by_hand() if n == 30 else None)
    report = run(make(tmp_path, clock, store, sender=sender, producer=producer,
                      pilot=PilotPolicy(size=5, wait_seconds=60)).tick())
    assert report.outcome == "frenado:a_mano"
    assert len(sender.calls) == 30
    assert ("frenado", "a_mano") not in store.transitions and not producer.admitted


def test_the_cut_hour_stops_a_send_that_fell_behind(tmp_path):
    clock = Clock(CENTER)
    store = FakeStore(clock, 60)
    sender = FakeSender(clock, seconds_per_call=2.0)  # Chatwoot lento: 2 s por envio
    report = run(make(tmp_path, clock, store, sender=sender, cut_grace=timedelta(seconds=5)).tick())
    assert report.outcome == "frenado:corte_por_hora"
    assert len(sender.calls) == 20 and store.send["state"] == "frenado"
    # Un corte no es un freno de Meta: no entra en la regla de las 24 h.
    assert PortfolioLock(tmp_path / "candado").read_brakes() == []


def test_the_send_stops_when_it_leaves_the_allowed_hours(tmp_path):
    edge = datetime(2026, 11, 30, 2, 59, 30, tzinfo=timezone.utc)  # 20:59:30 de CDMX
    clock = Clock(edge - timedelta(seconds=4))
    store = FakeStore(clock, 20, scheduled_at=edge, mode="desde_la_hora")
    clock.now = edge
    sender = FakeSender(clock, seconds_per_call=3.0)
    report = run(make(tmp_path, clock, store, sender=sender, pilot=PilotPolicy(size=5, wait_seconds=10),
                      cut_grace=timedelta(hours=1)).tick())
    assert report.outcome == "frenado:fuera_de_horario"
    assert len(sender.calls) == 5


# ---------------------------------------------------------------------------
# El registro: reservado sin cierre, reanudar.
# ---------------------------------------------------------------------------

def test_reserved_without_close_is_dubious_and_a_restart_never_resends(tmp_path):
    clock = Clock(CENTER)
    store = FakeStore(clock, 60)
    store.fail_close_at = 30  # la base se cae justo despues de mandarle a la 30
    sender = FakeSender(clock)
    with pytest.raises(RuntimeError):
        run(make(tmp_path, clock, store, sender=sender).tick())
    assert store.send["state"] == "enviando" and store.recipients[30]["state"] == "reservado"
    # El candado no quedo tomado por el proceso que se cayo.
    assert not PortfolioLock(tmp_path / "candado").busy_elsewhere()
    store.fail_close_at = None
    clock.now += timedelta(minutes=2)
    report = run(make(tmp_path, clock, store, sender=sender).tick())  # otro proceso: el bridge reiniciado
    assert report.outcome == "terminado"
    assert store.recipients[30]["state"] == "dudoso" and store.recipients[30]["reason"] == "reservado_sin_cierre"
    positions = sender.positions()
    assert len(positions) == len(set(positions)) == 60
    assert store.states() == {"enviado": 59, "dudoso": 1}


def test_a_sender_failure_with_chatwoot_in_between_is_dubious_and_a_g3_block_is_a_veto(tmp_path):
    clock = Clock(CENTER)
    store = FakeStore(clock, 3)

    class Odd(FakeSender):
        async def send_first_touch(self, *, phone, buyer_name, buyer_email, content, delivery_id,
                                   template_params=None):
            position = int(delivery_id.rsplit(":", 1)[1])
            if position == 0:
                return FirstTouchResult("failed", None, None, "chatwoot_http_error")
            if position == 1:
                return FirstTouchResult("blocked", None, None, "contact_active_conversation")
            return await super().send_first_touch(phone=phone, buyer_name=buyer_name, buyer_email=buyer_email,
                                                  content=content, delivery_id=delivery_id,
                                                  template_params=template_params)

    assert run(make(tmp_path, clock, store, sender=Odd(clock)).tick()).outcome == "terminado"
    assert store.recipients[0]["state"] == "dudoso" and store.recipients[0]["reason"] == "fallo:chatwoot_http_error"
    assert store.recipients[1]["state"] == "vetado" and store.recipients[1]["reason"] == "veto:escribio_hace_poco"
    assert store.recipients[2]["state"] == "enviado"


# ---------------------------------------------------------------------------
# Vetos por tanda y preparadas.
# ---------------------------------------------------------------------------

def test_the_vetoes_of_each_lot_and_the_revalidation_of_the_prepared(tmp_path):
    clock = Clock(CENTER)
    store = FakeStore(clock, 40, prepared=(5, 6, 7))
    sender = FakeSender(clock)
    verdicts = {5: (False, "veto_con_equipo"), 6: (False, "prep_no_abierta"), 7: (True, "preparada")}
    worker = make(tmp_path, clock, store, sender=sender, vetoes={3: "compro", 33: "baja"},
                  revalidate=lambda prepared: verdicts[prepared.position])
    assert run(worker.tick()).outcome == "terminado"
    assert store.recipients[3] == {**store.recipients[3], "state": "vetado", "reason": "veto:compro"}
    assert store.recipients[33]["reason"] == "veto:baja"
    assert store.recipients[5]["state"] == "vetado" and store.recipients[5]["chatwoot_conversation_id"] == 505
    kinds = {position: kind for kind, position, _, _ in sender.calls}
    assert kinds[6] == "first_touch" and kinds[7] == "to_conversation" and 3 not in kinds and 5 not in kinds
    assert all(len(c) <= 25 for c in worker.veto_calls)  # type: ignore[attr-defined]


def test_vetoes_that_cannot_be_read_three_times_brake(tmp_path):
    clock = Clock(CENTER)
    store = FakeStore(clock, 40)
    sender = FakeSender(clock)
    worker = make(tmp_path, clock, store, sender=sender)

    async def broken(send, members, prepared):
        raise RuntimeError("chatwoot")

    worker._vetoes = broken  # noqa: SLF001
    assert run(worker.tick()).outcome == "frenado:vetos_no_se_leen" and not sender.calls


def test_batch_vetoes_ask_chatwoot_only_for_the_unprepared_of_the_lot():
    looked: list[str] = []
    now = CENTER

    class FakeChatwoot:
        inbox_id = 13

        def __init__(self) -> None:
            self.client = self

        async def find_contacts(self, phone):
            looked.append(phone)
            if phone.endswith("1"):
                return [{"id": 11, "blocked": True}]
            if phone.endswith("2"):
                return [{"id": 12, "blocked": False}]
            return []

        async def list_contact_conversations(self, *, contact_id, inbox_id, messages_since_epoch):
            return [ContactConversation(conversation_id=99, status="resolved", last_activity_at=1,
                                        labels=("cliente",), assignee_present=False, team_present=False,
                                        last_inbound_at=None)]

    class FakeBase:
        async def base_vetoes(self, *, list_id, product_refs):
            assert product_refs == ["maribel_forever"]
            return {4: "compro", 900: "baja"}

    source = ChatwootBatchVetoSource(FakeChatwoot(), clock=lambda: now)  # type: ignore[arg-type]
    vetoes = batch_vetoes(base=FakeBase(), chatwoot=source)
    store = FakeStore(Clock(now), 5)
    send = BulkSendSnapshotFrom(store)
    members = [BatchMember(i, f"57300{i:07d}") for i in range(5)]
    result = run(vetoes(send, members, frozenset({0})))
    assert result == {1: "bloqueado", 2: "clienta", 4: "compro"}
    assert "573000000000" not in looked and "573000000001" in looked


# ---------------------------------------------------------------------------
# El candado del portfolio y la regla de las 24 h.
# ---------------------------------------------------------------------------

def test_a_busy_portfolio_lock_waits_and_sends_nothing(tmp_path):
    clock = Clock(CENTER)
    store = FakeStore(clock, 10)
    sender = FakeSender(clock)
    other = PortfolioLock(tmp_path / "candado")  # el bridge de ATT1, en la misma carpeta del host
    assert other.try_acquire({"instance": "att1", "send": "otro"})
    worker = make(tmp_path, clock, store, sender=sender)
    assert run(worker.tick()).outcome == "candado_ocupado"
    assert store.send["state"] == "confirmado" and not sender.calls
    other.release()
    assert run(worker.tick()).outcome == "terminado"


def test_the_24_hour_rule_after_a_meta_brake_survives_a_restart(tmp_path):
    clock = Clock(CENTER)
    lock_dir = tmp_path / "candado"
    PortfolioLock(lock_dir).record_brake(FinishedSend("a1b2c3d4e5f6", CENTER - timedelta(hours=3),
                                                      "meta_codigo:131048_x1"), instance="att1", now=CENTER)
    store = FakeStore(clock, 10, confirmed_at=CENTER - timedelta(hours=5))
    sender = FakeSender(clock)
    report = run(make(tmp_path, clock, store, sender=sender, lock_dir=lock_dir).tick())
    assert report.outcome == "frenado:regla_24h:freno_meta_a1b2c3d4e5f6" and not sender.calls
    # Confirmado de nuevo despues del freno: arranca.
    store = FakeStore(clock, 10, confirmed_at=CENTER - timedelta(hours=1))
    assert run(make(tmp_path, clock, store, sender=sender, lock_dir=lock_dir).tick()).outcome == "terminado"
    # Pasadas las 24 h, tampoco frena.
    clock.now = CENTER + timedelta(hours=22)
    store = FakeStore(clock, 10, confirmed_at=CENTER - timedelta(hours=5), scheduled_at=clock.now)
    assert run(make(tmp_path, clock, store, sender=FakeSender(clock), lock_dir=lock_dir).tick()).outcome \
        == "terminado"


# ---------------------------------------------------------------------------
# Las plantillas de reintento y la configuracion.
# ---------------------------------------------------------------------------

def test_a_bulk_template_in_the_retry_list_fails_the_configuration_and_brakes_the_send(tmp_path):
    with pytest.raises(BulkSendConfigError):
        assert_not_retried({"maribel_masivo_prueba_01"}, {"maribel_carrito_01", "maribel_masivo_prueba_01"})
    with pytest.raises(BulkSendConfigError):
        BulkSendWorkerConfig.from_env({"BULK_SEND_INSTANCE_KEY": "maribel", "BULK_SEND_PORTFOLIO_LOCK_DIR": "/x",
                                       "META_FIRST_TOUCH_RETRY_TEMPLATES": "maribel_carrito_01,maribel_masivo_prueba_01",
                                       "BULK_SEND_TEMPLATES": "maribel_masivo_prueba_01"})
    config = BulkSendWorkerConfig.from_env({"BULK_SEND_INSTANCE_KEY": "maribel", "BULK_SEND_PORTFOLIO_LOCK_DIR": "/x",
                                            "META_FIRST_TOUCH_RETRY_TEMPLATES": "maribel_carrito_01",
                                            "BULK_SEND_SEND_HOURS": "10-20"})
    assert config.retry_templates == {"maribel_carrito_01"} and config.send_hours == (10, 20)
    with pytest.raises(BulkSendConfigError):
        BulkSendWorkerConfig.from_env({"BULK_SEND_INSTANCE_KEY": "maribel"})
    assert parse_portfolio_numbers("maribel=100000000001,att1=100000000002") == {
        "maribel": "100000000001", "att1": "100000000002"}
    clock = Clock(CENTER)
    store = FakeStore(clock, 10, template_name="maribel_carrito_01")
    sender = FakeSender(clock)
    worker = make(tmp_path, clock, store, sender=sender, retry_templates=frozenset({"maribel_carrito_01"}))
    assert run(worker.tick()).outcome == "frenado:plantilla_con_reintento" and not sender.calls


def test_a_template_meta_does_not_have_approved_brakes(tmp_path):
    clock = Clock(CENTER)
    store = FakeStore(clock, 10)
    worker = make(tmp_path, clock, store)
    worker._templates = FakeTemplates(None)  # noqa: SLF001
    assert run(worker.tick()).outcome == "frenado:plantilla_no_aprobada"


# ---------------------------------------------------------------------------
# Los adaptadores reales.
# ---------------------------------------------------------------------------

def test_the_portfolio_health_from_the_captured_reading():
    client = _meta_client(copy.deepcopy(META_HEALTH["crudas"]))
    readings = run(MetaPortfolioHealthReader({"maribel": (client, "100000000001"),
                                              "att1": (client, "100000000002")}).read())
    for key in ("maribel", "att1"):
        expected = META_HEALTH["normalizadas"][key]
        assert readings[key] == {"calidad": expected["calidad"], "limite": expected["limite"],
                                 "estado_numero": expected["estado_numero"], "puede_enviar": expected["puede_enviar"]}


def test_send_first_touch_to_conversation_takes_the_bulk_template_params():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={
            "id": 5884, "conversation_id": 143, "message_type": 1, "private": False, "content": body["content"],
            "content_attributes": body["content_attributes"], "sender": {"type": "agent_bot", "id": 2}})

    client = ChatwootClient(base_url="https://chatwoot.test", account_id=3, access_token="control",
                            agent_bot_access_token="bot", agent_bot_id=2, transport=httpx.MockTransport(handler))
    sender = ChatwootMessageSender(chatwoot=client, inbox_id=13, allowed_jid=None, dynamic_recipient_enabled=True,
                                   template=None)
    params = template_params(TEMPLATE, {"1": "qué tal", "2": "hoy a las 8"}, None)
    result = run(sender.send_first_touch_to_conversation(conversation_id=143, phone="573001234567", buyer_name="",
                                                         content="Hola qué tal, hoy a las 8",
                                                         delivery_id="bulk:x:1", template_params=params))
    assert result == FirstTouchResult("sent", 143, 5884)
    assert seen[0]["template_params"] == params
    expected = hashlib.sha256(b"followup:bulk:x:1").hexdigest()
    assert seen[0]["content_attributes"]["recovery_followup_hash"] == expected
    # Sin los parametros y sin plantilla, lo de siempre: no sale.
    blocked = run(sender.send_first_touch_to_conversation(conversation_id=143, phone="573001234567",
                                                          buyer_name="Ana", content="x", delivery_id="d"))
    assert blocked.status == "blocked" and len(seen) == 1


def test_the_brake_notice_has_no_people():
    from bridge.bulk_send_worker import brake_notice

    notice = brake_notice(send_id=SEND_ID, reason="salud:att1:calidad_GREEN_a_YELLOW", occurred_at=CENTER)
    assert notice.event_code == "SYS-007" and notice.reason_code == "salud:att1:calidad_green_a_yellow"
    assert notice == brake_notice(send_id=SEND_ID, reason="salud:att1:calidad_GREEN_a_YELLOW", occurred_at=CENTER)
