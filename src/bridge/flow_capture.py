"""La captura de ATT1 para la paridad del motor: su forma cerrada, el fixture
recortado y la vuelta de las constantes.

Diseno del motor de flujos (2026-10-16), §5.3, §6.2 y §6.4. La captura es la
salida de ``tests/fixtures/flow_engine/att1_recovery_capture_query_v2.sql``
(``captura-paridad-recuperacion-v2``), leida de att1-db en una sesion de solo
lectura. Al repo entra un fixture recortado, que arma ``build_fixture`` y que
el cargador de la paridad (``flow_parity.Capture.from_json``) lee despues de
``restore_fixture``. Lo comparten el constructor
(``scripts/flow_engine_capture_fixture.py``), la prueba de privacidad y el
cargador.

La forma es la prueba de privacidad. Cada seccion tiene su lista cerrada de
claves, tambien en los objetos anidados, y cada clave su forma: un uuid, una
hora en UTC, una clave de persona (16 hex), un motivo, un codigo, un entero
con su rango, un booleano. Ademas, ninguna cadena lleva una arroba, y ninguna
lleva nueve digitos seguidos (sin contar los separadores de un telefono),
salvo las formas que ya fijan su largo y su alfabeto: el uuid, la clave de
persona, el sha256, la hora, la fecha y el codigo de producto de Hotmart. Un
error dice el camino y la forma que falto, nunca el valor.

Un nulo es una clave ausente: la consulta los trae, el fixture no. Las listas
no llevan nulos adentro.

Es puro: no lee el entorno, no hace red ni toca bases.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Union

__all__ = [
    "FIRING_EVENT_TYPES", "FIRST_CONTACT_KEYS", "FIXTURE_MAX_BYTES", "INTENT_KEYS",
    "QUERY_NAME", "ROW_SECTIONS", "TOP_SECTIONS", "TRANSFORMATIONS",
    "CaptureFixtureError", "CaptureShapeError", "FixtureTooLargeError", "ShapeProblem",
    "build_fixture", "count_sections", "firings", "is_fixture", "parse_instant", "restore_fixture",
    "section_sizes", "serialize", "shape_problems", "validate_shape",
]

QUERY_NAME = "captura-paridad-recuperacion-v2"
# El limite del fixture del repo (P#12). No se sube en silencio: si una captura
# no entra, se decide que mas recortar y se dice en el reporte (§6.4, paso 7).
FIXTURE_MAX_BYTES = 1_000_000
# Los disparos de la recuperacion: el carrito y el pago fallido (§6.5).
FIRING_EVENT_TYPES = ("PURCHASE_OUT_OF_SHOPPING_CART", "PURCHASE_CANCELED")
# Las secciones de filas, en el orden de la consulta.
ROW_SECTIONS = (
    "eventos", "conflictos_de_compra", "intenciones", "contactos", "casos",
    "acciones", "autorizaciones", "reevaluaciones", "primer_contacto",
)
TOP_SECTIONS = ("_captured", "config") + ROW_SECTIONS
# Lo que T1 lee de la siembra (§6.4, paso 2): del primer contacto, los planes
# creados desde «desde» con estas claves; de las intenciones, las que toca uno
# de esos planes o un disparo desde «desde», con estas claves.
FIRST_CONTACT_KEYS = (
    "purchase_intent_id", "contact_id", "recovery_case_id", "outcome", "reason_code",
    "created_at", "accion_status", "accion_terminal_reason", "accion_due_at",
    "accion_cerrada_at", "request_started_at",
)
INTENT_KEYS = (
    "id", "product_ref", "offer_ref", "submitted_at", "lifecycle_state",
    "current_classification", "person_key",
)
# Lo que hizo el constructor, en el orden en que lo hace. Las horas van antes
# que las constantes: dos horas iguales escritas distinto ('...:00+00:00' y
# '...:00.000000+00:00') son la misma constante.
TRANSFORMATIONS = (
    "valida_forma", "recorta_siembra", "horas_en_z", "sin_nulos", "constantes",
    "claves_ordenadas_compacta",
)


# ---------------------------------------------------------------------------
# Los errores.
# ---------------------------------------------------------------------------

class CaptureFixtureError(ValueError):
    """La captura o el fixture no se pueden usar: el mensaje dice donde, nunca el valor."""


@dataclass(frozen=True)
class ShapeProblem:
    """Un problema de forma: el camino de la clave y que le falto."""

    path: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path or '(raiz)'}: {self.reason}"


class CaptureShapeError(CaptureFixtureError):
    """Algo esta fuera de la lista cerrada o de su forma (§5.3)."""

    def __init__(self, problems: tuple[ShapeProblem, ...]) -> None:
        first = problems[0]
        more = f" (y {len(problems) - 1} mas)" if len(problems) > 1 else ""
        super().__init__(f"{first}{more}")
        self.problems = problems
        self.path = first.path


class FixtureTooLargeError(CaptureFixtureError):
    """El fixture pasa del limite: dice cuanto pesa cada seccion (§6.4, paso 7)."""

    def __init__(self, size: int, limit: int, sections: Mapping[str, int]) -> None:
        detail = ", ".join(f"{name}={size}" for name, size in sections.items())
        super().__init__(f"fixture_too_large: {size} bytes, el limite es {limit} ({detail})")
        self.size = size
        self.limit = limit
        self.sections = dict(sections)


# ---------------------------------------------------------------------------
# Las formas.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Form:
    """La forma de una hoja: su tipo y, en un texto, su patron (con fullmatch).

    ``fixed`` marca las formas que ya fijan su largo y su alfabeto: quedan
    fuera de la regla de los nueve digitos. ``instant`` y ``day`` piden ademas
    que la hora o la fecha existan.
    """

    name: str
    kind: str                                   # "text", "int" o "bool"
    pattern: re.Pattern[str] | None = None
    minimum: int = 0
    maximum: int = 0
    fixed: bool = False
    instant: bool = False
    day: bool = False


@dataclass(frozen=True)
class Record:
    """Un objeto con su lista cerrada de claves; ``required`` no puede faltar ni ser nulo."""

    name: str
    fields: Mapping[str, "Spec"] = field(default_factory=dict)
    required: tuple[str, ...] = ()


@dataclass(frozen=True)
class ListOf:
    """Una lista: cada elemento con la misma forma, y ninguno nulo."""

    item: "Spec"


Spec = Union[Form, Record, ListOf]


def _text(name: str, pattern: str, *, fixed: bool = False, instant: bool = False,
          day: bool = False) -> Form:
    return Form(name, "text", re.compile(pattern), fixed=fixed, instant=instant, day=day)


def _int(name: str, minimum: int, maximum: int) -> Form:
    return Form(name, "int", minimum=minimum, maximum=maximum)


# Se usan con fullmatch: un "$" de re acepta un salto de linea al final.
UUID = _text("uuid", r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", fixed=True)
# La hora siempre en UTC: la consulta corre con la sesion en UTC y el fixture
# la escribe con Z. La fraccion es opcional en la captura (Postgres corta los
# ceros) y va con seis cifras en el fixture.
INSTANT = _text(
    "hora_utc",
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00|\+00)",
    fixed=True, instant=True,
)
DAY = _text("fecha", r"[0-9]{4}-[0-9]{2}-[0-9]{2}", fixed=True, day=True)
# left(sha256(sal || prefijo || valor), 16), con una sal por corrida (§6.2).
PERSON_KEY = _text("clave_de_persona", r"[0-9a-f]{16}", fixed=True)
SHA256 = _text("sha256", r"[0-9a-f]{64}", fixed=True)
# Los motivos que la consulta deja pasar (processing_error, reason_code,
# terminal_reason, purchase_correlation): el resto sale como 'otro'.
REASON = _text("motivo", r"[a-z0-9_:]{1,80}")
# Los estados y los tipos de la base: status, outcome, phase, mode, decision...
CODE = _text("codigo", r"[a-z][a-z0-9_]{0,79}")
# to_state, con la regex de la 1a (20261015000200:1016).
STATE = _text("estado_del_control", r"[a-z][a-z_]{0,31}")
# Las claves de configuracion: scope, politica, tenant, embudo y la consulta.
KEY = _text("clave_de_configuracion", r"[a-z0-9][a-z0-9_:-]{0,79}")
PROVIDER_EVENT = _text("tipo_de_evento_de_hotmart", r"[A-Z][A-Z0-9_]{0,79}")
CENSUS_EVENT = _text("tipo_de_evento", r"[A-Za-z0-9_.]{1,80}")
SOURCE = _text("fuente", r"[a-z_]{1,40}")
PURCHASE_STATUS = _text("estado_de_compra", r"[A-Z_]{1,40}")
OFFER = _text("codigo_de_oferta", r"[A-Za-z0-9]{1,32}")
# El id numerico del producto en Hotmart (data.product.id, 7 cifras en ATT1).
# Es la forma de la consulta, y la regla de los digitos lo deja en 8 cifras:
# si Hotmart llega a 9, la prueba falla en voz alta y la forma se revisa.
PRODUCT_ID = _text("id_de_producto", r"[0-9]{1,12}")
# intenciones.product_ref: el codigo del producto en el checkout de Hotmart
# (pay.hotmart.com/<codigo>), una letra, las cifras y una letra; en ATT1 son
# 10 caracteres con 8 cifras, en Johanna 11 con 9 (F106691755G, en las
# migraciones). No es de nadie: es el producto. Su forma fija letra, cifras y
# letra, asi que queda fuera de la regla de los digitos.
PRODUCT_CODE = _text("codigo_de_producto", r"[A-Z][0-9]{6,10}[A-Z]", fixed=True)
ZONE = _text("zona_horaria", r"[A-Za-z][A-Za-z0-9_+/-]{0,63}")
_INTERVAL_UNIT = r"[0-9]{1,4} (?:years?|mons?|days?)"
_CLOCK = r"[0-9]{2,4}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?"
# Un interval de Postgres como texto: '1 day', '01:00:00', '2 days 03:00:00'.
INTERVAL = _text("intervalo", rf"{_INTERVAL_UNIT}(?: {_INTERVAL_UNIT})*(?: {_CLOCK})?|{_CLOCK}")
HOUR_MINUTE = _text("hora_del_dia", r"[0-9]{2}:[0-9]{2}")
# Los textos fijos que escribe la consulta (origin, desde_origen, redactions):
# ASCII imprimible, y con la arroba y los digitos vigilados como en el resto.
QUERY_TEXT = _text("texto_de_la_consulta", r"[ -~]{1,300}")
COUNT = _int("entero", 0, 99_999_999)
POSITIVE = _int("entero_positivo", 1, 99_999_999)
# creation_date y approved_date de Hotmart, en milisegundos: de 2017 a 2049.
# Un telefono como numero (5215512345678) queda fuera del rango.
EPOCH_MS = _int("milisegundos_de_epoca", 1_500_000_000_000, 2_500_000_000_000)
WEEKDAY = _int("dia_de_la_semana", 0, 7)
BOOLEAN = Form("booleano", "bool")

_ATTEMPT = Record("intento", {
    "attempt_number": POSITIVE, "mode": CODE, "phase": CODE, "outcome": CODE,
    "started_at": INSTANT, "request_started_at": INSTANT, "accepted_at": INSTANT,
    "reason_code": REASON, "finalized_next_attempt_at": INSTANT,
    "reconciliation_resolution": CODE, "reconciled_at": INSTANT,
})

ROWS: Mapping[str, Record] = {
    "eventos": Record("evento", {
        "id": UUID, "event_type": PROVIDER_EVENT, "received_at": INSTANT,
        "processed_at": INSTANT, "processing_status": CODE, "processing_error": REASON,
        "creation_date_ms": EPOCH_MS, "approved_date_ms": EPOCH_MS,
        "purchase_shape_ok": BOOLEAN, "product_id": PRODUCT_ID, "offer_code": OFFER,
        "purchase_status": PURCHASE_STATUS, "person_key": PERSON_KEY,
        "digits_key": PERSON_KEY, "email_key": PERSON_KEY, "funnel_ref": KEY,
        "cart_correlation": Record("correlacion_del_carrito", {
            "outcome": CODE, "purchase_intent_id": UUID, "reason_code": REASON,
            "observed_at": INSTANT,
        }),
        "payment_failure_correlation": Record("correlacion_del_pago_fallido", {
            "outcome": CODE, "purchase_intent_id": UUID,
        }),
    }),
    "conflictos_de_compra": Record("conflicto_de_compra", {
        "id": UUID, "existing_event_id": UUID, "detected_at": INSTANT, "resolved_at": INSTANT,
    }),
    "intenciones": Record("intencion", {
        "id": UUID, "funnel_ref": KEY, "product_ref": PRODUCT_CODE, "offer_ref": OFFER,
        "submitted_at": INSTANT, "lifecycle_state": CODE, "current_classification": CODE,
        "whatsapp_contact_authorized": BOOLEAN, "provisional": BOOLEAN,
        "provider_observed": BOOLEAN, "activation_authorized": BOOLEAN,
        "updated_at": INSTANT, "person_key": PERSON_KEY,
    }),
    "contactos": Record("contacto", {
        "id": UUID, "person_keys": ListOf(PERSON_KEY), "digits_keys": ListOf(PERSON_KEY),
        "email_keys": ListOf(PERSON_KEY),
    }),
    "casos": Record("caso", {
        "id": UUID, "contact_id": UUID, "abandonment_event_id": UUID,
        "purchase_event_id": UUID, "external_product_id": PRODUCT_ID, "offer_code": OFFER,
        "status": CODE, "grace_expires_at": INSTANT, "created_at": INSTANT,
        "closed_at": INSTANT, "won_at": INSTANT, "policy_key": KEY,
        "policy_version": POSITIVE, "trigger_kind": CODE, "purchase_correlation": REASON,
        "has_conversation": BOOLEAN,
        "binding": Record("binding", {
            "scope_key": KEY, "scope_version": POSITIVE, "source_event_id": UUID,
            "audience_mode": CODE, "audience_purchase_intent_id": UUID, "bound_at": INSTANT,
        }),
        "eventos": ListOf(Record("evento_del_caso", {
            "webhook_event_id": UUID, "event_role": CODE, "observed_at": INSTANT,
        })),
    }),
    "acciones": Record("accion", {
        "id": UUID, "recovery_case_id": UUID, "action_type": CODE, "status": CODE,
        "due_at": INSTANT, "expires_at": INSTANT, "step_key": CODE, "anchor_type": CODE,
        "anchor_observed_at": INSTANT, "next_attempt_at": INSTANT,
        "execution_attempt_count": COUNT, "terminal_reason": REASON,
        "executed_at": INSTANT, "created_at": INSTANT, "updated_at": INSTANT,
        "intentos": ListOf(Record("tramo_de_intentos", {
            "n": POSITIVE, "primero": _ATTEMPT, "ultimo": _ATTEMPT,
        })),
    }),
    "autorizaciones": Record("autorizacion", {
        "scope_key": KEY, "scope_version": POSITIVE, "action_id": UUID, "contact_id": UUID,
        "local_budget_date": DAY, "runtime_generation": COUNT, "authorized_at": INSTANT,
    }),
    "reevaluaciones": Record("tramo_de_reevaluaciones", {
        "recovery_case_id": UUID, "related_action_id": UUID, "event_type": CODE,
        "actor_type": CODE,
        "data": Record("datos_de_la_reevaluacion", {
            "decision": CODE, "reason_code": REASON, "policy_key": KEY,
            "policy_version": POSITIVE, "chatwoot_checked": BOOLEAN,
            "chatwoot_inbound_after_anchor": BOOLEAN,
            "chatwoot_human_activity_after_anchor": BOOLEAN,
            "chatwoot_automation_paused": BOOLEAN,
        }),
        "n": POSITIVE, "primero_at": INSTANT, "ultimo_at": INSTANT,
    }),
    "primer_contacto": Record("plan_del_primer_contacto", {
        "purchase_intent_id": UUID, "contact_id": UUID, "recovery_case_id": UUID,
        "scope_key": KEY, "scope_version": POSITIVE, "outcome": CODE, "reason_code": REASON,
        "created_at": INSTANT, "caso_status": CODE, "accion_status": CODE,
        "accion_terminal_reason": REASON, "accion_due_at": INSTANT,
        "accion_expires_at": INSTANT, "accion_cerrada_at": INSTANT,
        "request_started_at": INSTANT,
    }),
}

CONFIG = Record("config", {
    # Sin approved_by, channel_account_ref, la cuenta ni la bandeja (§6.2).
    "pilot_scope_versions": ListOf(Record("version_del_scope", {
        "scope_key": KEY, "version": POSITIVE, "status": CODE, "tenant_key": KEY,
        "source": SOURCE, "source_event_type": PROVIDER_EVENT,
        "additional_source_event_types": ListOf(PROVIDER_EVENT), "purpose": CODE,
        "timezone": ZONE, "external_product_id": PRODUCT_ID, "offer_code": OFFER,
        "additional_offer_codes": ListOf(OFFER), "policy_key": KEY,
        "policy_version": POSITIVE, "audience_mode": CODE,
        "max_outbound_request_starts_per_day": COUNT,
        "max_outbound_request_starts_total": COUNT, "published_at": INSTANT,
    })),
    "followup_policy_versions": ListOf(Record("version_de_la_politica", {
        "policy_key": KEY, "version": POSITIVE, "status": CODE, "purpose": CODE,
        "timezone": ZONE,
        "business_windows": ListOf(Record("ventana", {
            "days": ListOf(WEEKDAY), "start": HOUR_MINUTE, "end": HOUR_MINUTE,
        })),
        "grace_period": INTERVAL, "expires_after": INTERVAL,
        "max_automatic_messages": COUNT,
        "steps": ListOf(Record("paso", {"step_key": CODE, "mode": CODE})),
        "published_at": INSTANT,
    })),
    # Sin changed_by ni change_reason.
    "pilot_runtime_controls": ListOf(Record("control", {
        "scope_key": KEY, "scope_version": POSITIVE, "runtime_state": CODE,
        "generation": COUNT, "changed_at": INSTANT,
    })),
    "pilot_proactive_contact_caps": ListOf(Record("tope_por_persona", {
        "tenant_key": KEY, "max_request_starts": COUNT, "request_window": INTERVAL,
    })),
    # Sin actor.
    "pilot_control_events_control_level": ListOf(Record("evento_de_control", {
        "scope_key": KEY, "scope_version": POSITIVE, "event_type": CODE,
        "runtime_generation": COUNT, "reason_code": REASON, "to_state": STATE,
        "created_at": INSTANT,
    })),
}, required=(
    "pilot_scope_versions", "followup_policy_versions", "pilot_runtime_controls",
    "pilot_proactive_contact_caps", "pilot_control_events_control_level",
))

COUNTS = Record("conteos", {name: COUNT for name in (
    "eventos", "disparos_desde", "casos", "acciones", "intentos", "intentos_tramos",
    "envios", "reevaluaciones", "reevaluaciones_tramos", "autorizaciones",
    "primer_contacto", "intenciones", "contactos",
)})

# Lo que el constructor anota en _captured.fixture (§6.4, paso 8). Las
# constantes de cada seccion tienen la forma de su clave en la fila.
FIXTURE_META = Record("fixture", {
    "source_sha256": SHA256, "source_bytes": COUNT, "fixture_bytes": COUNT,
    "max_bytes": COUNT, "transformaciones": ListOf(CODE),
    "constantes": Record("constantes", {
        section: Record(f"constantes_de_{section}", {
            key: spec for key, spec in ROWS[section].fields.items() if isinstance(spec, Form)
        })
        for section in ROW_SECTIONS
    }),
    "recortes": Record("recortes", {
        section: Record(f"recorte_de_{section}", {"captura": COUNT, "fixture": COUNT})
        for section in ("primer_contacto", "intenciones")
    }),
}, required=("source_sha256", "fixture_bytes", "max_bytes", "transformaciones",
             "constantes", "recortes"))

CAPTURED = Record("_captured", {
    "origin": QUERY_TEXT, "query": KEY, "captured_at": INSTANT, "desde": INSTANT,
    "desde_origen": QUERY_TEXT, "siembra": INSTANT, "redactions": ListOf(QUERY_TEXT),
    "conteos": COUNTS,
    "censo_webhook_events": ListOf(Record("censo", {
        "source": SOURCE, "event_type": CENSUS_EVENT, "n": COUNT,
        "primero": INSTANT, "ultimo": INSTANT,
    })),
    "fixture": FIXTURE_META,
}, required=("query", "desde", "conteos"))

CAPTURE = Record("captura", {
    "_captured": CAPTURED, "config": CONFIG,
    **{section: ListOf(ROWS[section]) for section in ROW_SECTIONS},
}, required=TOP_SECTIONS)


# ---------------------------------------------------------------------------
# La validacion.
# ---------------------------------------------------------------------------

_SAFE_KEY = re.compile(r"[a-z_][a-z0-9_]{0,63}")
# Los separadores de un telefono no cortan la cuenta de digitos (+52 55 1234 5678).
_PHONE_SEPARATORS = frozenset(" \t-.()+/")
_DIGIT_RUN = 9


def _key_in_path(key: str) -> str:
    # Una clave fuera de la lista puede ser un dato (un objeto con un telefono
    # como clave): en el camino va solo si tiene forma de nombre de columna.
    return key if _SAFE_KEY.fullmatch(key) else "<clave con forma no permitida>"


def _join(path: str, key: str) -> str:
    key = _key_in_path(key)
    return f"{path}.{key}" if path else key


def _longest_digit_run(text: str) -> int:
    longest = run = 0
    for character in text:
        if character in _PHONE_SEPARATORS:
            continue
        run = run + 1 if character.isdigit() else 0
        longest = max(longest, run)
    return longest


def _leaf_problem(value: Any, form: Form) -> str | None:
    if form.kind == "bool":
        return None if isinstance(value, bool) else f"se esperaba {form.name}"
    if form.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"se esperaba {form.name}"
        if not form.minimum <= value <= form.maximum:
            return f"{form.name} fuera de rango"
        return None
    if not isinstance(value, str):
        return f"se esperaba {form.name}"
    if "@" in value:
        return "tiene una arroba"
    assert form.pattern is not None
    if not form.pattern.fullmatch(value):
        return f"no tiene la forma {form.name}"
    if form.instant and _instant_or_none(value) is None:
        return f"{form.name} no es una hora que exista"
    if form.day and _day_or_none(value) is None:
        return f"{form.name} no es una fecha que exista"
    if not form.fixed and _longest_digit_run(value) >= _DIGIT_RUN:
        return "tiene nueve o mas digitos seguidos"
    return None


def _walk(value: Any, spec: Spec, path: str, out: list[ShapeProblem]) -> None:
    if value is None:
        return
    if isinstance(spec, Form):
        reason = _leaf_problem(value, spec)
        if reason is not None:
            out.append(ShapeProblem(path, reason))
        return
    if isinstance(spec, ListOf):
        if not isinstance(value, list):
            out.append(ShapeProblem(path, "se esperaba una lista"))
            return
        for index, item in enumerate(value):
            where = f"{path}[{index}]"
            if item is None:
                out.append(ShapeProblem(where, "un nulo dentro de una lista"))
            else:
                _walk(item, spec.item, where, out)
        return
    if not isinstance(value, dict):
        out.append(ShapeProblem(path, f"se esperaba un objeto ({spec.name})"))
        return
    for key in spec.required:
        if value.get(key) is None:
            out.append(ShapeProblem(_join(path, key), "falta"))
    for key, item in value.items():
        where = _join(path, key)
        child = spec.fields.get(key)
        if child is None:
            out.append(ShapeProblem(where, f"clave fuera de la lista de {spec.name}"))
        else:
            _walk(item, child, where, out)


def shape_problems(data: Any) -> list[ShapeProblem]:
    """Todos los problemas de forma de una captura o de un fixture, en el orden del documento."""
    out: list[ShapeProblem] = []
    _walk(data, CAPTURE, "", out)
    if data is None:
        out.append(ShapeProblem("", "se esperaba un objeto (captura)"))
    return out


def validate_shape(data: Any) -> None:
    """Levanta ``CaptureShapeError`` con el camino del primer problema; nunca dice el valor."""
    problems = shape_problems(data)
    if problems:
        raise CaptureShapeError(tuple(problems))


def is_fixture(data: Mapping[str, Any]) -> bool:
    """Si es un fixture del constructor (trae ``_captured.fixture``) y no la captura completa."""
    captured = data.get("_captured")
    return isinstance(captured, Mapping) and captured.get("fixture") is not None


# ---------------------------------------------------------------------------
# Las horas.
# ---------------------------------------------------------------------------

_INSTANT_PARTS = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2})T([0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,6}))?(?:Z|\+00:00|\+00)"
)


def _instant_or_none(text: str) -> datetime | None:
    match = _INSTANT_PARTS.fullmatch(text)
    if match is None:
        return None
    day, clock, fraction = match.groups()
    try:
        return datetime.fromisoformat(f"{day}T{clock}.{(fraction or '').ljust(6, '0')}+00:00")
    except ValueError:
        return None


def _day_or_none(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def parse_instant(text: str) -> datetime:
    """Una hora de la captura o del fixture, con zona UTC."""
    instant = _instant_or_none(text)
    if instant is None:
        raise CaptureFixtureError("no es una hora en UTC de la captura")
    return instant.astimezone(timezone.utc)


def _z(text: str) -> str:
    # De '2026-10-08T22:42:19.89+00:00' a '2026-10-08T22:42:19.890000Z'.
    match = _INSTANT_PARTS.fullmatch(text)
    assert match is not None
    day, clock, fraction = match.groups()
    return f"{day}T{clock}.{(fraction or '').ljust(6, '0')}Z"


def _times_in_z(value: Any, spec: Spec) -> Any:
    if value is None:
        return None
    if isinstance(spec, Form):
        return _z(value) if spec.instant else value
    if isinstance(spec, ListOf):
        return [_times_in_z(item, spec.item) for item in value]
    return {key: _times_in_z(item, spec.fields[key]) for key, item in value.items()}


# ---------------------------------------------------------------------------
# El constructor.
# ---------------------------------------------------------------------------

def _without_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_nulls(item) for item in value]
    return value


def _same(left: Any, right: Any) -> bool:
    # True no es 1: el tipo cuenta.
    return type(left) is type(right) and left == right


def _trim_seed(data: dict[str, Any]) -> dict[str, dict[str, int]]:
    """El paso 2 de §6.4: deja del primer contacto y de las intenciones lo que T1 lee."""
    desde = parse_instant(data["_captured"]["desde"])
    plans: list[dict[str, Any]] = []
    for index, row in enumerate(data["primer_contacto"]):
        if parse_instant(_present(row, "created_at", f"primer_contacto[{index}]")) >= desde:
            plans.append({key: row[key] for key in FIRST_CONTACT_KEYS if key in row})
    touched = {plan["purchase_intent_id"] for plan in plans if plan.get("purchase_intent_id")}
    for event in firings(data):
        for name in ("cart_correlation", "payment_failure_correlation"):
            correlation = event.get(name) or {}
            if correlation.get("purchase_intent_id"):
                touched.add(correlation["purchase_intent_id"])
    intents = [
        {key: row[key] for key in INTENT_KEYS if key in row}
        for row in data["intenciones"] if row.get("id") in touched
    ]
    trims = {
        "primer_contacto": {"captura": len(data["primer_contacto"]), "fixture": len(plans)},
        "intenciones": {"captura": len(data["intenciones"]), "fixture": len(intents)},
    }
    data["primer_contacto"] = plans
    data["intenciones"] = intents
    return trims


def _extract_constants(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """El paso 3 de §6.4: una columna con el mismo valor en todas las filas de su seccion.

    Solo en las secciones de filas, con dos filas o mas, y solo las hojas (no
    un objeto ni una lista). Corre despues de sacar los nulos: una columna
    ausente en alguna fila no es constante.
    """
    constants: dict[str, dict[str, Any]] = {}
    for section in ROW_SECTIONS:
        rows = data[section]
        if len(rows) < 2:
            continue
        fields = ROWS[section].fields
        found = {
            key: value for key, value in rows[0].items()
            if isinstance(fields[key], Form)
            and all(key in row and _same(row[key], value) for row in rows)
        }
        if found:
            for row in rows:
                for key in found:
                    del row[key]
            constants[section] = found
    return constants


def serialize(data: Any) -> bytes:
    """Compacta y determinista: claves ordenadas, sin espacios y con un salto de linea al final."""
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)
    return (text + "\n").encode("utf-8")


def section_sizes(data: Mapping[str, Any]) -> dict[str, int]:
    """Cuanto pesa cada seccion, compacta, sin el salto de linea."""
    return {key: len(serialize(value)) - 1 for key, value in data.items()}


def build_fixture(capture: Mapping[str, Any], *, source_sha256: str,
                  source_bytes: int | None = None) -> dict[str, Any]:
    """Arma el fixture recortado de una captura completa, con los pasos de §6.4.

    1. valida la forma (una clave fuera de la lista aborta: no se descarta en
       silencio);
    2. recorta lo que T1 no lee de la siembra (el primer contacto y las
       intenciones);
    3. pasa las horas a ``YYYY-MM-DDTHH:MM:SS.ffffffZ``;
    4. saca los nulos;
    5. saca las columnas constantes a ``_captured.fixture.constantes``;
    6. y 7. serializa compacto y mide: si pasa de ``FIXTURE_MAX_BYTES``,
       ``FixtureTooLargeError`` con el peso de cada seccion;
    8. anota en ``_captured.fixture`` el sha256 y el peso de la captura, el
       del fixture, las constantes, los recortes y las transformaciones.

    ``_captured.conteos`` queda como lo midio la consulta: el recorte de las
    dos secciones se lee en ``_captured.fixture.recortes``.
    """
    validate_shape(capture)
    if is_fixture(capture):
        raise CaptureFixtureError(
            "_captured.fixture: la entrada ya es un fixture; se arma desde la captura")
    for name, value, form in (("source_sha256", source_sha256, SHA256),
                              ("source_bytes", source_bytes, COUNT)):
        reason = None if value is None and name == "source_bytes" else _leaf_problem(value, form)
        if reason is not None:
            raise CaptureFixtureError(f"{name}: {reason}")
    data: dict[str, Any] = copy.deepcopy(dict(capture))
    trims = _trim_seed(data)
    data = _times_in_z(data, CAPTURE)
    data = _without_nulls(data)
    constants = _extract_constants(data)
    meta: dict[str, Any] = {
        "source_sha256": source_sha256,
        "max_bytes": FIXTURE_MAX_BYTES,
        "constantes": constants,
        "recortes": trims,
        "transformaciones": list(TRANSFORMATIONS),
        "fixture_bytes": 0,
    }
    if source_bytes is not None:
        meta["source_bytes"] = source_bytes
    data["_captured"]["fixture"] = meta
    # El peso del fixture va adentro del fixture: se itera hasta que el numero
    # escrito es el peso con ese numero escrito (dos vueltas, salvo que cambie
    # la cantidad de cifras).
    for _ in range(8):
        size = len(serialize(data))
        if meta["fixture_bytes"] == size:
            break
        meta["fixture_bytes"] = size
    else:  # pragma: no cover - el peso converge en dos o tres vueltas
        raise CaptureFixtureError("fixture_bytes: el peso no converge")
    if size > FIXTURE_MAX_BYTES:
        raise FixtureTooLargeError(size, FIXTURE_MAX_BYTES, section_sizes(data))
    validate_shape(data)
    return data


def restore_fixture(data: Mapping[str, Any]) -> dict[str, Any]:
    """Vuelve a poner las constantes en cada fila de su seccion (§6.5).

    Una clave ausente se lee como nula, asi que los nulos no se reponen. Con
    una captura completa (sin ``_captured.fixture``) devuelve una copia igual.
    """
    out: dict[str, Any] = copy.deepcopy(dict(data))
    if not is_fixture(out):
        return out
    constants = out["_captured"]["fixture"].get("constantes") or {}
    for section, values in constants.items():
        for index, row in enumerate(out.get(section) or []):
            for key, value in values.items():
                if key in row:
                    raise CaptureFixtureError(
                        f"{section}[{index}].{_key_in_path(key)}: la constante ya esta en la fila")
                row[key] = copy.deepcopy(value)
    return out


# ---------------------------------------------------------------------------
# Lo que se recuenta.
# ---------------------------------------------------------------------------

def _present(row: Mapping[str, Any], key: str, where: str) -> Any:
    # Las filas no exigen claves (una constante sale de la fila y un nulo es
    # una ausencia): lo que se recuenta las pide aca, con el camino.
    value = row.get(key)
    if value is None:
        raise CaptureFixtureError(f"{where}.{key}: falta")
    return value


def firings(data: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """Los disparos: los carritos y los pagos fallidos recibidos desde «desde» (§6.5).

    Sobre una captura completa o un fixture restaurado (``restore_fixture``).
    """
    desde = parse_instant(data["_captured"]["desde"])
    for index, event in enumerate(data["eventos"]):
        if event.get("event_type") not in FIRING_EVENT_TYPES:
            continue
        if parse_instant(_present(event, "received_at", f"eventos[{index}]")) >= desde:
            yield event


def count_sections(data: Mapping[str, Any]) -> dict[str, int]:
    """Los conteos de ``_captured.conteos``, recontados desde las filas.

    Sobre una captura completa o un fixture restaurado. Los tramos se expanden
    para contar: los intentos y las reevaluaciones suman su ``n``, y un envio
    es un intento con ``request_started_at`` (en un tramo, todos sus intentos
    tienen la misma fase y el mismo resultado).
    """
    tramos = [
        (f"acciones[{index}].intentos[{position}]", tramo)
        for index, action in enumerate(data["acciones"])
        for position, tramo in enumerate(action.get("intentos") or [])
    ]
    attempts = [(_present(tramo, "n", where), tramo) for where, tramo in tramos]
    reevaluations = [_present(row, "n", f"reevaluaciones[{index}]")
                     for index, row in enumerate(data["reevaluaciones"])]
    return {
        "eventos": len(data["eventos"]),
        "disparos_desde": sum(1 for _ in firings(data)),
        "casos": len(data["casos"]),
        "acciones": len(data["acciones"]),
        "intentos": sum(n for n, _ in attempts),
        "intentos_tramos": len(attempts),
        "envios": sum(n for n, tramo in attempts
                      if (tramo.get("primero") or {}).get("request_started_at") is not None),
        "reevaluaciones": sum(reevaluations),
        "reevaluaciones_tramos": len(reevaluations),
        "autorizaciones": len(data["autorizaciones"]),
        "primer_contacto": len(data["primer_contacto"]),
        "intenciones": len(data["intenciones"]),
        "contactos": len(data["contactos"]),
    }
