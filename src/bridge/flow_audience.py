"""El disparo por audiencia: ``dispara.fecha``, ``dispara.cada`` y ``dispara.manual`` (E-484, H6).

Diseno: ``coordinacion/etapa-1/mapa-ae-todos-los-flujos.md`` §2 y el hito H6 del vault;
contrato: ``docs/contracts/flow-audience-trigger-v1.md``.

Los tres disparadores nuevos no nacen de una persona, nacen de un momento, y el
«a quien» es una audiencia (la clave nueva ``para``). Un solo mecanismo con tres
formas:

* ``fecha``: una vez, a una hora de una zona (``{el: 2026-12-01T10:00, zona: ...}``);
* ``cada``: recurrente, ciertos dias de la semana a una hora (``{dias: [lun, jue],
  hora: "10:00", zona: ..., desde: 2026-12-01, hasta: 2026-12-31}``); una vez por dia
  como mucho, que es el intervalo minimo de 24 h del mapa;
* ``manual``: cuando una persona lo pide. El AE nunca dispara (D7): una persona
  confirma en la pagina con el conteo exacto y un segundo paso.

Cada ocurrencia materializa la audiencia en una lista congelada
(``flow_audience_occurrences`` + ``flow_audience_members``, 20261112220000),
idempotente por flujo, version y ocurrencia, y la suelta al ritmo por hora
(``ritmo``). Solo recibe quien tiene permiso: ``formulario`` (dejo el formulario con
la aclaracion de WhatsApp en la ventana ``formulario_en``) o, para una clienta,
``compra`` con el ``ok_del_cliente`` del item (D6, que ya exige el ciclo). La baja
manda siempre.

Este modulo es puro: la forma (lo llama la regla 1 de ``flow_definition``), la
lectura de la definicion, las ocurrencias que vencen y el filtro de la audiencia.
No lee la base, ni Chatwoot, ni el reloj: todo entra por argumento.

Los valores por defecto marcados ``DEFAULT_REVISAR_DAN`` son los del mapa (D8, D10)
y los tiene que revisar Dan.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

AUDIENCE_FORMS = ("fecha", "cada", "manual")
WEEKDAYS = ("lun", "mar", "mie", "jue", "vie", "sab", "dom")
# El estado de cliente de la persona (customer-status-v1): prospecta o clienta.
AUDIENCE_STATUSES = ("prospecta", "clienta")
# El permiso que corresponde a cada estado (D6): una prospecta solo con el formulario
# con permiso; una clienta solo con compra (y el ok_del_cliente del ciclo, regla 17).
PERMISSION_BY_STATUS: Mapping[str, str] = {"prospecta": "formulario", "clienta": "compra"}
PARA_KEYS = ("es", "etiquetas", "producto", "formulario_en", "excluye")
RITMO_KEYS = ("por_hora", "por_ocurrencia")
FECHA_KEYS = ("el", "zona")
CADA_KEYS = ("dias", "hora", "zona", "desde", "hasta")

# DEFAULT_REVISAR_DAN (D10): 200 por hora y 1.000 por ocurrencia; el cliente los baja.
# Sin verificar contra los limites actuales de Meta para un numero nuevo (mapa §2.3).
DEFAULT_PER_HOUR = 200
DEFAULT_PER_OCCURRENCE = 1000
# Los techos que el esquema acepta: mas que esto no es un envio a una audiencia con
# permiso, es una campana masiva, y no entra en la v1.
MAX_PER_HOUR = 1000
MAX_PER_OCCURRENCE = 5000
# DEFAULT_REVISAR_DAN: «dejo el formulario con permiso en los ultimos 30 dias».
DEFAULT_FORM_WINDOW = "30d"
FORM_WINDOW_MAX_DAYS = 365
# Cuanto despues de su hora todavia se materializa una ocurrencia de fecha o cada.
# Un bridge caido mas que esto no manda tarde: la ocurrencia queda como perdida.
OCCURRENCE_GRACE = timedelta(hours=6)
MAX_LABELS = 20

_LABEL = re.compile(r"[a-z0-9][a-z0-9_-]{0,254}")
_PRODUCT = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")
_HOUR = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
_LOCAL_DATETIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T([01][0-9]|2[0-3]):[0-5][0-9]")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DAYS = re.compile(r"([1-9][0-9]{0,2})d")

PROBLEM_UNKNOWN = "unknown"
PROBLEM_INVALID = "invalid"
PROBLEM_MISSING = "missing"


@dataclass(frozen=True)
class Problem:
    """Un problema de forma: ``unknown`` (clave que no va), ``invalid`` o ``missing``.

    ``flow_definition`` lo traduce a ``unknown_key``, ``invalid_value`` o
    ``required_key_missing`` de la regla 1; aca no se importa para no hacer un ciclo.
    """

    kind: str
    path: tuple[str | int, ...]
    reason: str | None = None


# ---------------------------------------------------------------------------
# La forma (regla 1).
# ---------------------------------------------------------------------------

def audience_form(trigger: object) -> str | None:
    """``fecha``, ``cada`` o ``manual`` si ``dispara`` trae exactamente esa clave y ninguna otra."""
    if not isinstance(trigger, Mapping) or len(trigger) != 1:
        return None
    (key,) = tuple(trigger)
    return key if key in AUDIENCE_FORMS else None


def _zone_ok(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 64:
        return False
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _date_ok(value: object) -> bool:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _local_datetime_ok(value: object) -> bool:
    if not isinstance(value, str) or not _LOCAL_DATETIME.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _keys(out: list[Problem], value: Mapping[str, Any], path: tuple[str | int, ...],
          allowed: Sequence[str], required: Sequence[str] = ()) -> None:
    for key in value:
        if key not in allowed:
            out.append(Problem(PROBLEM_UNKNOWN, path + (key,)))
    for key in required:
        if key not in value:
            out.append(Problem(PROBLEM_MISSING, path + (key,)))


def check_audience_trigger(form: str, value: object) -> list[Problem]:
    """La forma de ``dispara.<form>`` para ``fecha``, ``cada`` o ``manual``."""
    path: tuple[str | int, ...] = ("dispara", form)
    out: list[Problem] = []
    if not isinstance(value, Mapping):
        out.append(Problem(PROBLEM_INVALID, path, f"{form} es un objeto."))
        return out
    if form == "manual":
        _keys(out, value, path, ())
        return out
    if form == "fecha":
        _keys(out, value, path, FECHA_KEYS, FECHA_KEYS)
        if "el" in value and not _local_datetime_ok(value["el"]):
            out.append(Problem(PROBLEM_INVALID, path + ("el",),
                               "el es una fecha y hora local, AAAA-MM-DDTHH:MM."))
    else:
        _keys(out, value, path, CADA_KEYS, ("dias", "hora", "zona"))
        days = value.get("dias")
        if "dias" in value:
            if not isinstance(days, list) or not days or len(days) > len(WEEKDAYS) or any(
                    not isinstance(day, str) or day not in WEEKDAYS for day in days) or len(set(days)) != len(days):
                out.append(Problem(PROBLEM_INVALID, path + ("dias",),
                                   "dias es una lista sin repetir de lun, mar, mie, jue, vie, sab y dom."))
        if "hora" in value and not (isinstance(value["hora"], str) and _HOUR.fullmatch(value["hora"])):
            out.append(Problem(PROBLEM_INVALID, path + ("hora",), "hora es HH:MM."))
        for key in ("desde", "hasta"):
            if key in value and not _date_ok(value[key]):
                out.append(Problem(PROBLEM_INVALID, path + (key,), f"{key} es una fecha AAAA-MM-DD."))
        if _date_ok(value.get("desde")) and _date_ok(value.get("hasta")) and value["desde"] > value["hasta"]:
            out.append(Problem(PROBLEM_INVALID, path + ("hasta",), "hasta no puede ser antes que desde."))
    if "zona" in value and not _zone_ok(value["zona"]):
        out.append(Problem(PROBLEM_INVALID, path + ("zona",), "zona es una zona horaria IANA."))
    return out


def form_window_days(value: object) -> int | None:
    """Los dias de ``formulario_en`` (``30d``), o None si no tiene la forma."""
    if not isinstance(value, str):
        return None
    match = _DAYS.fullmatch(value)
    if match is None:
        return None
    days = int(match.group(1))
    return days if 1 <= days <= FORM_WINDOW_MAX_DAYS else None


def _labels_ok(value: object) -> bool:
    return isinstance(value, list) and len(value) <= MAX_LABELS and len(set(map(str, value))) == len(value) and all(
        isinstance(label, str) and _LABEL.fullmatch(label) is not None for label in value)


def check_para(value: object) -> list[Problem]:
    """La forma de ``para``: la audiencia."""
    path: tuple[str | int, ...] = ("para",)
    out: list[Problem] = []
    if not isinstance(value, Mapping):
        out.append(Problem(PROBLEM_INVALID, path, "para es un objeto."))
        return out
    _keys(out, value, path, PARA_KEYS, ("es",))
    if "es" in value and value["es"] not in AUDIENCE_STATUSES:
        out.append(Problem(PROBLEM_INVALID, path + ("es",), "es es prospecta o clienta."))
    for key in ("etiquetas", "excluye"):
        if key in value and not _labels_ok(value[key]):
            out.append(Problem(PROBLEM_INVALID, path + (key,),
                               f"{key} es una lista de etiquetas de Chatwoot, hasta {MAX_LABELS}."))
    if "producto" in value and not (isinstance(value["producto"], str) and _PRODUCT.fullmatch(value["producto"])):
        out.append(Problem(PROBLEM_INVALID, path + ("producto",),
                           "El producto tiene la forma del manifiesto: minúsculas, dígitos o -, hasta 128."))
    if "formulario_en" in value and form_window_days(value["formulario_en"]) is None:
        out.append(Problem(PROBLEM_INVALID, path + ("formulario_en",),
                           f"formulario_en es una cantidad de días, como 30d, hasta {FORM_WINDOW_MAX_DAYS}d."))
    labels = value.get("etiquetas")
    excluded = value.get("excluye")
    if isinstance(labels, list) and isinstance(excluded, list) and set(map(str, labels)) & set(map(str, excluded)):
        out.append(Problem(PROBLEM_INVALID, path + ("excluye",), "Una etiqueta no puede pedirse y excluirse."))
    return out


def check_ritmo(value: object) -> list[Problem]:
    """La forma de ``ritmo``: cuantos por hora y cuantos por ocurrencia."""
    path: tuple[str | int, ...] = ("ritmo",)
    out: list[Problem] = []
    if not isinstance(value, Mapping):
        out.append(Problem(PROBLEM_INVALID, path, "ritmo es un objeto."))
        return out
    _keys(out, value, path, RITMO_KEYS)
    limits = {"por_hora": MAX_PER_HOUR, "por_ocurrencia": MAX_PER_OCCURRENCE}
    for key, ceiling in limits.items():
        if key in value:
            number = value[key]
            if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= ceiling:
                out.append(Problem(PROBLEM_INVALID, path + (key,), f"{key} es un entero de 1 a {ceiling}."))
    return out


# ---------------------------------------------------------------------------
# La lectura de una definicion publicada.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AudienceSpec:
    """Lo que el barredor saca de una definicion con disparo por audiencia."""

    form: str
    zone: str | None
    at_local: datetime | None              # fecha
    weekdays: tuple[int, ...]              # cada: 0 = lunes
    hour: time | None                      # cada
    since: date | None                     # cada
    until: date | None                     # cada
    status: str
    permission: str
    labels: tuple[str, ...]
    excluded: tuple[str, ...]
    product: str | None
    form_window_days: int
    per_hour: int
    per_occurrence: int
    fingerprint: str = field(compare=False)


class AudienceSpecInvalid(ValueError):
    """La definicion no es un disparo por audiencia que este modulo sepa leer."""


def audience_fingerprint(definition: Mapping[str, Any]) -> str:
    """La huella de la consulta: sha256 del ``para``, el permiso y el ``ritmo`` en JSON canonico."""
    entry = definition.get("entra")
    payload = {
        "para": definition.get("para"),
        "permiso": entry.get("permiso") if isinstance(entry, Mapping) else None,
        "ritmo": definition.get("ritmo"),
    }
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def audience_spec(definition: Mapping[str, Any]) -> AudienceSpec:
    """La configuracion de un disparo por audiencia; ``AudienceSpecInvalid`` si no lo es.

    Vuelve a mirar la forma aunque el validador ya la miro al publicar: el barredor
    solo corre una definicion que sabe leer entera.
    """
    trigger = definition.get("dispara")
    form = audience_form(trigger)
    if form is None:
        raise AudienceSpecInvalid("no_es_audiencia")
    assert isinstance(trigger, Mapping)
    body = trigger[form]
    para = definition.get("para")
    ritmo = definition.get("ritmo", {})
    problems = check_audience_trigger(form, body) + check_para(para) + check_ritmo(ritmo)
    if problems:
        raise AudienceSpecInvalid("forma")
    assert isinstance(body, Mapping) and isinstance(para, Mapping) and isinstance(ritmo, Mapping)
    entry = definition.get("entra")
    permission = entry.get("permiso") if isinstance(entry, Mapping) else None
    status = para["es"]
    if permission != PERMISSION_BY_STATUS[status]:
        raise AudienceSpecInvalid("permiso")
    zone = body.get("zona")
    at_local = datetime.fromisoformat(body["el"]) if form == "fecha" else None
    weekdays: tuple[int, ...] = ()
    hour = None
    since = until = None
    if form == "cada":
        weekdays = tuple(sorted(WEEKDAYS.index(day) for day in body["dias"]))
        hh, mm = body["hora"].split(":")
        hour = time(int(hh), int(mm))
        since = date.fromisoformat(body["desde"]) if "desde" in body else None
        until = date.fromisoformat(body["hasta"]) if "hasta" in body else None
    window = form_window_days(para.get("formulario_en", DEFAULT_FORM_WINDOW))
    assert window is not None
    per_hour = ritmo.get("por_hora", DEFAULT_PER_HOUR)
    per_occurrence = ritmo.get("por_ocurrencia", DEFAULT_PER_OCCURRENCE)
    return AudienceSpec(
        form=form, zone=zone, at_local=at_local, weekdays=weekdays, hour=hour, since=since, until=until,
        status=status, permission=permission, labels=tuple(para.get("etiquetas", ())),
        excluded=tuple(para.get("excluye", ())),
        # «Compro» es del producto del flujo, como en la lista y la admision de la base (el validador exige
        # para.producto igual al producto del flujo).
        product=para.get("producto", definition.get("producto")),
        form_window_days=window, per_hour=per_hour, per_occurrence=per_occurrence,
        fingerprint=audience_fingerprint(definition),
    )


# ---------------------------------------------------------------------------
# Las ocurrencias.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Occurrence:
    """Una ocurrencia que vencio: su clave (idempotencia) y su hora programada en UTC."""

    key: str
    scheduled_at: datetime


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now_must_be_aware")
    return now.astimezone(timezone.utc)


def due_occurrence(spec: AudienceSpec, *, now: datetime) -> Occurrence | None:
    """La ocurrencia que toca materializar ahora, o None.

    ``fecha``: la unica, entre su hora y su hora mas ``OCCURRENCE_GRACE``. ``cada``: la
    del dia local de hoy (o de ayer, si la gracia cruza la medianoche), en un dia
    pedido y dentro de ``desde``/``hasta``. ``manual``: nunca (la crea una persona).

    La clave es la idempotencia del lado de la base: la misma ocurrencia no se
    materializa dos veces aunque el bridge se reinicie.
    """
    current = _aware(now)
    if spec.form == "manual" or spec.zone is None:
        return None
    zone = ZoneInfo(spec.zone)
    if spec.form == "fecha":
        assert spec.at_local is not None
        scheduled = spec.at_local.replace(tzinfo=zone).astimezone(timezone.utc)
        if scheduled <= current < scheduled + OCCURRENCE_GRACE:
            return Occurrence(f"fecha:{scheduled.strftime('%Y-%m-%dT%H:%MZ')}", scheduled)
        return None
    assert spec.hour is not None
    local_today = current.astimezone(zone).date()
    for day in (local_today, local_today - timedelta(days=1)):
        if day.weekday() not in spec.weekdays:
            continue
        if spec.since is not None and day < spec.since:
            continue
        if spec.until is not None and day > spec.until:
            continue
        scheduled = datetime.combine(day, spec.hour, tzinfo=zone).astimezone(timezone.utc)
        if scheduled <= current < scheduled + OCCURRENCE_GRACE:
            return Occurrence(f"cada:{day.isoformat()}", scheduled)
    return None


# ---------------------------------------------------------------------------
# La audiencia.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """Una persona que la fuente de la audiencia propone, con lo que hace falta para filtrarla.

    ``form_at`` es la ultima vez que dejo el formulario con la aclaracion de WhatsApp
    (``purchase_intents.whatsapp_contact_authorized``); ``customer_products``, los
    productos que compro (customer-status-v1). Sin nombre, mail ni conversacion.
    """

    contact_id: str
    phone: str
    labels: frozenset[str]
    opted_out: bool
    form_at: datetime | None
    customer_products: frozenset[str]


OMIT_OPTED_OUT = "baja"
OMIT_NO_PERMISSION = "sin_permiso"
OMIT_LABELS = "etiquetas"
OMIT_EXCLUDED = "excluida"
OMIT_STATUS = "estado"
OMIT_DUPLICATE = "repetida"
OMIT_OVER_CAP = "tope_por_ocurrencia"


def select_members(spec: AudienceSpec, candidates: Iterable[Candidate], *, now: datetime
                   ) -> tuple[tuple[Candidate, ...], Mapping[str, int]]:
    """La lista congelada y lo que se dejo afuera, contado por motivo.

    El orden es el de la fuente; los primeros ``per_occurrence`` entran y el resto
    cuenta como ``tope_por_ocurrencia``. La baja manda siempre. La base vuelve a mirar
    la baja y el permiso del formulario al materializar (la segunda capa).
    """
    current = _aware(now)
    since = current - timedelta(days=spec.form_window_days)
    chosen: list[Candidate] = []
    seen: set[str] = set()
    omitted: dict[str, int] = {}

    def omit(reason: str) -> None:
        omitted[reason] = omitted.get(reason, 0) + 1

    for candidate in candidates:
        if candidate.contact_id in seen:
            omit(OMIT_DUPLICATE)
            continue
        seen.add(candidate.contact_id)
        if candidate.opted_out:
            omit(OMIT_OPTED_OUT)
            continue
        if spec.labels and not set(spec.labels) <= candidate.labels:
            omit(OMIT_LABELS)
            continue
        if spec.excluded and set(spec.excluded) & candidate.labels:
            omit(OMIT_EXCLUDED)
            continue
        is_customer = bool(candidate.customer_products) if spec.product is None else (
            spec.product in candidate.customer_products)
        if spec.status == "clienta":
            if not is_customer:
                omit(OMIT_STATUS)
                continue
        else:
            if is_customer:
                omit(OMIT_STATUS)
                continue
            form_at = candidate.form_at
            if form_at is None or _aware(form_at) < since:
                omit(OMIT_NO_PERMISSION)
                continue
        if len(chosen) >= spec.per_occurrence:
            omit(OMIT_OVER_CAP)
            continue
        chosen.append(candidate)
    return tuple(chosen), omitted
