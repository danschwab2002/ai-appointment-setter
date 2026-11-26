"""Exportar contactos y listas (contact-export-v1, funcion 1.3; pieza 4 del mapa de Z1 del 2026-11-26).

Contrato: ``docs/contracts/contact-export-v1.md``; la base: ``20261126300000_contact_export_v1.sql``.

Todo detras de ``CONTACT_EXPORT_ENABLED`` (apagado por defecto: sin el flag las rutas no existen y dan 404):

* ``POST {PREFIX}/contact-exports`` (el AE): valida los filtros, lee la base (y Chatwoot si hacen falta las
  etiquetas), cuenta y guarda el pedido con ``create_contact_export_v1``. Devuelve **solo el conteo y el link** de
  la pagina (D7, D12): ningun telefono, mail ni nombre, ni enmascarado.
* ``GET {PREFIX}/contact-exports/{id}`` (el AE): el estado del pedido (pendiente, bajado, vencido) y los conteos.
* ``GET /flow-approval/exportar/{id}`` (la pagina, con «Entrar con Slack»): los filtros, cuantas coincidian al
  pedirlo, cuando vence y el boton «Bajar el CSV».
* ``POST /flow-approval/exportar/{id}/csv`` (la pagina, con CSRF): vuelve a leer la base y las etiquetas, arma el
  CSV y recien despues lo marca bajado (``approver_mark_contact_export_downloaded_v1``). Se baja una vez, y solo
  dentro de los 30 minutos del pedido.

Exportar no manda nada a nadie. Los logs llevan el id del pedido y conteos; nunca un telefono, un mail ni un nombre.
"""

from __future__ import annotations

import asyncio
import csv
import hmac
import io
import logging
import os
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import phonenumbers
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response

from .bulk_send_page import _SAFE_CSV_START
from .flow_approval import PREFIX as APPROVAL_PREFIX
from .flow_approval import PageTools, SessionInvalid, _canonical_uuid, _chip, _esc, _hidden, _login, _page, _sha
from .flow_approval import StoreRejected as PageStoreRejected
from .flow_approval import StoreUnavailable as PageStoreUnavailable
from .flow_approval import _shell, _when
from .flow_audience_store import AudienceLabelsUnavailable, LabelIndex
from .flow_change_api import (
    CONTRACT,
    CONTRACT_VERSION,
    PREFIX,
    EntryRefused,
    FlowChangeStore,
    StoreRejected,
    StoreUnavailable,
    _body,
    _check_keys,
    _keys,
    _requested_by,
)
from .phones import canonical_whatsapp_phone, equivalent_whatsapp_phones

__all__ = [
    "CONTACT_EXPORT_ENV",
    "DEFAULT_COLUMNS",
    "EXPORTS",
    "PAGE_PATH",
    "ContactExportStore",
    "ExportFilters",
    "Person",
    "check_settings",
    "collect",
    "export_csv",
    "export_e164",
    "page_routes",
    "register_contact_export_routes",
]

logger = logging.getLogger(__name__)

CONTACT_EXPORT_ENV = "CONTACT_EXPORT_ENABLED"
EXPORTS = f"{PREFIX}/contact-exports"
PAGE_PATH = APPROVAL_PREFIX + "/exportar"
TITLE = "Exportar contactos"
# D-13.3: de a 1.000 personas por pagina (~210 KB medido en ATT1) y a lo sumo 20.000 por pedido.
PEOPLE_PAGE = 1000
MAX_PEOPLE = 20_000
# D12: el link vence a los 30 minutos (lo fija la base; aca solo se dice).
EXPIRES_MINUTES = 30
DEFAULT_COLUMNS = ("nombre", "telefono", "email", "etiquetas", "compro", "ultimo_formulario")
OPTIONAL_COLUMNS = ("pais", "baja", "productos")
ALL_COLUMNS = DEFAULT_COLUMNS + OPTIONAL_COLUMNS
LABEL_DAYS_DEFAULT = 90
LABEL_DAYS_MAX = 365
_MAX_LIST = 10
_LABEL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PRODUCT = re.compile(r"^[a-z0-9_-]{1,100}$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_PHONE = re.compile(r"^[1-9][0-9]{7,14}$")
_MX = re.compile(r"^52[0-9]{10}$")
_AR = re.compile(r"^54[0-9]{10}$")
_FILTER_KEYS = {"etiquetas", "sin_etiquetas", "compro", "producto", "formulario_desde", "formulario_hasta", "paises",
                "incluir_bajas", "etiquetas_dias"}
# Lo que el AE puede leer de la vista de la base: ni quien lo pidio ni quien lo bajo.
_VIEW_KEYS = ("export_id", "state", "filters", "columns", "matched_count", "created_at", "expires_at",
              "downloaded_at", "downloaded_count")
_RAISED = {"contact_export_invalid", "contact_export_not_found"}
EXPORT_NOTE = (
    "Este link abre la página para bajar la lista en CSV. Entra un aprobador con «Entrar con Slack» y la baja una "
    "sola vez; vence a los 30 minutos. Los teléfonos, los mails y los nombres solo están en el archivo: nunca en "
    "este chat."
)


class ExportTooLarge(Exception):
    """La base tiene mas personas que el tope: no sale un CSV a medias."""


class ExportStoreUnavailable(Exception):
    """La base contesto algo fuera de la forma: no se cuenta ni se baja."""


# ---------------------------------------------------------------------------
# Los filtros.
# ---------------------------------------------------------------------------

def _refused(code: str, field: str, problem: str | None = None) -> EntryRefused:
    reason: dict[str, Any] = {"field": field}
    if problem is not None:
        reason["problem"] = problem
    return EntryRefused(code, reason)


def _string_set(raw: Mapping[str, Any], key: str, pattern: re.Pattern[str], *, upper: bool = False) -> frozenset[str]:
    value = raw.get(key)
    if value is None:
        return frozenset()
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_LIST:
        raise _refused("contact_export_filter_invalid", key, "list_of_1_to_10")
    out = set()
    for item in value:
        if not isinstance(item, str):
            raise _refused("contact_export_filter_invalid", key, "not_a_string")
        text = item.strip().upper() if upper else item.strip().lower()
        if not pattern.fullmatch(text):
            raise _refused("contact_export_filter_invalid", key, "format")
        out.add(text)
    return frozenset(out)


def _day(raw: Mapping[str, Any], key: str) -> date | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise _refused("contact_export_filter_invalid", key, "yyyy_mm_dd")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise _refused("contact_export_filter_invalid", key, "yyyy_mm_dd") from None


@dataclass(frozen=True)
class ExportFilters:
    """Los filtros de un pedido (contrato, «Los filtros»). Sin ninguno, todas las personas sin baja."""

    labels: frozenset[str] = frozenset()
    without_labels: frozenset[str] = frozenset()
    purchased: bool | None = None
    product: str | None = None
    form_from: date | None = None
    form_to: date | None = None
    countries: frozenset[str] = frozenset()
    include_opted_out: bool = False
    label_days: int = LABEL_DAYS_DEFAULT

    @classmethod
    def parse(cls, raw: Any, *, products: frozenset[str]) -> ExportFilters:
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise _refused("contact_export_filter_invalid", "filtros", "not_object")
        extra = set(raw) - _FILTER_KEYS
        if extra:
            # Los nombres de las claves pasan por el mismo filtro que el resto de las rutas del AE.
            raise EntryRefused("contact_export_filter_invalid", {"field": "filtros", "extra": _keys(extra)})
        purchased = raw.get("compro")
        if purchased is not None and not isinstance(purchased, bool):
            raise _refused("contact_export_filter_invalid", "compro", "boolean")
        include = raw.get("incluir_bajas", False)
        if not isinstance(include, bool):
            raise _refused("contact_export_filter_invalid", "incluir_bajas", "boolean")
        days = raw.get("etiquetas_dias", LABEL_DAYS_DEFAULT)
        if type(days) is not int or not 1 <= days <= LABEL_DAYS_MAX:
            raise _refused("contact_export_filter_invalid", "etiquetas_dias", "between_1_and_365")
        form_from, form_to = _day(raw, "formulario_desde"), _day(raw, "formulario_hasta")
        if form_from is not None and form_to is not None and form_from > form_to:
            raise _refused("contact_export_filter_invalid", "formulario_hasta", "before_formulario_desde")
        product = raw.get("producto")
        if product is not None:
            if not isinstance(product, str) or not _PRODUCT.fullmatch(product):
                raise _refused("contact_export_filter_invalid", "producto", "format")
            if product not in products:
                raise _refused("contact_export_product_invalid", "producto", "not_of_this_instance")
            # D-13.4: el producto acota «compro» y el formulario; solo, no dice nada.
            if purchased is None and form_from is None and form_to is None:
                raise _refused("contact_export_filter_invalid", "producto", "needs_compro_or_formulario")
        labels = _string_set(raw, "etiquetas", _LABEL)
        without = _string_set(raw, "sin_etiquetas", _LABEL)
        if labels & without:
            raise _refused("contact_export_filter_invalid", "sin_etiquetas", "overlaps_etiquetas")
        countries = _string_set(raw, "paises", _COUNTRY, upper=True)
        return cls(labels=labels, without_labels=without, purchased=purchased, product=product, form_from=form_from,
                   form_to=form_to, countries=countries, include_opted_out=include, label_days=days)

    def to_json(self) -> dict[str, Any]:
        """Lo que se guarda en el pedido: las mismas claves que pidio el AE, sin las que no vinieron."""
        out: dict[str, Any] = {}
        if self.labels:
            out["etiquetas"] = sorted(self.labels)
        if self.without_labels:
            out["sin_etiquetas"] = sorted(self.without_labels)
        if self.purchased is not None:
            out["compro"] = self.purchased
        if self.product is not None:
            out["producto"] = self.product
        if self.form_from is not None:
            out["formulario_desde"] = self.form_from.isoformat()
        if self.form_to is not None:
            out["formulario_hasta"] = self.form_to.isoformat()
        if self.countries:
            out["paises"] = sorted(self.countries)
        if self.include_opted_out:
            out["incluir_bajas"] = True
        if self.label_days != LABEL_DAYS_DEFAULT:
            out["etiquetas_dias"] = self.label_days
        return out

    def needs_labels(self, columns: Sequence[str]) -> bool:
        return bool(self.labels or self.without_labels) or "etiquetas" in columns


def parse_columns(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_COLUMNS
    if not isinstance(raw, list) or not 1 <= len(raw) <= len(ALL_COLUMNS):
        raise _refused("contact_export_columns_invalid", "columnas", "list_of_1_to_9")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or item not in ALL_COLUMNS or item in out:
            raise _refused("contact_export_columns_invalid", "columnas", "unknown_or_repeated")
        out.append(item)
    return tuple(out)


# ---------------------------------------------------------------------------
# Las personas.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Form:
    at: datetime
    product: str


@dataclass(frozen=True)
class Person:
    """Una persona de la base (un telefono canonico) y lo que el CSV dice de ella."""

    phone: str
    name: str | None
    email: str | None
    forms: tuple[Form, ...]
    purchased_products: frozenset[str]
    opted_out: bool
    labels: frozenset[str] = frozenset()

    @property
    def last_form_at(self) -> datetime | None:
        return max((form.at for form in self.forms), default=None)


def _text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExportStoreUnavailable("row_text")
    text = value.strip()
    return text[:limit] if text else None


def _moment(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ExportStoreUnavailable("row_form")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ExportStoreUnavailable("row_form") from error
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def parse_people(body: Any) -> tuple[list[Person], str | None]:
    """Una pagina de ``_contact_export_people``. Una fila fuera de la forma frena todo (falla cerrado)."""
    if not isinstance(body, Mapping) or not isinstance(body.get("people"), list):
        raise ExportStoreUnavailable("page_shape")
    after = body.get("next_after")
    if after is not None and (not isinstance(after, str) or not _PHONE.fullmatch(after)):
        raise ExportStoreUnavailable("page_cursor")
    people: list[Person] = []
    for row in body["people"]:
        if not isinstance(row, Mapping):
            raise ExportStoreUnavailable("row_shape")
        phone = row.get("phone")
        forms, products = row.get("forms"), row.get("purchased_products")
        if (not isinstance(phone, str) or not _PHONE.fullmatch(phone) or not isinstance(forms, list)
                or not isinstance(products, list) or not isinstance(row.get("opted_out"), bool)
                or not all(isinstance(p, str) for p in products)):
            raise ExportStoreUnavailable("row_shape")
        parsed_forms = []
        for form in forms:
            if not isinstance(form, Mapping) or not isinstance(form.get("product"), str):
                raise ExportStoreUnavailable("row_form")
            parsed_forms.append(Form(at=_moment(form.get("at")), product=form["product"]))
        people.append(Person(phone=phone, name=_text(row.get("name"), 200), email=_text(row.get("email"), 320),
                             forms=tuple(parsed_forms), purchased_products=frozenset(products),
                             opted_out=row["opted_out"]))
    return people, after


def export_e164(phone: str) -> str:
    """D-13.2: el telefono en E.164. Mexico ``+52`` + 10 (la forma de hoy, sin el 1); Argentina ``+549`` + 10 (el
    movil); el resto, ``+`` y los digitos canonicos."""
    canonical = canonical_whatsapp_phone(phone) or phone
    if _AR.fullmatch(canonical):
        return "+549" + canonical[2:]
    return "+" + canonical


def country_of(phone: str) -> str:
    """El pais del telefono por el indicativo (ISO de dos letras), o ``""`` si no se sabe."""
    try:
        region = phonenumbers.region_code_for_number(phonenumbers.parse(export_e164(phone)))
    except phonenumbers.NumberParseException:
        return ""
    return region if isinstance(region, str) and _COUNTRY.fullmatch(region) and region != "ZZ" else ""


def _local_day(moment: datetime, zone: ZoneInfo) -> date:
    return moment.astimezone(zone).date()


def matches(person: Person, filters: ExportFilters, *, zone: ZoneInfo) -> bool:
    if person.opted_out and not filters.include_opted_out:
        return False
    if filters.labels and not (person.labels & filters.labels):
        return False
    if filters.without_labels and person.labels & filters.without_labels:
        return False
    if filters.purchased is not None:
        bought = (filters.product in person.purchased_products) if filters.product else bool(person.purchased_products)
        if bought is not filters.purchased:
            return False
    if filters.form_from is not None or filters.form_to is not None:
        found = False
        for form in person.forms:
            if filters.product is not None and form.product != filters.product:
                continue
            day = _local_day(form.at, zone)
            if (filters.form_from is None or day >= filters.form_from) and \
                    (filters.form_to is None or day <= filters.form_to):
                found = True
                break
        if not found:
            return False
    return not (filters.countries and country_of(person.phone) not in filters.countries)


@dataclass(frozen=True)
class Collected:
    """Lo que salio de leer la base: quienes cumplen, cuantas personas tiene la base y cuantas bajas cumplian
    todo lo demas y quedaron afuera (D-13.5: por default las bajas no salen). Solo conteos para el AE."""

    people: list[Person]
    total: int
    opted_out_excluded: int


async def collect(read_page: Callable[[str | None, int], Awaitable[Any]], *, labels: LabelIndex | None,
                  filters: ExportFilters, columns: Sequence[str], zone: ZoneInfo) -> Collected:
    """Las personas que cumplen los filtros (``Collected``).

    Lee la base de a ``PEOPLE_PAGE`` con cursor por telefono hasta el final; mas de ``MAX_PEOPLE`` es
    ``ExportTooLarge``. Las etiquetas se leen una vez, solo si hacen falta; si no se pueden leer enteras,
    ``AudienceLabelsUnavailable`` (una lista por etiqueta no sale a medias).
    """
    people: list[Person] = []
    after: str | None = None
    while True:
        page, after = parse_people(await read_page(after, PEOPLE_PAGE))
        people.extend(page)
        if len(people) > MAX_PEOPLE:
            raise ExportTooLarge()
        if after is None:
            break
    index: Mapping[str, frozenset[str]] = {}
    if filters.needs_labels(columns):
        if labels is None:
            raise AudienceLabelsUnavailable("sin_chatwoot")
        index = await labels.labels_by_phone(active_days=filters.label_days)
    out = []
    opted_out_excluded = 0
    with_opted_out = replace(filters, include_opted_out=True)
    for person in people:
        if index:
            found: set[str] = set()
            for variant in equivalent_whatsapp_phones(person.phone) or (person.phone,):
                found |= {label.lower() for label in index.get(variant, frozenset())}
            person = Person(phone=person.phone, name=person.name, email=person.email, forms=person.forms,
                            purchased_products=person.purchased_products, opted_out=person.opted_out,
                            labels=frozenset(found))
        if not matches(person, with_opted_out, zone=zone):
            continue
        if person.opted_out and not filters.include_opted_out:
            opted_out_excluded += 1
            continue
        out.append(person)
    return Collected(people=out, total=len(people), opted_out_excluded=opted_out_excluded)


# ---------------------------------------------------------------------------
# El CSV.
# ---------------------------------------------------------------------------

def _cell(value: str) -> str:
    # Una celda que arranca con = + - @ la lee la planilla como formula (la regla de la bajada del masivo).
    return "'" + value if _SAFE_CSV_START.match(value) else value


def _value(person: Person, column: str, zone: ZoneInfo) -> str:
    if column == "nombre":
        return person.name or ""
    if column == "telefono":
        return export_e164(person.phone)
    if column == "email":
        return person.email or ""
    if column == "etiquetas":
        return ";".join(sorted(person.labels))
    if column == "compro":
        return "si" if person.purchased_products else "no"
    if column == "ultimo_formulario":
        last = person.last_form_at
        return last.astimezone(zone).isoformat(timespec="seconds") if last is not None else ""
    if column == "pais":
        return country_of(person.phone)
    if column == "baja":
        return "si" if person.opted_out else "no"
    if column == "productos":
        return ";".join(sorted(person.purchased_products))
    raise ValueError(column)


def export_csv(people: Iterable[Person], columns: Sequence[str], *, zone: ZoneInfo) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(columns)
    for person in people:
        writer.writerow([_cell(_value(person, column, zone)) for column in columns])
    return buffer.getvalue()


def _zone(time_zone: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(time_zone or "UTC")
    except (ValueError, KeyError):
        return ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# La base (service_role) y el flag.
# ---------------------------------------------------------------------------

class ContactExportStore(FlowChangeStore):
    """Las tres funciones del bridge (20261126300000), con la clave de ``service_role``."""

    async def create_export(self, instance: Mapping[str, str], export: Mapping[str, Any]) -> dict[str, Any]:
        return await self._rpc("create_contact_export_v1", {"p_instance": instance, "p_export": export})

    async def read_export(self, instance: Mapping[str, str], export_id: str) -> dict[str, Any]:
        return await self._rpc("read_contact_export_v1", {"p_instance": instance, "p_export_id": export_id})

    async def read_people(self, instance: Mapping[str, str], *, after: str | None, limit: int) -> dict[str, Any]:
        return await self._rpc("read_contact_export_people_v1",
                               {"p_instance": instance, "p_after": after, "p_limit": limit})


def check_settings(settings: Any) -> None:
    """Lo que exige ``CONTACT_EXPORT_ENABLED`` al arrancar: las rutas del AE, la pagina (el link) y la base."""
    if not getattr(settings, "contact_export_enabled", False):
        return
    if not getattr(settings, "automation_expert_change_enabled", False):
        raise ValueError(f"{CONTACT_EXPORT_ENV} requires AUTOMATION_EXPERT_CHANGE_ENABLED")
    if not getattr(settings, "flow_approval_enabled", False) or not getattr(settings, "flow_approval_base_url", None):
        raise ValueError(f"{CONTACT_EXPORT_ENV} requires FLOW_APPROVAL_ENABLED and FLOW_APPROVAL_BASE_URL")
    if getattr(settings, "instance_manifest", None) is None:
        raise ValueError(f"{CONTACT_EXPORT_ENV} requires an instance manifest")
    if not getattr(settings, "supabase_base_url", None) or not getattr(settings, "supabase_service_role_key", None):
        raise ValueError(f"{CONTACT_EXPORT_ENV} requires SUPABASE_BASE_URL and SUPABASE_SERVICE_ROLE_KEY")


def page_url(origin: str, export_id: str) -> str:
    return f"{origin.strip().rstrip('/')}{PAGE_PATH}/{UUID(export_id)}"


def _uuid_or_none(value: Any) -> str | None:
    try:
        return str(UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def _ae_view(view: Mapping[str, Any], origin: str) -> dict[str, Any]:
    out = {key: view.get(key) for key in _VIEW_KEYS}
    export_id = _uuid_or_none(view.get("export_id"))
    out["page_url"] = page_url(origin, export_id) if export_id is not None and view.get("state") == "pendiente" \
        else None
    return out


# ---------------------------------------------------------------------------
# Las rutas del AE.
# ---------------------------------------------------------------------------

def register_contact_export_routes(app: FastAPI, *, token: str, store: ContactExportStore, tenant_ref: str,
                                   ally_ref: str, product_refs: frozenset[str], time_zone: str | None,
                                   labels: LabelIndex | None, approval_origin: str,
                                   git_sha: str | None = None) -> None:
    """Las dos rutas del AE, detras del flag (lo mira quien llama: apagado no existen)."""
    instance = {"tenant_ref": tenant_ref, "ally_ref": ally_ref}
    sha = git_sha or os.environ.get("GIT_SHA", "") or "unknown"
    zone = _zone(time_zone)
    slots = asyncio.Semaphore(1)

    def authenticate(authorization: str | None) -> None:
        expected = f"Bearer {token}"
        if authorization is None or not hmac.compare_digest(authorization.encode("utf-8"),
                                                            expected.encode("utf-8")):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="automation_expert_change_authentication_required",
                                headers={"WWW-Authenticate": "Bearer"})

    def envelope(data: Mapping[str, Any]) -> dict[str, Any]:
        return {"contract": CONTRACT, "contract_version": CONTRACT_VERSION, "instance": dict(instance),
                "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "bridge_git_sha": sha, "data": dict(data)}

    def failure(error: Exception, route: str, export_id: str | None = None) -> HTTPException:
        if isinstance(error, StoreRejected) and error.code in _RAISED:
            code, http_status = error.code, error.http_status
        elif isinstance(error, ExportTooLarge):
            code, http_status = "contact_export_too_large", 422
        elif isinstance(error, AudienceLabelsUnavailable):
            code, http_status = "contact_export_labels_unavailable", 503
        elif isinstance(error, StoreUnavailable):
            code, http_status = error.code, 503
        else:
            code, http_status = "automation_expert_change_unavailable", 503
        logger.info("contact_export_ae route=%s export_id=%s status=%s code=%s", route, export_id, http_status, code)
        return HTTPException(status_code=http_status, detail=code)

    @app.post(EXPORTS, status_code=201)
    async def propose_contact_export(request: Request, authorization: str | None = Header(default=None)) -> Any:
        """El pedido de exportacion: cuenta y devuelve el link. Ningun dato personal vuelve."""
        authenticate(authorization)
        body = _check_keys(await _body(request), required={"requested_by"},
                           allowed={"filtros", "columnas", "requested_by"})
        requested_by = _requested_by(body)
        filters = ExportFilters.parse(body.get("filtros"), products=product_refs)
        columns = parse_columns(body.get("columnas"))
        if slots.locked():
            raise HTTPException(status_code=503, detail="automation_expert_change_busy")
        async with slots:
            try:
                found = await collect(
                    lambda after, limit: store.read_people(instance, after=after, limit=limit),
                    labels=labels, filters=filters, columns=columns, zone=zone)
                created = await store.create_export(instance, {
                    "filters": filters.to_json(), "columns": list(columns), "matched_count": len(found.people),
                    "requested_by": requested_by})
            except (StoreUnavailable, StoreRejected, ExportTooLarge, AudienceLabelsUnavailable) as error:
                raise failure(error, "propose") from None
            except ExportStoreUnavailable as error:
                raise failure(StoreUnavailable(), "propose") from error
        if created.get("ok") is not True:
            code = created.get("reason_code")
            code = code if code == "contact_export_rate_limited" else "automation_expert_change_unavailable"
            logger.info("contact_export_ae route=propose status=429 code=%s", code)
            raise HTTPException(status_code=429 if code == "contact_export_rate_limited" else 503, detail=code)
        export_id = _uuid_or_none(created.get("export_id"))
        if export_id is None:
            raise failure(StoreUnavailable(), "propose")
        logger.info("contact_export_ae route=propose export_id=%s status=201 matched=%s total=%s columns=%s",
                    export_id, len(found.people), found.total, len(columns))
        return envelope({"export_id": export_id, "matched_count": len(found.people), "people_in_base": found.total,
                         "opted_out_excluded": found.opted_out_excluded,
                         "filters": filters.to_json(), "columns": list(columns),
                         "expires_at": created.get("expires_at"), "expires_in_minutes": EXPIRES_MINUTES,
                         "page_url": page_url(approval_origin, export_id), "note": EXPORT_NOTE})

    @app.get(f"{EXPORTS}/{{export_id}}")
    async def get_contact_export(export_id: str, authorization: str | None = Header(default=None)) -> Any:
        """Un pedido: estado y conteos. Solo lee."""
        authenticate(authorization)
        checked = _uuid_or_none(export_id)
        if checked is None:
            raise HTTPException(status_code=422, detail="invalid_contact_export_id")
        try:
            view = await store.read_export(instance, checked)
        except (StoreUnavailable, StoreRejected) as error:
            raise failure(error, "read", checked) from None
        logger.info("contact_export_ae route=read export_id=%s status=200", checked)
        return envelope(_ae_view(view, approval_origin))


# ---------------------------------------------------------------------------
# La pagina.
# ---------------------------------------------------------------------------

_STATE = {
    "pendiente": ("Lista para bajar", "chip--ok"),
    "bajado": ("Ya se bajó", None),
    "vencido": ("Venció", "chip--warn"),
}
_COLUMN_TEXT = {
    "nombre": "nombre", "telefono": "teléfono", "email": "mail", "etiquetas": "etiquetas", "compro": "compró",
    "ultimo_formulario": "último formulario", "pais": "país", "baja": "baja", "productos": "productos comprados",
}


def describe_filters(filters: Mapping[str, Any]) -> list[str]:
    """Los filtros en palabras, para la pagina. Solo nombres de etiquetas, productos, fechas y paises."""
    out = []
    if filters.get("etiquetas"):
        out.append("Con alguna de las etiquetas: " + ", ".join(str(x) for x in filters["etiquetas"]) + ".")
    if filters.get("sin_etiquetas"):
        out.append("Sin ninguna de las etiquetas: " + ", ".join(str(x) for x in filters["sin_etiquetas"]) + ".")
    product = filters.get("producto")
    if filters.get("compro") is True:
        out.append(f"Compraron{' ' + str(product) if product else ''}.")
    elif filters.get("compro") is False:
        out.append(f"No compraron{' ' + str(product) if product else ''}.")
    since, until = filters.get("formulario_desde"), filters.get("formulario_hasta")
    if since or until:
        window = (f"entre el {since} y el {until}" if since and until else
                  f"desde el {since}" if since else f"hasta el {until}")
        out.append(f"Dejaron un formulario{' de ' + str(product) if product else ''} {window}.")
    if filters.get("paises"):
        out.append("Teléfonos de: " + ", ".join(str(x) for x in filters["paises"]) + ".")
    out.append("Incluye a quienes pidieron no recibir más mensajes." if filters.get("incluir_bajas")
               else "Sin quienes pidieron no recibir más mensajes.")
    return out


def render_export(view: Mapping[str, Any], *, csrf: str, time_zone: str | None) -> str:
    export_id = str(view.get("export_id"))
    state = str(view.get("state"))
    label, kind = _STATE.get(state, (state, None))
    filters = view.get("filters") if isinstance(view.get("filters"), Mapping) else {}
    columns = [c for c in view.get("columns") or () if isinstance(c, str)]
    items = "".join(f"<li>{_esc(text)}</li>" for text in describe_filters(filters))
    parts = [
        f'<section class="panel"><div class="panel-heading"><h2>{_esc(TITLE)}</h2> {_chip(label, kind)}</div>',
        '<div class="panel-body">',
        f"<p>Cuando se pidió coincidían <strong>{_esc(view.get('matched_count'))}</strong> personas. "
        "El archivo se arma de nuevo al bajarlo, así que el número puede cambiar un poco.</p>",
        f"<ul>{items}</ul>",
        "<p>Columnas: " + _esc(", ".join(_COLUMN_TEXT.get(c, c) for c in columns)) + ".</p>",
        f"<p class=\"meta\">Pedido el {_when(view.get('created_at'), time_zone)}. "
        f"Vence el {_when(view.get('expires_at'), time_zone)}.</p>",
    ]
    if state == "pendiente":
        parts.append(
            f'<form method="post" action="{PAGE_PATH}/{_esc(export_id)}/csv">{_hidden("csrf_token", csrf)}'
            '<p>El archivo trae teléfonos, mails y nombres: guardalo donde corresponda y no lo pegues en un chat. '
            'Se baja una sola vez.</p><button class="button primary" type="submit">Bajar el CSV</button></form>')
    elif state == "bajado":
        parts.append(f"<p>Se bajó el {_when(view.get('downloaded_at'), time_zone)} "
                     f"({_esc(view.get('downloaded_count'))} filas). Para otra, pedile al agente un pedido nuevo.</p>")
    else:
        parts.append("<p>Pasaron los 30 minutos. Para bajarla, pedile al agente un pedido nuevo.</p>")
    parts.append("</div></section>")
    return "".join(parts)


_DOWNLOAD_REFUSED = {
    "contact_export_already_downloaded": ("Esta lista ya se bajó. Para otra, pedile al agente un pedido nuevo.", 409),
    "contact_export_expired": ("Pasaron los 30 minutos. Para bajarla, pedile al agente un pedido nuevo.", 410),
}


def page_routes(*, labels: LabelIndex | None, clock: Callable[[], datetime] | None = None
                ) -> Callable[[FastAPI, PageTools], None]:
    """Las rutas de la pagina, para ``register_flow_approval_routes(extra_routes=...)``. Solo con el flag."""
    now = clock or (lambda: datetime.now(UTC))

    def register(app: FastAPI, tools: PageTools) -> None:
        store, instance = tools.store, tools.instance
        zone = _zone(tools.time_zone)

        def not_found(title: str) -> Response:
            return _page(title, "No existe ese pedido en esta instancia.", 404)

        async def read(session_hash: str, export_id: str) -> dict[str, Any]:
            return await store._rpc("approver_read_contact_export_v1", {  # noqa: SLF001 - la tienda de la pagina
                "p_instance": instance, "p_session_hash": session_hash, "p_export_id": export_id})

        @app.get(PAGE_PATH + "/{export_id}")
        async def contact_export_page(export_id: str, request: Request) -> Response:
            if not _canonical_uuid(export_id):
                return not_found(TITLE)
            who = await tools.who(request)
            if who is None:
                return tools.logged_out(_login(None))
            user, secret = who
            try:
                view = await read(_sha(secret), export_id)
            except SessionInvalid:
                return tools.logged_out(_login(None))
            except PageStoreRejected as rejected:
                if rejected.code == "contact_export_not_found":
                    return not_found(TITLE)
                return _page(TITLE, "El almacén no contestó. Probá en un rato.", 503)
            except PageStoreUnavailable:
                logger.info("flow_approval route=contact_export status=503")
                return _page(TITLE, "El almacén no contestó. Probá en un rato.", 503)
            csrf = tools.config.csrf_token(secret)
            body = render_export(view, csrf=csrf, time_zone=tools.time_zone)
            return HTMLResponse(_shell(TITLE, body, user=user, csrf=csrf, show_user_id=False))

        @app.post(PAGE_PATH + "/{export_id}/csv")
        async def contact_export_download(export_id: str, request: Request) -> Response:
            title = "Bajar la lista"
            if not _canonical_uuid(export_id):
                return not_found(title)
            checked = await tools.write_form(request, title=title, fields={"csrf_token"})
            if isinstance(checked, Response):
                return checked
            _, secret, _ = checked
            session = _sha(secret)
            try:
                view = await read(session, export_id)
                if view.get("state") != "pendiente":
                    code = ("contact_export_already_downloaded" if view.get("state") == "bajado"
                            else "contact_export_expired")
                    text, http_status = _DOWNLOAD_REFUSED[code]
                    return _page(title, text, http_status)
                filters = ExportFilters.parse(view.get("filters"), products=frozenset(
                    [view["filters"]["producto"]] if isinstance(view.get("filters"), Mapping)
                    and isinstance(view["filters"].get("producto"), str) else []))
                columns = parse_columns(list(view.get("columns") or ()))

                async def read_page(after: str | None, limit: int) -> Any:
                    body = await store._rpc("approver_read_contact_export_people_v1", {  # noqa: SLF001
                        "p_instance": instance, "p_session_hash": session, "p_export_id": export_id,
                        "p_after": after, "p_limit": limit})
                    if body.get("ok") is not True:
                        raise _Refused(str(body.get("reason_code")))
                    return body

                found = await collect(read_page, labels=labels, filters=filters, columns=columns, zone=zone)
                matched, total = found.people, found.total
                text = export_csv(matched, columns, zone=zone)
                marked = await store._rpc("approver_mark_contact_export_downloaded_v1", {  # noqa: SLF001
                    "p_instance": instance, "p_session_hash": session, "p_export_id": export_id,
                    "p_count": len(matched)})
                if marked.get("ok") is not True:
                    raise _Refused(str(marked.get("reason_code")))
            except SessionInvalid:
                return tools.logged_out(_login(None))
            except PageStoreRejected as rejected:
                if rejected.code == "contact_export_not_found":
                    return not_found(title)
                return _page(title, "El almacén no contestó. Probá en un rato.", 503)
            except _Refused as refused:
                text, http_status = _DOWNLOAD_REFUSED.get(refused.code, ("No se pudo bajar la lista.", 409))
                logger.info("flow_approval route=contact_export_csv export_id=%s status=%s code=%s", export_id,
                            http_status, refused.code)
                return _page(title, text, http_status)
            except ExportTooLarge:
                return _page(title, f"La base tiene más de {MAX_PEOPLE} personas: no se arma un archivo a medias.",
                             422)
            except AudienceLabelsUnavailable:
                logger.info("flow_approval route=contact_export_csv export_id=%s status=503 code=labels", export_id)
                return _page(title, "No se pudieron leer las etiquetas de Chatwoot enteras. Probá en un rato.", 503)
            except (PageStoreUnavailable, ExportStoreUnavailable, EntryRefused):
                logger.info("flow_approval route=contact_export_csv export_id=%s status=503", export_id)
                return _page(title, "El almacén no contestó. Probá en un rato.", 503)
            logger.info("flow_approval route=contact_export_csv export_id=%s status=200 rows=%s total=%s",
                        export_id, len(matched), total)
            stamp = now().astimezone(zone).strftime("%Y%m%d-%H%M")
            return Response(content=("﻿" + text).encode("utf-8"), media_type="text/csv; charset=utf-8",
                            headers={"Content-Disposition": f'attachment; filename="contactos-{stamp}.csv"'})

    return register


class _Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

