"""Preparar un masivo (encargo 7 del masivo manejado por el Automation Expert, pieza 8).

Mapa: ``OS/productos/soporte-infoproductores/documentacion/2026-11-25-mapa-masivo-por-el-ae.md``,
«8. Preparar» y «9. El motor de envio». Base: ``20261125000200_bulk_send_store_v1.sql``.
Prototipo: ``preparar_persona`` / ``entrar_persona`` / ``correr_preparar`` de masivo.py (el
preparar del 23/11 del masivo del Black de Maribel).

Para un envio confirmado, deja hecho en Chatwoot, por cada persona de la lista, el
contacto, la conversacion de la bandeja y la etiqueta de la campana, **sin mandar
nada**, y anota que quedo preparado. El dia del envio el motor (encargo 9) le manda
a cada preparada con una sola llamada (``send_first_touch_to_conversation``,
``messaging.py``) despues de revalidarla con ``revalidate_prepared``; si no sirve, va
por el camino de siempre (``send_first_touch``).

Es una funcion (``prepare_bulk_send``), no un trabajador: el bucle asincrono, el
candado contra un envio corriendo y la hora de corte los arma el encargo 9.

Reglas:

* **Nunca manda.** No hay ninguna llamada a ``/messages`` en este modulo.
* **Nunca ``/contacts/search``** (Rack::Attack de Chatwoot v4.13.0 le pone 100 por
  minuto por cuenta, y lo usa el bridge para cada primer toque). El contacto se
  busca con ``POST /contacts/filter`` (``phone_number`` ``equal_to``, exacto), que
  solo cuenta contra los 3.000 por minuto por IP, en cada forma de
  ``equivalent_whatsapp_phones``; se crea en la forma de ``whatsapp_delivery_phone``,
  como ``send_first_touch``. Si estan las dos formas, gana la de envio (D14 de
  ``phones.py``).
* **Idempotente.** Lo ya anotado como preparado no se toca. Lo que se corto a mitad
  de camino se repite sin duplicar: el contacto se encuentra por el filtro, la
  conversacion viva (abierta o pendiente) se reusa, y la etiqueta solo se suma si
  falta (el POST de Chatwoot reemplaza la lista: va la union).
* **Los vetos de ultimo momento, los mismos que el envio.** Por cada tanda corre
  ``vetoes`` (el de los vetos del encargo 4, inyectado); si no se pueden leer, frena
  (sin vetos no se toca a nadie). Ademas, de lo que Chatwoot devuelve: contacto
  bloqueado, etiqueta ``cliente`` o ``automation_opted_out`` en alguna conversacion de
  la bandeja, conversacion viva con el equipo (asignada, con equipo o
  ``automation_paused``) y un entrante de la persona en las ultimas 6 h (la misma
  lectura que la guarda G3 de ``send_first_touch``).
* **Fail-closed ante la API.** Cada pedido HTTP a Chatwoot cuenta (un transporte que
  los ve todos, tambien los del ``ChatwootClient`` que se reusa). Con 20 pedidos o
  mas entre los ultimos 100, si fallan mas del 20 % (error de red, 5xx, 429, 401 o
  403), frena.
* **1 persona por segundo** por defecto (``rate_per_second``). Lo ya preparado y lo
  vetado por la base no esperan: no tocan Chatwoot.

Lo que la migracion de hoy no tiene (se propone para un encargo aparte): donde
anotar la conversacion preparada por persona y leer la lista con cursor. Por eso la
base entra como ``BulkPrepareStore``, una interfaz; la implementacion contra
Supabase llega con esa migracion.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

import httpx

from bridge.chatwoot import ChatwootClient, ChatwootProtocolError, ContactConversation
from bridge.messaging import FirstTouchRecipient, first_touch_recipient_matches
from bridge.phones import (
    canonical_whatsapp_phone,
    equivalent_whatsapp_phones,
    whatsapp_delivery_phone,
)

logger = logging.getLogger(__name__)

DEFAULT_RATE_PER_SECOND = 1.0
MAX_RATE_PER_SECOND = 5.0
DEFAULT_BATCH_SIZE = 25
# Quien escribio hace poco queda afuera (decision 14 del mapa, masivo.py: 6 h).
DEFAULT_RECENT_INBOUND_SECONDS = 6 * 3600
# El freno por la API (pieza 10 del mapa, evaluar_freno de masivo.py).
API_WINDOW = 100
API_MIN_SAMPLE = 20
API_MAX_FAILURE_RATIO = 0.2

# Etiquetas que vetan, con el motivo de exclusion de bulk_list_members.
VETO_LABELS: Mapping[str, str] = {"cliente": "clienta", "automation_opted_out": "baja"}
_LIVE_STATUSES = frozenset({"open", "pending"})
# Estados del envio desde los que se prepara.
_PREPARABLE_STATES = frozenset({"confirmado", "preparando"})

OutcomeKind = Literal["preparado", "ya_preparado", "vetado", "omitido", "fallido"]


@dataclass(frozen=True)
class BulkMember:
    """Una persona de la lista: su posicion, el telefono en forma de envio y el primer nombre."""

    position: int
    phone: str
    first_name: str | None = None


@dataclass(frozen=True)
class SendSnapshot:
    """Lo que preparar necesita del envio (``bulk_sends``)."""

    state: str
    campaign_label: str | None


@dataclass(frozen=True)
class PreparedConversation:
    """Lo que quedo preparado para una persona: con esto el motor manda con una llamada.

    ``conversation_id`` es el numero de la conversacion en la cuenta (el ``id`` de la
    API, ``display_id`` en la base de Chatwoot). ``how``: ``nuevo`` (contacto y
    conversacion nuevos), ``conversacion_nueva`` (contacto que ya estaba) o ``reusa``
    (conversacion viva que ya estaba).
    """

    position: int
    contact_id: int
    conversation_id: int
    source_id: str
    label: str | None
    how: Literal["nuevo", "conversacion_nueva", "reusa"]


@dataclass(frozen=True)
class PersonOutcome:
    position: int
    kind: OutcomeKind
    reason: str
    conversation_id: int | None = None


@dataclass
class PrepareReport:
    """Como termino la corrida. ``stop_reason``: ``terminado`` o por que paro."""

    stop_reason: str
    counts: Counter[str] = field(default_factory=Counter)
    outcomes: list[PersonOutcome] = field(default_factory=list)
    api_requests: int = 0
    api_failures: int = 0

    @property
    def finished(self) -> bool:
        return self.stop_reason == "terminado"


class BulkPrepareStore(Protocol):
    """La base del masivo, vista desde preparar.

    ``read_send`` y ``start_preparing`` salen de lo que ya existe
    (``read_bulk_send_v1`` y ``transition_bulk_send_v1`` a ``preparando``). Los otros
    tres piden la migracion propuesta en el PR: ``read_members`` lee las personas
    sin exclusion en el orden de la lista a partir de una posicion
    (``read_bulk_send_batch_v1`` no tiene cursor y saltea solo a quien ya tiene fila
    en ``bulk_send_recipients``, que preparar no escribe), y ``read_prepared`` /
    ``record_prepared`` guardan la conversacion preparada por persona y envio.
    """

    async def read_send(self, send_id: str) -> SendSnapshot: ...

    async def start_preparing(self, send_id: str) -> None: ...

    async def read_members(
        self, send_id: str, *, after_position: int, limit: int
    ) -> Sequence[BulkMember]: ...

    async def read_prepared(
        self, send_id: str, positions: Sequence[int]
    ) -> Mapping[int, PreparedConversation]: ...

    async def record_prepared(self, send_id: str, prepared: PreparedConversation) -> None: ...


# Los vetos de ultimo momento del encargo 4: posicion -> motivo, para las personas dadas.
LastMinuteVetoes = Callable[[Sequence[BulkMember]], Awaitable[Mapping[int, str]]]


class _Skip(Exception):
    """La persona no se prepara, por un motivo que no es una falla de la API."""

    def __init__(self, kind: OutcomeKind, reason: str) -> None:
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


class _CountingTransport(httpx.AsyncBaseTransport):
    """Ve cada pedido a Chatwoot y anota si fallo, para el freno por la API."""

    def __init__(self, inner: httpx.AsyncBaseTransport, window: int = API_WINDOW) -> None:
        self._inner = inner
        self.window: deque[bool] = deque(maxlen=window)
        self.requests = 0
        self.failures = 0

    def _note(self, failed: bool) -> None:
        self.requests += 1
        self.failures += int(failed)
        self.window.append(failed)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            response = await self._inner.handle_async_request(request)
        except BaseException:
            self._note(True)
            raise
        status = response.status_code
        self._note(status >= 500 or status in (401, 403, 429))
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


class BulkPrepareChatwoot:
    """Chatwoot para preparar: contacto, conversacion y etiqueta, sin mandar ni buscar.

    Reusa ``ChatwootClient`` para crear la conversacion y leer las conversaciones del
    contacto (con la guarda de 20 y la lectura del ultimo entrante de G3). Lo que el
    cliente no tiene sin ``/contacts/search`` va aca: el filtro exacto por telefono,
    crear el contacto leyendo su ``source_id`` de la respuesta (``send_first_touch``
    lo vuelve a buscar) y sumar una etiqueta con el POST directo (``ensure_conversation_label``
    lo hace con el macro de pausa). Usa el token de control, como el bridge.
    """

    def __init__(
        self,
        *,
        base_url: str,
        account_id: int,
        access_token: str,
        inbox_id: int,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 15,
    ) -> None:
        if not isinstance(inbox_id, int) or isinstance(inbox_id, bool) or inbox_id <= 0:
            raise ValueError("invalid inbox id")
        self._base_url = base_url.rstrip("/")
        self._account_id = account_id
        self._token = access_token
        self.inbox_id = inbox_id
        self._timeout = timeout_seconds
        self.transport = _CountingTransport(transport or httpx.AsyncHTTPTransport())
        self.client = ChatwootClient(
            base_url=base_url,
            account_id=account_id,
            access_token=access_token,
            transport=self.transport,
        )

    def _path(self, suffix: str) -> str:
        return f"/api/v1/accounts/{self._account_id}{suffix}"

    async def _request(self, method: str, suffix: str, body: object | None = None) -> httpx.Response:
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers={"api_access_token": self._token},
            transport=self.transport,
            timeout=self._timeout,
            follow_redirects=False,
        ) as client:
            return await client.request(method, self._path(suffix), json=body)

    @staticmethod
    def _json(response: httpx.Response) -> object:
        try:
            return response.json()
        except ValueError as exc:
            raise ChatwootProtocolError("invalid_json") from exc

    async def label_exists(self, label: str) -> bool:
        response = await self._request("GET", "/labels")
        response.raise_for_status()
        body = self._json(response)
        labels = body.get("payload") if isinstance(body, dict) else None
        if not isinstance(labels, list) or not all(isinstance(item, dict) for item in labels):
            raise ChatwootProtocolError("invalid_labels_payload")
        return any(item.get("title") == label for item in labels)

    async def find_contacts(self, phone_digits: str) -> list[dict[str, object]]:
        """Los contactos con ese telefono exacto (``+`` + digitos). Solo lee."""
        e164 = "+" + phone_digits
        response = await self._request("POST", "/contacts/filter", {"payload": [{
            "attribute_key": "phone_number", "filter_operator": "equal_to",
            "values": [e164], "query_operator": None}]})
        response.raise_for_status()
        body = self._json(response)
        payload = body.get("payload") if isinstance(body, dict) else None
        meta = body.get("meta") if isinstance(body, dict) else None
        count = meta.get("count") if isinstance(meta, dict) else None
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise ChatwootProtocolError("invalid_contact_filter_payload")
        if not isinstance(count, int) or isinstance(count, bool) or count != len(payload):
            # Una pagina cortada (o un conteo que no cierra) no alcanza para decidir.
            raise ChatwootProtocolError("contact_filter_incomplete")
        return [item for item in payload if item.get("phone_number") == e164]

    async def create_contact(self, *, phone_digits: str, name: str | None) -> tuple[int, str | None]:
        """(contact_id, source_id de la bandeja). ``None`` si el telefono ya estaba (422)."""
        body: dict[str, object] = {"inbox_id": self.inbox_id, "phone_number": "+" + phone_digits,
                                   "blocked": False}
        if name:
            body["name"] = name
        response = await self._request("POST", "/contacts", body)
        if response.status_code == 422:
            detail = self._json(response)
            attributes = detail.get("attributes") if isinstance(detail, dict) else None
            if isinstance(attributes, list) and "phone_number" in attributes:
                raise _Skip("fallido", "contacto_aparecio")
            raise ChatwootProtocolError("contact_rejected")
        response.raise_for_status()
        parsed = self._json(response)
        payload = parsed.get("payload") if isinstance(parsed, dict) else None
        contact = payload.get("contact") if isinstance(payload, dict) else None
        contact_id = contact.get("id") if isinstance(contact, dict) else None
        if not isinstance(contact_id, int) or isinstance(contact_id, bool) or contact_id <= 0:
            raise ChatwootProtocolError("invalid_contact_id")
        binding = payload.get("contact_inbox") if isinstance(payload, dict) else None
        source_id = binding.get("source_id") if isinstance(binding, dict) else None
        if not isinstance(source_id, str) or not source_id:
            source_id = _inbox_source(contact, self.inbox_id)
        return contact_id, source_id

    async def add_label(self, *, conversation_id: int, label: str, current: Sequence[str]) -> bool:
        """Suma la etiqueta si falta; ``current`` son las que ya tiene. True si la puso."""
        if label in current:
            return False
        response = await self._request(
            "POST", f"/conversations/{conversation_id}/labels",
            {"labels": sorted({*current, label})})
        response.raise_for_status()
        body = self._json(response)
        after = body.get("payload") if isinstance(body, dict) else None
        if not isinstance(after, list) or label not in after:
            raise ChatwootProtocolError("label_not_confirmed")
        return True


def _inbox_source(contact: object, inbox_id: int) -> str | None:
    """El ``source_id`` del contacto en la bandeja, de ``contact_inboxes``; dos distintos fallan."""
    if not isinstance(contact, dict) or not isinstance(contact.get("contact_inboxes"), list):
        return None
    sources = set()
    for item in contact["contact_inboxes"]:
        inbox = item.get("inbox") if isinstance(item, dict) else None
        if isinstance(inbox, dict) and inbox.get("id") == inbox_id:
            source = item.get("source_id")
            if isinstance(source, str) and source:
                sources.add(source)
    if len(sources) > 1:
        raise ChatwootProtocolError("ambiguous_contact_inbox_source")
    return next(iter(sources)) if sources else None


async def _resolve_contact(
    chatwoot: BulkPrepareChatwoot, phone: str
) -> tuple[int, str] | None:
    """El contacto de la bandeja para ese telefono, en sus formas; ``None`` si no hay.

    Bloqueado, sin la bandeja o con un ``source_id`` que no es el telefono de la lista
    no se prepara (``_Skip``).
    """
    delivery = whatsapp_delivery_phone(phone)
    found: list[tuple[str, dict[str, object]]] = []
    for variant in equivalent_whatsapp_phones(phone):
        for contact in await chatwoot.find_contacts(variant):
            found.append((variant, contact))
    if not found:
        return None
    ids = {contact.get("id") for _, contact in found}
    in_delivery = [contact for variant, contact in found if variant == delivery]
    if len(in_delivery) > 1 or (len(ids) > 1 and not in_delivery):
        raise _Skip("omitido", "contacto_ambiguo")
    contact = in_delivery[0] if in_delivery else found[0][1]
    contact_id = contact.get("id")
    if not isinstance(contact_id, int) or isinstance(contact_id, bool) or contact_id <= 0:
        raise ChatwootProtocolError("invalid_contact_id")
    if contact.get("blocked") is True:
        raise _Skip("vetado", "bloqueado")
    source_id = _inbox_source(contact, chatwoot.inbox_id)
    if source_id is None:
        raise _Skip("omitido", "contacto_sin_bandeja")
    return contact_id, source_id


def _check_recipient(source_id: str | None, phone: str) -> str:
    if not source_id:
        raise _Skip("omitido", "contacto_sin_bandeja")
    try:
        recipient = FirstTouchRecipient(wa_id=source_id)
    except ValueError:
        raise _Skip("omitido", "contacto_otro_telefono") from None
    if not first_touch_recipient_matches(recipient, phone=phone):
        raise _Skip("omitido", "contacto_otro_telefono")
    return source_id


def _conversation_veto(conversations: Sequence[ContactConversation], since: int | None) -> str | None:
    for conversation in conversations:
        for label, reason in VETO_LABELS.items():
            if label in conversation.labels:
                return reason
    for conversation in conversations:
        if (since is not None and conversation.last_inbound_at is not None
                and conversation.last_inbound_at >= since):
            return "escribio_hace_poco"
    return None


def _live(conversations: Sequence[ContactConversation]) -> ContactConversation | None:
    # list_contact_conversations las devuelve de la mas activa a la menos.
    for conversation in conversations:
        if conversation.status in _LIVE_STATUSES:
            return conversation
    return None


async def _prepare_person(
    chatwoot: BulkPrepareChatwoot,
    member: BulkMember,
    *,
    label: str | None,
    now: int,
    recent_inbound_seconds: int,
) -> PreparedConversation:
    phone = whatsapp_delivery_phone(member.phone)
    if phone is None or canonical_whatsapp_phone(member.phone) is None:
        raise _Skip("omitido", "telefono_invalido")
    resolved = await _resolve_contact(chatwoot, phone)
    since = now - recent_inbound_seconds if recent_inbound_seconds > 0 else None
    if resolved is None:
        contact_id, source_id = await chatwoot.create_contact(phone_digits=phone, name=member.first_name)
        source_id = _check_recipient(source_id, phone)
        conversation_id = await chatwoot.client.create_conversation(
            inbox_id=chatwoot.inbox_id, contact_id=contact_id, source_id=source_id)
        current: Sequence[str] = ()
        how: Literal["nuevo", "conversacion_nueva", "reusa"] = "nuevo"
    else:
        contact_id, source_id = resolved
        source_id = _check_recipient(source_id, phone)
        conversations = await chatwoot.client.list_contact_conversations(
            contact_id=contact_id, inbox_id=chatwoot.inbox_id, messages_since_epoch=since)
        veto = _conversation_veto(conversations, since)
        if veto is not None:
            raise _Skip("vetado", veto)
        live = _live(conversations)
        if live is not None and live.with_team:
            raise _Skip("vetado", "con_equipo")
        if live is not None:
            conversation_id, current, how = live.conversation_id, live.labels, "reusa"
        else:
            conversation_id = await chatwoot.client.create_conversation(
                inbox_id=chatwoot.inbox_id, contact_id=contact_id, source_id=source_id)
            current, how = (), "conversacion_nueva"
    if label is not None:
        await chatwoot.add_label(conversation_id=conversation_id, label=label, current=current)
    return PreparedConversation(position=member.position, contact_id=contact_id,
                                conversation_id=conversation_id, source_id=source_id,
                                label=label, how=how)


def _api_brake(transport: _CountingTransport) -> str | None:
    total = len(transport.window)
    failed = sum(transport.window)
    if total >= API_MIN_SAMPLE and failed / total > API_MAX_FAILURE_RATIO:
        return f"chatwoot_api_{failed}_de_{total}"
    return None


async def prepare_bulk_send(
    *,
    send_id: str,
    store: BulkPrepareStore,
    chatwoot: BulkPrepareChatwoot,
    vetoes: LastMinuteVetoes,
    rate_per_second: float = DEFAULT_RATE_PER_SECOND,
    batch_size: int = DEFAULT_BATCH_SIZE,
    recent_inbound_seconds: int = DEFAULT_RECENT_INBOUND_SECONDS,
    should_stop: Callable[[], bool] = lambda: False,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> PrepareReport:
    """Prepara a toda la lista de un envio confirmado, sin mandar. Se puede repetir.

    Para (``stop_reason``) sin tocar a nadie mas cuando: el envio no esta confirmado
    ni preparando (``estado_<x>``, tambien si lo frenaron en el medio), la etiqueta de
    la campana no existe en Chatwoot (``etiqueta_no_existe``), los vetos no se pueden
    leer (``sin_vetos``), la API falla de mas (``chatwoot_api_<f>_de_<n>``) o
    ``should_stop`` dice que pare (``parar``). Una persona que falla no frena sola: queda
    sin anotar y la proxima corrida la vuelve a intentar.
    """
    if not 0 < rate_per_second <= MAX_RATE_PER_SECOND:
        raise ValueError("invalid bulk prepare rate")
    if not 1 <= batch_size <= 500:
        raise ValueError("invalid bulk prepare batch size")
    report = PrepareReport(stop_reason="terminado")

    def finish(reason: str) -> PrepareReport:
        report.stop_reason = reason
        report.api_requests = chatwoot.transport.requests
        report.api_failures = chatwoot.transport.failures
        logger.info("bulk_prepare_stopped send=%s reason=%s counts=%s", send_id, reason,
                    dict(report.counts))
        return report

    send = await store.read_send(send_id)
    if send.state not in _PREPARABLE_STATES:
        return finish(f"estado_{send.state}")
    label = send.campaign_label
    try:
        if label is not None and not await chatwoot.label_exists(label):
            return finish("etiqueta_no_existe")
    except (httpx.HTTPError, ChatwootProtocolError):
        return finish("etiqueta_no_se_pudo_leer")
    if send.state == "confirmado":
        await store.start_preparing(send_id)

    interval = 1.0 / rate_per_second
    after = -1
    while True:
        if should_stop():
            return finish("parar")
        send = await store.read_send(send_id)
        if send.state != "preparando":
            return finish(f"estado_{send.state}")
        members = list(await store.read_members(send_id, after_position=after, limit=batch_size))
        if not members:
            return finish("terminado")
        after = members[-1].position
        already = await store.read_prepared(send_id, [member.position for member in members])
        pending = [member for member in members if member.position not in already]
        for position in sorted(already):
            report.counts["ya_preparado"] += 1
            report.outcomes.append(PersonOutcome(position, "ya_preparado", "ya_preparado",
                                                 already[position].conversation_id))
        if not pending:
            continue
        try:
            vetoed = dict(await vetoes(pending))
        except Exception:
            logger.warning("bulk_prepare_vetoes_failed send=%s", send_id)
            return finish("sin_vetos")
        for member in pending:
            if should_stop():
                return finish("parar")
            reason = vetoed.get(member.position)
            if reason is not None:
                report.counts[f"vetado_{reason}"] += 1
                report.outcomes.append(PersonOutcome(member.position, "vetado", reason))
                continue
            started = monotonic()
            outcome = await _one(chatwoot, member, label=label, now=int(clock()),
                                 recent_inbound_seconds=recent_inbound_seconds)
            if outcome.kind == "preparado":
                assert isinstance(outcome, _Prepared)
                await store.record_prepared(send_id, outcome.prepared)
            report.counts[outcome.kind if outcome.kind == "preparado" else
                          f"{outcome.kind}_{outcome.reason}"] += 1
            report.outcomes.append(PersonOutcome(member.position, outcome.kind, outcome.reason,
                                                 outcome.conversation_id))
            brake = _api_brake(chatwoot.transport)
            if brake is not None:
                return finish(brake)
            wait = interval - (monotonic() - started)
            if wait > 0:
                await sleep(wait)


@dataclass(frozen=True)
class _Outcome:
    kind: OutcomeKind
    reason: str
    conversation_id: int | None = None


@dataclass(frozen=True)
class _Prepared(_Outcome):
    prepared: PreparedConversation | None = None


async def _one(
    chatwoot: BulkPrepareChatwoot,
    member: BulkMember,
    *,
    label: str | None,
    now: int,
    recent_inbound_seconds: int,
) -> _Outcome:
    try:
        prepared = await _prepare_person(chatwoot, member, label=label, now=now,
                                         recent_inbound_seconds=recent_inbound_seconds)
    except _Skip as skip:
        return _Outcome(skip.kind, skip.reason)
    except ChatwootProtocolError as exc:
        return _Outcome("fallido", str(exc)[:60] or "chatwoot_protocol_error")
    except httpx.HTTPStatusError as exc:
        return _Outcome("fallido", f"chatwoot_http_{exc.response.status_code}")
    except httpx.HTTPError:
        return _Outcome("fallido", "chatwoot_http_error")
    return _Prepared("preparado", prepared.how, prepared.conversation_id, prepared)


async def revalidate_prepared(
    chatwoot: BulkPrepareChatwoot,
    prepared: PreparedConversation,
    *,
    phone: str,
    now: int,
    recent_inbound_seconds: int = DEFAULT_RECENT_INBOUND_SECONDS,
) -> tuple[bool, str]:
    """Antes de mandarle a una preparada (encargo 9): ``(sirve, motivo)``.

    Sirve si la conversacion sigue en la bandeja, del mismo contacto y telefono, viva
    (abierta o pendiente), sin el equipo, sin etiquetas de veto, sin un entrante en
    las ultimas 6 h y con la etiqueta de la campana. Un motivo ``veto_<x>`` no manda;
    cualquier otro (``prep_<x>``) vuelve al camino de siempre (``send_first_touch``).
    Es ``validar_preparada`` de masivo.py por la API (masivo.py leia la base de
    Chatwoot): una lectura, con los mensajes de las conversaciones con actividad
    reciente. Un error de Chatwoot sube: el motor lo cuenta para su freno.
    """
    if not same_phone(prepared.source_id, phone):
        return False, "prep_otro_telefono"
    since = now - recent_inbound_seconds if recent_inbound_seconds > 0 else None
    conversations = await chatwoot.client.list_contact_conversations(
        contact_id=prepared.contact_id, inbox_id=chatwoot.inbox_id, messages_since_epoch=since)
    veto = _conversation_veto(conversations, since)
    if veto is not None:
        return False, f"veto_{veto}"
    match = [c for c in conversations if c.conversation_id == prepared.conversation_id]
    if not match:
        return False, "prep_no_existe"
    conversation = match[0]
    if conversation.status not in _LIVE_STATUSES:
        return False, "prep_no_abierta"
    if conversation.with_team:
        return False, "veto_con_equipo"
    if prepared.label is not None and prepared.label not in conversation.labels:
        return False, "prep_sin_etiqueta"
    return True, "preparada"


def same_phone(source_id: str, phone: str) -> bool:
    try:
        return first_touch_recipient_matches(FirstTouchRecipient(wa_id=source_id), phone=phone)
    except ValueError:
        return False
