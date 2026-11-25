"""La prueba de punta a punta del masivo por el AE (encargo 11), sobre un Postgres de verdad.

Mapa: ``OS/productos/soporte-infoproductores/documentacion/2026-11-25-mapa-masivo-por-el-ae.md``. Corre
en el CI (el paso «Verify the bulk send end to end on PostgreSQL» de ``.github/workflows/ci.yml``, con
``BULK_SEND_E2E_DATABASE_URL``); sin esa variable se saltea. El arnes y de donde sale cada respuesta de
Chatwoot y de Meta estan en ``tests/bulk_send_e2e_harness.py``.

Todo lo del bridge es lo real y armado por ``bulk_send_wiring.build_bulk_send`` (el mismo armado que
prende ``BULK_SEND_ENABLED``): la pagina de aprobacion con su sesion de «Entrar con Slack», la subida y
la normalizacion (``bulk_import``), los vetos (la base y Chatwoot), preparar, el motor con el piloto, los
lotes, la ventana, el candado y la regla de las 24 h, los frenos, el aviso ``SYS-007`` y el reporte. La
base es la cadena entera de migraciones detras de un PostgREST emulado. Lo unico que no es de produccion
es el reloj del bridge: arranca en la hora real y avanza con cada espera (el piloto espera 5 minutos y
la retencion 15), y el horario permitido va de 0 a 24 para que la prueba no dependa de la hora del CI.

El recorrido:

1. una lista en CSV (con filas que no sirven) se sube con sesion, se normaliza, se revisan los vetos
   (baja y compra de la base; clienta, con el equipo y bloqueado de Chatwoot) y se confirma con el
   numero escrito; preparar deja contacto, conversacion y etiqueta; con el candado del portfolio tomado
   por otro bridge no sale nada; suelto, sale la tanda piloto de 20, espera 5 minutos mirando la
   entrega y sigue en lotes de 25 dentro de la ventana; el reporte se lee en la pagina;
2. una lista en JSON y otra en Excel: la primera frena por la calidad de otro numero del portfolio
   (leida en vivo de Meta) y avisa; la segunda, confirmada antes de ese freno, frena por la regla de las
   24 h;
3. una lista en la que Meta no confirma la entrega: el piloto frena por retencion y el reporte lo dice;
4. una lista en la que Meta rechaza una plantilla con un codigo que corta (el 131042 capturado de
   Maribel): el piloto frena al primero y el reporte lo explica en castellano.

Deuda declarada: el camino de una persona sin preparar (``send_first_touch``) no se recorre, porque busca
el contacto por ``/contacts/search`` y no hay una captura de esa respuesta; todas las personas de la
prueba pasan por preparar. Y la respuesta del POST de un mensaje del AgentBot no esta capturada (ver el
arnes).
"""

from __future__ import annotations

import asyncio
import csv
import functools
import io
import json
import os
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from bulk_send_e2e_harness import (
    ACCOUNT,
    AGENT_BOT_ID,
    APPROVER_KEY,
    CHATWOOT_URL,
    DATABASE_URL_VARIABLE,
    DELIVERED,
    FAILED_131042,
    INBOX,
    INSTANCE,
    LABEL,
    READ,
    REST_URL,
    SENT,
    SERVICE_KEY,
    ChatwootE2E,
    Clock,
    Database,
    MetaE2E,
    PostgrestOverPsql,
    healthy_file,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_bulk_send_wiring import Producer
from test_flow_approval_page import CLIENT_ID, CLIENT_SECRET, DAN, ORIGIN, SlackEmulator, _settings, _sign_in

from bridge import bulk_send_wiring as wiring
from bridge import flow_approval
from bridge.bulk_import import read_bulk_list
from bridge.bulk_send_prepare import prepare_bulk_send
from bridge.bulk_send_worker import PortfolioLock, snapshot_of
from bridge.chatwoot import ChatwootClient
from bridge.meta_templates import MetaTemplatesClient

DATABASE_URL = os.environ.get(DATABASE_URL_VARIABLE)
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason=f"sin {DATABASE_URL_VARIABLE}: corre en el CI")

FIXTURES = Path(__file__).parent / "fixtures"
PAGE = "/flow-approval/masivos"
COUNTRY = "52"  # ATT1 esta en la zona de CDMX
TEMPLATE = "att1_carrito_abandonado_01"
NAMES = ("Ana", "Beto", "Cami", "Dani", "Eva", "Fede", "Gabi", "Hugo")


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


@pytest.fixture(scope="module")
def db() -> Database:
    assert DATABASE_URL
    base = Database(DATABASE_URL)
    base.install()
    base.seed()
    return base


def ghl_csv(people: int, *, start: int, extra_rows: list[list[str]] = ()) -> bytes:
    """Una exportacion de contactos de GHL como la del fixture ``lista_ghl_sintetica_cp1252_punto_y_coma.csv``
    (sus encabezados, ``;``, CRLF y cp1252), con ``people`` moviles de CDMX sinteticos de digitos repetidos."""
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow(["First Name", "Last Name", "Phone", "Email", "Tags"])
    for i in range(start, start + people):
        writer.writerow([NAMES[i % len(NAMES)], "Prueba", f"+52 1 55 {i:04d} {i:04d}", f"persona{i}@example.com",
                         "vivo"])
    for row in extra_rows:
        writer.writerow(row)
    return out.getvalue().encode("cp1252")


class _EpochView:
    """El reloj como lo lee ``_sign_in`` (``clock.now`` en segundos)."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    @property
    def now(self) -> float:
        return self._clock.epoch()


class World:
    """Un bridge de ATT1 con el masivo prendido, sobre la base de la prueba, con su candado y su salud."""

    def __init__(self, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, outcome=None) -> None:
        self.db = db
        self.clock = Clock()
        self.pg = PostgrestOverPsql(db)
        self.cw = ChatwootE2E(self.clock, outcome=outcome)
        self.meta = MetaE2E()
        self.lock_dir = tmp_path / "portfolio"
        self.lock_dir.mkdir()
        self.health = tmp_path / "estado.json"
        healthy_file(self.health, self.clock)
        env = {"BULK_SEND_INSTANCE_KEY": "att1", "BULK_SEND_PORTFOLIO_LOCK_DIR": str(self.lock_dir),
               "BULK_SEND_PORTFOLIO_NUMBERS": "att1=100000000002,maribel=100000000001",
               "BULK_SEND_META_HEALTH_PATH": str(self.health), "BULK_SEND_SEND_HOURS": "0-24"}
        control = ChatwootClient(base_url=CHATWOOT_URL, account_id=ACCOUNT, access_token="token-de-control",
                                 agent_bot_access_token="token-del-agentbot", agent_bot_id=AGENT_BOT_ID,
                                 transport=self.cw.transport())
        meta = MetaTemplatesClient(access_token="EAA" + "x" * 40, waba_id="1234567890123",
                                   transport=self.meta.transport())
        # Preparar va a 1 por segundo con el reloj del bridge (build_bulk_send no recibe el reloj de preparar).
        monkeypatch.setattr(wiring, "prepare_bulk_send", functools.partial(
            prepare_bulk_send, sleep=self.clock.sleep, clock=self.clock.epoch, monotonic=self.clock.epoch))
        self.producer = Producer()
        bulk = wiring.build_bulk_send(
            enabled=True, environ=env, supabase_base_url=REST_URL, supabase_service_role_key=SERVICE_KEY,
            tenant_ref=INSTANCE["tenant_ref"], ally_ref=INSTANCE["ally_ref"], time_zone="America/Mexico_City",
            chatwoot_base_url=CHATWOOT_URL, chatwoot_account_id=ACCOUNT, chatwoot_inbox_id=INBOX,
            chatwoot_control_token="token-de-control", agent_bot_ready=True, control_client=control,
            meta_access_token=None, meta_waba_id=None, meta_graph_version="v23.0", producer=self.producer,
            campaigns=None, meta_client=meta, supabase_transport=self.pg.transport(),
            chatwoot_transport=self.cw.transport(), clock=self.clock.dt)
        assert bulk is not None
        bulk.worker._sleep = self.clock.sleep  # noqa: SLF001 - el reloj de la prueba tambien para las esperas
        self.bulk = bulk
        settings = _settings(supabase_base_url=REST_URL, supabase_service_role_key=SERVICE_KEY,
                             flow_change_approver_db_key=APPROVER_KEY)
        config = flow_approval.check_settings(settings)
        assert config is not None
        self.slack = SlackEmulator()
        app = FastAPI()
        flow_approval.register_flow_approval_routes(
            app, config=config,
            store=flow_approval.ApprovalStore(base_url=REST_URL, db_key=APPROVER_KEY, transport=self.pg.transport()),
            oidc=flow_approval.SlackIdTokenClient(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                                  transport=httpx.MockTransport(self.slack.handler),
                                                  clock=self.clock.epoch),
            time_zone="America/Mexico_City", clock=self.clock.dt, extra_routes=bulk.page)
        self.client = TestClient(app, base_url=ORIGIN)
        self.outcomes: list[str] = []

    # -- la pagina, con sesion -------------------------------------------------------------------
    def sign_in(self) -> World:
        response = _sign_in(self.client, self.slack, _EpochView(self.clock), user=DAN, proposal=None)
        assert response.status_code == 303, response.text
        return self

    def page(self, send_id: str) -> str:
        response = self.client.get(f"{PAGE}/{send_id}")
        assert response.status_code == 200, response.text
        return response.text

    def csrf(self, send_id: str) -> str:
        match = re.search(r'name="csrf_token" value="([^"]+)"', self.page(send_id))
        assert match is not None
        return match.group(1)

    @staticmethod
    def done(response: httpx.Response) -> str:
        assert response.status_code == 303, response.text
        return parse_qs(urlsplit(response.headers["location"]).query)["hecho"][0]

    def upload(self, send_id: str, data: bytes, filename: str) -> str:
        fields = {"csrf_token": self.csrf(send_id), "origen": "inscritas_ghl", "aviso": "si",
                  "nota": "inscritas al vivo"}
        response = self.client.post(f"{PAGE}/{send_id}/subir", data=fields,
                                    files={"archivo": (filename, data, "application/octet-stream")},
                                    headers={"Origin": ORIGIN}, follow_redirects=False)
        return self.done(response)

    def confirm(self, send_id: str, count: int | str) -> str:
        response = self.client.post(
            f"{PAGE}/{send_id}/confirmar",
            content=urlencode({"csrf_token": self.csrf(send_id), "conteo": str(count), "confirmo": "si"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": ORIGIN}, follow_redirects=False)
        return self.done(response)

    # -- lo que hace el AE: el borrador ----------------------------------------------------------
    def draft(self, *, in_minutes: float) -> str:
        scheduled = datetime.now(timezone.utc) + timedelta(minutes=in_minutes)
        out = self.pg.rpc("create_bulk_send_v1", p_instance=INSTANCE, p_send={
            "template_name": TEMPLATE, "template_language": "es_MX",
            "template_variables": [{"de": "nombre"}, "el Protocolo"], "campaign_label": LABEL,
            "scheduled_at": scheduled.isoformat(), "product_refs": ["att1"]})
        assert out["ok"] is True
        return str(out["send_id"])

    def view(self, send_id: str) -> dict[str, Any]:
        return self.pg.rpc("read_bulk_send_v1", p_instance=INSTANCE, p_send_id=send_id)

    def window(self, send_id: str) -> Any:
        snapshot = snapshot_of(self.view(send_id))
        return self.bulk.worker.window_for(snapshot, now=self.clock.dt())

    # -- el motor --------------------------------------------------------------------------------
    def tick(self) -> str:
        report = run(self.bulk.worker.tick())
        self.outcomes.append(report.outcome)
        return report.outcome

    def advance_to(self, moment: datetime) -> None:
        if moment > self.clock.now:
            self.clock.now = moment


def normalized(data: bytes, filename: str) -> list[Any]:
    return list(read_bulk_list(data, country_code=COUNTRY, filename=filename).members)


# ---------------------------------------------------------------------------
# 1. Subir, revisar, confirmar, preparar, el candado, mandar y el reporte.
# ---------------------------------------------------------------------------


def test_a_csv_list_goes_up_is_vetted_confirmed_prepared_sent_by_lots_and_reported(db, tmp_path, monkeypatch):
    invalid = [["Sin", "Telefono", "", "x@example.com", "vivo"], ["Corto", "Uno", "601 555", "", "vivo"],
               ["Beto", "Repetida", "+52 1 55 0001 0001", "", "vivo"]]
    data = ghl_csv(55, start=1, extra_rows=invalid)
    members = normalized(data, "lista.csv")
    assert len(members) == 55
    phones = [m.phone for m in members]
    fates = {phones[i]: READ for i in (3, 7, 11)}

    world = World(db, tmp_path, monkeypatch, outcome=lambda phone: fates.get(phone, DELIVERED))
    # Lo que ya hay antes del masivo: en Chatwoot, una clienta, una con el equipo y un bloqueado; en la base,
    # una baja y una compra del producto del envio.
    world.cw.seed_conversation(phones[0], labels=("cliente",))
    world.cw.seed_conversation(phones[1], assignee=True, status="open")
    world.cw.seed_conversation(phones[2], blocked=True)
    db.opt_out(phones[4])
    db.purchase(phone=phones[5], email="otra5@example.com", transaction="HPE2E0000005")
    world.cw.replies[phones[7]] = "No más mensajes"
    world.cw.replies[phones[8]] = "Hola, me interesa"
    world.sign_in()

    send_id = world.draft(in_minutes=40)
    # Subir con sesion: la pagina normaliza, guarda solo las filas validas y corre los vetos.
    assert world.upload(send_id, data, "lista.csv") == "lista_revisada"
    view = world.view(send_id)
    listed = view["list"]
    assert listed["member_count"] == 55 and listed["state"] == "revisada"
    assert listed["excluded_by_reason"] == {"clienta": 1, "con_equipo": 1, "bloqueado": 1, "baja": 1, "compro": 1}
    assert listed["eligible_count"] == 50
    assert sum(listed["rejected_counts"].values()) == len(invalid)
    page = world.page(send_id)
    assert "Van: <strong>50</strong> personas" in page and TEMPLATE in page
    # El archivo crudo no llega a la base: ni nombres de pila de mas ni los telefonos con formato.
    assert "+52 1 55" not in json.dumps(view)

    # Confirmar con el numero escrito: otro numero no confirma.
    assert world.confirm(send_id, 49) == "count_mismatch"
    assert world.confirm(send_id, 50) == "confirmado"
    assert world.view(send_id)["state"] == "confirmado"

    # Preparar: antes de los 10 minutos previos al arranque, contacto, conversacion y etiqueta, sin mandar.
    first = world.tick()
    assert first.startswith("preparar:"), first
    prepared = int(db.query(f"select count(*) from public.bulk_send_prepared where send_id = '{send_id}'"))
    assert prepared == 50
    assert not world.cw.templates_sent
    for phone in phones[6:10]:
        (conversation,) = world.cw.conversation_of_phone(phone)
        assert LABEL in conversation.labels

    # El candado del portfolio tomado por otro bridge (Maribel): a la hora no sale nada.
    window = world.window(send_id)
    world.advance_to(window.start)
    other = PortfolioLock(world.lock_dir)
    assert other.try_acquire({"instance": "maribel", "send": "otro-masivo", "since": world.clock.dt().isoformat()})
    assert world.tick() == "candado_ocupado"
    assert not world.cw.templates_sent and world.view(send_id)["state"] in ("confirmado", "preparando")
    other.release()

    # Suelto el candado: piloto de 20, 5 minutos mirando la entrega, y el resto en lotes de 25.
    base_calls_before = sum(1 for _, name in world.pg.calls if name == "read_bulk_list_base_vetoes_v1")
    outcome = world.tick()
    assert outcome == "terminado", (outcome, world.outcomes)
    sent = world.cw.templates_sent
    assert len(sent) == 50 and len({s["phone"] for s in sent}) == 50
    assert {s["phone"] for s in sent}.isdisjoint([phones[i] for i in (0, 1, 2, 4, 5)])
    created = [m["created_at"] for m in sorted(world.cw.template_messages(), key=lambda m: m["id"])]
    assert created[20] - created[19] >= 300, "despues del piloto de 20, la espera de 5 minutos"
    assert max(b - a for a, b in zip(created[:19], created[1:20])) < 300
    # Una lectura de los vetos de la base por lote: el piloto y dos lotes de 25 (30 personas).
    base_calls = sum(1 for _, name in world.pg.calls if name == "read_bulk_list_base_vetoes_v1") - base_calls_before
    assert base_calls == 3, base_calls
    # La ventana: centrada en la hora pedida (D = 50 / 2,5 = 20 s); arranca al principio y, con la espera del
    # piloto, termina pasado el fin pero antes de la hora de corte.
    assert window.seconds == 20.0
    assert window.start.timestamp() - 1 <= created[0] and created[-1] <= window.cut.timestamp()
    # Cada persona con su nombre de pila y la variable fija.
    by_phone = {m.phone: m for m in members}
    for item in sent:
        params = item["params"]
        assert params["name"] == TEMPLATE and params["language"] == "es_MX"
        assert params["processed_params"]["body"] == {"1": by_phone[item["phone"]].first_name, "2": "el Protocolo"}
    recipients = db.query(f"""select json_object_agg(state, n) from (select state, count(*) n
        from public.bulk_send_recipients where send_id = '{send_id}' group by state) x""")
    assert json.loads(recipients) == {"enviado": 50}
    assert world.view(send_id)["state"] == "terminado"

    # Despues del envio, una de las que recibio compra (por el mail de la lista).
    db.purchase(phone="525599999999", email="persona10@example.com", transaction="HPE2E0000010")

    # El reporte en la pagina, leido de la base y de Chatwoot.
    page = world.page(send_id)
    assert "Mandados 50, entregados 50 (leídos 3)" in page, re.findall(r"Mandados[^<]*", page)
    assert "Respuestas 2, bajas 1" in page and "compras 1" in page
    undelivered = world.client.get(f"{PAGE}/{send_id}/no-entregados.csv")
    assert undelivered.status_code == 200
    assert undelivered.text.lstrip("\ufeff").strip().splitlines() == ["telefono,conversacion,estado,codigo_meta,explicacion"]
    assert world.producer.commands == []


# ---------------------------------------------------------------------------
# 2. Frenar a mano, la calidad de otro numero del portfolio y la regla de las 24 h.
# ---------------------------------------------------------------------------


def test_brakes_by_hand_by_the_quality_of_another_number_and_by_the_24h_rule(db, tmp_path, monkeypatch):
    json_list = (FIXTURES / "bulk_import" / "lista_sintetica.json").read_bytes()
    xlsx_list = (FIXTURES / "bulk_import" / "lista_sintetica_openpyxl.xlsx").read_bytes()
    world = World(db, tmp_path, monkeypatch).sign_in()
    # La salud en vivo cada 10 envios (200 por defecto: con 30 personas no se llegaria a leer en el medio).
    world.bulk.worker._config = replace(world.bulk.worker._config, health_every=10)  # noqa: SLF001
    sent_so_far = {"n": 0}

    def degrade(phone: str) -> str:
        # Despues del quinto envio, el numero de Maribel (mismo portfolio) pasa de verde a amarillo en Meta.
        sent_so_far["n"] += 1
        if sent_so_far["n"] == 5:
            world.meta.raw["maribel"]["quality_rating"] = "YELLOW"
        return DELIVERED

    world.cw.outcome = degrade

    # Un Excel: subido, confirmado y frenado a mano desde la pagina antes de la hora.
    by_hand = world.draft(in_minutes=30)
    assert world.upload(by_hand, xlsx_list, "lista.xlsx") == "lista_revisada"
    eligible = world.view(by_hand)["list"]["eligible_count"]
    assert eligible == len(normalized(xlsx_list, "lista.xlsx")) > 0
    assert world.confirm(by_hand, eligible) == "confirmado"
    response = world.client.post(f"{PAGE}/{by_hand}/frenar", content=urlencode({"csrf_token": world.csrf(by_hand)}),
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": ORIGIN},
                                 follow_redirects=False)
    assert World.done(response) == "frenado"
    assert world.view(by_hand)["state"] == "frenado" and world.view(by_hand)["braked_by"] == f"slack:{DAN}"

    # Un CSV de 30 y un JSON, confirmados los dos antes de que nada frene.
    first = world.draft(in_minutes=40)
    second = world.draft(in_minutes=80)
    assert world.upload(first, ghl_csv(30, start=300), "lista.csv") == "lista_revisada"
    assert world.upload(second, json_list, "lista.json") == "lista_revisada"
    assert world.confirm(first, 30) == "confirmado"
    eligible = world.view(second)["list"]["eligible_count"]
    assert eligible == len(normalized(json_list, "lista.json")) > 0
    assert world.confirm(second, eligible) == "confirmado"

    # Preparar los dos (una pasada cada uno) antes de la hora.
    assert [world.tick() for _ in range(3)] == ["preparar:terminado", "preparar:terminado", "esperando"]
    # El primero arranca y frena en el medio cuando empeora la salud de otro numero del portfolio.
    world.advance_to(world.window(first).start)
    outcome = None
    for _ in range(6):
        outcome = world.tick()
        if outcome.startswith("frenado"):
            break
    assert outcome is not None and "salud:maribel:calidad_green_a_yellow" in outcome, (outcome, world.outcomes)
    assert world.view(first)["state"] == "frenado"
    # La salud se vuelve a leer antes del lote siguiente: freno con el piloto de 20 afuera y el resto sin salir.
    assert len(world.cw.templates_sent) == 20
    assert [c.event_code for c in world.producer.commands] == ["SYS-007"]
    assert (world.lock_dir / "frenos-meta.json").exists()

    # Maribel vuelve a verde. El segundo, confirmado antes de ese freno, no arranca en las 24 h siguientes.
    world.meta.raw["maribel"]["quality_rating"] = "GREEN"
    before = len(world.cw.templates_sent)
    world.advance_to(world.window(second).start)
    for _ in range(6):
        outcome = world.tick()
        if outcome.startswith("frenado"):
            break
    assert outcome.startswith("frenado:regla_24h"), (outcome, world.outcomes)
    assert world.view(second)["state"] == "frenado" and len(world.cw.templates_sent) == before
    assert [c.event_code for c in world.producer.commands] == ["SYS-007", "SYS-007"]


# ---------------------------------------------------------------------------
# 3. Meta retiene la tanda piloto.
# ---------------------------------------------------------------------------


def test_meta_holding_the_pilot_brakes_after_fifteen_minutes_and_the_report_says_it(db, tmp_path, monkeypatch):
    data = ghl_csv(30, start=200)
    world = World(db, tmp_path, monkeypatch, outcome=lambda phone: SENT).sign_in()
    send_id = world.draft(in_minutes=40)
    assert world.upload(send_id, data, "lista.csv") == "lista_revisada"
    assert world.confirm(send_id, 30) == "confirmado"
    assert world.tick().startswith("preparar:")
    world.advance_to(world.window(send_id).start)
    outcome = world.tick()
    assert outcome.startswith("frenado:piloto:entrega:sin_entrega_20_de_20"), (outcome, world.outcomes)
    assert len(world.cw.templates_sent) == 20
    assert world.clock.slept >= 900
    assert world.view(send_id)["state"] == "frenado"
    assert [c.event_code for c in world.producer.commands] == ["SYS-007"]

    page = world.page(send_id)
    assert "Mandados 20, entregados 0 (leídos 0)" in page and "sin confirmar todavía 20" in page
    undelivered = world.client.get(f"{PAGE}/{send_id}/no-entregados.csv").text.lstrip("\ufeff").strip().splitlines()
    assert len(undelivered) == 1 + 30  # 20 sin confirmar y 10 que no llegaron a salir
    assert sum(1 for row in undelivered if ",sin confirmar," in row) == 20
    assert sum(1 for row in undelivered if ",no llegó a salir," in row) == 10


# ---------------------------------------------------------------------------
# 4. Un codigo de Meta que corta.
# ---------------------------------------------------------------------------


def test_a_meta_code_that_cuts_brakes_the_pilot_at_the_first_and_the_report_explains_it(db, tmp_path, monkeypatch):
    data = ghl_csv(25, start=400)
    members = normalized(data, "lista.csv")
    rejected = members[3].phone
    world = World(db, tmp_path, monkeypatch,
                  outcome=lambda phone: FAILED_131042 if phone == rejected else DELIVERED).sign_in()
    send_id = world.draft(in_minutes=40)
    assert world.upload(send_id, data, "lista.csv") == "lista_revisada"
    assert world.confirm(send_id, 25) == "confirmado"
    assert world.tick().startswith("preparar:")
    world.advance_to(world.window(send_id).start)
    outcome = world.tick()
    assert outcome == "frenado:piloto:meta_codigo:131042_x1", (outcome, world.outcomes)
    assert len(world.cw.templates_sent) == 20
    page = world.page(send_id)
    assert "fallidos 1" in page and "131042" in page
    undelivered = world.client.get(f"{PAGE}/{send_id}/no-entregados.csv").text.lstrip("\ufeff").strip().splitlines()
    (failed,) = [row for row in undelivered if ",fallido," in row]
    assert failed.startswith(rejected) and ",131042," in failed
