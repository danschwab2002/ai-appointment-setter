"""El motor de envio del masivo manejado por el Automation Expert (encargo 9, pieza 9).

Mapa: ``OS/productos/soporte-infoproductores/documentacion/2026-11-25-mapa-masivo-por-el-ae.md``,
«9. El motor de envio», «10. Los frenos» y las decisiones 2, 5, 6, 8, 10, 11, 12 y 13. La base:
``20261125000200_bulk_send_store_v1.sql`` (reservar, cerrar, vencer, la tanda y las transiciones) y
``20261125000300_bulk_send_prepared_and_base_vetoes_v1.sql`` (lo preparado y los vetos de la base). Los
frenos son los de ``bulk_send_brakes.py``; preparar, ``bulk_send_prepare.py``; los vetos,
``bulk_send_vetoes.py``. Prototipo: ``cmd_enviar`` de masivo.py (los masivos del 22/11 y del 29/11).

Un trabajador asincrono del bridge, del molde del barredor de audiencia
(``flow_audience_sweeper.py``): cada ``refresh_seconds`` hace una pasada (``tick``) sobre los envios de
la instancia.

* **A la hora.** La ventana va centrada en la hora pedida (decision 11): con D = personas ÷ ritmo,
  arranca D/2 antes y termina D/2 despues; con ``desde_la_hora``, arranca a la hora. Se calcula con
  las personas que faltan y se congela 10 minutos antes del arranque. Si la ventana no entra entera
  en el horario permitido (decision 8: 09:00 a 20:59 de CDMX) el envio se frena sin mandar.
* **Antes de arrancar.** La plantilla no puede estar en ``META_FIRST_TOUCH_RETRY_TEMPLATES`` (el
  barrido de rechazos de Meta reintentaria cientos de 131049 por su cuenta), tiene que estar aprobada
  en Meta y, si su encabezado es una imagen, el envio tiene que traerla. Despues el candado del
  portfolio (decision 6), la regla de las 24 h (decision 13) y la salud EN VIVO de los numeros del
  portfolio (decision 5). Recien ahi pasa a ``enviando``.
* **La tanda piloto** (decision 12): los primeros 20, y 5 minutos mirando la entrega con
  ``pilot_verdict`` antes de seguir. Un freno en el piloto no sigue.
* **Los lotes de 25** a 2,5 por segundo como mucho (decision 2), con ``adjust_rate``. Antes de cada
  lote: el estado del envio en la base (el freno manual de la pagina), la hora de corte y el horario,
  el freno por lote (``evaluate_batch``), el ritmo, y cada 200 envios la salud de los numeros del
  portfolio (``compare_portfolio``). Despues lee la tanda, lo preparado y los vetos de ESA tanda.
* **Por persona**, reservar y cerrar (``reserve_bulk_send_recipient_v1`` /
  ``close_bulk_send_recipient_v1``). Lo reservado sin cierre (el bridge se cayo entre medio) pasa a
  ``dudoso`` con ``expire_bulk_send_reservations_v1`` y nunca se reintenta. Una preparada se revalida
  (``revalidate_prepared``) y sale con ``send_first_touch_to_conversation``; si no sirve, o la persona
  no se preparo, va por ``send_first_touch``.

Los vetos van por tanda (las 25 del lote), no por la lista entera: la lectura de «escribio hace poco»
de ``ChatwootVetoReader`` revisa la bandeja entera con un techo de 1.000 conversaciones recientes, y con
dos masivos el mismo dia frena (lo dejo anotado la trabajadora del encargo 4). Aca la parte de
Chatwoot se lee por contacto (``ChatwootBatchVetoSource``: el filtro exacto por telefono y las
conversaciones del contacto, nunca ``/contacts/search``), y solo para las personas que no estan
preparadas: a una preparada la revalida ``revalidate_prepared``, que ya mira lo mismo.

El candado del portfolio y la regla de las 24 h viven en archivos de una carpeta del host montada en
los tres bridges del portfolio (``BULK_SEND_PORTFOLIO_LOCK_DIR``): ``masivo.lock`` con ``flock`` (un
solo masivo a la vez en las tres instancias; un preparar cede si otro esta mandando) y
``frenos-meta.json`` (los frenos de Meta, que sobreviven un reinicio). El montaje lo hace la subida.

Los frenos del motor avisan por el ``SYS-007`` del conector de Slack («Automatizacion pausada
automaticamente»), como el disparo por audiencia; el freno a mano de la pagina no avisa (lo hizo una
persona).

Nadie lo usa todavia (escalon «abierto»): el cableado en ``app.py`` con ``BULK_SEND_ENABLED`` va en un
PR posterior junto con la pagina. Todo lo de afuera entra por interfaces (``BulkSendStore``,
``BulkSender``, ``DeliveryStatusReader``, ``PortfolioHealthReader``...); los adaptadores reales de
Chatwoot y Meta estan aca y los usa la prueba contra respuestas capturadas.

Nada de esto escribe un telefono en un log: van ids de envio, posiciones y conteos.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import tempfile
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from bridge.bulk_send_brakes import (
    DEFAULT_PILOT_POLICY,
    DEFAULT_RATE_POLICY,
    DEFAULT_THRESHOLDS,
    PILOT_CONTINUE,
    PILOT_DONE,
    PILOT_STOP,
    STATUS_DELIVERED,
    STATUS_FAILED,
    STATUS_READ,
    STATUS_SENT,
    BrakeThresholds,
    FinishedSend,
    PilotPolicy,
    PilotState,
    RatePolicy,
    adjust_rate,
    compare_portfolio,
    evaluate_batch,
    is_meta_brake,
    pilot_verdict,
    portfolio_start_verdict,
    requested_rate,
    retained_in_window,
    start_after_meta_brake_verdict,
)
from bridge.bulk_send_prepare import (
    BulkPrepareChatwoot,
    PreparedConversation,
    _conversation_veto,
    _live,
)
from bridge.bulk_send_vetoes import (
    BaseVetoReader,
    VetoMember,
    build_list_vetoes,
)
from bridge.chatwoot import ChatwootProtocolError
from bridge.lead_first_name import TEMPLATE_NEUTRAL_GREETING, template_greeting_name_is_safe
from bridge.meta_error import meta_error_code_of
from bridge.meta_templates import MetaTemplatesClient, MetaTemplatesError
from bridge.messaging import FirstTouchResult
from bridge.phones import canonical_whatsapp_phone, equivalent_whatsapp_phones

logger = logging.getLogger(__name__)

FLAG = "BULK_SEND_ENABLED"
LOCK_DIR_VARIABLE = "BULK_SEND_PORTFOLIO_LOCK_DIR"
INSTANCE_KEY_VARIABLE = "BULK_SEND_INSTANCE_KEY"
NUMBERS_VARIABLE = "BULK_SEND_PORTFOLIO_NUMBERS"
SEND_HOURS_VARIABLE = "BULK_SEND_SEND_HOURS"
TIMEZONE_VARIABLE = "BULK_SEND_TIMEZONE"
TEMPLATES_VARIABLE = "BULK_SEND_TEMPLATES"
RETRY_TEMPLATES_VARIABLE = "META_FIRST_TOUCH_RETRY_TEMPLATES"

LOCK_FILE = "masivo.lock"
BRAKES_FILE = "frenos-meta.json"
BRAKES_LOCK_FILE = "frenos-meta.lock"
# Cuanto se guardan los frenos de Meta en el archivo: la regla mira 24 h; el resto es para leerlo a mano.
BRAKES_KEEP = timedelta(days=7)

DEFAULT_TIMEZONE = "America/Mexico_City"
DEFAULT_SEND_HOURS = (9, 21)  # 09:00 a 20:59 de CDMX (decision 8, masivo.py:125)
DEFAULT_LOT_SIZE = 25
DEFAULT_HEALTH_EVERY = 200
DEFAULT_REFRESH_SECONDS = 30.0
FREEZE_BEFORE = timedelta(minutes=10)
# DEFAULT_REVISAR_DAN: la hora de corte. La tabla no la guarda: el motor corta 30 minutos despues del fin
# calculado de la ventana (con el ritmo bajado a 1/s, una ventana de 30 minutos se estira a 75).
DEFAULT_CUT_GRACE = timedelta(minutes=30)
# DEFAULT_REVISAR_DAN: cuantos estados de mensajes se leen de Chatwoot por lote (una llamada cada uno).
DEFAULT_STATUS_READS_PER_LOT = 50
# DEFAULT_REVISAR_DAN: personas sin preparar por minuto. ``send_first_touch`` busca el contacto con
# ``/contacts/search`` (100 por minuto por cuenta en Chatwoot 4.13, y lo usa el bridge para cada primer
# toque): 20 personas son hasta 40 busquedas, y le dejan el resto al bridge (masivo.py: techo de 60).
DEFAULT_UNPREPARED_PER_MINUTE = 20
# Los vetos de una tanda que no se pueden leer: se reintenta la misma tanda y a la tercera se frena.
VETO_FAILURES_TO_BRAKE = 3
VETO_RETRY_SECONDS = 30.0
PILOT_POLL_SECONDS = 30.0
# Las reservas sin cierre: al reanudar y al terminar se vencen las de mas de 1 minuto.
RESERVATION_EXPIRE_MINUTES = 1
FINISH_ATTEMPTS = 3
# Los estados de un mensaje en la API de Chatwoot.
_STATUS_BY_NAME = {"sent": STATUS_SENT, "delivered": STATUS_DELIVERED, "read": STATUS_READ, "failed": STATUS_FAILED}
_FINAL = frozenset({STATUS_DELIVERED, STATUS_READ, STATUS_FAILED})
_REASON_TEXT = re.compile(r"[^a-z0-9_:]+")
_VARIABLE_BAD = re.compile(r"[\n\t]| {5,}")
# Lo que contesta ``send_first_touch`` cuando frena antes de mandar, con el motivo de veto que le
# corresponde (G3: escribio en la ventana, o la conversacion abierta la tiene el equipo).
_SENDER_VETOES = {"contact_active_conversation": "escribio_hace_poco",
                  "contact_conversation_with_team": "con_equipo"}

STATE_OFF = "apagado"
STATE_OK = "ok"
STATE_SENDING = "enviando"
STATE_PREPARING = "preparando"
STATE_FAILING = "failing"


class BulkSendConfigError(ValueError):
    """La configuracion del masivo no es segura: el bridge no arranca el motor."""


# ---------------------------------------------------------------------------
# La configuracion.
# ---------------------------------------------------------------------------

def assert_not_retried(bulk_templates: Iterable[str], retry_templates: Iterable[str]) -> None:
    """La guarda de configuracion: ninguna plantilla de masivo en ``META_FIRST_TOUCH_RETRY_TEMPLATES``.

    El barrido de rechazos de Meta reintenta una vez el 131049 de esas plantillas (``app.py``,
    ``meta_first_touch_retry_templates``): con una plantilla de masivo ahi reintentaria cientos de envios
    por su cuenta, sin ritmo ni frenos. Falla al arrancar en vez de avisar.
    """
    both = sorted(set(bulk_templates) & set(retry_templates))
    if both:
        raise BulkSendConfigError(
            f"{RETRY_TEMPLATES_VARIABLE} cannot include bulk send templates: {', '.join(both)}")


def _parse_hours(raw: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*([0-9]{1,2})-([0-9]{1,2})\s*", raw)
    if not match:
        raise BulkSendConfigError(f"{SEND_HOURS_VARIABLE} must be HH-HH")
    start, end = int(match.group(1)), int(match.group(2))
    if not 0 <= start < end <= 24:
        raise BulkSendConfigError(f"{SEND_HOURS_VARIABLE} must be HH-HH with start < end")
    return start, end


def parse_portfolio_numbers(raw: str) -> dict[str, str]:
    """``maribel=<phone_number_id>,att1=<id>,johanna=<id>``: la clave de cada instancia y su numero."""
    out: dict[str, str] = {}
    for part in (p.strip() for p in raw.split(",")):
        key, _, number = part.partition("=")
        key, number = key.strip(), number.strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key) or not re.fullmatch(r"[0-9]{6,32}", number) \
                or key in out:
            raise BulkSendConfigError(f"{NUMBERS_VARIABLE} must be key=phone_number_id, separated by commas")
        out[key] = number
    return out


@dataclass(frozen=True)
class BulkSendWorkerConfig:
    """Lo que gobierna al motor. Los valores por defecto son los del mapa y de masivo.py."""

    instance_key: str
    lock_dir: Path
    retry_templates: frozenset[str] = frozenset()
    timezone_name: str = DEFAULT_TIMEZONE
    send_hours: tuple[int, int] = DEFAULT_SEND_HOURS
    lot_size: int = DEFAULT_LOT_SIZE
    health_every: int = DEFAULT_HEALTH_EVERY
    refresh_seconds: float = DEFAULT_REFRESH_SECONDS
    cut_grace: timedelta = DEFAULT_CUT_GRACE
    status_reads_per_lot: int = DEFAULT_STATUS_READS_PER_LOT
    unprepared_per_minute: int = DEFAULT_UNPREPARED_PER_MINUTE
    pilot: PilotPolicy = DEFAULT_PILOT_POLICY
    rate_policy: RatePolicy = DEFAULT_RATE_POLICY
    thresholds: BrakeThresholds = DEFAULT_THRESHOLDS
    pilot_poll_seconds: float = PILOT_POLL_SECONDS
    veto_retry_seconds: float = VETO_RETRY_SECONDS

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", self.instance_key):
            raise BulkSendConfigError("invalid bulk send instance key")
        start, end = self.send_hours
        if not 0 <= start < end <= 24:
            raise BulkSendConfigError("invalid bulk send hours")
        ZoneInfo(self.timezone_name)
        if not 1 <= self.lot_size <= 500 or self.health_every < 1 or self.unprepared_per_minute < 1 \
                or self.status_reads_per_lot < 0:
            raise BulkSendConfigError("invalid bulk send worker limits")

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> BulkSendWorkerConfig:
        """La configuracion desde el entorno, con la guarda de las plantillas de reintento.

        ``BULK_SEND_TEMPLATES`` (opcional) son las plantillas de masivo que la instancia declara: si
        alguna esta tambien en ``META_FIRST_TOUCH_RETRY_TEMPLATES``, falla. Las plantillas de reintento
        quedan ademas en ``retry_templates``, y un envio con una de ellas se frena antes de mandar.
        """
        from bridge.meta_delivery import parse_retry_templates

        missing = [name for name in (INSTANCE_KEY_VARIABLE, LOCK_DIR_VARIABLE) if not environ.get(name, "").strip()]
        if missing:
            raise BulkSendConfigError(f"{FLAG} requires {', '.join(missing)}")
        raw_retry = environ.get(RETRY_TEMPLATES_VARIABLE, "").strip()
        try:
            retry = parse_retry_templates(raw_retry) if raw_retry else frozenset()
            raw_bulk = environ.get(TEMPLATES_VARIABLE, "").strip()
            bulk = parse_retry_templates(raw_bulk) if raw_bulk else frozenset()
        except ValueError as error:
            raise BulkSendConfigError(str(error)) from error
        assert_not_retried(bulk, retry)
        hours = _parse_hours(environ[SEND_HOURS_VARIABLE]) if environ.get(SEND_HOURS_VARIABLE, "").strip() \
            else DEFAULT_SEND_HOURS
        return cls(instance_key=environ[INSTANCE_KEY_VARIABLE].strip(),
                   lock_dir=Path(environ[LOCK_DIR_VARIABLE].strip()), retry_templates=retry,
                   timezone_name=environ.get(TIMEZONE_VARIABLE, "").strip() or DEFAULT_TIMEZONE,
                   send_hours=hours)


# ---------------------------------------------------------------------------
# El envio, leido de la base (``_bulk_send_view``).
# ---------------------------------------------------------------------------

def _when(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp without zone")
    return parsed


@dataclass(frozen=True)
class BulkSendSnapshot:
    """Lo que el motor usa de un envio. ``registered``: personas con fila en ``bulk_send_recipients``."""

    send_id: str
    state: str
    template_name: str
    template_language: str
    template_variables: tuple[object, ...]
    header_image_url: str | None
    scheduled_at: datetime
    window_mode: str
    rate_per_second: float
    eligible_count: int
    registered: int
    confirmed_at: datetime | None = None
    confirmed_health: str | None = None
    product_refs: tuple[str, ...] = ()
    list_id: str | None = None
    brake_reason: str | None = None
    braked_by: str | None = None

    @property
    def remaining(self) -> int:
        return max(0, self.eligible_count - self.registered)

    @property
    def ref(self) -> str:
        """Una referencia corta del envio, sin guiones (los motivos no los aceptan)."""
        return self.send_id.replace("-", "")[:12]


def snapshot_of(view: Mapping[str, Any]) -> BulkSendSnapshot:
    """De ``read_bulk_send_v1`` / un item de ``read_bulk_sends_v1`` al envio. Falla si falta algo."""
    send_id = view["send_id"]
    if not isinstance(send_id, str):
        raise ValueError("send_id")
    listing = view.get("list") if isinstance(view.get("list"), Mapping) else {}
    by_state = view.get("recipients_by_state") if isinstance(view.get("recipients_by_state"), Mapping) else {}
    scheduled = _when(view.get("scheduled_at"))
    if scheduled is None:
        raise ValueError("scheduled_at")
    variables = view.get("template_variables") or []
    products = view.get("product_refs") or []
    return BulkSendSnapshot(
        send_id=send_id, state=str(view["state"]), template_name=str(view["template_name"]),
        template_language=str(view["template_language"]), template_variables=tuple(variables),
        header_image_url=view.get("header_image_url") if isinstance(view.get("header_image_url"), str) else None,
        scheduled_at=scheduled, window_mode=str(view.get("window_mode") or "centrada"),
        rate_per_second=int(view.get("rate_millis") or 2500) / 1000.0,
        eligible_count=int(listing.get("eligible_count") or 0),
        registered=sum(int(n or 0) for n in by_state.values()),
        confirmed_at=_when(view.get("confirmed_at")),
        confirmed_health=view.get("confirmed_health") if isinstance(view.get("confirmed_health"), str) else None,
        product_refs=tuple(str(p) for p in products if isinstance(p, str)),
        list_id=listing.get("list_id") if isinstance(listing.get("list_id"), str) else None,
        brake_reason=view.get("brake_reason") if isinstance(view.get("brake_reason"), str) else None,
        braked_by=view.get("braked_by") if isinstance(view.get("braked_by"), str) else None,
    )


@dataclass(frozen=True)
class BatchMember:
    """Una persona de la tanda: su posicion, el telefono en forma de envio y el primer nombre."""

    position: int
    phone: str
    first_name: str | None = None


# ---------------------------------------------------------------------------
# La ventana (decision 11) y el horario (decision 8). Puros.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SendWindow:
    start: datetime
    end: datetime
    cut: datetime
    seconds: float


def send_window(people: int, rate: float, scheduled_at: datetime, mode: str, *,
                cut_grace: timedelta = DEFAULT_CUT_GRACE) -> SendWindow:
    """``centrada``: arranca D/2 antes de la hora y termina D/2 despues, con D = personas ÷ ritmo
    (``ventana_centrada`` de masivo.py). ``desde_la_hora``: arranca a la hora. La hora de corte es el
    fin mas ``cut_grace``."""
    if rate <= 0:
        raise ValueError("rate")
    seconds = max(0, people) / rate
    if mode == "desde_la_hora":
        start = scheduled_at
    else:
        start = scheduled_at - timedelta(seconds=seconds / 2)
    end = start + timedelta(seconds=seconds)
    return SendWindow(start=start, end=end, cut=end + cut_grace, seconds=seconds)


def within_send_hours(start: datetime, end: datetime, zone: ZoneInfo, hours: tuple[int, int]) -> bool:
    """La ventana entra entera en el horario: los dos extremos, en el mismo dia local."""
    local_start, local_end = start.astimezone(zone), end.astimezone(zone)
    if local_start.date() != local_end.date():
        return False
    return hours[0] <= local_start.hour < hours[1] and hours[0] <= local_end.hour < hours[1]


def in_send_hours(now: datetime, zone: ZoneInfo, hours: tuple[int, int]) -> bool:
    return hours[0] <= now.astimezone(zone).hour < hours[1]


# ---------------------------------------------------------------------------
# La plantilla.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BulkTemplate:
    """Lo que el motor necesita de la plantilla aprobada: categoria, cuerpo y formato del encabezado."""

    name: str
    language: str
    category: str
    body: str
    header_format: str | None = None
    status: str = "APPROVED"


class TemplateResolver(Protocol):
    async def resolve(self, name: str, language: str) -> BulkTemplate | None: ...


class MetaTemplateResolver:
    """La plantilla por la Graph API (``MetaTemplatesClient.find``), con su contenido y su estado."""

    def __init__(self, client: MetaTemplatesClient) -> None:
        self._client = client

    async def resolve(self, name: str, language: str) -> BulkTemplate | None:
        found = await self._client.find(name, language)
        if found is None or found.content is None or found.category is None:
            return None
        header, body, _, _ = found.content
        return BulkTemplate(name=name, language=language, category=found.category, body=str(body),
                            header_format=header[0] if header else None, status=found.status)


def template_values(variables: Sequence[object], first_name: str | None) -> dict[str, str]:
    """``{"1": ...}`` para la persona (``valores`` de masivo.py). ``{"de": "nombre"}`` es su primer nombre,
    o el saludo neutro del bridge si no tiene uno seguro; lo demas va literal."""
    values: dict[str, str] = {}
    for index, variable in enumerate(variables, 1):
        if isinstance(variable, Mapping) and variable.get("de") == "nombre":
            name = (first_name or "").strip()
            values[str(index)] = name if name and template_greeting_name_is_safe(name) else TEMPLATE_NEUTRAL_GREETING
        elif isinstance(variable, str):
            values[str(index)] = variable
        else:
            raise ValueError("variable")
    for value in values.values():
        if not value.strip() or _VARIABLE_BAD.search(value):
            raise ValueError("variable")
    return values


def render_body(body: str, values: Mapping[str, str]) -> str:
    return re.sub(r"\{\{(\d+)\}\}", lambda m: values.get(m.group(1), m.group(0)), body)


def template_params(template: BulkTemplate, values: Mapping[str, str], image_url: str | None) -> dict[str, object]:
    """El formato de Chatwoot 4.13 (``template_params`` de masivo.py): el cuerpo por posicion y, con un
    encabezado de imagen, ``header.media_url``."""
    processed: dict[str, object] = {"body": dict(values)}
    if template.header_format == "IMAGE":
        if not image_url:
            raise ValueError("image")
        processed["header"] = {"media_url": image_url, "media_type": "image"}
    return {"name": template.name, "category": template.category, "language": template.language,
            "processed_params": processed}


# ---------------------------------------------------------------------------
# Lo que el motor usa de afuera.
# ---------------------------------------------------------------------------

class BulkSendStore(Protocol):
    """La base del masivo: las funciones de ``service_role`` de las dos migraciones, con su JSON."""

    async def read_sends(self) -> Sequence[Mapping[str, Any]]: ...  # read_bulk_sends_v1

    async def read_send(self, send_id: str) -> Mapping[str, Any]: ...  # read_bulk_send_v1

    async def transition(self, send_id: str, to: str, reason: str | None) -> Mapping[str, Any]: ...

    async def reserve(self, send_id: str, position: int) -> Mapping[str, Any]: ...

    async def close(self, send_id: str, position: int, outcome: Mapping[str, Any]) -> Mapping[str, Any]: ...

    async def expire(self, send_id: str, minutes: int) -> Mapping[str, Any]: ...

    async def read_batch(self, send_id: str, limit: int) -> Sequence[Mapping[str, Any]]: ...

    async def read_prepared(self, send_id: str, positions: Sequence[int]) -> Mapping[int, PreparedConversation]: ...


class BulkSender(Protocol):
    """Los dos caminos de ``ChatwootMessageSender``. El que se le pasa al motor va sin plantilla del
    manifiesto (``template=None``: las del masivo llegan armadas en ``template_params``), con el destino
    dinamico, la equivalencia de WhatsApp y G3 prendidos (6 h y reusar la conversacion abierta)."""

    async def send_first_touch(self, *, phone: str, buyer_name: str | None, buyer_email: str | None,
                               content: str, delivery_id: str,
                               template_params: dict[str, object] | None = None) -> FirstTouchResult: ...

    async def send_first_touch_to_conversation(self, *, conversation_id: int, phone: str, buyer_name: str,
                                               content: str, delivery_id: str,
                                               template_params: dict[str, object] | None = None
                                               ) -> FirstTouchResult: ...


# La revalidacion de una preparada: ``revalidate_prepared`` de bulk_send_prepare (sirve, motivo).
Revalidate = Callable[[PreparedConversation, str, int], Awaitable[tuple[bool, str]]]
# Los vetos de una tanda: posicion -> motivo de ``bulk_list_members.exclusion_reason``. ``prepared`` son
# las posiciones preparadas (la parte de Chatwoot las saltea: las revalida ``revalidate_prepared``).
BatchVetoes = Callable[[BulkSendSnapshot, Sequence[BatchMember], frozenset[int]], Awaitable[Mapping[int, str]]]
# Preparar (``prepare_bulk_send`` con su tienda y su Chatwoot), con el «pará» del motor.
Prepare = Callable[[str, Callable[[], bool]], Awaitable[Any]]


class DeliveryStatusReader(Protocol):
    async def read_status(self, *, conversation_id: int, message_id: int) -> tuple[str, str | None]:
        """``(status de Chatwoot, codigo de Meta o None)``. Lanza si no se pudo leer."""
        ...


class PortfolioHealthReader(Protocol):
    async def read(self) -> Mapping[str, Mapping[str, Any] | None]:
        """La salud de cada numero del portfolio, con la forma de ``normalizar`` de salud_meta.py
        (``{"error": ...}`` si no se pudo leer)."""
        ...


class BulkBrakeNotifier(Protocol):
    async def braked(self, send: BulkSendSnapshot, reason: str) -> None: ...


# ---------------------------------------------------------------------------
# Los adaptadores de Chatwoot y Meta.
# ---------------------------------------------------------------------------

class ChatwootDeliveryStatusReader:
    """El estado de un mensaje por la API de mensajes de Chatwoot (``messages.status`` y
    ``content_attributes.external_error``), como ``meta_delivery`` y el reporte."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def read_status(self, *, conversation_id: int, message_id: int) -> tuple[str, str | None]:
        messages = await self._client.get_conversation_messages(
            conversation_id=conversation_id, limit=20, required_message_ids=(message_id,))
        for message in messages:
            if isinstance(message, Mapping) and message.get("id") == message_id:
                status = message.get("status")
                if status not in _STATUS_BY_NAME:
                    raise ChatwootProtocolError("invalid_message_status")
                attributes = message.get("content_attributes")
                if isinstance(attributes, str):
                    try:
                        attributes = json.loads(attributes)
                    except ValueError:
                        attributes = {}
                error = attributes.get("external_error") if isinstance(attributes, Mapping) else None
                return str(status), meta_error_code_of(error) if status == "failed" else None
        raise ChatwootProtocolError("message_not_found")


class MetaPortfolioHealthReader:
    """La salud en vivo de los numeros del portfolio (decision 6): clave -> (cliente, phone_number_id).
    Un numero que no se puede leer vuelve como ``{"error": <codigo>}``: al arrancar no arranca, en el
    medio es un aviso."""

    def __init__(self, numbers: Mapping[str, tuple[MetaTemplatesClient, str]]) -> None:
        if not numbers:
            raise BulkSendConfigError("the portfolio needs at least one number")
        self._numbers = dict(numbers)

    async def read(self) -> Mapping[str, Mapping[str, Any] | None]:
        out: dict[str, Mapping[str, Any] | None] = {}
        for key, (client, phone_number_id) in sorted(self._numbers.items()):
            try:
                out[key] = (await client.read_phone_health(phone_number_id)).as_reading()
            except MetaTemplatesError as error:
                out[key] = {"error": error.code}
        return out


class ChatwootBatchVetoSource:
    """La parte de Chatwoot de los vetos de UNA tanda, por contacto: el filtro exacto por telefono
    (``POST /contacts/filter``, en cada forma) y las conversaciones del contacto en la bandeja, con los
    entrantes de las ultimas 6 h. Bloqueado, etiqueta ``cliente`` o ``automation_opted_out``, una
    conversacion viva con el equipo o un entrante reciente vetan. Una persona sin contacto no tiene
    nada que vetar en Chatwoot. Un error sube: el motor no manda esa tanda."""

    def __init__(self, chatwoot: BulkPrepareChatwoot, *, clock: Callable[[], datetime],
                 recent_seconds: int = 6 * 3600) -> None:
        self._chatwoot = chatwoot
        self._clock = clock
        self._recent = recent_seconds

    async def vetoes_by_phone(self, phones: Iterable[str]) -> Mapping[str, str]:
        out: dict[str, str] = {}
        since = int(self._clock().timestamp()) - self._recent
        for phone in phones:
            contacts: list[Mapping[str, object]] = []
            for variant in equivalent_whatsapp_phones(phone):
                contacts.extend(await self._chatwoot.find_contacts(variant))
            reason: str | None = None
            for contact in contacts:
                if contact.get("blocked") is True:
                    reason = "bloqueado"
                    break
                contact_id = contact.get("id")
                if not isinstance(contact_id, int) or isinstance(contact_id, bool):
                    raise ChatwootProtocolError("invalid_contact_id")
                conversations = await self._chatwoot.client.list_contact_conversations(
                    contact_id=contact_id, inbox_id=self._chatwoot.inbox_id, messages_since_epoch=since)
                reason = _conversation_veto(conversations, since)
                live = _live(conversations)
                if reason is None and live is not None and live.with_team:
                    reason = "con_equipo"
                if reason is not None:
                    break
            if reason is not None:
                out[phone] = reason
        return out


def batch_vetoes(*, base: BaseVetoReader, chatwoot: Any) -> BatchVetoes:
    """Los vetos de una tanda con ``build_list_vetoes`` del encargo 4: la base (baja y compra, por la lista)
    y Chatwoot por contacto, solo para las personas sin preparar."""

    class _OnlyUnprepared:
        def __init__(self, phones: frozenset[str]) -> None:
            self._phones = phones

        async def vetoes_by_phone(self, phones: Iterable[str]) -> Mapping[str, str]:
            wanted = [p for p in phones if p in self._phones]
            return await chatwoot.vetoes_by_phone(wanted) if wanted else {}

    async def run(send: BulkSendSnapshot, members: Sequence[BatchMember], prepared: frozenset[int]) -> Mapping[int, str]:
        if send.list_id is None:
            raise ValueError("send without list")
        # build_list_vetoes le pide a Chatwoot por el telefono canonico.
        unprepared = frozenset(canonical_whatsapp_phone(m.phone) or m.phone for m in members
                               if m.position not in prepared)
        result = await build_list_vetoes(
            list_id=send.list_id, members=[VetoMember(position=m.position, phone=m.phone) for m in members],
            product_refs=send.product_refs, base=base, chatwoot=_OnlyUnprepared(unprepared))
        return {int(e["position"]): str(e["reason"]) for e in result.exclusions}  # type: ignore[call-overload]

    return run


# ---------------------------------------------------------------------------
# El candado del portfolio (decision 6) y la regla de las 24 h (decision 13), en archivos del host.
# ---------------------------------------------------------------------------

class PortfolioLock:
    """``masivo.lock`` con ``flock``: un solo masivo a la vez en las instancias que montan la carpeta.
    ``frenos-meta.json``: los frenos de Meta del portfolio, para la regla de las 24 h; sobrevive un
    reinicio y lo ven los tres bridges. Se escribe entero en un archivo temporal y se reemplaza."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self, owner: Mapping[str, str]) -> bool:
        if self._fd is not None:
            return True
        self._dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._dir / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(dict(owner), sort_keys=True).encode("utf-8"))
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def busy_elsewhere(self) -> bool:
        """Si otro (otro bridge, u otro envio) tiene el candado. No lo toma."""
        if self._fd is not None:
            return False
        path = self._dir / LOCK_FILE
        if not path.exists():
            return False
        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def read_brakes(self) -> list[FinishedSend]:
        """Los frenos de Meta guardados. Un archivo que no se entiende falla (no se arranca sin saber)."""
        path = self._dir / BRAKES_FILE
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data.get("frenos") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ValueError("frenos-meta.json")
        out = []
        for row in rows:
            braked_at = _when(row.get("braked_at")) if isinstance(row, dict) else None
            if braked_at is None or not isinstance(row.get("send_ref"), str) or not isinstance(row.get("reason"), str):
                raise ValueError("frenos-meta.json")
            out.append(FinishedSend(send_ref=row["send_ref"], braked_at=braked_at, reason=row["reason"]))
        return out

    def record_brake(self, item: FinishedSend, *, instance: str, now: datetime) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._dir / BRAKES_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            path = self._dir / BRAKES_FILE
            rows: list[dict[str, str]] = []
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    rows = [r for r in data.get("frenos", []) if isinstance(r, dict)]
                except (ValueError, AttributeError):
                    logger.warning("bulk_send_brakes_file_unreadable_rewritten")
            rows = [r for r in rows if (_when(r.get("braked_at")) or now) > now - BRAKES_KEEP]
            rows.append({"send_ref": item.send_ref, "instance": instance,
                         "braked_at": item.braked_at.isoformat(), "reason": item.reason})
            handle, temporary = tempfile.mkstemp(dir=self._dir, prefix=".frenos-", suffix=".json")
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump({"frenos": rows}, out, ensure_ascii=False, indent=1)
            os.replace(temporary, path)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


# ---------------------------------------------------------------------------
# El aviso del freno (SYS-007).
# ---------------------------------------------------------------------------

BRAKE_EVENT_CODE = "SYS-007"
_NOTICE_NAMESPACE = b"bulk-send-brake-v1\0"


def brake_notice(*, send_id: str, reason: str, occurred_at: datetime) -> Any:
    """El aviso de un envio frenado por el motor: el envio y el motivo, sin personas. Uno por envio."""
    from slack_correlation.catalog import NotificationCommand

    digest = hashlib.sha256(_NOTICE_NAMESPACE + send_id.encode("ascii")).hexdigest()
    return NotificationCommand(
        event_id=str(uuid.UUID(hex=digest[:32])), event_code=BRAKE_EVENT_CODE, dedupe_key=digest,
        occurred_at=occurred_at, reason_code=machine_reason(reason)[:80],
        component=f"bulk_send:{send_id.replace('-', '')[:12]}", state="pending", count=1)


class SlackBulkBrakeNotifier:
    """El aviso por el productor del conector de Slack de la aliada (el de ``flow_audience_wiring``)."""

    def __init__(self, *, producer: Any, clock: Callable[[], datetime] | None = None) -> None:
        self._producer = producer
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def braked(self, send: BulkSendSnapshot, reason: str) -> None:
        await self._producer.admit(brake_notice(send_id=send.send_id, reason=reason, occurred_at=self._clock()))


def machine_reason(reason: str) -> str:
    """Un motivo con la forma de ``bulk_sends.brake_reason`` (``^[a-z][a-z0-9_:]{0,63}$``)."""
    text = _REASON_TEXT.sub("_", reason.strip().lower()).strip("_") or "freno"
    if not text[0].isalpha():
        text = "f_" + text
    return text[:64]


# ---------------------------------------------------------------------------
# El motor.
# ---------------------------------------------------------------------------

@dataclass
class _Tracked:
    conversation_id: int
    message_id: int
    sent_at: datetime
    pilot: bool
    status: int = STATUS_SENT
    code: str | None = None


@dataclass
class _Run:
    """El estado de un envio que corre, en memoria. Despues de un reinicio arranca vacio: los frenos
    por entrega miran solo lo que se mando desde ahi."""

    send: BulkSendSnapshot
    requested: float
    rate: float
    baselines: dict[str, Mapping[str, Any]]
    window: SendWindow
    tracked: dict[int, _Tracked] = field(default_factory=dict)
    call_failures: deque[bool] = field(default_factory=lambda: deque(maxlen=200))
    since_health: int = 0
    processed: int = 0
    veto_failures: int = 0
    unprepared_at: deque[datetime] = field(default_factory=deque)
    counts: dict[str, int] = field(default_factory=dict)

    def note(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1


class _Stop(Exception):
    """El envio deja de correr con este resultado (``frenado:<motivo>``, ``terminado``, ``estado_<x>``)."""

    def __init__(self, outcome: str) -> None:
        super().__init__(outcome)
        self.outcome = outcome


@dataclass(frozen=True)
class TickReport:
    """Lo que hizo una pasada. Sin telefonos."""

    outcome: str
    send_id: str | None = None
    counts: Mapping[str, int] = field(default_factory=dict)


class BulkSendWorker:
    def __init__(self, *, store: BulkSendStore, sender: BulkSender, revalidate: Revalidate,
                 vetoes: BatchVetoes, status_reader: DeliveryStatusReader, health: PortfolioHealthReader,
                 lock: PortfolioLock, templates: TemplateResolver, config: BulkSendWorkerConfig,
                 notifier: BulkBrakeNotifier | None = None, prepare: Prepare | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 sleep: Callable[[float], Awaitable[object]] = asyncio.sleep) -> None:
        self._store = store
        self._sender = sender
        self._revalidate = revalidate
        self._vetoes = vetoes
        self._status = status_reader
        self._health = health
        self._lock = lock
        self._templates = templates
        self._config = config
        self._notifier = notifier
        self._prepare = prepare
        self._clock = clock
        self._sleep = sleep
        self._frozen: dict[str, SendWindow] = {}
        self._prepared_done: set[str] = set()
        self._busy = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._state = STATE_OK
        self._last: TickReport | None = None

    # -- la pasada -----------------------------------------------------------

    def readiness(self) -> dict[str, str]:
        last = self._last
        return {"bulk_send": self._state,
                "bulk_send_last_outcome": "none" if last is None else last.outcome,
                "bulk_send_last_send": "none" if last is None or last.send_id is None else last.send_id}

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock without zone")
        return now

    def window_for(self, send: BulkSendSnapshot, *, now: datetime) -> SendWindow:
        """La ventana del envio: calculada con las personas que faltan, congelada 10 minutos antes."""
        frozen = self._frozen.get(send.send_id)
        if frozen is not None:
            return frozen
        people = send.remaining if send.state != "enviando" else send.eligible_count
        window = send_window(people, requested_rate(send.rate_per_second), send.scheduled_at, send.window_mode,
                             cut_grace=self._config.cut_grace)
        if now >= window.start - FREEZE_BEFORE or send.state == "enviando":
            self._frozen[send.send_id] = window
        return window

    async def tick(self) -> TickReport:
        async with self._busy:
            report = await self._tick()
            self._last = report
            return report

    async def _tick(self) -> TickReport:
        now = self._now()
        sends: list[BulkSendSnapshot] = []
        for view in await self._store.read_sends():
            try:
                sends.append(snapshot_of(view))
            except (KeyError, TypeError, ValueError):
                logger.warning("bulk_send_unreadable_send")
        for send in sends:
            if send.state == "enviando":
                return await self._run(send, resume=True)
        waiting = sorted((s for s in sends if s.state in ("confirmado", "preparando")),
                         key=lambda s: self.window_for(s, now=now).start)
        for send in waiting:
            window = self.window_for(send, now=now)
            if now >= window.cut:
                return TickReport(await self._brake(send, "corte_por_hora", notify=True), send.send_id)
            if now >= window.start:
                return await self._run(send, resume=False)
        if self._prepare is not None:
            for send in waiting:
                window = self.window_for(send, now=now)
                if send.send_id in self._prepared_done or now >= window.start - FREEZE_BEFORE:
                    continue
                return await self._run_prepare(send, waiting)
        return TickReport("esperando")

    async def _run_prepare(self, send: BulkSendSnapshot, waiting: Sequence[BulkSendSnapshot]) -> TickReport:
        """Preparar el envio que viene mientras no haya otro mandando en el portfolio; para 10 minutos
        antes del arranque de cualquier envio de la instancia."""
        assert self._prepare is not None
        if self._lock.busy_elsewhere():
            return TickReport("preparar_espera_candado", send.send_id)
        starts = [self.window_for(s, now=self._now()).start - FREEZE_BEFORE for s in waiting]
        first = min(starts)
        self._state = STATE_PREPARING

        def should_stop() -> bool:
            return self._stopped.is_set() or self._now() >= first or self._lock.busy_elsewhere()

        try:
            result = await self._prepare(send.send_id, should_stop)
        finally:
            self._state = STATE_OK
        if getattr(result, "finished", False):
            self._prepared_done.add(send.send_id)
        return TickReport(f"preparar:{getattr(result, 'stop_reason', '?')}", send.send_id)

    # -- un envio ------------------------------------------------------------

    async def _brake(self, send: BulkSendSnapshot, reason: str, *, notify: bool) -> str:
        reason = machine_reason(reason)
        result = await self._store.transition(send.send_id, "frenado", reason)
        if not result.get("ok"):
            logger.warning("bulk_send_brake_refused send=%s reason=%s state=%s", send.send_id, reason,
                           result.get("state"))
            return f"estado_{result.get('state')}"
        now = self._now()
        logger.warning("bulk_send_braked send=%s reason=%s", send.send_id, reason)
        if is_meta_brake(reason):
            try:
                self._lock.record_brake(FinishedSend(send.ref, now, reason), instance=self._config.instance_key,
                                        now=now)
            except OSError as error:
                logger.error("bulk_send_meta_brake_not_recorded send=%s error=%s", send.send_id,
                             type(error).__name__)
        if notify and self._notifier is not None:
            try:
                await self._notifier.braked(send, reason)
            except Exception as error:  # noqa: BLE001 - el aviso no deshace el freno
                logger.warning("bulk_send_brake_notice_failed error=%s", type(error).__name__)
        return f"frenado:{reason}"

    async def _preflight(self, send: BulkSendSnapshot) -> BulkTemplate | str | None:
        """La plantilla, el motivo de un freno, o None si Meta no contesto (se vuelve a mirar en la
        pasada siguiente; pasada la hora de corte, frena por corte)."""
        if send.template_name in self._config.retry_templates:
            return "plantilla_con_reintento"
        try:
            template = await self._templates.resolve(send.template_name, send.template_language)
        except MetaTemplatesError:
            return None
        if template is None or template.status != "APPROVED":
            return "plantilla_no_aprobada"
        if template.header_format == "IMAGE" and not send.header_image_url:
            return "plantilla_sin_imagen"
        try:
            template_values(send.template_variables, None)
        except ValueError:
            return "variables_invalidas"
        return template

    async def _run(self, send: BulkSendSnapshot, *, resume: bool) -> TickReport:
        now = self._now()
        window = self.window_for(send, now=now)
        prepared = await self._preflight(send)
        if prepared is None:
            return TickReport("plantilla_no_se_pudo_leer", send.send_id)
        if isinstance(prepared, str):
            return TickReport(await self._brake(send, prepared, notify=True), send.send_id)
        template = prepared
        if not resume and not within_send_hours(window.start, window.end, self._config.zone, self._config.send_hours):
            return TickReport(await self._brake(send, "fuera_de_horario", notify=True), send.send_id)
        owner = {"instance": self._config.instance_key, "send": send.ref, "since": now.isoformat()}
        if not self._lock.try_acquire(owner):
            logger.info("bulk_send_lock_busy send=%s", send.send_id)
            return TickReport("candado_ocupado", send.send_id)
        self._state = STATE_SENDING
        try:
            if not resume:
                try:
                    history = self._lock.read_brakes()
                except (OSError, ValueError):
                    return TickReport("frenos_ilegibles", send.send_id)
                verdict = start_after_meta_brake_verdict(history, now=now, confirmed_at=send.confirmed_at)
                if not verdict.allows:
                    return TickReport(await self._brake(send, verdict.reason or "regla_24h", notify=True),
                                      send.send_id)
            readings = dict(await self._health.read())
            start = portfolio_start_verdict(self._config.instance_key, readings,
                                            yellow_confirmed=send.confirmed_health == "amarillo")
            if not start.allows and (start.reason or "").endswith("ilegible_al_arrancar"):
                # Sin una lectura que se entienda no se manda, pero tampoco se frena: Meta puede no haber
                # contestado una vez. Se vuelve a leer en la pasada siguiente.
                return TickReport(f"salud_ilegible:{start.reason}", send.send_id)
            if not start.allows:
                return TickReport(await self._brake(send, start.reason or "salud", notify=True), send.send_id)
            if not resume:
                moved = await self._store.transition(send.send_id, "enviando", None)
                if not moved.get("ok"):
                    return TickReport(f"estado_{moved.get('state')}", send.send_id)
            else:
                await self._store.expire(send.send_id, RESERVATION_EXPIRE_MINUTES)
            rate = requested_rate(send.rate_per_second)
            run = _Run(send=send, requested=send.rate_per_second, rate=rate, window=window,
                       baselines={k: v for k, v in readings.items() if isinstance(v, Mapping) and "error" not in v})
            logger.info("bulk_send_started send=%s resume=%s remaining=%s rate=%.1f", send.send_id, resume,
                        send.remaining, rate)
            try:
                if send.registered < self._config.pilot.size:
                    await self._pilot(run, template)
                await self._lots(run, template, limit=None)
                outcome = await self._finish(run)
            except _Stop as stop:
                outcome = stop.outcome
            logger.info("bulk_send_stopped send=%s outcome=%s counts=%s", send.send_id, outcome,
                        json.dumps(run.counts, sort_keys=True))
            return TickReport(outcome, send.send_id, dict(run.counts))
        finally:
            self._lock.release()
            self._state = STATE_OK

    async def _finish(self, run: _Run) -> str:
        for attempt in range(FINISH_ATTEMPTS):
            await self._store.expire(run.send.send_id, RESERVATION_EXPIRE_MINUTES)
            result = await self._store.transition(run.send.send_id, "terminado", None)
            if result.get("ok"):
                return "terminado"
            if result.get("reason_code") != "reservations_open":
                return f"estado_{result.get('state')}"
            await self._sleep(60.0 * RESERVATION_EXPIRE_MINUTES + 1)
        return "terminado_con_reservas_abiertas"

    async def _pilot(self, run: _Run, template: BulkTemplate) -> None:
        """Los primeros del piloto y la espera mirando la entrega (decision 12)."""
        size = self._config.pilot.size - run.send.registered
        await self._lots(run, template, limit=size, pilot=True)
        finished_at = self._now()
        while True:
            await self._check_state(run)
            await self._refresh(run, pilot_only=True)
            pilot = [t for t in run.tracked.values() if t.pilot]
            statuses, codes = self._counts(run)
            brake = evaluate_batch(statuses, codes, list(run.call_failures)[-50:], self._config.thresholds)
            finals = sum(1 for t in pilot if t.status in _FINAL)
            failed = sum(1 for t in pilot if t.status == STATUS_FAILED)
            verdict, why = pilot_verdict(PilotState(
                recipients=run.send.eligible_count, sent=run.send.registered + run.processed, now=self._now(),
                finished_at=finished_at, finals=finals, failed=failed, brake=brake), self._config.pilot)
            if verdict == PILOT_STOP:
                raise _Stop(await self._brake(run.send, why or "piloto", notify=True))
            if verdict in (PILOT_CONTINUE, PILOT_DONE):
                run.note(f"piloto_{verdict}")
                return
            await self._sleep(self._config.pilot_poll_seconds)

    def _counts(self, run: _Run) -> tuple[dict[int, int], dict[str, int]]:
        statuses: dict[int, int] = {}
        codes: dict[str, int] = {}
        for t in run.tracked.values():
            statuses[t.status] = statuses.get(t.status, 0) + 1
            if t.code:
                codes[t.code] = codes.get(t.code, 0) + 1
        return statuses, codes

    async def _refresh(self, run: _Run, *, pilot_only: bool = False) -> None:
        """Lee de Chatwoot el estado de lo mandado que sigue sin resultado: los del piloto, o los de los
        ultimos 10 minutos (los mas viejos primero), hasta ``status_reads_per_lot``."""
        now = self._now()
        pending = [t for t in run.tracked.values() if t.status not in _FINAL]
        if pilot_only:
            chosen = [t for t in pending if t.pilot]
        else:
            low, high = now - timedelta(minutes=10), now - timedelta(seconds=10)
            chosen = sorted((t for t in pending if low <= t.sent_at <= high), key=lambda t: t.sent_at)
            chosen = chosen[: self._config.status_reads_per_lot]
        for t in chosen:
            try:
                status, code = await self._status.read_status(conversation_id=t.conversation_id,
                                                              message_id=t.message_id)
            except Exception:  # noqa: BLE001 - una lectura que falla cuenta para el freno de la API
                run.call_failures.append(True)
                continue
            run.call_failures.append(False)
            t.status = _STATUS_BY_NAME[status]
            t.code = code

    async def _check_state(self, run: _Run) -> None:
        """El freno a mano (el estado en la base), la hora de corte y el horario."""
        current = snapshot_of(await self._store.read_send(run.send.send_id))
        if current.state != "enviando":
            if current.state == "frenado":
                raise _Stop(f"frenado:{current.brake_reason}")
            raise _Stop(f"estado_{current.state}")
        now = self._now()
        if now >= run.window.cut:
            raise _Stop(await self._brake(run.send, "corte_por_hora", notify=True))
        if not in_send_hours(now, self._config.zone, self._config.send_hours):
            raise _Stop(await self._brake(run.send, "fuera_de_horario", notify=True))

    async def _before_lot(self, run: _Run) -> None:
        await self._check_state(run)
        await self._refresh(run)
        statuses, codes = self._counts(run)
        brake = evaluate_batch(statuses, codes, list(run.call_failures)[-50:], self._config.thresholds)
        if brake is not None:
            raise _Stop(await self._brake(run.send, brake.reason, notify=True))
        now = self._now()
        retained, total = retained_in_window(((t.sent_at, t.status) for t in run.tracked.values()), now=now)
        rate, event = adjust_rate(run.rate, run.requested, retained, total, self._config.rate_policy)
        if event:
            logger.info("bulk_send_rate send=%s %s", run.send.send_id, event)
            run.note("ritmo_bajado" if rate < run.rate else "ritmo_subido")
        run.rate = rate
        if run.since_health >= self._config.health_every:
            run.since_health = 0
            readings = await self._health.read()
            health_brake, notice = compare_portfolio(run.baselines, readings)
            if notice:
                logger.info("bulk_send_health send=%s %s", run.send.send_id, notice)
            if health_brake is not None:
                raise _Stop(await self._brake(run.send, health_brake.reason, notify=True))

    async def _lots(self, run: _Run, template: BulkTemplate, *, limit: int | None, pilot: bool = False) -> None:
        done = 0
        send_id = run.send.send_id
        while limit is None or done < limit:
            await self._before_lot(run)
            size = self._config.lot_size if limit is None else min(self._config.lot_size, limit - done)
            rows = await self._store.read_batch(send_id, size)
            members = [BatchMember(int(r["position"]), str(r["phone"]),
                                   r.get("first_name") if isinstance(r.get("first_name"), str) else None)
                       for r in rows]
            if not members:
                return
            try:
                prepared = dict(await self._store.read_prepared(send_id, [m.position for m in members]))
            except Exception as error:  # noqa: BLE001 - sin lo preparado van por el camino de siempre
                logger.warning("bulk_send_prepared_unreadable send=%s error=%s", send_id, type(error).__name__)
                prepared = {}
            try:
                vetoed = dict(await self._vetoes(run.send, members, frozenset(prepared)))
            except Exception as error:  # noqa: BLE001 - sin los vetos de la tanda no se le manda a nadie
                run.veto_failures += 1
                logger.warning("bulk_send_vetoes_failed send=%s n=%s error=%s", send_id, run.veto_failures,
                               type(error).__name__)
                if run.veto_failures >= VETO_FAILURES_TO_BRAKE:
                    raise _Stop(await self._brake(run.send, "vetos_no_se_leen", notify=True)) from None
                await self._sleep(self._config.veto_retry_seconds)
                continue
            run.veto_failures = 0
            for member in members:
                await self._one(run, template, member, vetoed.get(member.position), prepared.get(member.position),
                                pilot=pilot)
                done += 1
                run.processed += 1

    async def _unprepared_slot(self, run: _Run) -> None:
        while True:
            now = self._now()
            while run.unprepared_at and run.unprepared_at[0] <= now - timedelta(minutes=1):
                run.unprepared_at.popleft()
            if len(run.unprepared_at) < self._config.unprepared_per_minute:
                run.unprepared_at.append(now)
                return
            wait = (run.unprepared_at[0] + timedelta(minutes=1) - now).total_seconds()
            await self._sleep(max(0.2, wait))

    async def _close(self, run: _Run, position: int, outcome: dict[str, Any]) -> None:
        result = await self._store.close(run.send.send_id, position, outcome)
        if not result.get("ok"):
            logger.warning("bulk_send_close_refused send=%s position=%s reason=%s", run.send.send_id, position,
                           result.get("reason_code"))
        run.note(str(outcome["state"]) + ("" if outcome.get("reason") is None else f"_{outcome['reason']}"))

    async def _one(self, run: _Run, template: BulkTemplate, member: BatchMember, veto: str | None,
                   prepared: PreparedConversation | None, *, pilot: bool) -> None:
        send_id = run.send.send_id
        reserved = await self._store.reserve(send_id, member.position)
        if not reserved.get("ok"):
            if reserved.get("reason_code") == "not_sending":
                raise _Stop(f"estado_{reserved.get('state')}")
            run.note(f"no_reservado_{reserved.get('reason_code')}")
            return
        phone = str(reserved.get("phone") or member.phone)
        first_name = reserved.get("first_name") if isinstance(reserved.get("first_name"), str) else member.first_name
        if veto is not None:
            await self._close(run, member.position, {"state": "vetado", "reason": machine_reason(f"veto:{veto}")})
            return
        started = self._now()
        values = template_values(run.send.template_variables, first_name)
        content = render_body(template.body, values)
        params = template_params(template, values, run.send.header_image_url)
        delivery_id = f"bulk:{send_id}:{member.position}"
        result: FirstTouchResult | None = None
        how = "camino_de_siempre"
        if prepared is not None:
            try:
                ok, why = await self._revalidate(prepared, phone, int(started.timestamp()))
                run.call_failures.append(False)
            except Exception:  # noqa: BLE001 - la revalidacion que falla manda por el camino de siempre
                ok, why = False, "prep_error"
                run.call_failures.append(True)
            if why.startswith("veto_"):
                await self._close(run, member.position, {"state": "vetado", "reason": machine_reason(f"veto:{why[5:]}"),
                                                         "chatwoot_conversation_id": prepared.conversation_id})
                return
            if ok:
                how = "preparada"
                result = await self._guarded(self._sender.send_first_touch_to_conversation(
                    conversation_id=prepared.conversation_id, phone=phone, buyer_name=first_name or "",
                    content=content, delivery_id=delivery_id, template_params=params))
        if result is None:
            await self._unprepared_slot(run)
            result = await self._guarded(self._sender.send_first_touch(
                phone=phone, buyer_name=first_name, buyer_email=None, content=content, delivery_id=delivery_id,
                template_params=params))
        outcome = self._outcome(result, how)
        run.call_failures.append(result.status == "failed")
        await self._close(run, member.position, outcome)
        if outcome["state"] == "enviado":
            assert result.conversation_id is not None and result.message_id is not None
            run.tracked[result.message_id] = _Tracked(result.conversation_id, result.message_id, self._now(), pilot)
            run.since_health += 1
            wait = 1.0 / run.rate - (self._now() - started).total_seconds()
            if wait > 0:
                await self._sleep(wait)

    @staticmethod
    async def _guarded(call: Awaitable[FirstTouchResult]) -> FirstTouchResult:
        try:
            return await call
        except Exception as error:  # noqa: BLE001 - no se sabe si salio: queda dudoso
            return FirstTouchResult("failed", None, None, f"excepcion_{type(error).__name__.lower()}")

    @staticmethod
    def _outcome(result: FirstTouchResult, how: str) -> dict[str, Any]:
        """``enviado`` con el mensaje; ``vetado`` si G3 lo freno; ``fallido`` si se freno antes de tocar
        Chatwoot; ``dudoso`` si fallo con Chatwoot de por medio (pudo haber salido: no se reintenta)."""
        conversation = result.conversation_id
        base: dict[str, Any] = {} if conversation is None else {"chatwoot_conversation_id": conversation}
        if result.status == "sent":
            if result.message_id is None:
                return {**base, "state": "dudoso", "reason": "enviado_sin_id"}
            return {**base, "state": "enviado", "chatwoot_message_id": result.message_id, "reason": how}
        reason = result.reason or "sin_motivo"
        if result.status == "blocked":
            if reason in _SENDER_VETOES:
                return {**base, "state": "vetado", "reason": machine_reason(f"veto:{_SENDER_VETOES[reason]}")}
            return {**base, "state": "fallido", "reason": machine_reason(f"bloqueado:{reason}")}
        return {**base, "state": "dudoso", "reason": machine_reason(f"fallo:{reason}")}

    # -- el bucle ------------------------------------------------------------

    async def start(self) -> None:
        if self._task is None:
            self._stopped.clear()
            self._task = asyncio.create_task(self._loop(), name="bulk_send_worker")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - parar no levanta
                pass
            self._task = None
        self._lock.release()

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.tick()
                if self._state == STATE_FAILING:
                    self._state = STATE_OK
            except Exception as error:  # noqa: BLE001 - el bucle no muere por una pasada
                self._state = STATE_FAILING
                logger.warning("bulk_send_tick_failed error=%s", type(error).__name__)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._config.refresh_seconds)
            except asyncio.TimeoutError:
                pass


__all__ = [
    "BatchMember", "BulkSendConfigError", "BulkSendSnapshot", "BulkSendWorker", "BulkSendWorkerConfig",
    "BulkTemplate", "ChatwootBatchVetoSource", "ChatwootDeliveryStatusReader", "MetaPortfolioHealthReader",
    "MetaTemplateResolver", "PortfolioLock", "SendWindow", "SlackBulkBrakeNotifier", "assert_not_retried",
    "batch_vetoes", "brake_notice", "in_send_hours", "machine_reason", "parse_portfolio_numbers",
    "render_body", "send_window", "snapshot_of", "template_params", "template_values", "within_send_hours",
]
