"""Los vetos de la lista de un masivo manejado por el AE (encargo 4, pieza 5 del mapa).

Mapa: ``OS/productos/soporte-infoproductores/documentacion/2026-11-25-mapa-masivo-por-el-ae.md``;
la base: ``20261125000200_bulk_send_store_v1.sql`` (``apply_bulk_list_vetoes_v1``).
Porta ``vetos_bases`` y ``excluir`` de masivo.py (el kit del 22/11 y del 29/11),
que leia la base de Chatwoot por ``psql``: aca Chatwoot se lee por su API.

Saca de la lista, con un motivo cerrado por persona:

* ``baja``: la baja durable de la base del setter (lector de la base) o la
  etiqueta ``automation_opted_out`` en Chatwoot;
* ``baja_ghl``: las bajas de GHL que la persona sube con la lista (decision 4);
* ``bloqueado``: el contacto bloqueado en Chatwoot;
* ``compro``: quien compro el producto del envio, por telefono o por la huella
  del mail (lector de la base);
* ``clienta``: la etiqueta ``cliente`` en Chatwoot;
* ``con_equipo``: una conversacion abierta o pendiente asignada a una persona o
  con ``automation_paused``;
* ``escribio_hace_poco``: un entrante en las ultimas 6 h.

Si una persona tiene varios, gana el primero de ``REASON_PRECEDENCE``: primero
lo que no cambia (las bajas, el bloqueo, la compra), al final lo que pasa solo
(estar con el equipo, haber escrito hace poco). Solo cambia el conteo por motivo.

Chatwoot se lee con el listado de conversaciones de la bandeja
(``list_recent_conversations_with_messages`` sin mensajes, paginado con el
techo de ``MAX_CONVERSATION_SCAN_PAGES`` por estado) y, solo para las personas
de la lista con actividad en la ventana de 6 h, con los mensajes de esa
conversacion. Nunca con ``/contacts/search`` (100 por minuto por cuenta, lo usa
el bridge). Si el listado no se puede leer entero, o una conversacion con una
marca de veto no trae telefono, no se arma nada: ``BulkVetoesUnavailable``
(falla cerrado, la misma regla que la audiencia).

La parte de la base (``BaseVetoReader``) es ``read_bulk_list_base_vetoes_v1``
(20261125000300): las bajas y las compras del producto del envio, que
``service_role`` no lee de las tablas. El producto sale de la columna del envio.

Nada de esto imprime un telefono: los logs llevan ids de lista y conteos. Lo usan la
pagina y el motor del masivo, solo con ``BULK_SEND_ENABLED`` (``bulk_send_wiring``).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import httpx

from bridge.chatwoot import MAX_CONVERSATION_SCAN_PAGES
from bridge.phones import canonical_whatsapp_phone

logger = logging.getLogger(__name__)

# Los motivos cerrados de bulk_list_members.exclusion_reason, en el orden en que ganan.
REASON_PRECEDENCE: tuple[str, ...] = (
    "baja", "baja_ghl", "bloqueado", "compro", "clienta", "con_equipo", "escribio_hace_poco")
VETO_REASONS = frozenset(REASON_PRECEDENCE)
BASE_REASONS = frozenset({"baja", "compro"})
# Las etiquetas de Chatwoot que vetan (ETIQUETAS_VETO de masivo.py).
LABEL_REASONS: Mapping[str, str] = {"automation_opted_out": "baja", "cliente": "clienta"}
PAUSED_LABEL = "automation_paused"
TEAM_STATUSES = frozenset({"open", "pending"})
CHATWOOT_STATUSES: tuple[str, ...] = ("open", "pending", "snoozed", "resolved")

RECENT_HOURS = 6
# Cuanta historia de la bandeja se lee para etiquetas, equipo y bloqueados.
HISTORY_DAYS = 365
# Cuantas conversaciones de la lista con actividad en la ventana se leen mensaje por mensaje
# (una llamada cada una). Pasado el techo no se arma: ver el docstring de ChatwootVetoReader.
MAX_RECENT_CONVERSATIONS = 1000
# Los mensajes de cada una: una pagina de la API de Chatwoot.
RECENT_MESSAGES_LIMIT = 20

MAX_RESPONSE_BYTES = 8_000_000
_PHONE = re.compile(r"[1-9][0-9]{7,14}")
_NON_DIGIT = re.compile(r"[^0-9]")
_REASON = re.compile(r"[a-z][a-z0-9_.:-]{0,79}")
_INCOMING = 0


class BulkVetoesUnavailable(Exception):
    """Una fuente de vetos no se pudo leer entera: la lista no se revisa (falla cerrado)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BulkVetoesRejected(Exception):
    """La base rechazo la entrada (22023) o no encontro lo pedido (P0002), con su motivo."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class VetoMember:
    """Una persona de la lista: su posicion en ``bulk_list_members`` y su telefono en forma de envio.

    ``ghl_opted_out`` es la columna de baja de GHL de la misma lista (decision 4), si la trae.
    """

    position: int
    phone: str
    ghl_opted_out: bool = False


@dataclass(frozen=True)
class VetoResult:
    """Los vetos de una lista: las exclusiones para la base y los conteos para la pagina y el AE."""

    exclusions: tuple[Mapping[str, object], ...]
    excluded_by_reason: Mapping[str, int]
    eligible: int
    sources: Mapping[str, int]


class BaseVetoReader(Protocol):
    """Lo que esta en la base del setter: posicion -> ``baja`` o ``compro``."""

    async def base_vetoes(self, *, list_id: str, product_refs: Sequence[str]) -> Mapping[int, str]: ...


class ChatwootVetoSource(Protocol):
    async def vetoes_by_phone(self, phones: Iterable[str]) -> Mapping[str, str]: ...


def _canonical(phone: object) -> str | None:
    if not isinstance(phone, str):
        return None
    digits = _NON_DIGIT.sub("", phone)
    if not _PHONE.fullmatch(digits):
        return None
    return canonical_whatsapp_phone(digits)


def _stronger(current: str | None, candidate: str) -> str:
    if current is None:
        return candidate
    return min(current, candidate, key=REASON_PRECEDENCE.index)


# ---------------------------------------------------------------------------
# Chatwoot.
# ---------------------------------------------------------------------------

class ChatwootVetoReader:
    """Los vetos que viven en Chatwoot, por telefono canonico, solo para los telefonos pedidos.

    Una pasada por el listado de la bandeja (los cuatro estados, ``HISTORY_DAYS``
    hacia atras, sin show ni mensajes) da etiquetas, estado, asignacion y
    contacto bloqueado de cada conversacion. Las conversaciones de la lista
    con ``last_activity_at`` dentro de las ultimas ``RECENT_HOURS`` se leen con
    sus ultimos ``RECENT_MESSAGES_LIMIT`` mensajes para saber si hubo un
    entrante: el ultimo saliente propio tambien mueve ``last_activity_at`` y no
    es motivo de veto. Si esa pagina viene llena y entera dentro de la ventana,
    no se puede descartar un entrante anterior: se veta.

    Falla cerrado (``BulkVetoesUnavailable``) si el listado no cierra (mas de
    ``MAX_CONVERSATION_SCAN_PAGES`` paginas por estado, un orden o un conteo que
    no corresponde, la red), si un item no tiene la forma del listado, si una
    conversacion con una marca de veto no trae telefono, o si hay mas de
    ``max_recent_conversations`` conversaciones de la lista para leer mensaje
    por mensaje (un segundo masivo el mismo dia lo alcanza: ver el PR).
    """

    def __init__(self, *, chatwoot: Any, inbox_id: int, clock: Callable[[], datetime] | None = None,
                 recent_hours: int = RECENT_HOURS, history_days: int = HISTORY_DAYS,
                 max_pages: int = MAX_CONVERSATION_SCAN_PAGES,
                 max_recent_conversations: int = MAX_RECENT_CONVERSATIONS) -> None:
        if (not isinstance(inbox_id, int) or isinstance(inbox_id, bool) or inbox_id <= 0
                or not 1 <= recent_hours <= 72 or not 1 <= history_days <= 3650
                or not 1 <= max_pages <= MAX_CONVERSATION_SCAN_PAGES
                or not 1 <= max_recent_conversations <= 5000):
            raise ValueError("invalid chatwoot veto reader configuration")
        self._chatwoot = chatwoot
        self._inbox_id = inbox_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._recent_hours = recent_hours
        self._history_days = history_days
        self._max_pages = max_pages
        self._max_recent = max_recent_conversations
        self.last_counts: dict[str, int] = {}

    async def vetoes_by_phone(self, phones: Iterable[str]) -> Mapping[str, str]:
        wanted = {c for c in (_canonical(p) for p in phones) if c is not None}
        now = self._clock()
        history_since = int((now - timedelta(days=self._history_days)).timestamp())
        recent_since = int((now - timedelta(hours=self._recent_hours)).timestamp())

        def skip(item: Mapping[str, object]) -> bool:
            # Una conversacion de alguien que no esta en la lista no se guarda; una sin telefono
            # legible si, para decidir abajo si traia una marca de veto.
            phone = _sender(item).get("phone_number")
            canonical = _canonical(phone)
            return canonical is not None and canonical not in wanted

        try:
            items = await self._chatwoot.list_recent_conversations_with_messages(
                expected_inbox_id=self._inbox_id, statuses=CHATWOOT_STATUSES,
                active_since_epoch=max(1, history_since), max_pages=self._max_pages,
                include_messages=False, skip_listing_item=skip)
        except Exception as error:  # noqa: BLE001 - cualquier falla del listado frena la revision
            raise BulkVetoesUnavailable(f"chatwoot_listado:{type(error).__name__}") from error

        vetoes: dict[str, str] = {}
        recent: list[tuple[int, str]] = []
        counts = {"conversaciones": 0, "etiqueta_baja": 0, "etiqueta_cliente": 0, "con_equipo": 0,
                  "bloqueado": 0, "recientes_leidas": 0, "escribio_hace_poco": 0}
        for item in items:
            conversation = item.get("conversation") if isinstance(item, Mapping) else None
            if not isinstance(conversation, Mapping):
                raise BulkVetoesUnavailable("chatwoot_item")
            conversation_id = conversation.get("id")
            labels = conversation.get("labels")
            status = conversation.get("status")
            activity = conversation.get("last_activity_at")
            meta = conversation.get("meta")
            sender = meta.get("sender") if isinstance(meta, Mapping) else None
            if (not isinstance(conversation_id, int) or isinstance(conversation_id, bool)
                    or not isinstance(labels, list) or not all(isinstance(x, str) for x in labels)
                    or not isinstance(status, str) or not isinstance(activity, int)
                    or not isinstance(meta, Mapping) or not isinstance(sender, Mapping)):
                raise BulkVetoesUnavailable("chatwoot_item")
            blocked = sender.get("blocked")
            if not isinstance(blocked, bool):
                raise BulkVetoesUnavailable("chatwoot_bloqueado_ilegible")
            reasons: list[str] = [LABEL_REASONS[label] for label in labels if label in LABEL_REASONS]
            if status in TEAM_STATUSES and (meta.get("assignee") is not None or PAUSED_LABEL in labels):
                reasons.append("con_equipo")
            if blocked:
                reasons.append("bloqueado")
            phone = _canonical(sender.get("phone_number"))
            if phone is None:
                if reasons:
                    raise BulkVetoesUnavailable("chatwoot_sin_telefono")
                continue
            counts["conversaciones"] += 1
            counts["etiqueta_baja"] += "automation_opted_out" in labels
            counts["etiqueta_cliente"] += "cliente" in labels
            counts["con_equipo"] += "con_equipo" in reasons
            counts["bloqueado"] += blocked
            for reason in reasons:
                vetoes[phone] = _stronger(vetoes.get(phone), reason)
            if activity >= recent_since:
                recent.append((conversation_id, phone))

        # Solo se leen los mensajes de quien todavia no tiene un veto mas fuerte.
        to_read = [(cid, phone) for cid, phone in recent if phone not in vetoes]
        if len(to_read) > self._max_recent:
            raise BulkVetoesUnavailable("chatwoot_demasiadas_recientes")
        for conversation_id, phone in to_read:
            if phone in vetoes:
                continue
            try:
                messages = await self._chatwoot.get_conversation_messages(
                    conversation_id=conversation_id, limit=RECENT_MESSAGES_LIMIT)
            except Exception as error:  # noqa: BLE001
                raise BulkVetoesUnavailable(f"chatwoot_mensajes:{type(error).__name__}") from error
            counts["recientes_leidas"] += 1
            if _wrote_since(messages, recent_since, limit=RECENT_MESSAGES_LIMIT):
                vetoes[phone] = "escribio_hace_poco"
                counts["escribio_hace_poco"] += 1
        self.last_counts = counts
        return vetoes


def _sender(item: Mapping[str, object]) -> Mapping[str, object]:
    meta = item.get("meta") if isinstance(item, Mapping) else None
    sender = meta.get("sender") if isinstance(meta, Mapping) else None
    return sender if isinstance(sender, Mapping) else {}


def _wrote_since(messages: object, since: int, *, limit: int) -> bool:
    """Un entrante (``message_type`` 0, no privado) desde ``since``; o no se puede descartar."""
    if not isinstance(messages, list):
        raise BulkVetoesUnavailable("chatwoot_mensajes_forma")
    oldest: int | None = None
    for message in messages:
        created = message.get("created_at") if isinstance(message, Mapping) else None
        kind = message.get("message_type") if isinstance(message, Mapping) else None
        if (not isinstance(created, int) or isinstance(created, bool)
                or not isinstance(kind, int) or isinstance(kind, bool)):
            raise BulkVetoesUnavailable("chatwoot_mensajes_forma")
        oldest = created if oldest is None else min(oldest, created)
        if kind == _INCOMING and message.get("private") is not True and created >= since:
            return True
    # La pagina vino llena y entera dentro de la ventana: un entrante anterior no se puede descartar.
    return len(messages) >= limit and oldest is not None and oldest >= since


# ---------------------------------------------------------------------------
# La base.
# ---------------------------------------------------------------------------

class SupabaseBulkVetoStore:
    """Las funciones de la base que usan los vetos, con la clave de ``service_role``.

    * ``apply``: ``apply_bulk_list_vetoes_v1`` (20261125000200), que reemplaza las
      exclusiones y deja la lista revisada.
    * ``base_vetoes``: ``read_bulk_list_base_vetoes_v1`` (20261125000300). Sin
      ``product_refs`` la base usa los del envio; si la base no contesta, esto es
      ``BulkVetoesUnavailable``: la lista no se revisa.

    Un error de estado vuelve como ``{"ok": false, "reason_code"}``; 22023 y P0002
    son ``BulkVetoesRejected``; lo demas, ``BulkVetoesUnavailable``.
    """

    def __init__(self, *, base_url: str, service_role_key: str, instance: Mapping[str, str],
                 transport: httpx.AsyncBaseTransport | None = None, timeout_seconds: float = 20.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._key = service_role_key
        self._instance = dict(instance)
        self._transport = transport
        self._timeout = timeout_seconds

    async def _rpc(self, function: str, payload: Mapping[str, Any]) -> Any:
        headers = {"apikey": self._key, "Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(base_url=self._base_url, headers=headers, transport=self._transport,
                                         timeout=self._timeout) as client:
                response = await client.post(f"/rest/v1/rpc/{function}", content=json.dumps(payload))
        except httpx.HTTPError as error:
            raise BulkVetoesUnavailable(f"{function}_unavailable") from error
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise BulkVetoesUnavailable(f"{function}_too_large")
        try:
            body = response.json()
        except ValueError as error:
            raise BulkVetoesUnavailable(f"{function}_not_json") from error
        if response.status_code == 200:
            return body
        code = body.get("code") if isinstance(body, dict) else None
        message = body.get("message") if isinstance(body, dict) else None
        if code in ("22023", "P0002") and isinstance(message, str) and _REASON.fullmatch(message):
            raise BulkVetoesRejected(message)
        raise BulkVetoesUnavailable(f"{function}_http_{response.status_code}")

    async def apply(self, list_id: str, exclusions: Sequence[Mapping[str, object]]) -> Mapping[str, Any]:
        body = await self._rpc("apply_bulk_list_vetoes_v1", {
            "p_instance": self._instance, "p_list_id": list_id, "p_exclusions": list(exclusions)})
        if not isinstance(body, dict) or not isinstance(body.get("ok"), bool):
            raise BulkVetoesUnavailable("apply_bulk_list_vetoes_v1_invalid_shape")
        return body

    async def base_vetoes(self, *, list_id: str, product_refs: Sequence[str]) -> Mapping[int, str]:
        body = await self._rpc("read_bulk_list_base_vetoes_v1", {
            "p_instance": self._instance, "p_list_id": list_id, "p_product_refs": list(product_refs)})
        if not isinstance(body, list):
            raise BulkVetoesUnavailable("read_bulk_list_base_vetoes_v1_invalid_shape")
        out: dict[int, str] = {}
        for row in body:
            position = row.get("position") if isinstance(row, dict) else None
            reason = row.get("reason") if isinstance(row, dict) else None
            if (not isinstance(position, int) or isinstance(position, bool) or position < 0
                    or reason not in BASE_REASONS):
                raise BulkVetoesUnavailable("read_bulk_list_base_vetoes_v1_invalid_row")
            out[position] = _stronger(out.get(position), reason)
        return out


# ---------------------------------------------------------------------------
# Armar y aplicar.
# ---------------------------------------------------------------------------

def _ghl_set(phones: Iterable[str]) -> set[str]:
    out = set()
    for phone in phones:
        canonical = _canonical(phone)
        if canonical is None:
            # Una baja que no se puede leer no se saltea: podria ser alguien de la lista.
            raise BulkVetoesUnavailable("baja_ghl_ilegible")
        out.add(canonical)
    return out


async def build_list_vetoes(*, list_id: str, members: Sequence[VetoMember], product_refs: Sequence[str],
                            base: BaseVetoReader, chatwoot: ChatwootVetoSource,
                            ghl_opt_out_phones: Iterable[str] = ()) -> VetoResult:
    """Los vetos de ``members`` (la lista entera, o una tanda del envio), sin escribir nada.

    ``product_refs``: los productos del envio, para «compro». Vacio quiere decir «los del envio»: la
    base usa ``bulk_sends.product_refs`` (20261125000300, que el AE fija en el borrador), y sin ninguno
    falla con ``bulk_send_products_missing`` (un masivo sin producto no sabe a quien no mandarle). Si
    llegan, tienen que ser los mismos de la columna (``bulk_send_products_mismatch``).
    """
    if not all(isinstance(p, str) and p.strip() for p in product_refs):
        raise ValueError("invalid product_refs")
    by_phone: dict[str, list[VetoMember]] = {}
    positions: set[int] = set()
    for member in members:
        canonical = _canonical(member.phone)
        if (canonical is None or not isinstance(member.position, int) or isinstance(member.position, bool)
                or member.position < 0 or member.position in positions):
            raise ValueError("invalid veto member")
        positions.add(member.position)
        by_phone.setdefault(canonical, []).append(member)

    ghl = _ghl_set(ghl_opt_out_phones)
    base_rows = await base.base_vetoes(list_id=list_id, product_refs=list(product_refs))
    chatwoot_rows = await chatwoot.vetoes_by_phone(by_phone.keys())

    decided: dict[int, str] = {}
    for phone, group in by_phone.items():
        for member in group:
            reason: str | None = None
            if member.ghl_opted_out or phone in ghl:
                reason = _stronger(reason, "baja_ghl")
            if member.position in base_rows:
                reason = _stronger(reason, base_rows[member.position])
            if phone in chatwoot_rows:
                reason = _stronger(reason, chatwoot_rows[phone])
            if reason is not None:
                decided[member.position] = reason
    exclusions = tuple({"position": position, "reason": decided[position]} for position in sorted(decided))
    by_reason: dict[str, int] = {}
    for reason in decided.values():
        by_reason[reason] = by_reason.get(reason, 0) + 1
    sources = {"base": len([p for p in base_rows if p in positions]), "chatwoot": len(chatwoot_rows),
               "baja_ghl_subidas": len(ghl)}
    counts = getattr(chatwoot, "last_counts", None)
    if isinstance(counts, Mapping):
        sources.update({f"chatwoot_{k}": int(v) for k, v in counts.items()})
    return VetoResult(exclusions=exclusions, excluded_by_reason=by_reason,
                      eligible=len(positions) - len(decided), sources=sources)


async def apply_list_vetoes(*, store: SupabaseBulkVetoStore, list_id: str, members: Sequence[VetoMember],
                            product_refs: Sequence[str], chatwoot: ChatwootVetoSource,
                            ghl_opt_out_phones: Iterable[str] = (),
                            base: BaseVetoReader | None = None) -> Mapping[str, Any]:
    """Arma los vetos de la lista entera y los deja en la base (``apply_bulk_list_vetoes_v1``).

    ``members`` tiene que ser la lista entera: la base reemplaza todas las exclusiones.
    Si una fuente no se lee entera no se escribe nada (``BulkVetoesUnavailable``).
    """
    result = await build_list_vetoes(list_id=list_id, members=members, product_refs=product_refs,
                                     base=base or store, chatwoot=chatwoot, ghl_opt_out_phones=ghl_opt_out_phones)
    applied = await store.apply(list_id, result.exclusions)
    logger.info("bulk_send_vetoes list_id=%s ok=%s excluded=%s eligible=%s by_reason=%s",
                list_id, applied.get("ok"), len(result.exclusions), result.eligible,
                json.dumps(result.excluded_by_reason, sort_keys=True))
    if applied.get("ok") is True and (applied.get("excluded") != len(result.exclusions)
                                      or applied.get("eligible") != result.eligible):
        raise BulkVetoesUnavailable("apply_bulk_list_vetoes_v1_count_mismatch")
    return {**applied, "excluded_by_reason": dict(result.excluded_by_reason), "sources": dict(result.sources)}
