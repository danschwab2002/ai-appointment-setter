"""El arnes de la prueba de punta a punta del masivo por el AE (encargo 11).

Lo usa ``tests/test_bulk_send_e2e_postgres.py``. Tres piezas, cada una lo mas real que se pudo:

* **La base.** Un Postgres de verdad (el 17 del CI, o uno en Docker) con la cadena entera de
  migraciones aplicada (``supabase/baseline`` + ``supabase/migrations``), y adelante un PostgREST
  emulado (``PostgrestOverPsql``): ``POST /rest/v1/rpc/<funcion>`` con el cuerpo JSON se vuelve
  ``select public.<funcion>(p_x => ...)`` con los tipos que dice ``pg_proc``, en una transaccion con
  ``set local role`` del rol del JWT (``service_role`` para el bridge, ``flow_change_approver`` para la
  pagina). Los errores vuelven como PostgREST: ``{"code": <sqlstate>, "message": ...}``. Lo que la
  base hace es lo de las migraciones, sin doble.
* **Chatwoot** (``ChatwootE2E``): la API v4.13 que tocan la pagina, los vetos, preparar, el motor y
  el reporte, con estado, sobre lo capturado:

  - el catalogo de plantillas de la bandeja 11 de ATT1
    (``chatwoot_inbox_11_message_templates_20261010.json``, capturado);
  - la forma de toda conversacion (listado, show y conversaciones del contacto) sale del item
    capturado de la 9 de Maribel (``chatwoot_contact_conversations_inbox_13_contact_767_20261029.json``)
    con la cuenta, la bandeja, el id, el contacto y las etiquetas cambiados, como en
    ``test_bulk_send_vetoes.py`` y ``test_bulk_send_prepare.py``;
  - el envoltorio del listado de la bandeja, ``{"data": {"meta": {"all_count"}, "payload"}}``, el de
    ``chatwoot_daily_feedback_conversations_20260926.json`` (capturado), como en los vetos;
  - todo mensaje sale de los mensajes capturados de la 6 de Maribel
    (``chatwoot_conversation_messages_inbox_13_conv_6_20261029.json``): el saliente del AgentBot (4588)
    para la plantilla y el entrante del contacto (4587) para una respuesta;
  - el rechazo de Meta en la plantilla, ``external_error`` del 131042 capturado de Maribel
    (``chatwoot_db_messages_inbox_13_maribel_131042_20261107.json``);
  - el filtro de contactos, crear contacto, crear conversacion y el POST de etiquetas: **fixture de
    documentacion, falta captura real** (``chatwoot_v4_13_bulk_prepare_documentacion_20261125.json``,
    la misma deuda del #505); el index de etiquetas, documentacion
    (``automation_expert_chatwoot/chatwoot_v4_13_labels_macros_documentacion_20261101.json``);
  - **deuda: la respuesta del POST de un mensaje del AgentBot no esta capturada.** Se arma sobre el
    saliente capturado 4588 (el mismo serializador de mensajes de Chatwoot,
    ``app/views/api/v1/accounts/conversations/messages/create.json.jbuilder`` usa el partial de
    ``index``), con el id, la conversacion, el contenido y la marca ``recovery_followup_hash`` del
    envio, que es lo que el cliente del bridge valida. Se reemplaza con la primera captura del
    ensayo (encargo 12).
* **Meta** (``MetaE2E``): la salud de los numeros sale de
  ``bulk_send_worker/meta_phone_health_att1_maribel_20261125.json`` (#510; la forma cruda declarada en
  el fixture), y la lista de plantillas de la Graph API es el envoltorio documentado
  ``data`` + ``paging`` de ``meta_graph_templates_documented_20261108.json`` con las plantillas
  capturadas que Chatwoot copia de Meta (el mismo precedente que ``test_meta_templates.py``).

Nada de esto habla con produccion: los hosts son ``*.e2e.test`` y la base tiene que ser descartable
(``bulk_send_e2e*``, vacia).
"""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
DATABASE_URL_VARIABLE = "BULK_SEND_E2E_DATABASE_URL"
PSQL = os.environ.get("PSQL", "psql")

ACCOUNT = 2
INBOX = 11
AGENT_BOT_ID = 2
CHATWOOT_URL = "https://chatwoot.e2e.test"
REST_URL = "https://att1-rest.e2e.test"
INSTANCE = {"tenant_ref": "lancemos", "ally_ref": "att1"}
LABEL = "vivo-29-11"
NUMBERS = {"100000000002": "att1", "100000000001": "maribel"}


def _jwt(claims: dict[str, Any]) -> str:
    part = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{part({'alg': 'RS256', 'kid': 'k'})}.{part(claims)}.firma"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


CATALOG = _load("chatwoot_inbox_11_message_templates_20261010.json")
CONVERSATIONS_767 = _load("chatwoot_contact_conversations_inbox_13_contact_767_20261029.json")
MESSAGES_6 = _load("chatwoot_conversation_messages_inbox_13_conv_6_20261029.json")
CW_131042 = _load("chatwoot_db_messages_inbox_13_maribel_131042_20261107.json")
PREPARE_DOC = _load("chatwoot_v4_13_bulk_prepare_documentacion_20261125.json")
LABELS_DOC = _load("automation_expert_chatwoot/chatwoot_v4_13_labels_macros_documentacion_20261101.json")
META_HEALTH = _load("bulk_send_worker/meta_phone_health_att1_maribel_20261125.json")

CONVERSATION_ITEM = next(c for c in CONVERSATIONS_767["payload"] if c["id"] == 9)
OUTGOING_MESSAGE = next(m for m in MESSAGES_6["payload"] if m["id"] == 4588)
INCOMING_MESSAGE = next(m for m in MESSAGES_6["payload"] if m["id"] == 4587)
REJECTED_131042 = CW_131042["rechazados_131042"][0]


# ---------------------------------------------------------------------------
# El reloj: el de la base corre de verdad; el del bridge arranca en el real y avanza con cada espera.
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)
        self.slept = 0.0

    def dt(self) -> datetime:
        return self.now

    def epoch(self) -> float:
        return self.now.timestamp()

    async def sleep(self, seconds: float) -> None:
        step = max(0.0, float(seconds))
        self.slept += step
        self.now += timedelta(seconds=step)

    def catch_up(self) -> None:
        """El bridge nunca queda atras de la base (los envios y los cierres los fecha la base)."""
        self.now = max(self.now, datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# La base de verdad, por psql.
# ---------------------------------------------------------------------------


def pg_env(url: str) -> dict[str, str]:
    parsed = urlsplit(url)
    database = unquote(parsed.path.lstrip("/"))
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not database:
        raise RuntimeError(f"{DATABASE_URL_VARIABLE} must identify a PostgreSQL database")
    env = os.environ.copy()
    env.update(PGHOST=parsed.hostname, PGPORT=str(parsed.port or 5432), PGDATABASE=database)
    if parsed.username:
        env["PGUSER"] = unquote(parsed.username)
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    sslmode = parse_qs(parsed.query).get("sslmode")
    if sslmode:
        env["PGSSLMODE"] = sslmode[-1]
    return env


class SqlError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


_ERROR = re.compile(r"ERROR:\s+([0-9A-Z]{5}):\s+(.*)")


class Database:
    """La base descartable de la prueba. Cada llamada es una sesion de psql (como cada pedido de PostgREST)."""

    def __init__(self, url: str) -> None:
        self.env = pg_env(url)
        self.calls = 0

    def query(self, sql: str) -> str:
        self.calls += 1
        result = subprocess.run(
            [PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=verbose", "-c", sql],
            env=self.env, text=True, capture_output=True, check=False)
        if result.returncode:
            match = _ERROR.search(result.stderr)
            if match:
                raise SqlError(match.group(1), match.group(2).strip())
            raise RuntimeError(result.stderr.strip())
        return result.stdout.strip()

    def apply(self, path: Path) -> None:
        result = subprocess.run([PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", str(path)],
                                env=self.env, text=True, capture_output=True, check=False)
        if result.returncode:
            raise RuntimeError(f"{path.name}: {result.stderr.strip()}")

    def install(self) -> None:
        """Roles de Supabase, la base de la cadena y todas las migraciones, sobre una base vacia."""
        database = self.query("select current_database()")
        if not database.startswith("bulk_send_e2e"):
            raise RuntimeError("the database name must start with bulk_send_e2e")
        existing = self.query("""
          select (select count(*) from pg_class where relnamespace = 'public'::regnamespace)
               + (select count(*) from pg_proc where pronamespace = 'public'::regnamespace)""")
        if existing != "0":
            raise RuntimeError("refusing a non-empty database")
        self.query("""
          do $$ begin
            if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin; end if;
            if not exists (select 1 from pg_roles where rolname = 'authenticated') then
              create role authenticated nologin; end if;
            if not exists (select 1 from pg_roles where rolname = 'service_role') then
              create role service_role nologin bypassrls; end if;
          end $$;
          alter default privileges in schema public grant execute on functions to anon, authenticated;
          alter default privileges in schema public grant all on functions to service_role;
          alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
          alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;
          grant usage on schema public to service_role, anon, authenticated;""")
        self.apply(ROOT / "supabase/baseline/20260803_public_schema.sql")
        for migration in sorted((ROOT / "supabase/migrations").glob("*.sql")):
            self.apply(migration)
        acl = self.query(Path(ROOT / "scripts/supabase_acl_inventory.sql").read_text(encoding="utf-8"))
        rows = [line.split("|") for line in acl.splitlines() if line]
        if not rows or any(row[-1] != "ok" for row in rows):
            raise RuntimeError("ACL inventory failed")

    def seed(self) -> None:
        """La instancia de ATT1 (la misma fila que los validadores de PGlite del masivo)."""
        self.query("""
          set session_replication_role = replica;
          insert into public.commercial_ally_runtime_bindings (tenant_ref, funnel_ref, binding_version, status,
            ally_ref, lead_ally_name, lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
            product_name, product_price, currency, offer_code, consent_copy_version, hotmart_product_id,
            chatwoot_account_id, chatwoot_inbox_id, inbound_scope_key, inbound_scope_version)
          values ('lancemos', 'att1', 1, 'active', 'att1', 'Aliada de prueba', 'sitio', 'landing', 'prueba.example',
            '/landing', 'H00000000', 'Producto de prueba', 10, 'MXN', 'OFR11', 'copy-v1', 1011, 2, 11,
            'att1-inbound', 1);""")

    # -- los datos que vetan, sembrados como los dejaria el bridge --------------------------------

    def opt_out(self, phone: str) -> None:
        self.query(f"""
          insert into public.contact_opt_out_events (contact_id, channel, purpose, source, canonical_account_id,
              canonical_inbox_id, canonical_conversation_id, canonical_message_id, external_user_id, occurred_at,
              normalized_rule_key, correlation_status)
          values (null, 'whatsapp', 'cart_recovery', 'chatwoot', {ACCOUNT}, {INBOX}, 501, 601, '{phone}', now(),
                  'baja', 'unmatched')""")

    def purchase(self, *, phone: str, email: str, transaction: str, funnel: str = "att1") -> None:
        """Un PURCHASE_APPROVED capturado de Maribel (ya anonimizado) con el telefono, el mail y la
        transaccion de la persona, correlacionado al producto (lo que deja el correlador)."""
        payload = copy.deepcopy(_load("hotmart_purchase_approved_maribel_20261029.json")["payload"])
        payload["data"]["buyer"]["checkout_phone"] = phone
        payload["data"]["buyer"]["email"] = email
        payload["data"]["purchase"]["transaction"] = transaction
        body = json.dumps(payload).replace("$j$", "")
        event = self.query(f"""
          insert into public.webhook_events (source, external_event_id, event_type, payload)
          values ('hotmart', 'e2e-{transaction}', 'PURCHASE_APPROVED', $j${body}$j$::jsonb) returning id""")
        self.query(f"""
          insert into public.portable_hotmart_purchase_correlations (webhook_event_id, tenant_ref, funnel_ref,
              binding_version, policy_max_lookback, outcome, candidate_count, reason_code, observed_at)
          values ('{event}', 'lancemos', '{funnel}', 1, interval '30 days', 'unmatched', 0, 'sin_intencion', now())""")


# ---------------------------------------------------------------------------
# PostgREST, emulado sobre la base de verdad.
# ---------------------------------------------------------------------------


def _role_of(authorization: str | None) -> str:
    token = (authorization or "").removeprefix("Bearer ").strip()
    try:
        claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    except (IndexError, ValueError):
        raise PermissionError("jwt") from None
    role = claims.get("role")
    if role not in ("service_role", "flow_change_approver"):
        raise PermissionError(f"role {role}")
    return str(role)


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _literal(value: Any, sql_type: str) -> str:
    if value is None:
        return f"null::{sql_type}"
    if sql_type in ("jsonb", "json"):
        encoded = json.dumps(value, separators=(",", ":"))
        if "$pg$" in encoded:
            raise ValueError("unsafe json")
        return f"$pg${encoded}$pg$::{sql_type}"
    if sql_type.endswith("[]"):
        if not isinstance(value, list):
            raise ValueError(f"{sql_type} needs a list")
        if not value:
            return f"'{{}}'::{sql_type}"
        base = sql_type[:-2]
        return "array[" + ", ".join(_literal(v, base) for v in value) + f"]::{sql_type}"
    if isinstance(value, bool):
        return f"{'true' if value else 'false'}::{sql_type}"
    if isinstance(value, (int, float)):
        return f"{value}::{sql_type}"
    if isinstance(value, str):
        return f"{_quote(value)}::{sql_type}"
    raise ValueError(f"unsupported value for {sql_type}")


@dataclass
class _Function:
    names: list[str]
    types: list[str]
    returns_set: bool
    composite: bool


class PostgrestOverPsql:
    """``POST /rest/v1/rpc/<funcion>`` de PostgREST sobre la base de verdad, con el rol del JWT."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.calls: list[tuple[str, str]] = []
        self._functions: dict[str, list[_Function]] = {}

    def _signature(self, name: str) -> list[_Function]:
        if name not in self._functions:
            if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
                raise ValueError("function name")
            raw = self.db.query(f"""
              select coalesce(json_agg(json_build_object(
                  'names', coalesce(p.proargnames, '{{}}'::text[]),
                  'types', array(select format_type(t, null) from unnest(p.proargtypes::oid[]) t),
                  'set', p.proretset, 'composite', (select t.typtype = 'c' from pg_type t where t.oid = p.prorettype))),
                  '[]')::text
                from pg_proc p where p.pronamespace = 'public'::regnamespace and p.proname = {_quote(name)}""")
            self._functions[name] = [_Function(f["names"][:len(f["types"])], f["types"], f["set"], bool(f["composite"]))
                                     for f in json.loads(raw)]
        return self._functions[name]

    def handler(self, request: httpx.Request) -> httpx.Response:
        prefix = "/rest/v1/rpc/"
        if request.method != "POST" or not request.url.path.startswith(prefix):
            return httpx.Response(404, json={"code": "PGRST000", "message": "not an rpc"})
        name = request.url.path[len(prefix):]
        try:
            role = _role_of(request.headers.get("authorization"))
        except PermissionError:
            return httpx.Response(401, json={"code": "PGRST301", "message": "JWT invalid"})
        body = json.loads(request.content or b"{}")
        self.calls.append((role, name))
        candidates = [f for f in self._signature(name) if set(body) <= set(f.names)]
        if not candidates:
            return httpx.Response(404, json={"code": "PGRST202", "message": f"Could not find the function {name}"})
        function = candidates[0]
        args = ", ".join(f"{arg} => {_literal(body[arg], typ)}" for arg, typ in zip(function.names, function.types)
                         if arg in body)
        if function.returns_set or function.composite:
            select = f"select coalesce(jsonb_agg(to_jsonb(r)), '[]'::jsonb)::text from public.{name}({args}) r"
        else:
            select = f"select coalesce(to_jsonb(public.{name}({args})), 'null'::jsonb)::text"
        claims = json.dumps({"role": role})
        sql = (f"begin; set local role {role}; set local request.jwt.claims = {_quote(claims)}; "
               f"{select}; commit;")
        try:
            out = self.db.query(sql)
        except SqlError as error:
            status = 403 if error.code.startswith("28") or error.code == "42501" else (
                404 if error.code == "P0002" else 400)
            return httpx.Response(status, json={"code": error.code, "message": error.message, "details": None,
                                                "hint": None})
        result = json.loads(out)
        if function.composite and not function.returns_set and isinstance(result, list):
            result = result[0] if result else None
        return httpx.Response(200, json=result)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def rpc(self, name: str, **body: Any) -> Any:
        """Una llamada como ``service_role`` (lo que hace el AE con ``create_bulk_send_v1``)."""
        request = httpx.Request("POST", f"{REST_URL}/rest/v1/rpc/{name}", json=body,
                                headers={"Authorization": f"Bearer {SERVICE_KEY}"})
        response = self.handler(request)
        assert response.status_code == 200, response.text
        return response.json()


SERVICE_KEY = _jwt({"role": "service_role"})
APPROVER_KEY = _jwt({"role": "flow_change_approver"})


# ---------------------------------------------------------------------------
# Chatwoot.
# ---------------------------------------------------------------------------

DELIVERED = "delivered"
READ = "read"
SENT = "sent"
FAILED_131042 = "failed_131042"


@dataclass
class _Contact:
    id: int
    phone: str  # E.164
    name: str | None
    blocked: bool = False


@dataclass
class _Conversation:
    id: int
    contact_id: int
    status: str = "open"
    labels: list[str] = field(default_factory=list)
    assignee: bool = False
    last_activity_at: int = 0


class ChatwootE2E:
    """La API de Chatwoot v4.13 que usa el masivo, con estado. Nunca busca por ``/contacts/search``.

    ``outcome(phone)`` decide que le pasa a la plantilla de cada persona: ``delivered``, ``read``,
    ``sent`` (Meta no confirma: retenida) o ``failed_131042``. ``replies`` son los telefonos que
    contestan (un entrante despues de la plantilla) con su texto.
    """

    def __init__(self, clock: Clock, *, outcome: Callable[[str], str] | None = None) -> None:
        self.clock = clock
        self.outcome = outcome or (lambda phone: DELIVERED)
        self.replies: dict[str, str] = {}
        self.contacts: dict[int, _Contact] = {}
        self.conversations: dict[int, _Conversation] = {}
        self.messages: dict[int, list[dict[str, Any]]] = {}
        self.labels = [*LABELS_DOC["labels_index_account_2"]["body"]["payload"],
                       {"id": 20, "title": LABEL, "description": None, "color": "#1f93ff", "show_on_sidebar": True}]
        self.requests: list[tuple[str, str]] = []
        self.templates_sent: list[dict[str, Any]] = []
        self._next_contact = 900
        self._next_conversation = 4000
        self._next_message = 90000

    # -- siembra (lo que ya hay en la bandeja antes del masivo) ----------------------------------
    def seed_conversation(self, phone_digits: str, *, labels: tuple[str, ...] = (), assignee: bool = False,
                          blocked: bool = False, status: str = "resolved", hours_ago: float = 72) -> int:
        self._next_contact += 1
        contact = _Contact(self._next_contact, "+" + phone_digits, "Contacto de prueba", blocked=blocked)
        self.contacts[contact.id] = contact
        self._next_conversation += 1
        conversation = _Conversation(self._next_conversation, contact.id, status=status, labels=list(labels),
                                     assignee=assignee,
                                     last_activity_at=int(self.clock.epoch() - hours_ago * 3600))
        self.conversations[conversation.id] = conversation
        self.messages[conversation.id] = []
        return conversation.id

    # -- vistas, sobre lo capturado ------------------------------------------------------------
    def _contact_view(self, contact: _Contact) -> dict[str, Any]:
        view = copy.deepcopy(CONVERSATION_ITEM["meta"]["sender"])
        view.update(id=contact.id, phone_number=contact.phone, blocked=contact.blocked, name=contact.name)
        return view

    def _conversation_view(self, conversation: _Conversation) -> dict[str, Any]:
        item = copy.deepcopy(CONVERSATION_ITEM)
        item.update(id=conversation.id, account_id=ACCOUNT, inbox_id=INBOX, status=conversation.status,
                    labels=list(conversation.labels), last_activity_at=conversation.last_activity_at,
                    timestamp=conversation.last_activity_at)
        item["meta"]["sender"] = self._contact_view(self.contacts[conversation.contact_id])
        item["meta"].pop("assignee", None)
        if conversation.assignee:
            item["meta"]["assignee"] = {"id": 7, "type": "user"}
        item["messages"] = copy.deepcopy(self.messages[conversation.id][-1:])
        return item

    def _message(self, base: Mapping[str, Any], conversation_id: int, **changes: Any) -> dict[str, Any]:
        self._next_message += 1
        message = copy.deepcopy(dict(base))
        message.update(id=self._next_message, conversation_id=conversation_id, inbox_id=INBOX,
                       created_at=int(self.clock.epoch()), source_id=f"wamid.E2E.{self._next_message}")
        message.update(changes)
        return message

    # -- lo que mira la prueba -------------------------------------------------------------------
    def template_messages(self) -> list[dict[str, Any]]:
        return [m for ms in self.messages.values() for m in ms if m.get("_template")]

    def conversation_of_phone(self, digits: str) -> list[_Conversation]:
        ids = {c.id for c in self.contacts.values() if c.phone == "+" + digits}
        return [c for c in self.conversations.values() if c.contact_id in ids]

    def count(self, method: str, suffix_start: str) -> int:
        return sum(1 for m, s in self.requests if m == method and s.startswith(suffix_start))

    # -- el transporte ---------------------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        prefix = f"/api/v1/accounts/{ACCOUNT}"
        method, path = request.method, request.url.path
        assert path.startswith(prefix), path
        suffix = path[len(prefix):]
        self.requests.append((method, suffix))
        assert "/contacts/search" not in suffix, "el masivo nunca busca contactos por /contacts/search"
        query = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        body = json.loads(request.content) if request.content else None
        parts = suffix.strip("/").split("/")

        if method == "GET" and suffix == f"/inboxes/{INBOX}":
            return httpx.Response(200, json={"id": INBOX, "channel_type": CATALOG["channel_type"],
                                             "message_templates": copy.deepcopy(CATALOG["message_templates"])})
        if method == "GET" and suffix == "/labels":
            return httpx.Response(200, json={"payload": self.labels})
        if method == "GET" and suffix == "/conversations":
            assert query.get("inbox_id") == str(INBOX)
            rows = sorted((c for c in self.conversations.values() if c.status == query.get("status")),
                          key=lambda c: -c.last_activity_at)
            page = int(query.get("page", "1"))
            chunk = [self._conversation_view(c) for c in rows[(page - 1) * 25: page * 25]]
            return httpx.Response(200, json={"data": {"meta": {"all_count": len(rows)}, "payload": chunk}})
        if method == "POST" and suffix == "/contacts/filter":
            rule = body["payload"][0]
            assert rule["attribute_key"] == "phone_number" and rule["filter_operator"] == "equal_to"
            wanted = "+" + str(rule["values"][0]).lstrip("+")
            found = []
            for contact in self.contacts.values():
                if contact.phone == wanted:
                    view = copy.deepcopy(PREPARE_DOC["contact_filter_one"]["body"]["payload"][0])
                    view.update(id=contact.id, phone_number=contact.phone, blocked=contact.blocked)
                    inbox = view["contact_inboxes"][0]["inbox"]
                    view["contact_inboxes"] = [{"source_id": contact.phone.lstrip("+"), "inbox": {**inbox, "id": INBOX}}]
                    found.append(view)
            return httpx.Response(200, json={"meta": {"count": len(found), "current_page": 1}, "payload": found})
        if method == "POST" and suffix == "/contacts":
            if any(c.phone == body["phone_number"] for c in self.contacts.values()):
                return httpx.Response(422, json=PREPARE_DOC["contact_create_phone_taken"]["body"])
            self._next_contact += 1
            contact = _Contact(self._next_contact, body["phone_number"], body.get("name"))
            self.contacts[contact.id] = contact
            out = copy.deepcopy(PREPARE_DOC["contact_create"]["body"])
            view = copy.deepcopy(PREPARE_DOC["contact_filter_one"]["body"]["payload"][0])
            view.update(id=contact.id, phone_number=contact.phone, blocked=False, name=contact.name)
            inbox = view["contact_inboxes"][0]["inbox"]
            view["contact_inboxes"] = [{"source_id": contact.phone.lstrip("+"), "inbox": {**inbox, "id": INBOX}}]
            out["payload"]["contact"] = view
            out["payload"]["contact_inbox"]["source_id"] = contact.phone.lstrip("+")
            return httpx.Response(200, json=out)
        if method == "POST" and suffix == "/conversations":
            contact = self.contacts[body["contact_id"]]
            assert body["source_id"] == contact.phone.lstrip("+") and body["inbox_id"] == INBOX
            self._next_conversation += 1
            conversation = _Conversation(self._next_conversation, contact.id, status="pending",
                                         last_activity_at=int(self.clock.epoch()))
            self.conversations[conversation.id] = conversation
            self.messages[conversation.id] = []
            out = copy.deepcopy(PREPARE_DOC["conversation_create"]["body"])
            out.update(id=conversation.id, inbox_id=INBOX)
            out["meta"]["sender"]["id"] = contact.id
            return httpx.Response(200, json=out)
        if method == "GET" and len(parts) == 3 and parts[0] == "contacts" and parts[2] == "conversations":
            contact_id = int(parts[1])
            items = [self._conversation_view(c) for c in self.conversations.values() if c.contact_id == contact_id]
            return httpx.Response(200, json={"payload": items})
        if len(parts) >= 2 and parts[0] == "conversations":
            conversation = self.conversations.get(int(parts[1]))
            if conversation is None:
                return httpx.Response(404, json={"error": "Resource could not be found"})
            if method == "GET" and len(parts) == 2:
                return httpx.Response(200, json=self._conversation_view(conversation))
            if method == "GET" and parts[2:] == ["messages"]:
                before = query.get("before")
                rows = [m for m in self.messages[conversation.id] if before is None or m["id"] < int(before)]
                return httpx.Response(200, json={"meta": copy.deepcopy(MESSAGES_6["meta"]),
                                                 "payload": [self._public(m) for m in rows[-20:]]})
            if method == "POST" and parts[2:] == ["labels"]:
                conversation.labels = list(body["labels"])
                return httpx.Response(200, json={"payload": list(conversation.labels)})
            if method == "POST" and parts[2:] == ["messages"]:
                return self._post_message(request, conversation, body)
        raise AssertionError(f"pedido no esperado: {method} {suffix}")

    @staticmethod
    def _public(message: Mapping[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in message.items() if not k.startswith("_")}

    def _post_message(self, request: httpx.Request, conversation: _Conversation, body: Mapping[str, Any]
                      ) -> httpx.Response:
        assert request.headers.get("api_access_token") == "token-del-agentbot", "la plantilla sale con el AgentBot"
        assert body.get("template_params"), "un masivo manda siempre una plantilla"
        assert body["template_params"]["name"] == "att1_carrito_abandonado_01"
        contact = self.contacts[conversation.contact_id]
        phone = contact.phone.lstrip("+")
        fate = self.outcome(phone)
        status = {DELIVERED: "delivered", READ: "read", SENT: "sent", FAILED_131042: "failed"}[fate]
        attributes = dict(body.get("content_attributes") or {})
        if fate == FAILED_131042:
            attributes["external_error"] = REJECTED_131042["content_attributes"]["external_error"]
        message = self._message(OUTGOING_MESSAGE, conversation.id, content=body["content"], status="sent",
                                content_attributes=attributes, _template=True, _fate=status)
        message["sender"] = {**message["sender"], "id": AGENT_BOT_ID}
        self.messages[conversation.id].append(message)
        self.templates_sent.append({"conversation_id": conversation.id, "phone": phone,
                                    "params": copy.deepcopy(body["template_params"])})
        conversation.last_activity_at = message["created_at"]
        if conversation.status == "resolved":
            conversation.status = "open"
        created = self._public(message)
        # Meta confirma despues: lo que se lee en la API ya tiene el estado final de la persona.
        message["status"] = status
        if fate in (DELIVERED, READ) and phone in self.replies:
            reply = self._message(INCOMING_MESSAGE, conversation.id, content=self.replies[phone], status="sent",
                                  content_attributes={})
            reply["created_at"] = message["created_at"] + 60
            reply["sender"] = {**reply["sender"], "id": contact.id, "phone_number": contact.phone}
            self.messages[conversation.id].append(reply)
        return httpx.Response(200, json=created)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


# ---------------------------------------------------------------------------
# Meta.
# ---------------------------------------------------------------------------


class MetaE2E:
    """La salud de los numeros del portfolio y la lista de plantillas de la Graph API."""

    def __init__(self) -> None:
        self.raw = copy.deepcopy(META_HEALTH["crudas"])
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"].startswith("Bearer ") and "access_token" not in str(request.url)
        path = request.url.path
        self.requests.append(path)
        last = path.rsplit("/", 1)[1]
        if last in NUMBERS:
            return httpx.Response(200, json=copy.deepcopy(self.raw[NUMBERS[last]]))
        if last == "message_templates":
            rows = [{"id": str(9000 + i), **{k: t[k] for k in ("name", "language", "status", "category", "components")}}
                    for i, t in enumerate(CATALOG["message_templates"])]
            return httpx.Response(200, json={"data": rows, "paging": {"cursors": {"before": "a", "after": "b"}}})
        raise AssertionError(f"pedido no esperado a Meta: {path}")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def healthy_file(path: Path, clock: Clock, **overrides: str) -> None:
    """El ``estado.json`` que deja el timer de la salud (la forma de ``meta_salud.py`` del OS que lee la pagina)."""
    when = clock.dt().isoformat()
    readings = {key: {"calidad": "GREEN", "limite": "TIER_10K", "puede_enviar": "AVAILABLE", "leido_en": when}
                for key in ("att1", "maribel")}
    for key, quality in overrides.items():
        readings[key]["calidad"] = quality
    path.write_text(json.dumps(readings), encoding="utf-8")


__all__ = [
    "ACCOUNT", "AGENT_BOT_ID", "APPROVER_KEY", "CHATWOOT_URL", "Clock", "ChatwootE2E", "DATABASE_URL_VARIABLE",
    "DELIVERED", "Database", "FAILED_131042", "INBOX", "INSTANCE", "LABEL", "MetaE2E", "PostgrestOverPsql",
    "READ", "REST_URL", "SENT", "SERVICE_KEY", "SqlError", "healthy_file",
]
