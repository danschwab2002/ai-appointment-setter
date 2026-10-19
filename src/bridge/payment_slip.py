"""La ficha de pago en efectivo (OXXO y parecidos): que tienda, cuando vence y el boton.

Funciones puras, sin red ni base. La base guarda los hechos crudos de la ficha
tal como llegan de Hotmart (``PURCHASE_BILLET_PRINTED``); la tienda, el
vencimiento y el sufijo del boton se calculan aca, al componer, contra la tabla
``[[ficha.medios]]`` del manifiesto. Contrato:
``docs/contracts/portable-payment-slip-first-contact-v1.md``.

Lo medido que sostiene el calculo del vencimiento (fixture
``tests/fixtures/hotmart_payment_slip_printed_att1_20261018.json``, capturado el
2026-10-18): en las 5 fichas de OXXO de Mexico, ``billet_barcode`` tiene 34
digitos y los digitos 19 a 26 son el vencimiento AAAAMMDD, igual al dia de
``order_date`` en hora de CDMX mas 3.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ── Motivos ──────────────────────────────────────────────────────────

# Ninguna fila habilitada de la tabla coincide con el medio y el pais.
PAYMENT_SLIP_METHOD_NOT_ENABLED = "payment_slip_method_not_enabled"
# El link de la ficha no empieza con el prefijo del boton aprobado en Meta.
PAYMENT_SLIP_URL_PREFIX_MISMATCH = "payment_slip_url_prefix_mismatch"
# Lo que sigue al prefijo no es un sufijo seguro para la variable del boton.
PAYMENT_SLIP_BUTTON_SUFFIX_INVALID = "payment_slip_button_suffix_invalid"
# Ni la referencia ni la fecha de generada permiten saber cuando vence.
PAYMENT_SLIP_DUE_DATE_UNKNOWN = "payment_slip_due_date_unknown"

PAYMENT_SLIP_REASONS = frozenset({
    PAYMENT_SLIP_METHOD_NOT_ENABLED,
    PAYMENT_SLIP_URL_PREFIX_MISMATCH,
    PAYMENT_SLIP_BUTTON_SUFFIX_INVALID,
    PAYMENT_SLIP_DUE_DATE_UNKNOWN,
})

DueDateSource = Literal["reference", "generated_plus_days"]

# El sufijo va como variable de un boton URL de Meta: solo caracteres que no
# cambian la URL (sin '&', '#', '/', '%' ni espacios). El hash medido de OXXO
# tiene 48 hex en minuscula; el patron no lo fija para no atarse a un proveedor.
_BUTTON_SUFFIX = re.compile(r"[A-Za-z0-9_-]{8,128}")
_ASCII_DIGITS = re.compile(r"[0-9]+")
_MEDIO = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
_PAIS = re.compile(r"[A-Z]{2}")
_DUE_DATE_DIGITS = 8  # AAAAMMDD
# Una fecha de la referencia fuera de esta ventana, contada desde el dia de
# generada, no se cree: se usa el respaldo (generada + dias).
_MAX_REFERENCE_DUE_DAYS = 31
_MAX_FALLBACK_DAYS = 31
_MAX_REFERENCE_LENGTH = 128
_MAX_TIENDA_LENGTH = 64
_MAX_DATETIME_TIMESTAMP_MS = 253_402_300_799_999

_METHOD_KEYS = frozenset({
    "medio",
    "pais",
    "tienda",
    "zona_horaria",
    "referencia_largo",
    "vencimiento_digitos",
    "vencimiento_dias_si_falta",
    "boton_prefijo",
    "habilitada",
})

_DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MESES = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)


# ── La tabla por medio y pais ────────────────────────────────────────


def _positive_int(value: object) -> bool:
    return type(value) is int and value >= 1


@dataclass(frozen=True)
class PaymentSlipMethod:
    """Una fila de ``[[ficha.medios]]``: un medio de pago en un pais."""

    medio: str
    pais: str
    tienda: str
    zona_horaria: str
    referencia_largo: int
    # Posiciones 1-based e inclusivas del vencimiento AAAAMMDD en la referencia.
    vencimiento_digitos: tuple[int, int]
    vencimiento_dias_si_falta: int
    boton_prefijo: str
    habilitada: bool

    def __post_init__(self) -> None:
        if not isinstance(self.medio, str) or _MEDIO.fullmatch(self.medio) is None:
            raise ValueError(
                "ficha.medios medio must be an uppercase Hotmart payment type"
            )
        if not isinstance(self.pais, str) or _PAIS.fullmatch(self.pais) is None:
            raise ValueError("ficha.medios pais must be an uppercase ISO 3166-1 alpha-2 code")
        if (
            not isinstance(self.tienda, str)
            or not self.tienda.strip()
            or self.tienda != self.tienda.strip()
            or len(self.tienda) > _MAX_TIENDA_LENGTH
            or any(not char.isprintable() for char in self.tienda)
        ):
            raise ValueError(
                "ficha.medios tienda must be a non-blank printable name of at most "
                f"{_MAX_TIENDA_LENGTH} characters"
            )
        if not isinstance(self.zona_horaria, str) or not self.zona_horaria:
            raise ValueError("ficha.medios zona_horaria must be an IANA time zone")
        try:
            ZoneInfo(self.zona_horaria)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                "ficha.medios zona_horaria must be an IANA time zone"
            ) from exc
        if (
            not _positive_int(self.referencia_largo)
            or self.referencia_largo > _MAX_REFERENCE_LENGTH
        ):
            raise ValueError(
                "ficha.medios referencia_largo must be an integer between 1 and "
                f"{_MAX_REFERENCE_LENGTH}"
            )
        digits = self.vencimiento_digitos
        if (
            not isinstance(digits, tuple)
            or len(digits) != 2
            or not all(_positive_int(value) for value in digits)
        ):
            raise ValueError(
                "ficha.medios vencimiento_digitos must be two positive integers"
            )
        first, last = digits
        if last - first + 1 != _DUE_DATE_DIGITS or last > self.referencia_largo:
            raise ValueError(
                "ficha.medios vencimiento_digitos must span 8 digits (AAAAMMDD) "
                "inside referencia_largo"
            )
        if (
            type(self.vencimiento_dias_si_falta) is not int
            or not 0 <= self.vencimiento_dias_si_falta <= _MAX_FALLBACK_DAYS
        ):
            raise ValueError(
                "ficha.medios vencimiento_dias_si_falta must be an integer between "
                f"0 and {_MAX_FALLBACK_DAYS}"
            )
        prefix = self.boton_prefijo
        if (
            not isinstance(prefix, str)
            or not prefix.startswith("https://")
            or not prefix.endswith(("=", "/"))
            or any(char.isspace() or not char.isprintable() for char in prefix)
            or "{" in prefix
            or "}" in prefix
            or "#" in prefix
            or not urlsplit(prefix).hostname
        ):
            raise ValueError(
                "ficha.medios boton_prefijo must be an https URL with a host that "
                "ends in '=' or '/'"
            )
        if type(self.habilitada) is not bool:
            raise ValueError("ficha.medios habilitada must be a boolean")

    @property
    def button_url_template(self) -> str:
        """La URL del boton tal como se aprueba en Meta: prefijo y una variable."""

        return f"{self.boton_prefijo}{{{{1}}}}"


def parse_payment_slip_methods(raw: object) -> tuple[PaymentSlipMethod, ...]:
    """Valida las filas de ``[[ficha.medios]]`` tal como salen del TOML.

    Levanta ``ValueError`` con el numero de fila ante cualquier forma invalida.
    Una lista vacia es valida: ningun medio habilitado.
    """

    if not isinstance(raw, (list, tuple)):
        raise ValueError("ficha.medios must be a list of tables")
    methods: list[PaymentSlipMethod] = []
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(raw):
        where = f"ficha.medios[{index}]"
        if not isinstance(row, dict):
            raise ValueError(f"{where} must be a table")
        keys = set(row)
        if keys != _METHOD_KEYS:
            missing = sorted(_METHOD_KEYS - keys)
            extra = sorted(str(key) for key in keys - _METHOD_KEYS)
            raise ValueError(
                f"{where} must contain exactly the supported keys "
                f"(missing: {missing}, unknown: {extra})"
            )
        digits = row["vencimiento_digitos"]
        if not isinstance(digits, (list, tuple)):
            raise ValueError(f"{where} vencimiento_digitos must be a list of two integers")
        try:
            method = PaymentSlipMethod(
                medio=row["medio"],
                pais=row["pais"],
                tienda=row["tienda"],
                zona_horaria=row["zona_horaria"],
                referencia_largo=row["referencia_largo"],
                vencimiento_digitos=tuple(digits),  # type: ignore[arg-type]
                vencimiento_dias_si_falta=row["vencimiento_dias_si_falta"],
                boton_prefijo=row["boton_prefijo"],
                habilitada=row["habilitada"],
            )
        except ValueError as exc:
            raise ValueError(f"{where}: {exc}") from exc
        key = (method.medio, method.pais)
        if key in seen:
            raise ValueError(
                f"{where} repeats medio {method.medio!r} and pais {method.pais!r}"
            )
        seen.add(key)
        methods.append(method)
    return tuple(methods)


# ── Los hechos de la ficha ───────────────────────────────────────────


@dataclass(frozen=True)
class PaymentSlipFacts:
    """Lo que se lee de un ``PURCHASE_BILLET_PRINTED``; ``None`` si falta."""

    payment_type: str | None
    checkout_country_iso: str | None
    billet_url: str | None
    billet_barcode: str | None
    generated_at: datetime | None


def _json_object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _country(value: object) -> str | None:
    cleaned = _str(value)
    return cleaned.upper() if cleaned is not None else None


def _epoch_ms(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 < value <= _MAX_DATETIME_TIMESTAMP_MS:
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def payment_slip_facts(payload: object) -> PaymentSlipFacts:
    """Lee los hechos de la ficha de un payload de Hotmart 2.0.0.

    Nunca levanta por un campo ausente o de otro tipo: lo deja en ``None``.
    El pais sale de ``data.purchase.checkout_country.iso`` (donde llega en las
    13 fichas capturadas) y, si falta, de ``data.buyer.address.country_iso``.
    """

    event = _json_object(payload)
    data = _json_object(event.get("data"))
    buyer = _json_object(data.get("buyer"))
    address = _json_object(buyer.get("address"))
    purchase = _json_object(data.get("purchase"))
    payment = _json_object(purchase.get("payment"))
    country = _json_object(purchase.get("checkout_country"))

    checkout_country_iso = _country(country.get("iso"))
    if checkout_country_iso is None:
        checkout_country_iso = _country(address.get("country_iso"))
    return PaymentSlipFacts(
        payment_type=_str(payment.get("type")),
        checkout_country_iso=checkout_country_iso,
        billet_url=_str(payment.get("billet_url")),
        billet_barcode=_str(payment.get("billet_barcode")),
        generated_at=_epoch_ms(purchase.get("order_date")),
    )


# ── La resolucion ────────────────────────────────────────────────────


@dataclass(frozen=True)
class PaymentSlipResolution:
    """Lo que la plantilla de la ficha necesita, calculado de los hechos."""

    method: PaymentSlipMethod
    tienda: str
    due_date: date
    due_date_source: DueDateSource
    button_suffix: str
    button_url_template: str


def _reference_due_date(
    barcode: str | None, method: PaymentSlipMethod, generated_on: date | None
) -> date | None:
    if (
        barcode is None
        or len(barcode) != method.referencia_largo
        or _ASCII_DIGITS.fullmatch(barcode) is None
    ):
        return None
    first, last = method.vencimiento_digitos
    segment = barcode[first - 1 : last]
    try:
        due = date(int(segment[0:4]), int(segment[4:6]), int(segment[6:8]))
    except ValueError:
        return None
    if generated_on is not None and not (
        generated_on <= due <= generated_on + timedelta(days=_MAX_REFERENCE_DUE_DAYS)
    ):
        return None
    return due


def resolve_payment_slip(
    facts: PaymentSlipFacts, methods: tuple[PaymentSlipMethod, ...]
) -> PaymentSlipResolution | str:
    """Elige la fila habilitada y calcula tienda, vencimiento y boton.

    Devuelve la resolucion, o uno de los motivos ``PAYMENT_SLIP_*`` en el orden
    en que se chequean: medio y pais, prefijo, sufijo, vencimiento.
    """

    method = next(
        (
            row
            for row in methods
            if row.habilitada
            and row.medio == facts.payment_type
            and row.pais == facts.checkout_country_iso
        ),
        None,
    )
    if method is None:
        return PAYMENT_SLIP_METHOD_NOT_ENABLED

    url = facts.billet_url
    if url is None or not url.startswith(method.boton_prefijo):
        return PAYMENT_SLIP_URL_PREFIX_MISMATCH
    suffix = url[len(method.boton_prefijo) :]
    if _BUTTON_SUFFIX.fullmatch(suffix) is None:
        return PAYMENT_SLIP_BUTTON_SUFFIX_INVALID

    generated_on = (
        facts.generated_at.astimezone(ZoneInfo(method.zona_horaria)).date()
        if facts.generated_at is not None
        else None
    )
    due = _reference_due_date(facts.billet_barcode, method, generated_on)
    source: DueDateSource = "reference"
    if due is None:
        if generated_on is None:
            return PAYMENT_SLIP_DUE_DATE_UNKNOWN
        due = generated_on + timedelta(days=method.vencimiento_dias_si_falta)
        source = "generated_plus_days"

    return PaymentSlipResolution(
        method=method,
        tienda=method.tienda,
        due_date=due,
        due_date_source=source,
        button_suffix=suffix,
        button_url_template=method.button_url_template,
    )


def format_due_date_es(value: date) -> str:
    """La fecha como va en la plantilla: «jueves 22 de octubre». Sin locale."""

    return f"{_DIAS[value.weekday()]} {value.day} de {_MESES[value.month - 1]}"
