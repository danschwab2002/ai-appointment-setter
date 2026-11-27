"""La pagina de un masivo manejado por el AE (encargo 5 del mapa del 2026-11-25, piezas 3 y 6).

Mapa: ``OS/productos/soporte-infoproductores/documentacion/2026-11-25-mapa-masivo-por-el-ae.md``,
«3. Como llega el archivo», «6. La vista previa y la aprobacion» y las decisiones 1, 3, 5, 6, 7,
10, 11 y 12. La base: ``20261125000200_bulk_send_store_v1.sql`` (las funciones ``approver_*``).

Una seccion de la pagina de aprobacion (``register_flow_approval_routes(extra_routes=...)``),
con la misma sesion de «Entrar con Slack», la misma lista de aprobadores (decision 7: suben y
confirman los aprobadores de flujos; confirmar el propio envio esta permitido) y el mismo
CSRF. Solo existe con ``BULK_SEND_ENABLED``: el que registra estas rutas lo hace detras del
flag, y sin el no hay ninguna.

El AE arma el borrador (encargo 6) y manda el link ``/flow-approval/masivos/<id>``. Desde ahi:

* ``GET  /flow-approval/masivos/{id}``: la vista previa. La plantilla con su categoria, sus
  botones y la imagen (peso y que se pueda bajar); la lista: cuantas personas van, cuantas
  quedaron afuera y por que; cinco ejemplos enmascarados con el mensaje armado; cuanto dura
  (D = personas / ritmo) y a que hora arranca y termina en UTC, Argentina, Colombia y CDMX,
  con la ventana centrada en la hora pedida (decision 11); si la campana con la etiqueta esta
  vigente; la salud de los numeros del portfolio (decision 6). Ningun telefono entero.
* ``POST /flow-approval/masivos/{id}/subir``: la lista (CSV, Excel o JSON) y, opcional, las
  bajas de GHL en un segundo archivo (decision 4). Lee el cuerpo crudo ``multipart/form-data``
  con su propio tope (5 MB por defecto), sin pasar por ``_bounded_form`` (4.096 bytes). El
  archivo crudo no se guarda: la base recibe las filas ya normalizadas y la huella sha256. La
  persona marca de donde sale la lista y que tiene el aviso previo (decision 3). Despues pasa
  la lista por los vetos (``reviewer``, encargo 4): sin vetos aplicados no se puede confirmar.
  Volver a subir el mismo archivo (misma huella) vuelve a pasar los vetos, que es como se
  reintenta si una fuente no contesto.
* ``POST /flow-approval/masivos/{id}/no-validos.csv``: el CSV de las filas que no sirvieron,
  con el telefono como estaba escrito. Como el archivo no se guarda, la persona vuelve a
  elegir el mismo archivo y lo baja; la pagina no guarda nada.
* ``POST /flow-approval/masivos/{id}/confirmar``: **el si de una persona**. El numero exacto de
  personas que van y «Confirmo»; con el numero que manda en amarillo, la segunda marca
  (decision 5). La salud la lee la pagina en ese momento, no la manda el formulario. En rojo,
  ``BLOCKED`` o sin lectura no confirma.
* ``POST /flow-approval/masivos/{id}/frenar``: frenar a mano. Un frenado no sale nunca mas.
* ``GET  /flow-approval/masivos/{id}/no-entregados.csv``: los que no recibieron, con el
  telefono entero (los aprobadores son quienes pueden verlo, ``bulk_send_report``).

Con ``BULK_SEND_WARMUP_ENABLED`` (F2, ``bulk_send_warmup``), la vista previa suma «El calentamiento del
numero» (el tope del dia de la hora pedida, si entra y desde cuando se puede la proxima tanda grande) y
confirmar se niega si el masivo no entra (``warmup_blocks``), o si el calentamiento no se pudo leer
(``warmup_unreadable``). Apagado, la pagina es la de siempre.

Nada de esto manda un mensaje: preparar y mandar son del motor (encargos 7 y 9).
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response

from .bulk_import import DEFAULT_MAX_BYTES, DUPLICATE_REASON, BulkImportError, BulkListImport, read_bulk_list
from .bulk_send_brakes import Verdict, portfolio_start_verdict
from .bulk_send_report import BulkSendReport, page_view, undelivered_csv
from .flow_approval import (
    _HMAC,
    PREFIX,
    PageTools,
    SessionInvalid,
    StoreRejected,
    StoreUnavailable,
    _canonical_uuid,
    _chip,
    _done_from_query,
    _esc,
    _hidden,
    _login,
    _page,
    _same_origin,
    _sha,
    _shell,
)
from .flow_audience_sweeper import HEALTH_MAX_AGE
from .flow_validator import FOLLOWUP_OPT_OUT_BUTTON
from .lead_first_name import TEMPLATE_NEUTRAL_GREETING

__all__ = [
    "PATH",
    "BulkListReviewer",
    "BulkSendReportSource",
    "ImageCheck",
    "ListMember",
    "country_code_for_time_zone",
    "routes",
    "send_window",
]

logger = logging.getLogger(__name__)

PATH = PREFIX + "/masivos"
TITLE = "Masivo"
# Lo que pesa una subida aparte del archivo: los campos del formulario y los bordes del multipart.
_MULTIPART_OVERHEAD = 64 * 1024
_MULTIPART_MAX_PARTS = 8
_UPLOAD_FIELDS = {"csrf_token", "origen", "nota", "aviso", "archivo", "bajas"}
_UPLOAD_FILES = {"archivo", "bajas"}
_SOURCE_NOTE_MAX = 280
_PILOT_COUNT = 20  # decision 12: la tanda piloto
_PILOT_WAIT = timedelta(minutes=5)
# El horario permitido (decision 8, masivo.py HORAS_PERMITIDAS): 09:00 a 20:59 de CDMX, en horas UTC.
ALLOWED_HOURS_UTC = frozenset(tuple(range(15, 24)) + (0, 1, 2))
# Las zonas que se muestran (pieza 6).
ZONES: tuple[tuple[str, str], ...] = (
    ("UTC", "UTC"),
    ("Argentina", "America/Argentina/Buenos_Aires"),
    ("Colombia", "America/Bogota"),
    ("CDMX", "America/Mexico_City"),
)
# El pais de la instancia, por su zona (el manifiesto no declara el codigo de pais).
_COUNTRY_BY_ZONE_PREFIX = (
    ("America/Bogota", "57"),
    ("America/Mexico_City", "52"),
    ("America/Argentina/", "54"),
)
# La imagen del encabezado: lo que acepta Meta (masivo.py, probar_imagen) y lo que conviene (el mapa).
_IMAGE_TYPES = ("image/png", "image/jpeg")
_IMAGE_MAX_BYTES = 5 * 1024 * 1024
_IMAGE_LIGHT_BYTES = 300 * 1024
_IMAGE_CACHE_SECONDS = 600.0
_SAFE_CSV_START = re.compile(r"^[=+\-@\t\r]")

_STATE = {
    "borrador": ("Esperando la lista y el sí", "chip--info"),
    "confirmado": ("Confirmado, por salir", "chip--ok"),
    "preparando": ("Preparando", "chip--ok"),
    "enviando": ("Saliendo", "chip--ok"),
    "terminado": ("Terminado", None),
    "frenado": ("Frenado", "chip--warn"),
}
_SOURCES = {
    "inscritas_ghl": "Inscritas de GHL a las que se les avisó que les íbamos a escribir",
    "compradoras": "Compradoras",
    "otra": "Otra (contá de dónde en la nota)",
}
_REJECTED = {
    "vacio": "sin teléfono",
    "sin_digitos": "el teléfono no tiene números",
    "sin_pais": "sin código de país",
    "co_no_movil": "de Colombia, pero no es un celular",
    "mx_largo": "de México, con un largo que no es el de un celular",
    "ar_largo": "de Argentina, con un largo que no es el de un celular",
    "largo": "con un largo que no es el de un teléfono",
    "no_movil": "no es un celular",
    DUPLICATE_REASON: "repetidos (queda la primera fila)",
}
_EXCLUDED = {
    "baja": "pidieron no recibir más",
    "baja_ghl": "están en las bajas de GHL",
    "compro": "ya compraron",
    "clienta": "son clientas",
    "con_equipo": "están conversando con el equipo",
    "bloqueado": "están bloqueados en Chatwoot",
    "escribio_hace_poco": "escribieron en las últimas 6 horas",
}
_BRAKE = {"a_mano": "lo frenó una persona", "lista_purgada": "se borraron los teléfonos de la lista"}
# F2: un masivo que el motor freno al arrancar porque no entraba en el calentamiento del numero.
_WARMUP_BRAKE = "calentamiento:"
_WARMUP_BRAKE_TEXT = ("no entraba en el calentamiento del número al arrancar (el tope del día o el espaciado "
                      "entre tandas grandes)")
_IMPORT_ERRORS = {
    "vacio": "El archivo está vacío.",
    "demasiado_grande": "El archivo pesa más de lo que se puede subir.",
    "formato_desconocido": "No es un CSV, un Excel (.xlsx) ni un JSON.",
    "codificacion": "No se pudo leer el texto del archivo (la codificación no es UTF-8 ni la de Excel).",
    "json_invalido": "El JSON no se puede leer.",
    "json_no_es_lista": "El JSON tiene que ser una lista de objetos, una persona por objeto.",
    "xlsx_invalido": "El Excel no se puede leer.",
    "sin_filas": "El archivo no tiene filas debajo de los encabezados.",
    "demasiadas_filas": "El archivo tiene demasiadas filas.",
    "sin_columna_telefono": "No se encontró la columna del teléfono.",
    "columna_inexistente": "No existe esa columna.",
}
_DONE = {
    "lista_revisada": ("Lista subida y revisada contra las bajas, las compras y las conversaciones. "
                       "Revisá abajo cuántas van y confirmá."),
    "lista_sin_revisar": ("La lista quedó subida, pero la revisión contra las bajas y las compras todavía no "
                          "está instalada en esta instancia: no se puede confirmar."),
    "revision_fallida": ("La lista quedó subida, pero no se pudo revisar contra las bajas, las compras y las "
                         "conversaciones (alguna fuente no contestó). Volvé a subir el mismo archivo para reintentar."),
    "confirmado": "Confirmado. Sale a la hora pedida, al ritmo del envío, y se frena solo si empeora la salud.",
    "frenado": "Frenado. Este masivo no sale más.",
    "count_mismatch": "El número que escribiste no es el de las personas que van. No se confirmó.",
    "confirmation_missing": "Falta marcar «Confirmo». No se confirmó.",
    "yellow_ack_missing": "El número está en amarillo: falta la segunda marca. No se confirmó.",
    "health_blocks": "La salud de los números no deja arrancar. No se confirmó.",
    "warmup_blocks": ("Este masivo no entra en el calentamiento del número ese día: mirá abajo «El calentamiento "
                      "del número», con el tope y desde cuándo se puede. No se confirmó."),
    "warmup_unreadable": "No se pudo leer el calentamiento del número. Probá en un rato. No se confirmó.",
    "list_missing": "Todavía no hay lista. Subila arriba.",
    "list_not_vetted": "La lista todavía no pasó la revisión. Volvé a subir el mismo archivo.",
    "nobody_to_send": "No queda nadie a quien mandarle.",
    "over_cap": "La lista tiene más personas que el tope del envío.",
    "not_waiting_for_confirmation": "Este masivo ya no espera confirmación.",
    "not_a_draft": "Este masivo ya no acepta una lista.",
    "list_already_attached": "Este masivo ya tiene otra lista. Para mandar otra, pedile un masivo nuevo al AE.",
    "notice_not_declared": "Falta marcar que tenés el aviso previo. No se subió.",
    "empty_list": "Ninguna fila del archivo sirve. No se subió.",
    "not_brakeable": "Este masivo ya terminó o ya estaba frenado.",
    "bulk_list_duplicate_phone": "La lista trae un teléfono repetido. No se subió.",
    "bulk_list_invalid": "La lista no tiene la forma que espera la base. No se subió.",
}


# ---------------------------------------------------------------------------
# Lo que la pagina usa de afuera (por interfaz: vetos, reporte, imagen).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ListMember:
    """Una persona de la lista recien subida: su posicion en ``bulk_list_members`` y su telefono en
    forma de envio. Es lo que necesita ``bulk_send_vetoes.VetoMember`` (encargo 4)."""

    position: int
    phone: str


class BulkListReviewer(Protocol):
    """Los vetos de la lista (encargo 4, ``bulk_send_vetoes.apply_list_vetoes``): arma las exclusiones de
    la lista entera y las deja en la base con ``apply_bulk_list_vetoes_v1`` (que la deja ``revisada``).

    ``ghl_opt_out_phones`` son las bajas de GHL que la persona subio aparte (decision 4). Si una fuente
    no se puede leer entera, levanta una excepcion y no escribe nada (falla cerrado).
    """

    async def review(self, *, send_id: str, list_id: str, members: Sequence[ListMember],
                     ghl_opt_out_phones: Sequence[str]) -> Mapping[str, Any]: ...


class BulkSendReportSource(Protocol):
    """El reporte del envio (encargo 10, ``bulk_send_report.build_report`` sobre el registro del motor)."""

    async def report(self, send_id: str) -> BulkSendReport: ...


@dataclass(frozen=True)
class ImageCheck:
    """Lo que se sabe de la imagen del encabezado: ``problem`` en castellano si no sirve."""

    ok: bool
    size: int | None = None
    content_type: str | None = None
    problem: str | None = None


async def probe_image(url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> ImageCheck:
    """Baja la imagen (hasta 5 MB + 1 byte) como ``probar_imagen`` de masivo.py: https, 200, png o jpeg."""
    if not re.fullmatch(r"https://\S+", url or ""):
        return ImageCheck(False, problem="la imagen tiene que ir con https://")
    try:
        async with httpx.AsyncClient(transport=transport, timeout=15.0, follow_redirects=True) as client:
            async with client.stream("GET", url, headers={"User-Agent": "bulk-send-page/1"}) as response:
                if response.status_code != 200:
                    return ImageCheck(False, problem=f"no se pudo bajar (HTTP {response.status_code})")
                kind = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > _IMAGE_MAX_BYTES:
                        break
    except httpx.HTTPError as error:
        return ImageCheck(False, problem=f"no se pudo bajar ({type(error).__name__})")
    if kind not in _IMAGE_TYPES:
        return ImageCheck(False, size=size, content_type=kind or None,
                          problem=f"es {kind or 'de un tipo desconocido'}: Meta acepta png o jpeg")
    if size > _IMAGE_MAX_BYTES:
        return ImageCheck(False, size=size, content_type=kind, problem="pesa más de 5 MB")
    return ImageCheck(True, size=size, content_type=kind)


def country_code_for_time_zone(time_zone: str | None) -> str | None:
    """El codigo de pais de la instancia por su zona: Colombia 57, CDMX 52, Argentina 54; si no, None
    (entonces solo sirve un telefono que ya trae su codigo, ``normalize_bulk_phone``)."""
    for prefix, code in _COUNTRY_BY_ZONE_PREFIX:
        if time_zone and (time_zone == prefix or (prefix.endswith("/") and time_zone.startswith(prefix))):
            return code
    return None


# ---------------------------------------------------------------------------
# Las cuentas de la vista previa.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SendWindow:
    start: datetime
    end: datetime
    seconds: float
    pilot_wait: timedelta
    inside_hours: bool


def send_window(count: int, rate_millis: int, scheduled_at: datetime, *, window_mode: str) -> SendWindow:
    """D = personas / ritmo (masivo.py ``ventana_centrada``). Centrada: arranca D/2 antes de la hora
    y termina D/2 despues (decision 11); ``desde_la_hora``: arranca a la hora. Con mas de 20 personas
    se suma la espera de la tanda piloto al fin (decision 12). ``inside_hours``: los dos extremos
    dentro de 09:00 a 20:59 de CDMX y menos de 12 h (``dentro_de_horario``)."""
    rate = max(rate_millis, 1) / 1000.0
    seconds = max(count, 0) / rate
    pilot = _PILOT_WAIT if count > _PILOT_COUNT else timedelta(0)
    span = timedelta(seconds=seconds)
    if window_mode == "desde_la_hora":
        start = scheduled_at
    else:
        start = scheduled_at - span / 2
    end = start + span + pilot
    start_utc, end_utc = start.astimezone(UTC), end.astimezone(UTC)
    inside = (start_utc.hour in ALLOWED_HOURS_UTC and end_utc.hour in ALLOWED_HOURS_UTC
              and end - start < timedelta(hours=12))
    return SendWindow(start, end, seconds, pilot, inside)


def _duration_text(seconds: float) -> str:
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {secs} s" if secs else f"{minutes} min"
    return f"{secs} s"


def _moment(value: datetime) -> str:
    parts = []
    for label, zone in ZONES:
        local = value.astimezone(ZoneInfo(zone))
        parts.append(f"{label} {local:%d/%m %H:%M}")
    return " · ".join(parts)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


# ---------------------------------------------------------------------------
# La salud del portfolio (decisiones 5 y 6).
# ---------------------------------------------------------------------------

def _readings(raw: Mapping[str, Any] | None, keys: Sequence[str], now: datetime) -> dict[str, Mapping[str, Any] | None]:
    """Las lecturas de ``estado.json`` (``meta_salud.py``) de cada numero del portfolio. Una lectura sin
    hora o de mas de 26 horas no cuenta: queda como ilegible y no deja arrancar."""
    out: dict[str, Mapping[str, Any] | None] = {}
    for key in keys:
        reading = raw.get(key) if isinstance(raw, Mapping) else None
        if not isinstance(reading, Mapping):
            out[key] = None
            continue
        read_at = _parse_time(reading.get("leido_en"))
        if read_at is None:
            out[key] = {**reading, "error": "sin_hora"}
        elif now - read_at > HEALTH_MAX_AGE:
            out[key] = {**reading, "error": "vieja"}
        else:
            out[key] = reading
    return out


def _health_verdict(readings: Mapping[str, Mapping[str, Any] | None], sending: str, *,
                    yellow_confirmed: bool) -> Verdict:
    present = {key: value for key, value in readings.items() if value is not None}
    missing = [key for key, value in readings.items() if value is None]
    if missing:
        return Verdict(False, f"salud:{missing[0]}:sin_lectura", f"no hay lectura de {missing[0]}")
    return portfolio_start_verdict(sending, present, yellow_confirmed=yellow_confirmed)


def _own_health(readings: Mapping[str, Mapping[str, Any] | None], sending: str) -> str | None:
    """Lo que la pagina le pasa a la base: ``verde`` o ``amarillo`` del numero que manda."""
    reading = readings.get(sending)
    quality = reading.get("calidad") if isinstance(reading, Mapping) else None
    return {"GREEN": "verde", "YELLOW": "amarillo"}.get(str(quality or "").upper())


_QUALITY = {"GREEN": ("verde", "chip--ok"), "YELLOW": ("amarillo", "chip--warn"), "RED": ("rojo", "chip--warn")}


def _health_html(readings: Mapping[str, Mapping[str, Any] | None], sending: str, verdict: Verdict,
                 now: datetime) -> str:
    rows = []
    for key in readings:
        reading = readings[key]
        who = f"{key} (manda este masivo)" if key == sending else key
        if reading is None:
            rows.append(f"<li><strong>{_esc(who)}</strong>: {_chip('sin lectura', 'chip--warn')}</li>")
            continue
        if reading.get("error") == "vieja":
            rows.append(f"<li><strong>{_esc(who)}</strong>: {_chip('lectura de más de 26 horas', 'chip--warn')}</li>")
            continue
        if reading.get("error"):
            rows.append(f"<li><strong>{_esc(who)}</strong>: {_chip('lectura sin hora', 'chip--warn')}</li>")
            continue
        quality = str(reading.get("calidad") or "").upper()
        label, chip = _QUALITY.get(quality, (quality or "?", "chip--warn"))
        extra = []
        if reading.get("limite"):
            extra.append(f"límite {reading.get('limite')}")
        state = reading.get("estado_numero", reading.get("estado"))
        if state:
            extra.append(f"número {state}")
        if reading.get("puede_enviar"):
            extra.append(f"cuenta {reading.get('puede_enviar')}")
        read_at = _parse_time(reading.get("leido_en"))
        if read_at is not None:
            extra.append(f"leída hace {_duration_text((now - read_at).total_seconds())}")
        rows.append(f"<li><strong>{_esc(who)}</strong>: {_chip(label, chip)} {_esc(' · '.join(extra))}</li>")
    if verdict.allows:
        text = ("El número está en amarillo: para confirmar hace falta la segunda marca."
                if verdict.reason and "amarillo" in verdict.reason else "La salud deja arrancar.")
    elif verdict.reason and verdict.reason.endswith("amarillo_sin_confirmar"):
        text = "El número que manda está en amarillo: para confirmar hace falta la segunda marca."
    else:
        text = f"La salud no deja arrancar: {verdict.detail or verdict.reason}."
    return (f"<ul>{''.join(rows)}</ul><p>{_esc(text)}</p>"
            "<p class=\"meta\">En el medio del envío frena solo si la salud empeora en cualquiera de estos números.</p>")


# ---------------------------------------------------------------------------
# La plantilla.
# ---------------------------------------------------------------------------

def _template_of(catalog: object, name: str, language: str) -> Mapping[str, Any] | None:
    if not isinstance(catalog, Mapping) or not isinstance(catalog.get("message_templates"), list):
        return None
    for template in catalog["message_templates"]:
        if isinstance(template, Mapping) and template.get("name") == name and template.get("language") == language:
            return template
    return None


def _components(template: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    return [c for c in template.get("components") or () if isinstance(c, Mapping) and c.get("type") == kind]


def _has_opt_out(template: Mapping[str, Any]) -> bool:
    from .opt_out import detect_explicit_opt_out

    for component in _components(template, "BUTTONS"):
        for button in component.get("buttons") or ():
            if not isinstance(button, Mapping) or button.get("type") != "QUICK_REPLY":
                continue
            text = button.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 25:
                continue
            if text == FOLLOWUP_OPT_OUT_BUTTON or detect_explicit_opt_out([text]) is not None:
                return True
    return False


def _body(template: Mapping[str, Any] | None) -> str | None:
    if template is None:
        return None
    for component in _components(template, "BODY"):
        if isinstance(component.get("text"), str):
            return component["text"]
    return None


def _render_message(body: str, variables: Sequence[object], initial: str | None) -> str:
    """El cuerpo con las variables puestas. ``{"de": "nombre"}`` va con el primer nombre de la persona; la
    pagina solo tiene la inicial, y sin nombre va el saludo neutro (masivo.py ``valores``)."""
    values: dict[str, str] = {}
    for index, variable in enumerate(variables, 1):
        if isinstance(variable, Mapping) and variable.get("de") == "nombre":
            values[str(index)] = initial if initial else TEMPLATE_NEUTRAL_GREETING
        else:
            values[str(index)] = str(variable)
    return re.sub(r"\{\{\s*(\d+)\s*\}\}", lambda m: values.get(m.group(1), m.group(0)), body)


def _template_html(template: Mapping[str, Any] | None, view: Mapping[str, Any], image: ImageCheck | None) -> str:
    name, language = view.get("template_name"), view.get("template_language")
    head = f"<p><strong>{_esc(name)}</strong> ({_esc(language)})"
    if template is None:
        return head + "</p><p>No se pudo leer la plantilla del catálogo de la bandeja (o ya no está).</p>"
    status = str(template.get("status") or "")
    head += f" · {_esc(template.get('category') or '?')} · " + _chip(status or "?", "chip--ok" if status == "APPROVED"
                                                                       else "chip--warn") + "</p>"
    lines = [head]
    headers = _components(template, "HEADER")
    header_format = str(headers[0].get("format") or "TEXT") if headers else None
    buttons = [b for c in _components(template, "BUTTONS") for b in c.get("buttons") or () if isinstance(b, Mapping)]
    if buttons:
        items = "".join(f"<li>{_esc(b.get('text'))} ({_esc(b.get('type'))})</li>" for b in buttons)
        lines.append(f"<p>Botones:</p><ul>{items}</ul>")
    else:
        lines.append("<p>Sin botones.</p>")
    if not _has_opt_out(template):
        lines.append('<div class="notice notice--warn"><p>La plantilla no tiene la respuesta rápida «No más '
                     "mensajes»: un masivo no sale sin ella.</p></div>")
    url = view.get("header_image_url")
    if header_format == "IMAGE" or url:
        if not url:
            lines.append('<div class="notice notice--warn"><p>La plantilla lleva imagen y el masivo no tiene el link '
                         "de la imagen.</p></div>")
        elif image is None:
            lines.append(f"<p>Imagen: {_esc(url)} (no se comprobó).</p>")
        elif image.ok:
            weight = f"{(image.size or 0) / 1024:.0f} KB"
            note = (" Pesa más de 300 KB: conviene una más liviana, Meta la baja por cada persona."
                    if (image.size or 0) > _IMAGE_LIGHT_BYTES else "")
            lines.append(f"<p>Imagen: {_esc(url)} · {_esc(image.content_type)} · {weight}, se puede bajar.{_esc(note)}</p>")
        else:
            lines.append(f'<div class="notice notice--warn"><p>Imagen: {_esc(url)}: {_esc(image.problem)}.</p></div>')
    return "".join(lines)


# ---------------------------------------------------------------------------
# La pagina.
# ---------------------------------------------------------------------------

def _counts_text(counts: object, labels: Mapping[str, str]) -> list[str]:
    if not isinstance(counts, Mapping):
        return []
    parts = []
    for reason, n in sorted(counts.items(), key=lambda item: (-int(item[1]) if _count(item[1]) else 0, item[0])):
        if _count(n):
            parts.append(f"{int(n)} {labels.get(str(reason), f'otro motivo ({reason})')}")
    return parts


def _done_html(done: Mapping[str, str]) -> str:
    code = done.get("hecho")
    if not code:
        return ""
    text = _DONE.get(code, "No se pudo completar. Probá de nuevo o avisá al equipo.")
    kind = " notice--ok" if code in ("lista_revisada", "confirmado") else ""
    return f'<div class="notice{kind}"><p>{_esc(text)}</p></div>'


def _upload_form(send_id: str, csrf: str, *, again: bool) -> str:
    sources = "".join(
        f'<label><input type="radio" name="origen" value="{key}" required> {_esc(text)}</label><br>'
        for key, text in _SOURCES.items())
    intro = ("Volvé a elegir el mismo archivo: se vuelve a revisar contra las bajas, las compras y las conversaciones."
             if again else "CSV, Excel (.xlsx) o JSON, hasta 5 MB. Se normalizan los teléfonos con el país de la "
             "instancia. El archivo no se guarda: quedan solo las filas que sirven.")
    return (f'<form method="post" action="{PATH}/{send_id}/subir" enctype="multipart/form-data">'
            f'{_hidden("csrf_token", csrf)}<p>{_esc(intro)}</p>'
            '<p><label>La lista: <input type="file" name="archivo" required '
            'accept=".csv,.txt,.xlsx,.json,text/csv,application/json"></label></p>'
            '<p><label>Las bajas de GHL (opcional, otro archivo con una columna de teléfono): '
            '<input type="file" name="bajas" accept=".csv,.txt,.xlsx,.json,text/csv,application/json"></label></p>'
            f"<p>¿De dónde sale la lista?</p><p>{sources}</p>"
            f'<p><label>Nota (opcional, hasta {_SOURCE_NOTE_MAX} caracteres): '
            f'<input type="text" name="nota" maxlength="{_SOURCE_NOTE_MAX}"></label></p>'
            '<p><label><input type="checkbox" name="aviso" value="si" required> A estas personas se les avisó que les '
            "íbamos a escribir por WhatsApp, y saqué a quienes pidieron no recibir más.</label></p>"
            f'<button type="submit" class="primary">{"Volver a revisar" if again else "Subir la lista"}</button></form>')


def _invalid_form(send_id: str, csrf: str) -> str:
    return (f'<form method="post" action="{PATH}/{send_id}/no-validos.csv" enctype="multipart/form-data">'
            f'{_hidden("csrf_token", csrf)}'
            '<p><label>Para bajar las filas que no sirvieron, elegí de nuevo el mismo archivo: '
            '<input type="file" name="archivo" required accept=".csv,.txt,.xlsx,.json,text/csv,application/json">'
            "</label></p><button type=\"submit\">Bajar las que no sirvieron (CSV)</button></form>")


def render_send(view: Mapping[str, Any], *, csrf: str, done: Mapping[str, str], template: Mapping[str, Any] | None,
                image: ImageCheck | None, readings: Mapping[str, Mapping[str, Any] | None], sending: str,
                campaign: str | None, report: Mapping[str, Any] | None, has_reviewer: bool,
                now: datetime, warmup: Mapping[str, Any] | None = None) -> str:
    """La vista previa de un masivo. Sin telefonos enteros: los ejemplos vienen enmascarados de la base."""
    send_id = str(view.get("send_id") or "")
    state = str(view.get("state") or "")
    label, chip = _STATE.get(state, (state, None))
    lst = view.get("list") if isinstance(view.get("list"), Mapping) else None
    eligible = _count(lst.get("eligible_count")) if lst else None
    vetted = bool(lst and lst.get("state") == "revisada")
    parts = [f'<header class="page-header"><p class="meta"><a href="{PREFIX}/">Aprobación de flujos</a></p>'
             f"<h1>Masivo: {_esc(view.get('template_name'))}</h1><p>{_chip(label, chip)}</p></header>",
             _done_html(done)]
    if state == "frenado":
        reason = str(view.get("brake_reason") or "")
        text = _WARMUP_BRAKE_TEXT if reason.startswith(_WARMUP_BRAKE) else _BRAKE.get(reason, reason)
        parts.append(f'<div class="notice notice--warn"><p>Frenado: {_esc(text)}.</p></div>')

    parts.append('<section class="panel"><div class="panel-heading"><h2>La plantilla</h2></div>'
                 f'<div class="panel-body">{_template_html(template, view, image)}</div></section>')

    # La lista.
    body = []
    if lst is None:
        if state == "borrador":
            body.append(_upload_form(send_id, csrf, again=False))
        else:
            body.append("<p>Este masivo no tiene lista.</p>")
    else:
        total_rejected = sum(int(n) for n in (lst.get("rejected_counts") or {}).values() if _count(n))
        members = _count(lst.get("member_count")) or 0
        body.append(f"<p>Subida por {_esc(lst.get('uploaded_by'))}, "
                    f"{_esc(_SOURCES.get(str(lst.get('source_kind')), lst.get('source_kind')))}"
                    + (f" ({_esc(lst.get('source_note'))})" if lst.get("source_note") else "")
                    + ". Tiene el aviso previo.</p>")
        body.append(f"<p>Del archivo sirvieron <strong>{members}</strong> personas; no sirvieron "
                    f"<strong>{total_rejected}</strong> filas.</p>")
        rejected = _counts_text(lst.get("rejected_counts"), _REJECTED)
        if rejected:
            body.append(f"<p>No sirvieron: {_esc('; '.join(rejected))}.</p>")
        if vetted and eligible is not None:
            body.append(f'<p class="count">Van: <strong>{eligible}</strong> personas.</p>')
            excluded = _counts_text(lst.get("excluded_by_reason"), _EXCLUDED)
            if excluded:
                body.append(f"<p>Quedaron afuera {members - eligible}: {_esc('; '.join(excluded))}.</p>")
        elif lst.get("state") == "purgada":
            body.append("<p>Los teléfonos de esta lista ya se borraron; quedan los conteos.</p>")
        else:
            body.append('<div class="notice notice--warn"><p>La lista todavía no pasó la revisión contra las bajas, '
                        "las compras y las conversaciones: no se puede confirmar.</p></div>")
            if state == "borrador" and has_reviewer:
                body.append(_upload_form(send_id, csrf, again=True))
        if total_rejected and state != "frenado":
            body.append(_invalid_form(send_id, csrf))
    parts.append('<section class="panel"><div class="panel-heading"><h2>La lista</h2>'
                 "<p>Subir la lista no manda nada: después ves cuántas van y confirmás.</p></div>"
                 f'<div class="panel-body">{"".join(body)}</div></section>')

    # Los ejemplos con el mensaje armado.
    examples = [e for e in (lst.get("examples") if lst else None) or [] if isinstance(e, Mapping)][:5]
    message = _body(template)
    variables = view.get("template_variables") if isinstance(view.get("template_variables"), list) else []
    if examples:
        items = []
        for example in examples:
            initial = example.get("initial") if isinstance(example.get("initial"), str) else None
            text = _render_message(message, variables, initial) if message else "(sin el cuerpo de la plantilla)"
            items.append(f"<li><p><strong>{_esc(example.get('phone'))}</strong> {_esc(initial or 'sin nombre')}</p>"
                         f'<p class="meta">{_esc(text)}</p></li>')
        parts.append('<section class="panel"><div class="panel-heading"><h2>Cinco ejemplos</h2>'
                     "<p>El teléfono, solo los últimos 4 números; el nombre va completo en el mensaje y acá se ve "
                     f"solo la inicial. Sin nombre va «{_esc(TEMPLATE_NEUTRAL_GREETING)}».</p></div>"
                     f'<div class="panel-body"><ul>{"".join(items)}</ul></div></section>')

    # Cuando sale.
    scheduled = _parse_time(view.get("scheduled_at"))
    rate_millis = _count(view.get("rate_millis")) or 0
    when = []
    if scheduled is not None and rate_millis:
        count = eligible if eligible is not None else (_count(lst.get("member_count")) if lst else None)
        when.append(f"<p>La hora pedida: {_esc(_moment(scheduled))}.</p>")
        when.append(f"<p>Ritmo: {rate_millis / 1000:.1f} por segundo como máximo"
                    + (", con la ventana centrada en la hora pedida." if view.get("window_mode") != "desde_la_hora"
                       else ", desde la hora pedida.") + "</p>")
        if count:
            window = send_window(count, rate_millis, scheduled, window_mode=str(view.get("window_mode") or ""))
            pilot = (f", más {int(window.pilot_wait.total_seconds() // 60)} minutos de la tanda piloto de "
                     f"{_PILOT_COUNT}" if window.pilot_wait else "")
            when.append(f"<p>Dura {_esc(_duration_text(window.seconds))} ({count} personas a ese ritmo{pilot}).</p>")
            when.append(f"<p>Arranca: {_esc(_moment(window.start))}.</p><p>Termina: {_esc(_moment(window.end))}.</p>")
            if not window.inside_hours:
                when.append('<div class="notice notice--warn"><p>La ventana sale del horario permitido (09:00 a 20:59 '
                            "de CDMX): lo que caiga afuera no sale.</p></div>")
            if count > (_count(view.get("recipient_cap")) or 0):
                when.append('<div class="notice notice--warn"><p>Son más personas que el tope del envío '
                            f"({_count(view.get('recipient_cap'))}).</p></div>")
        else:
            when.append("<p>La duración se calcula con la lista.</p>")
    if campaign:
        when.append(f"<p>{_esc(campaign)}</p>")
    parts.append('<section class="panel"><div class="panel-heading"><h2>Cuándo sale</h2></div>'
                 f'<div class="panel-body">{"".join(when) or "<p>Sin hora.</p>"}</div></section>')

    # La salud.
    verdict = _health_verdict(readings, sending, yellow_confirmed=False)
    parts.append('<section class="panel"><div class="panel-heading"><h2>La salud de los números</h2>'
                 "<p>Los números que comparten el portfolio de Meta.</p></div>"
                 f'<div class="panel-body">{_health_html(readings, sending, verdict, now)}</div></section>')

    if warmup is not None:
        parts.append('<section class="panel"><div class="panel-heading"><h2>El calentamiento del número</h2>'
                     "<p>Un número nuevo en masivos sube de a escalones: una tanda grande por día y más grande solo "
                     "si la anterior salió bien.</p></div>"
                     f'<div class="panel-body">{_warmup_html(warmup)}</div></section>')

    # Confirmar y frenar.
    actions = []
    if state == "borrador" and vetted and eligible:
        yellow = bool(verdict.reason and verdict.reason.endswith("amarillo_sin_confirmar"))
        actions.append(
            f'<form method="post" action="{PATH}/{send_id}/confirmar" class="confirm">{_hidden("csrf_token", csrf)}'
            f'<label>Para confirmar, escribí cuántas personas van ({eligible}):'
            ' <input type="number" name="conteo" min="0" max="20000" required inputmode="numeric"></label>'
            '<label><input type="checkbox" name="confirmo" value="si"> Confirmo que este mensaje sale a esas personas, '
            "a la hora y al ritmo de arriba.</label>"
            + ('<label><input type="checkbox" name="amarillo" value="si"> El número está en amarillo, lo mando '
               "igual.</label>" if yellow else "")
            + '<button type="submit" class="primary">Confirmar el masivo</button></form>')
    if state in ("borrador", "confirmado", "preparando", "enviando"):
        actions.append(f'<form method="post" action="{PATH}/{send_id}/frenar">{_hidden("csrf_token", csrf)}'
                       '<button type="submit">Frenar</button></form>')
    if actions:
        parts.append('<section class="panel"><div class="panel-heading"><h2>Confirmar</h2>'
                     "<p>El AE nunca confirma ni frena: lo hace un aprobador acá.</p></div>"
                     f'<div class="panel-body">{"".join(actions)}</div></section>')

    if report is not None:
        failed = "; ".join(f"{f['count']} {f['explanation']}" for f in report.get("failed_by_code") or [])
        parts.append(
            '<section class="panel"><div class="panel-heading"><h2>Cómo salió</h2></div><div class="panel-body">'
            f"<p>Mandados {report.get('sent')}, entregados {report.get('delivered')} (leídos {report.get('read')}), "
            f"fallidos {report.get('failed')}, sin confirmar todavía {report.get('awaiting_delivery')}.</p>"
            + (f"<p>Fallidos: {_esc(failed)}.</p>" if failed else "")
            + f"<p>Respuestas {report.get('replies')}, bajas {report.get('opt_outs')}, derivadas "
            f"{report.get('handoffs')}, compras {report.get('purchases')}.</p>"
            + (f'<p><a class="button" href="{PATH}/{send_id}/no-entregados.csv">Bajar los {report.get("undelivered_count")} '
               "que no recibieron (CSV)</a></p>" if report.get("undelivered_count") else "")
            + "</div></section>")
    return "".join(parts)


def _warmup_html(warmup: Mapping[str, Any]) -> str:
    """La seccion del calentamiento (F2): el texto lo arma ``bulk_send_warmup``, el mismo que ve el AE."""
    if warmup.get("error"):
        return ('<div class="notice notice--warn"><p>No se pudo leer el calentamiento del número: no se puede '
                "confirmar. Probá en un rato.</p></div>")
    steps = " · ".join(f"{n:,}".replace(",", ".") for n in warmup.get("escalones") or [] if isinstance(n, int))
    out = [f"<p>Escalones: {_esc(steps)}. Ese día ({_esc(warmup.get('dia'))}) el tope de una tanda grande es "
           f"<strong>{_esc(warmup.get('tope_del_dia'))}</strong> personas (escalón {_esc(warmup.get('escalon'))}).</p>",
           f"<p>{_esc(warmup.get('detalle'))}</p>"]
    mine = warmup.get("este_masivo")
    if isinstance(mine, Mapping):
        kind = "notice--ok" if mine.get("entra") else "notice--warn"
        out.append(f'<div class="notice {kind}"><p>{_esc(mine.get("detalle"))}</p></div>')
    return "".join(out)


# ---------------------------------------------------------------------------
# El cuerpo crudo de una subida.
# ---------------------------------------------------------------------------

class UploadInvalid(ValueError):
    pass


class UploadTooLarge(ValueError):
    pass


@dataclass(frozen=True)
class _Part:
    value: bytes
    filename: str | None


async def _read_multipart(request: Request, *, max_bytes: int) -> dict[str, _Part]:
    """Un ``multipart/form-data`` acotado: el cuerpo entero con su tope, partes con nombre, sin repetidos."""
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "multipart/form-data" or "boundary=" not in content_type:
        raise UploadInvalid("content_type")
    limit = max_bytes + _MULTIPART_OVERHEAD
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        raise UploadTooLarge()
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise UploadTooLarge()
        chunks.append(chunk)
    try:
        header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1")
    except UnicodeEncodeError as error:
        raise UploadInvalid("content_type") from error
    message = BytesParser(policy=policy.HTTP).parsebytes(header + b"".join(chunks))
    if not message.is_multipart():
        raise UploadInvalid("not_multipart")
    parts: dict[str, _Part] = {}
    for index, part in enumerate(message.iter_parts()):
        if index >= _MULTIPART_MAX_PARTS:
            raise UploadInvalid("too_many_parts")
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str) or name not in _UPLOAD_FIELDS or name in parts:
            raise UploadInvalid("field")
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            payload = b""
        filename = part.get_filename()
        parts[name] = _Part(payload, filename if isinstance(filename, str) else None)
    return parts


def _text(parts: Mapping[str, _Part], name: str) -> str | None:
    part = parts.get(name)
    if part is None:
        return None
    try:
        return part.value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UploadInvalid("text") from error


def _csv_cell(value: str) -> str:
    # Una celda que arranca con = + - @ la lee la planilla como formula.
    return "'" + value if _SAFE_CSV_START.match(value) else value


def _invalid_rows_csv(imported: BulkListImport) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(("fila", "telefono_como_estaba", "motivo"))
    for rejection in imported.rejections:
        writer.writerow((rejection.row, _csv_cell(rejection.raw_phone),
                         _REJECTED.get(rejection.reason, rejection.reason)))
    return buffer.getvalue()


def _csv_response(text: str, filename: str) -> Response:
    return Response(content=("﻿" + text).encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------------------
# Las rutas.
# ---------------------------------------------------------------------------

def routes(*, chatwoot: Any, inbox_id: int, country_code: str | None, sending_key: str,
           portfolio_keys: Sequence[str], health: Callable[[], Mapping[str, Any] | None],
           reviewer: BulkListReviewer | None = None, report: BulkSendReportSource | None = None,
           campaigns: Callable[[], Sequence[Any]] | None = None,
           image_probe: Callable[[str], Awaitable[ImageCheck]] | None = probe_image,
           max_upload_bytes: int = DEFAULT_MAX_BYTES,
           warmup: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]] | None = None,
           ) -> Callable[[FastAPI, PageTools], None]:
    """Las rutas, para ``register_flow_approval_routes(extra_routes=...)``. Se registran solo con
    ``BULK_SEND_ENABLED``.

    ``chatwoot`` e ``inbox_id``: el catalogo de plantillas de la bandeja (``get_inbox``).
    ``country_code``: el de la instancia, para normalizar los telefonos (``country_code_for_time_zone``).
    ``sending_key`` y ``portfolio_keys``: el numero que manda y todos los del portfolio (decision 6), como
    claves de ``estado.json``; ``health`` lo lee (``file_health_reader`` sin ``instance_key``).
    ``reviewer``: los vetos (encargo 4); sin el, la lista queda subida y no se puede confirmar.
    ``report``: el reporte del envio (encargo 10); sin el, la pagina no lo muestra.
    ``campaigns``: los segmentos de campana del bridge (``LiveSegmentCatalog.segments``), para decir si la
    etiqueta esta vigente; sin el, no se dice. ``image_probe``: baja la imagen; ``None`` no la baja.
    ``warmup``: el calentamiento del numero (``BulkSendWarmup.view_for``, F2); sin el (flag apagado) la
    pagina no lo muestra ni lo mira al confirmar.
    """
    keys = tuple(dict.fromkeys((sending_key, *portfolio_keys)))
    image_cache: dict[str, tuple[float, ImageCheck]] = {}

    def register(app: FastAPI, tools: PageTools) -> None:
        store, instance = tools.store, tools.instance

        async def read(session_hash: str, send_id: str) -> dict[str, Any]:
            return await store._rpc("approver_read_bulk_send_v1", {  # noqa: SLF001 - la misma tienda de la pagina
                "p_instance": instance, "p_session_hash": session_hash, "p_send_id": send_id})

        def readings() -> dict[str, Mapping[str, Any] | None]:
            try:
                raw = health()
            except Exception as error:  # noqa: BLE001 - sin lectura no arranca, y la pagina lo dice
                logger.warning("bulk_send_page health=unreadable error=%s", type(error).__name__)
                raw = None
            return _readings(raw, keys, tools.now())

        async def template_of(view: Mapping[str, Any]) -> Mapping[str, Any] | None:
            try:
                catalog = await chatwoot.get_inbox(inbox_id=inbox_id)
            except Exception as error:  # noqa: BLE001 - la pagina dice que no pudo leer la plantilla
                logger.warning("bulk_send_page template_catalog=unavailable error=%s", type(error).__name__)
                return None
            return _template_of(catalog, str(view.get("template_name")), str(view.get("template_language")))

        async def image_of(view: Mapping[str, Any]) -> ImageCheck | None:
            url = view.get("header_image_url")
            if image_probe is None or not isinstance(url, str) or not url:
                return None
            cached = image_cache.get(url)
            if cached is not None and time.monotonic() - cached[0] < _IMAGE_CACHE_SECONDS:
                return cached[1]
            try:
                checked = await image_probe(url)
            except Exception as error:  # noqa: BLE001
                checked = ImageCheck(False, problem=f"no se pudo comprobar ({type(error).__name__})")
            image_cache[url] = (time.monotonic(), checked)
            return checked

        def campaign_of(view: Mapping[str, Any]) -> str | None:
            label = view.get("campaign_label")
            if not label:
                return "Sin etiqueta de campaña: las respuestas entran al agente sin campaña."
            if campaigns is None:
                return f"Etiqueta de campaña: {label} (no se pudo comprobar si está vigente)."
            scheduled = _parse_time(view.get("scheduled_at"))
            try:
                found = [s for s in campaigns() if getattr(s, "label", None) == label]
            except Exception:  # noqa: BLE001
                return f"Etiqueta de campaña: {label} (no se pudo leer el catálogo de campañas)."
            if not found:
                return f"No hay una campaña con la etiqueta {label}: el agente no va a saber de qué se trata."
            if scheduled is None:
                return f"Campaña {label}."
            states = {s.state_at(scheduled) for s in found}
            if "vigente" in states:
                return f"La campaña {label} está vigente a la hora pedida."
            return f"La campaña {label} no está vigente a la hora pedida: el agente no la va a usar."

        async def report_of(view: Mapping[str, Any]) -> Mapping[str, Any] | None:
            if report is None or not view.get("started_at"):
                return None
            try:
                built = await report.report(str(view["send_id"]))
            except Exception as error:  # noqa: BLE001 - sin reporte, la pagina no lo muestra
                logger.warning("bulk_send_page report=unavailable error=%s", type(error).__name__)
                return None
            return page_view(built, viewer_can_see_phones=True)

        async def warmup_of(view: Mapping[str, Any]) -> Mapping[str, Any] | None:
            if warmup is None:
                return None
            try:
                return await warmup(view)
            except Exception as error:  # noqa: BLE001 - la pagina dice que no pudo leerlo y no confirma
                logger.warning("bulk_send_page warmup=unavailable error=%s", type(error).__name__)
                return {"error": "unavailable"}

        def not_found(title: str) -> Response:
            return _page(title, "No existe ese masivo en esta instancia.", 404)

        @app.get(PATH + "/{send_id}")
        async def bulk_send_page(send_id: str, request: Request) -> Response:
            if not _canonical_uuid(send_id):
                return not_found(TITLE)
            who = await tools.who(request)
            if who is None:
                return tools.logged_out(_login(None))
            user, secret = who
            try:
                view = await read(_sha(secret), send_id)
            except SessionInvalid:
                return tools.logged_out(_login(None))
            except StoreRejected as rejected:
                if rejected.code == "bulk_send_not_found":
                    return not_found(TITLE)
                return _page(TITLE, "El almacén no contestó. Probá en un rato.", 503)
            except StoreUnavailable:
                logger.info("flow_approval route=bulk_send status=503")
                return _page(TITLE, "El almacén no contestó. Probá en un rato.", 503)
            csrf = tools.config.csrf_token(secret)
            body = render_send(
                view, csrf=csrf, done=_done_from_query(request, tools.config, secret),
                template=await template_of(view), image=await image_of(view), readings=readings(),
                sending=sending_key, campaign=campaign_of(view), report=await report_of(view),
                has_reviewer=reviewer is not None, now=tools.now(), warmup=await warmup_of(view))
            return HTMLResponse(_shell(TITLE, body, user=user, csrf=csrf, show_user_id=False))

        async def upload_gate(request: Request, send_id: str, title: str
                              ) -> tuple[str, str, dict[str, _Part]] | Response:
            """Origen, sesion, cuerpo acotado y CSRF (lo de ``write_form``, para un multipart)."""
            if not _canonical_uuid(send_id):
                return not_found(title)
            if not _same_origin(request, tools.config.origin):
                return _page(title, "Origen no autorizado.", 403)
            who = await tools.who(request)
            if who is None:
                return tools.logged_out(_login(None))
            user, secret = who
            try:
                parts = await _read_multipart(request, max_bytes=max_upload_bytes)
            except UploadTooLarge:
                return _page(title, "El archivo pesa más de 5 MB.", 413)
            except (UploadInvalid, ValueError, UnicodeDecodeError):
                return _page(title, "Formulario inválido.", 422)
            try:
                token = _text(parts, "csrf_token")
            except UploadInvalid:
                token = None
            if not token or not _HMAC.fullmatch(token) or not hmac.compare_digest(
                    token, tools.config.csrf_token(secret)):
                return _page(title, "Formulario inválido.", 403)
            if "archivo" not in parts or not parts["archivo"].value:
                return _page(title, "Falta el archivo.", 422)
            return user, secret, parts

        def import_list(part: _Part, title: str, *, collect: bool = False) -> BulkListImport | Response:
            try:
                return read_bulk_list(part.value, country_code=country_code, filename=part.filename,
                                      max_bytes=max_upload_bytes, collect_rejections=collect)
            except BulkImportError as error:
                text = _IMPORT_ERRORS.get(error.code, "No se pudo leer el archivo.")
                if error.headers:
                    text += " Los encabezados del archivo: " + ", ".join(error.headers) + "."
                logger.info("flow_approval route=bulk_send_upload import_error=%s", error.code)
                return _page(title, text, 422)

        @app.post(PATH + "/{send_id}/subir")
        async def bulk_send_upload(send_id: str, request: Request) -> Response:
            title = "Subir la lista"
            gate = await upload_gate(request, send_id, title)
            if isinstance(gate, Response):
                return gate
            user, secret, parts = gate
            page = f"{PATH}/{send_id}"
            try:
                source = _text(parts, "origen")
                note = (_text(parts, "nota") or "").strip()
                notice = _text(parts, "aviso")
            except UploadInvalid:
                return _page(title, "Formulario inválido.", 422)
            if source not in _SOURCES or len(note) > _SOURCE_NOTE_MAX or any(not c.isprintable() for c in note):
                return _page(title, "Formulario inválido.", 422)
            if source == "otra" and not note:
                return _page(title, "Con «Otra», contá en la nota de dónde sale la lista.", 422)
            if notice != "si":
                return tools.back(secret, page, hecho="notice_not_declared")
            imported = import_list(parts["archivo"], title)
            if isinstance(imported, Response):
                return imported
            if not imported.members:
                return tools.back(secret, page, hecho="empty_list")
            ghl: list[str] = []
            if "bajas" in parts and parts["bajas"].value:
                opt_outs = import_list(parts["bajas"], title)
                if isinstance(opt_outs, Response):
                    return opt_outs
                ghl = [m.phone for m in opt_outs.members]
            file_sha = hashlib.sha256(parts["archivo"].value).hexdigest()
            session = _sha(secret)
            try:
                view = await read(session, send_id)
                attached = view.get("list") if isinstance(view.get("list"), Mapping) else None
                if view.get("state") != "borrador":
                    return tools.back(secret, page, hecho="not_a_draft")
                if attached is None:
                    rejected = dict(imported.rejected)
                    if imported.duplicates:
                        rejected[DUPLICATE_REASON] = imported.duplicates
                    created = await store._rpc("approver_create_bulk_list_v1", {  # noqa: SLF001
                        "p_instance": instance, "p_session_hash": session, "p_send_id": send_id,
                        "p_list": {"file_sha256": file_sha, "source_kind": source, "source_note": note or None,
                                   "notice_declared": True, "rejected_counts": rejected,
                                   "rows": [{"phone": m.phone, "first_name": m.first_name, "email": m.email}
                                            for m in imported.members]}})
                    if created.get("ok") is not True:
                        return tools.back(secret, page, hecho=str(created.get("reason_code") or "bulk_list_invalid"))
                    list_id = str(created.get("list_id") or "")
                elif (attached.get("file_sha256") == file_sha and attached.get("state") in ("cargada", "revisada")
                      and attached.get("member_count") == len(imported.members)):
                    list_id = str(attached.get("list_id") or "")
                else:
                    return tools.back(secret, page, hecho="list_already_attached")
            except SessionInvalid:
                return tools.logged_out(_login(None))
            except StoreRejected as rejected_error:
                if rejected_error.code == "bulk_send_not_found":
                    return not_found(title)
                return tools.back(secret, page, hecho=rejected_error.code)
            except StoreUnavailable:
                return _page(title, "El almacén no contestó. Probá en un rato.", 503)
            logger.info("flow_approval route=bulk_send_upload send=%s list=%s members=%s rejected=%s",
                        send_id, list_id, len(imported.members), sum(imported.rejected.values()) + imported.duplicates)
            if reviewer is None or not _canonical_uuid(list_id):
                return tools.back(secret, page, hecho="lista_sin_revisar")
            members = [ListMember(position, m.phone) for position, m in enumerate(imported.members)]
            try:
                reviewed = await reviewer.review(send_id=send_id, list_id=list_id, members=members,
                                                 ghl_opt_out_phones=ghl)
            except Exception as error:  # noqa: BLE001 - falla cerrado: la lista queda sin revisar
                logger.warning("flow_approval route=bulk_send_review send=%s error=%s", send_id, type(error).__name__)
                return tools.back(secret, page, hecho="revision_fallida")
            if reviewed.get("ok") is not True:
                return tools.back(secret, page, hecho=str(reviewed.get("reason_code") or "revision_fallida"))
            return tools.back(secret, page, hecho="lista_revisada")

        @app.post(PATH + "/{send_id}/no-validos.csv")
        async def bulk_send_invalid_rows(send_id: str, request: Request) -> Response:
            title = "Las filas que no sirvieron"
            gate = await upload_gate(request, send_id, title)
            if isinstance(gate, Response):
                return gate
            _, secret, parts = gate
            try:
                await read(_sha(secret), send_id)
            except SessionInvalid:
                return tools.logged_out(_login(None))
            except StoreRejected:
                return not_found(title)
            except StoreUnavailable:
                return _page(title, "El almacén no contestó. Probá en un rato.", 503)
            imported = import_list(parts["archivo"], title, collect=True)
            if isinstance(imported, Response):
                return imported
            logger.info("flow_approval route=bulk_send_invalid_rows send=%s rows=%s", send_id, len(imported.rejections))
            return _csv_response(_invalid_rows_csv(imported), "no-validos.csv")

        async def write(request: Request, send_id: str, *, title: str, fields: set[str], optional: frozenset[str],
                        call: Callable[[str, dict[str, str]], Awaitable[Mapping[str, Any]]], ok_code: str) -> Response:
            if not _canonical_uuid(send_id):
                return not_found(title)
            checked = await tools.write_form(request, title=title, fields=fields, optional=optional)
            if isinstance(checked, Response):
                return checked
            _, secret, form = checked
            page = f"{PATH}/{send_id}"
            try:
                result = await call(_sha(secret), form)
            except SessionInvalid:
                return tools.logged_out(_login(None))
            except StoreRejected as rejected:
                if rejected.code == "bulk_send_not_found":
                    return not_found(title)
                return tools.back(secret, page, hecho=rejected.code)
            except StoreUnavailable:
                return _page(title, "El almacén no contestó. Probá en un rato.", 503)
            code = ok_code if result.get("ok") is True else str(result.get("reason_code") or "bulk_send_final")
            logger.info("flow_approval route=bulk_send_write send=%s title=%s result=%s", send_id, title, code)
            return tools.back(secret, page, hecho=code)

        @app.post(PATH + "/{send_id}/confirmar")
        async def bulk_send_confirm(send_id: str, request: Request) -> Response:
            async def call(session: str, form: dict[str, str]) -> Mapping[str, Any]:
                raw = form["conteo"].strip()
                if not raw.isdigit() or len(raw) > 5:
                    return {"ok": False, "reason_code": "count_mismatch"}
                if form.get("confirmo") != "si":
                    return {"ok": False, "reason_code": "confirmation_missing"}
                # La salud se lee ahora, en el servidor: el formulario no la manda (decision 5).
                current = readings()
                yellow_ack = form.get("amarillo") == "si"
                verdict = _health_verdict(current, sending_key, yellow_confirmed=yellow_ack)
                if not verdict.allows:
                    blocked = bool(verdict.reason and verdict.reason.endswith("amarillo_sin_confirmar"))
                    logger.info("flow_approval route=bulk_send_confirm send=%s health=%s", send_id, verdict.reason)
                    return {"ok": False, "reason_code": "yellow_ack_missing" if blocked else "health_blocks"}
                if warmup is not None:
                    # F2: el masivo tiene que entrar en el calentamiento del numero el dia de la hora pedida.
                    warm = await warmup_of(await read(session, send_id))
                    mine = warm.get("este_masivo") if warm is not None else None
                    if warm is None or warm.get("error"):
                        return {"ok": False, "reason_code": "warmup_unreadable"}
                    if not isinstance(mine, Mapping) or mine.get("entra") is not True:
                        logger.info("flow_approval route=bulk_send_confirm send=%s warmup=%s", send_id,
                                    mine.get("motivo") if isinstance(mine, Mapping) else "sin_tamano")
                        return {"ok": False, "reason_code": "warmup_blocks"}
                return await store._rpc("approver_confirm_bulk_send_v1", {  # noqa: SLF001
                    "p_instance": instance, "p_session_hash": session, "p_send_id": send_id,
                    "p_expected_count": int(raw), "p_confirmed": True,
                    "p_health": _own_health(current, sending_key), "p_yellow_ack": yellow_ack})

            return await write(request, send_id, title="Confirmar el masivo", fields={"csrf_token", "conteo"},
                               optional=frozenset({"confirmo", "amarillo"}), call=call, ok_code="confirmado")

        @app.post(PATH + "/{send_id}/frenar")
        async def bulk_send_brake(send_id: str, request: Request) -> Response:
            async def call(session: str, _: dict[str, str]) -> Mapping[str, Any]:
                return await store._rpc("approver_brake_bulk_send_v1", {  # noqa: SLF001
                    "p_instance": instance, "p_session_hash": session, "p_send_id": send_id})

            return await write(request, send_id, title="Frenar", fields={"csrf_token"}, optional=frozenset(),
                               call=call, ok_code="frenado")

        @app.get(PATH + "/{send_id}/no-entregados.csv")
        async def bulk_send_undelivered(send_id: str, request: Request) -> Response:
            title = "Los que no recibieron"
            if not _canonical_uuid(send_id):
                return not_found(title)
            who = await tools.who(request)
            if who is None:
                return tools.logged_out(_login(None))
            _, secret = who
            try:
                view = await read(_sha(secret), send_id)
            except SessionInvalid:
                return tools.logged_out(_login(None))
            except StoreRejected:
                return not_found(title)
            except StoreUnavailable:
                return _page(title, "El almacén no contestó. Probá en un rato.", 503)
            if report is None or not view.get("started_at"):
                return _page(title, "Este masivo todavía no tiene reporte.", 404)
            try:
                built = await report.report(send_id)
            except Exception as error:  # noqa: BLE001
                logger.warning("bulk_send_page report=unavailable error=%s", type(error).__name__)
                return _page(title, "No se pudo leer el reporte. Probá en un rato.", 503)
            logger.info("flow_approval route=bulk_send_undelivered send=%s", send_id)
            return _csv_response(undelivered_csv(built, viewer_can_see_phones=True), "no-entregados.csv")

    return register
