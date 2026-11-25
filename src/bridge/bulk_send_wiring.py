"""El armado del masivo manejado por el AE en el bridge: la base, la pagina y el motor.

Mapa: ``OS/productos/soporte-infoproductores/documentacion/2026-11-25-mapa-masivo-por-el-ae.md``. Las
piezas ya estan en sus modulos (``bulk_import``, ``bulk_send_vetoes``, ``bulk_send_page``,
``bulk_send_prepare``, ``bulk_send_brakes``, ``bulk_send_worker``) y las dos migraciones
(``20261125000200`` y ``20261125000300``). Aca vive lo que las conecta, para que ``app.py`` solo lo
llame (crear, arrancar, parar, ``/ready`` y la seccion de la pagina), como ``flow_audience_wiring``:

* ``SupabaseBulkSendStore``: las funciones de ``service_role`` del motor (``BulkSendStore`` de
  ``bulk_send_worker``) y la purga; ``SupabasePrepareStore`` es la misma base vista desde preparar
  (``BulkPrepareStore`` de ``bulk_send_prepare``).
* ``BulkListVetoReviewer``: el ``BulkListReviewer`` de la pagina sobre ``apply_list_vetoes``. El producto
  del envio sale de la columna ``bulk_sends.product_refs`` (lo fija el AE en el borrador): el bridge no
  lo repite.
* ``CampaignLabelSender``: el ``BulkSender`` del motor sobre ``ChatwootMessageSender`` (el token del
  AgentBot: el webhook lo clasifica ``automation_outgoing`` y no pausa al agente), que a una persona sin
  preparar le pone la etiqueta de la campana justo antes de la plantilla (``before_send``, como el
  segmento del motor nativo, E-316).
* ``build_bulk_send``: con ``BULK_SEND_ENABLED`` arma todo, y si falta algo que lo hace seguro no
  arranca el bridge (``BulkSendConfigError``). Apagado devuelve None y nada de esto existe.

Lo que pide prendido, ademas de lo que ya exige ``check_bulk_send_settings``:

* ``BULK_SEND_INSTANCE_KEY``: la clave de la instancia (``maribel``, ``att1``), la misma del
  ``estado.json`` de la salud;
* ``BULK_SEND_PORTFOLIO_LOCK_DIR``: la carpeta del host montada en los tres bridges del portfolio
  (decision 6). Tiene que existir: el bridge no la crea, porque una carpeta creada adentro del
  contenedor seria un candado de un solo bridge y nadie se enteraria;
* ``BULK_SEND_PORTFOLIO_NUMBERS``: ``clave=phone_number_id`` de cada numero del portfolio, con el de la
  instancia adentro, para la salud en vivo (decision 6);
* ``BULK_SEND_META_HEALTH_PATH`` (o, si no esta, ``FLOW_AUDIENCE_META_HEALTH_PATH``): el ``estado.json``
  que deja el timer de la salud, para que la pagina muestre la salud y decida si se puede confirmar;
* ``META_TEMPLATES_ACCESS_TOKEN`` y ``META_WABA_ID``: la plantilla aprobada y la salud por la Graph API;
* el AgentBot de Chatwoot (``CHATWOOT_AGENT_BOT_ACCESS_TOKEN`` y ``CHATWOOT_AGENT_BOT_ID``) y el token de
  control con la cuenta y la bandeja.

Opcionales (con el default del mapa): ``BULK_SEND_SEND_HOURS`` (``9-21``), ``BULK_SEND_TIMEZONE``
(CDMX) y ``BULK_SEND_TEMPLATES``. El ritmo lo trae cada envio (``rate_millis``, 2500 por defecto) y lo
acota ``bulk_send_brakes.requested_rate`` a 5 por segundo.

Nada de esto escribe un telefono en un log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from bridge.bulk_send_page import ListMember, country_code_for_time_zone, routes as page_routes
from bridge.bulk_send_prepare import (
    BulkMember,
    BulkPrepareChatwoot,
    PreparedConversation,
    PrepareReport,
    SendSnapshot,
    prepare_bulk_send,
    revalidate_prepared,
)
from bridge.bulk_send_vetoes import (
    ChatwootVetoReader,
    SupabaseBulkVetoStore,
    VetoMember,
    apply_list_vetoes,
)
from bridge.bulk_send_worker import (
    FLAG,
    INSTANCE_KEY_VARIABLE,
    LOCK_DIR_VARIABLE,
    NUMBERS_VARIABLE,
    RETRY_TEMPLATES_VARIABLE,
    SEND_HOURS_VARIABLE,
    TEMPLATES_VARIABLE,
    TIMEZONE_VARIABLE,
    BulkSendConfigError,
    BulkSendWorker,
    BulkSendWorkerConfig,
    ChatwootBatchVetoSource,
    ChatwootDeliveryStatusReader,
    MetaPortfolioHealthReader,
    MetaTemplateResolver,
    PortfolioLock,
    SlackBulkBrakeNotifier,
    batch_vetoes,
    parse_portfolio_numbers,
)
from bridge.flow_audience_sweeper import HEALTH_PATH_VARIABLE as AUDIENCE_HEALTH_PATH_VARIABLE
from bridge.flow_audience_sweeper import file_health_reader
from bridge.messaging import ChatwootMessageSender, FirstTouchResult
from bridge.meta_templates import MetaTemplatesClient

logger = logging.getLogger(__name__)

HEALTH_PATH_VARIABLE = "BULK_SEND_META_HEALTH_PATH"
ENV_VARIABLES = (FLAG, INSTANCE_KEY_VARIABLE, LOCK_DIR_VARIABLE, NUMBERS_VARIABLE, SEND_HOURS_VARIABLE,
                 TIMEZONE_VARIABLE, TEMPLATES_VARIABLE, RETRY_TEMPLATES_VARIABLE, HEALTH_PATH_VARIABLE,
                 AUDIENCE_HEALTH_PATH_VARIABLE)

# Quien escribio hace poco no recibe (decision 14, masivo.py): la guarda de G3 del sender, en segundos.
RECENT_INBOUND_SECONDS = 6 * 3600
# Los envios que lee el motor en cada pasada (los mas nuevos primero): uno vivo nunca queda tan atras.
SENDS_PER_TICK = 50
# La purga de los telefonos (decision 9): la base decide que lista vencio; el bridge solo la llama.
PURGE_EVERY = timedelta(hours=6)
MAX_RESPONSE_BYTES = 2_000_000
_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}")
_DELIVERY = re.compile(r"bulk:([0-9a-f-]{36}):[0-9]+")


class BulkSendStoreUnavailable(Exception):
    """La base no contesto, o contesto algo que no tiene la forma de la funcion."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BulkSendStoreRejected(Exception):
    """La base rechazo el pedido con un codigo (22023 o P0002)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# La base.
# ---------------------------------------------------------------------------

class SupabaseBulkSendStore:
    """Las funciones de ``service_role`` del motor y la purga, sobre PostgREST.

    Las que llevan instancia (leer, listar, transicionar) la mandan siempre: el motor de una instancia no ve
    ni frena los envios de otra. Las que van por envio (reservar, cerrar, vencer, la tanda y lo preparado)
    no la llevan en la base; el motor solo las llama con un envio que leyo de su instancia.
    """

    def __init__(self, *, base_url: str, service_role_key: str, instance: Mapping[str, str],
                 transport: httpx.AsyncBaseTransport | None = None, timeout_seconds: float = 20.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._key = service_role_key
        self._instance = dict(instance)
        self._transport = transport
        self._timeout = timeout_seconds

    @property
    def instance(self) -> dict[str, str]:
        return dict(self._instance)

    async def _rpc(self, function: str, payload: Mapping[str, Any], *, shape: type) -> Any:
        headers = {"apikey": self._key, "Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(base_url=self._base_url, headers=headers, transport=self._transport,
                                         timeout=self._timeout) as client:
                response = await client.post(f"/rest/v1/rpc/{function}", content=json.dumps(payload))
        except httpx.HTTPError as error:
            raise BulkSendStoreUnavailable(f"{function}_unavailable") from error
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise BulkSendStoreUnavailable(f"{function}_too_large")
        try:
            body = response.json()
        except ValueError as error:
            raise BulkSendStoreUnavailable(f"{function}_not_json") from error
        if response.status_code == 200:
            if not isinstance(body, shape):
                raise BulkSendStoreUnavailable(f"{function}_invalid_shape")
            return body
        code = body.get("code") if isinstance(body, dict) else None
        message = body.get("message") if isinstance(body, dict) else None
        if code in ("22023", "P0002") and isinstance(message, str) and _REASON.fullmatch(message):
            raise BulkSendStoreRejected(message)
        raise BulkSendStoreUnavailable(f"{function}_http_{response.status_code}")

    # -- el motor (BulkSendStore de bulk_send_worker) -----------------------------

    async def read_sends(self) -> Sequence[Mapping[str, Any]]:
        return await self._rpc("read_bulk_sends_v1", {"p_instance": self._instance, "p_limit": SENDS_PER_TICK},
                               shape=list)

    async def read_send(self, send_id: str) -> Mapping[str, Any]:
        return await self._rpc("read_bulk_send_v1", {"p_instance": self._instance, "p_send_id": send_id}, shape=dict)

    async def transition(self, send_id: str, to: str, reason: str | None) -> Mapping[str, Any]:
        return await self._rpc("transition_bulk_send_v1", {
            "p_instance": self._instance, "p_send_id": send_id, "p_to": to, "p_reason": reason}, shape=dict)

    async def reserve(self, send_id: str, position: int) -> Mapping[str, Any]:
        # Un fallido no se reintenta desde el motor: es un G3 que lo freno antes de tocar Chatwoot.
        return await self._rpc("reserve_bulk_send_recipient_v1", {
            "p_send_id": send_id, "p_position": position, "p_retry_failed": False}, shape=dict)

    async def close(self, send_id: str, position: int, outcome: Mapping[str, Any]) -> Mapping[str, Any]:
        return await self._rpc("close_bulk_send_recipient_v1", {
            "p_send_id": send_id, "p_position": position, "p_outcome": dict(outcome)}, shape=dict)

    async def expire(self, send_id: str, minutes: int) -> Mapping[str, Any]:
        return await self._rpc("expire_bulk_send_reservations_v1", {"p_send_id": send_id, "p_minutes": minutes},
                               shape=dict)

    async def read_batch(self, send_id: str, limit: int) -> Sequence[Mapping[str, Any]]:
        rows = await self._rpc("read_bulk_send_batch_v1", {"p_send_id": send_id, "p_limit": limit}, shape=list)
        return [_member_row(row, "read_bulk_send_batch_v1") for row in rows]

    async def read_prepared(self, send_id: str, positions: Sequence[int]) -> Mapping[int, PreparedConversation]:
        if not positions:
            return {}
        rows = await self._rpc("read_bulk_send_prepared_v1", {
            "p_send_id": send_id, "p_positions": [int(p) for p in positions]}, shape=list)
        out: dict[int, PreparedConversation] = {}
        for row in rows:
            prepared = _prepared_row(row)
            out[prepared.position] = prepared
        return out

    # -- preparar ----------------------------------------------------------------

    async def read_members(self, send_id: str, *, after_position: int, limit: int) -> Sequence[Mapping[str, Any]]:
        rows = await self._rpc("read_bulk_send_members_v1", {
            "p_send_id": send_id, "p_after_position": after_position, "p_limit": limit}, shape=list)
        return [_member_row(row, "read_bulk_send_members_v1") for row in rows]

    async def record_prepared(self, send_id: str, prepared: PreparedConversation) -> Mapping[str, Any]:
        return await self._rpc("record_bulk_send_prepared_v1", {
            "p_send_id": send_id, "p_position": prepared.position,
            "p_prepared": {"contact_id": prepared.contact_id, "conversation_id": prepared.conversation_id,
                           "source_id": prepared.source_id, "label": prepared.label, "how": prepared.how}},
            shape=dict)

    # -- la purga (decision 9) ---------------------------------------------------

    async def purge(self, now: datetime) -> Mapping[str, Any]:
        return await self._rpc("purge_bulk_list_phones_v1", {"p_now": now.isoformat()}, shape=dict)


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _member_row(row: object, function: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise BulkSendStoreUnavailable(f"{function}_invalid_row")
    position, phone, first_name = row.get("position"), row.get("phone"), row.get("first_name")
    if (not isinstance(position, int) or isinstance(position, bool) or position < 0
            or not isinstance(phone, str) or not phone
            or (first_name is not None and not isinstance(first_name, str))):
        raise BulkSendStoreUnavailable(f"{function}_invalid_row")
    return {"position": position, "phone": phone, "first_name": first_name}


def _prepared_row(row: object) -> PreparedConversation:
    if not isinstance(row, dict):
        raise BulkSendStoreUnavailable("read_bulk_send_prepared_v1_invalid_row")
    position, label, how = row.get("position"), row.get("label"), row.get("how")
    if (not isinstance(position, int) or isinstance(position, bool) or position < 0
            or not _positive_int(row.get("contact_id")) or not _positive_int(row.get("conversation_id"))
            or not isinstance(row.get("source_id"), str) or not row.get("source_id")
            or (label is not None and not isinstance(label, str))
            or how not in ("nuevo", "conversacion_nueva", "reusa")):
        raise BulkSendStoreUnavailable("read_bulk_send_prepared_v1_invalid_row")
    return PreparedConversation(position=position, contact_id=int(row["contact_id"]),
                                conversation_id=int(row["conversation_id"]), source_id=str(row["source_id"]),
                                label=label, how=how)


class SupabasePrepareStore:
    """La base vista desde preparar (``BulkPrepareStore`` de ``bulk_send_prepare``)."""

    def __init__(self, store: SupabaseBulkSendStore) -> None:
        self._store = store

    async def read_send(self, send_id: str) -> SendSnapshot:
        view = await self._store.read_send(send_id)
        state, label = view.get("state"), view.get("campaign_label")
        if not isinstance(state, str) or (label is not None and not isinstance(label, str)):
            raise BulkSendStoreUnavailable("read_bulk_send_v1_invalid_shape")
        return SendSnapshot(state=state, campaign_label=label)

    async def start_preparing(self, send_id: str) -> None:
        moved = await self._store.transition(send_id, "preparando", None)
        if not moved.get("ok"):
            # Lo frenaron (o ya arranco) entre la lectura y la transicion: la vuelta siguiente lee el estado y
            # para sin tocar a nadie.
            logger.info("bulk_prepare_not_started send=%s state=%s", send_id, moved.get("state"))

    async def read_members(self, send_id: str, *, after_position: int, limit: int) -> Sequence[BulkMember]:
        rows = await self._store.read_members(send_id, after_position=after_position, limit=limit)
        return [BulkMember(position=r["position"], phone=r["phone"], first_name=r["first_name"]) for r in rows]

    async def read_prepared(self, send_id: str, positions: Sequence[int]) -> Mapping[int, PreparedConversation]:
        return await self._store.read_prepared(send_id, positions)

    async def record_prepared(self, send_id: str, prepared: PreparedConversation) -> None:
        result = await self._store.record_prepared(send_id, prepared)
        if result.get("ok") is True or result.get("reason_code") == "already_prepared":
            return
        # not_preparing (lo frenaron o ya arranco) o member_excluded: la persona no queda anotada y el motor
        # la manda por el camino de siempre; preparar lo ve en la vuelta siguiente.
        logger.info("bulk_prepare_not_recorded send=%s position=%s reason=%s", send_id, prepared.position,
                    result.get("reason_code"))


# ---------------------------------------------------------------------------
# Los vetos de la lista (la pagina) y de ultimo momento (preparar).
# ---------------------------------------------------------------------------

class BulkListVetoReviewer:
    """El ``BulkListReviewer`` de la pagina: ``apply_list_vetoes`` con el lector de Chatwoot de la bandeja.

    Sin ``product_refs``: la base usa los del envio (``bulk_sends.product_refs``, que el AE fija en el
    borrador); sin ninguno, la base falla con ``bulk_send_products_missing`` y la lista no se revisa.
    """

    def __init__(self, *, store: SupabaseBulkVetoStore, chatwoot: Any, inbox_id: int,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._store = store
        self._chatwoot = chatwoot
        self._inbox_id = inbox_id
        self._clock = clock

    async def review(self, *, send_id: str, list_id: str, members: Sequence[ListMember],
                     ghl_opt_out_phones: Sequence[str]) -> Mapping[str, Any]:
        # Un lector por revision: guarda los conteos de la ultima pasada.
        reader = ChatwootVetoReader(chatwoot=self._chatwoot, inbox_id=self._inbox_id, clock=self._clock)
        return await apply_list_vetoes(
            store=self._store, list_id=list_id,
            members=[VetoMember(position=m.position, phone=m.phone) for m in members],
            product_refs=(), chatwoot=reader, ghl_opt_out_phones=ghl_opt_out_phones)


def base_vetoes_for_prepare(*, store: SupabaseBulkSendStore, vetoes: SupabaseBulkVetoStore,
                            send_id: str) -> Callable[[Sequence[BulkMember]], Awaitable[Mapping[int, str]]]:
    """Los vetos de ultimo momento de preparar: los de la base (baja y compra) para esas personas. Los de
    Chatwoot los mira preparar al tocar cada contacto."""

    async def run(members: Sequence[BulkMember]) -> Mapping[int, str]:
        view = await store.read_send(send_id)
        listing = view.get("list")
        list_id = listing.get("list_id") if isinstance(listing, Mapping) else None
        if not isinstance(list_id, str):
            raise BulkSendStoreUnavailable("read_bulk_send_v1_without_list")
        rows = await vetoes.base_vetoes(list_id=list_id, product_refs=())
        wanted = {m.position for m in members}
        return {position: reason for position, reason in rows.items() if position in wanted}

    return run


# ---------------------------------------------------------------------------
# El sender del motor.
# ---------------------------------------------------------------------------

class CampaignLabelSender:
    """El ``BulkSender`` del motor: ``ChatwootMessageSender`` con la etiqueta de la campana.

    Una persona preparada ya tiene la etiqueta (``revalidate_prepared`` lo exige). A una que va por el
    camino de siempre se la pone ``before_send``, con la conversacion ya elegida o creada y antes de la
    plantilla, para que el agente vea la campana cuando la persona conteste. Es de falla blanda, como en
    ``send_first_touch``: si la etiqueta no se pudo poner, la plantilla sale igual y queda en el log.
    """

    def __init__(self, *, sender: ChatwootMessageSender, labels: Callable[[str], Awaitable[str | None]],
                 label_conversation: Callable[[int, str], Awaitable[object]]) -> None:
        self._sender = sender
        self._labels = labels
        self._label_conversation = label_conversation

    async def _label_of(self, delivery_id: str) -> str | None:
        match = _DELIVERY.fullmatch(delivery_id)
        if match is None:
            return None
        try:
            return await self._labels(match.group(1))
        except Exception as error:  # noqa: BLE001 - sin etiqueta sale igual; queda en el log
            logger.warning("bulk_send_label_unreadable error=%s", type(error).__name__)
            return None

    async def send_first_touch(self, *, phone: str, buyer_name: str | None, buyer_email: str | None,
                               content: str, delivery_id: str,
                               template_params: dict[str, object] | None = None) -> FirstTouchResult:
        label = await self._label_of(delivery_id)
        before_send = None
        if label is not None:
            async def before_send(conversation_id: int) -> None:
                await self._label_conversation(conversation_id, label)
        return await self._sender.send_first_touch(
            phone=phone, buyer_name=buyer_name, buyer_email=buyer_email, content=content, delivery_id=delivery_id,
            template_params=template_params, before_send=before_send)

    async def send_first_touch_to_conversation(self, *, conversation_id: int, phone: str, buyer_name: str,
                                               content: str, delivery_id: str,
                                               template_params: dict[str, object] | None = None
                                               ) -> FirstTouchResult:
        return await self._sender.send_first_touch_to_conversation(
            conversation_id=conversation_id, phone=phone, buyer_name=buyer_name, content=content,
            delivery_id=delivery_id, template_params=template_params)


def campaign_labels(store: SupabaseBulkSendStore) -> Callable[[str], Awaitable[str | None]]:
    """La etiqueta de la campana de un envio, leida una vez por envio (no cambia despues del borrador)."""
    cache: dict[str, str | None] = {}

    async def read(send_id: str) -> str | None:
        if send_id not in cache:
            view = await store.read_send(send_id)
            label = view.get("campaign_label")
            cache[send_id] = label if isinstance(label, str) and label else None
        return cache[send_id]

    return read


def conversation_labeler(*, control: Any, prepare: BulkPrepareChatwoot) -> Callable[[int, str], Awaitable[bool]]:
    """Suma la etiqueta sin pisar las que ya tiene: el POST de Chatwoot reemplaza la lista entera."""

    async def label(conversation_id: int, name: str) -> bool:
        current = await control.get_conversation_labels(conversation_id=conversation_id)
        return await prepare.add_label(conversation_id=conversation_id, label=name, current=current)

    return label


# ---------------------------------------------------------------------------
# El armado.
# ---------------------------------------------------------------------------

@dataclass
class BulkSend:
    """Lo que ``app.py`` guarda: el motor, la base y la seccion de la pagina."""

    worker: BulkSendWorker
    store: SupabaseBulkSendStore
    page: Callable[..., None]
    config: BulkSendWorkerConfig
    clock: Callable[[], datetime]
    purge_every: timedelta = PURGE_EVERY
    _purge_task: asyncio.Task[None] | None = None
    _stopping: asyncio.Event | None = None
    last_purge: str = "never"

    async def start(self) -> None:
        await self.worker.start()
        if self._purge_task is None:
            self._stopping = asyncio.Event()
            self._purge_task = asyncio.create_task(self._purge_loop(), name="bulk_send_purge")

    async def stop(self) -> None:
        if self._stopping is not None:
            self._stopping.set()
        if self._purge_task is not None:
            self._purge_task.cancel()
            try:
                await self._purge_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - parar no levanta
                pass
            self._purge_task = None
        await self.worker.stop()

    async def purge_once(self) -> str:
        try:
            result = await self.store.purge(self.clock())
        except Exception as error:  # noqa: BLE001 - se reintenta en la vuelta siguiente
            logger.warning("bulk_send_purge_failed error=%s", type(error).__name__)
            self.last_purge = "failed"
            return self.last_purge
        counts = {k: v for k, v in result.items() if isinstance(v, int) and not isinstance(v, bool)}
        logger.info("bulk_send_purge %s", json.dumps(counts, sort_keys=True))
        self.last_purge = "ok"
        return self.last_purge

    async def _purge_loop(self) -> None:
        assert self._stopping is not None
        while not self._stopping.is_set():
            await self.purge_once()
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.purge_every.total_seconds())
            except asyncio.TimeoutError:
                pass

    def readiness(self) -> dict[str, str]:
        worker = self.worker.readiness()
        return {"bulk_send_worker": worker.get("bulk_send", "?"),
                "bulk_send_last_outcome": worker.get("bulk_send_last_outcome", "none"),
                "bulk_send_last_send": worker.get("bulk_send_last_send", "none"),
                "bulk_send_instance_key": self.config.instance_key,
                "bulk_send_purge": self.last_purge}


def _environ() -> dict[str, str]:
    return {name: value for name in ENV_VARIABLES if (value := os.getenv(name)) is not None}


def build_bulk_send(*, enabled: bool, environ: Mapping[str, str] | None, supabase_base_url: str | None,
                    supabase_service_role_key: str | None, tenant_ref: str | None, ally_ref: str | None,
                    time_zone: str | None, chatwoot_base_url: str | None, chatwoot_account_id: int | None,
                    chatwoot_inbox_id: int | None, chatwoot_control_token: str | None,
                    agent_bot_ready: bool, control_client: Any, meta_access_token: str | None,
                    meta_waba_id: str | None, meta_graph_version: str, producer: Any,
                    campaigns: Callable[[], Sequence[Any]] | None,
                    meta_client: MetaTemplatesClient | None = None,
                    supabase_transport: httpx.AsyncBaseTransport | None = None,
                    chatwoot_transport: httpx.AsyncBaseTransport | None = None,
                    clock: Callable[[], datetime] | None = None) -> BulkSend | None:
    """El masivo de la instancia, o None con ``BULK_SEND_ENABLED`` apagado.

    ``environ`` None es el entorno del proceso (las variables de ``ENV_VARIABLES``). Prendido, todo lo que
    hace falta para mandar seguro tiene que estar; si no, ``BulkSendConfigError`` y el bridge no arranca
    (mejor que prender un motor que frena todo o, peor, uno sin el candado del portfolio).
    """
    if not enabled:
        return None
    env = _environ() if environ is None else dict(environ)
    health_path = (env.get(HEALTH_PATH_VARIABLE, "").strip() or env.get(AUDIENCE_HEALTH_PATH_VARIABLE, "").strip())
    missing = [name for name, value in (
        ("SUPABASE_BASE_URL", supabase_base_url), ("SUPABASE_SERVICE_ROLE_KEY", supabase_service_role_key),
        ("manifiesto: tenant_ref", tenant_ref), ("manifiesto: ally_ref", ally_ref),
        ("CHATWOOT_BASE_URL", chatwoot_base_url), ("CHATWOOT_ACCOUNT_ID", chatwoot_account_id),
        ("manifiesto: chatwoot inbox", chatwoot_inbox_id),
        ("CHATWOOT_CONTROL_API_ACCESS_TOKEN", chatwoot_control_token),
        ("el cliente de control de Chatwoot", control_client),
        ("CHATWOOT_AGENT_BOT_ACCESS_TOKEN y CHATWOOT_AGENT_BOT_ID", agent_bot_ready or None),
        ("META_TEMPLATES_ACCESS_TOKEN", meta_access_token or meta_client),
        ("META_WABA_ID", meta_waba_id or meta_client),
        (NUMBERS_VARIABLE, env.get(NUMBERS_VARIABLE, "").strip()),
        (f"{HEALTH_PATH_VARIABLE} (o {AUDIENCE_HEALTH_PATH_VARIABLE})", health_path),
    ) if not value]
    if missing:
        raise BulkSendConfigError(f"{FLAG} requires {', '.join(missing)}")
    assert supabase_base_url and supabase_service_role_key and tenant_ref and ally_ref
    assert chatwoot_base_url and chatwoot_account_id is not None and chatwoot_inbox_id is not None
    assert chatwoot_control_token
    config = BulkSendWorkerConfig.from_env(env)
    numbers = parse_portfolio_numbers(env[NUMBERS_VARIABLE])
    if config.instance_key not in numbers:
        raise BulkSendConfigError(f"{NUMBERS_VARIABLE} must include {config.instance_key}")
    if not config.lock_dir.is_dir() or not os.access(config.lock_dir, os.W_OK | os.X_OK):
        # El bridge no la crea: una carpeta creada adentro del contenedor seria un candado de un solo bridge.
        raise BulkSendConfigError(f"{LOCK_DIR_VARIABLE} must be an existing writable directory (the host mount)")
    now = clock or (lambda: datetime.now(timezone.utc))
    instance = {"tenant_ref": tenant_ref, "ally_ref": ally_ref}

    store = SupabaseBulkSendStore(base_url=supabase_base_url, service_role_key=supabase_service_role_key,
                                  instance=instance, transport=supabase_transport)
    veto_store = SupabaseBulkVetoStore(base_url=supabase_base_url, service_role_key=supabase_service_role_key,
                                       instance=instance, transport=supabase_transport)

    def prepare_chatwoot() -> BulkPrepareChatwoot:
        return BulkPrepareChatwoot(base_url=chatwoot_base_url, account_id=chatwoot_account_id,
                                   access_token=chatwoot_control_token, inbox_id=chatwoot_inbox_id,
                                   transport=chatwoot_transport)

    worker_chatwoot = prepare_chatwoot()
    meta = meta_client or MetaTemplatesClient(access_token=meta_access_token or "", waba_id=meta_waba_id or "",
                                              graph_version=meta_graph_version)
    # Todos los numeros del portfolio con el token de esta instancia (los tres estan en el mismo portfolio
    # de Meta). Un numero que no se puede leer no deja arrancar.
    health = MetaPortfolioHealthReader({key: (meta, number) for key, number in numbers.items()})
    sender = CampaignLabelSender(
        sender=ChatwootMessageSender(
            chatwoot=control_client, inbox_id=chatwoot_inbox_id, allowed_jid=None,
            dynamic_recipient_enabled=True, template=None, whatsapp_equivalence_enabled=True,
            active_conversation_guard_seconds=RECENT_INBOUND_SECONDS, reuse_open_conversation=True),
        labels=campaign_labels(store),
        label_conversation=conversation_labeler(control=control_client, prepare=worker_chatwoot))

    async def revalidate(prepared: PreparedConversation, phone: str, at: int) -> tuple[bool, str]:
        return await revalidate_prepared(worker_chatwoot, prepared, phone=phone, now=at)

    prepare_store = SupabasePrepareStore(store)

    async def prepare(send_id: str, should_stop: Callable[[], bool]) -> PrepareReport:
        # Un Chatwoot por corrida: el freno por la API de preparar cuenta solo lo de esta corrida.
        return await prepare_bulk_send(
            send_id=send_id, store=prepare_store, chatwoot=prepare_chatwoot(),
            vetoes=base_vetoes_for_prepare(store=store, vetoes=veto_store, send_id=send_id),
            should_stop=should_stop)

    worker = BulkSendWorker(
        store=store, sender=sender, revalidate=revalidate,
        vetoes=batch_vetoes(base=veto_store, chatwoot=ChatwootBatchVetoSource(worker_chatwoot, clock=now)),
        status_reader=ChatwootDeliveryStatusReader(control_client), health=health,
        lock=PortfolioLock(config.lock_dir), templates=MetaTemplateResolver(meta), config=config,
        notifier=SlackBulkBrakeNotifier(producer=producer) if producer is not None else None,
        prepare=prepare, clock=now)
    page = page_routes(
        chatwoot=control_client, inbox_id=chatwoot_inbox_id, country_code=country_code_for_time_zone(time_zone),
        sending_key=config.instance_key, portfolio_keys=tuple(numbers), health=file_health_reader(Path(health_path)),
        reviewer=BulkListVetoReviewer(store=veto_store, chatwoot=control_client, inbox_id=chatwoot_inbox_id),
        campaigns=campaigns)
    logger.info("bulk_send_enabled instance=%s numbers=%s", config.instance_key, ",".join(sorted(numbers)))
    return BulkSend(worker=worker, store=store, page=page, config=config, clock=now)


__all__ = [
    "BulkListVetoReviewer", "BulkSend", "BulkSendStoreRejected", "BulkSendStoreUnavailable",
    "CampaignLabelSender", "ENV_VARIABLES", "HEALTH_PATH_VARIABLE", "SupabaseBulkSendStore",
    "SupabasePrepareStore", "base_vetoes_for_prepare", "build_bulk_send", "campaign_labels",
    "conversation_labeler",
]
