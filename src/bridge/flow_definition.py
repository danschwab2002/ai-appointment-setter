"""El esquema de un flujo como dato (setter-flujo/v1).

Contrato: docs/contracts/flow-definition-v1.md («La definicion formal» y «Las
reglas del validador»); los items de una propuesta, docs/contracts/
flow-change-cycle-v1.md («Que lleva una propuesta»). Este modulo tiene los
tipos, el parseo estricto, las duraciones, las horas, la normalizacion, el
texto libre y el diff por JSON Pointer. Las reglas que cruzan flujos, el
catalogo de plantillas, las advertencias y el ciclo viven en
``flow_validator``.

Es puro: no lee el entorno, no hace red ni toca bases. Nada del bridge lo
importa todavia (pieza 1 de la 1b, el almacen).

Las rutas de los hallazgos son JSON Pointer sobre la lista de items de la
propuesta: ``/0/definicion/topes/bolsa`` es la bolsa del primer item.
"""

from __future__ import annotations

import copy
import functools
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from zoneinfo import available_timezones

from .automation_expert_read import looks_like_identifier

SCHEMA = "setter-flujo/v1"

# Las formas. Se usan con fullmatch: un "$" de re acepta un salto de linea al
# final, y la base compara con el texto entero.
_FLOW_KEY = re.compile(r"[a-z][a-z0-9_]{1,39}")
_PRODUCT = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")
_POOL = re.compile(r"[a-z][a-z0-9_-]{1,39}")
_DURATION = re.compile(r"(0|[1-9][0-9]{0,4})(m|h|d)")
_HOUR = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
_TEMPLATE_NAME = re.compile(r"[a-z0-9_]{1,512}")
_LANGUAGE = re.compile(r"[a-z]{2}(?:_[A-Z]{2})?")
# La forma del cupon que acepta el boton del seguimiento
# (followup_discount.COUPON_CODE_RE).
_COUPON = re.compile(r"[A-Za-z0-9_-]{1,64}")
# boton.cupon: personal es el codigo por persona que crea el worker del cupon en
# Hotmart y que vence (contrato 1.1, «Los pasos»). Tiene la forma de un codigo,
# asi que la forma no cambia; lo distinguen la regla 15 y la politica compilada.
PERSONAL_COUPON = "personal"
# A lo sumo dos cupones personales por camino: dos codigos por caso (regla del 17/10).
PERSONAL_COUPONS_PER_PATH = 2
# El codigo de Hotmart del medio de pago (CASHPAYMENT, MERCADO_PAGO...).
_PAYMENT_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,39}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_REF = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")
# Las formas que exige _flow_change_findings_are_valid: el codigo con la forma
# de los motivos, y la ruta, un JSON Pointer (RFC 6901) vacio o con ~0 y ~1.
_CODE = re.compile(r"[a-z][a-z0-9_:]{0,79}")
_POINTER = re.compile(r"(/([^/~]|~[01])*)*")
_OK_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

EVENTS = ("carrito", "pago_fallido", "ficha_impresa", "ficha_vencida", "compra", "intencion")
RECOVERY_EVENTS = ("carrito", "pago_fallido", "ficha_impresa", "ficha_vencida", "intencion")
FICHA_EVENTS = ("ficha_impresa", "ficha_vencida")
LAST_SPEAKERS = ("agente", "persona")
CONVERSATION_STATES = ("abierta", "resuelta")
PERMISSIONS = ("formulario", "conversacion", "compra")
EXITS = ("compra", "contesta", "ficha_vencida")
# cuando, precio y vence (1.1) son las del bridge para A1, Z y el seguimiento del pago
# rechazado (cart_first_touch.CART_DISCOUNT_VARIABLES, cart_followups.CART_LAST_TOUCH_VARIABLES,
# payment_failure_followups.PAYMENT_FAILURE_FOLLOWUP_VARIABLES): cuando, el momento del
# intento (el abandono) en la zona del pais del comprador; precio, el precio del producto que
# declara la instancia (RECOVERY_DISCOUNT_PRICE, «USD 47»); vence, el vencimiento del cupon
# personal en la zona del pais (cart_discount.cart_discount_values).
VARIABLES = ("nombre", "producto", "medio_de_pago", "cuando", "precio", "vence")
# Las que solo valen en un paso con cupon: hablan del cupon personal (regla 5).
COUPON_VARIABLES = ("precio", "vence")
# La que solo vale con un disparador de evento: es la hora del evento (regla 5).
EVENT_VARIABLES = ("cuando",)
LINKS = ("checkout", "ficha")
CONDITION_DATA = ("ventana", "medio_de_pago")
WINDOW_VALUES = ("abierta", "cerrada")
REPEAT_UNITS = ("persona", "persona_y_oferta", "conversacion")
FLOW_STATES = ("apagado", "prendido", "pausado")
STEP_TYPES = ("esperar", "plantilla", "conversar", "condicion", "derivar")
ITEM_TYPES = ("version", "volver", "prender", "apagar")
# El contrato del ciclo nombra tres; hasta el motor solo se acepta seguir
# (flow_validator, en_curso_not_supported).
IN_FLIGHT_POLICIES = ("seguir", "cancelar", "migrar")

NO_CAP = "sin_tope"
NEVER = "nunca"
ALWAYS = "siempre"
DRAFT = "borrador"
PUBLISHED = "publicada"
UNRESOLVED_VALUES = ("a_medir", "a_decidir")
TO_CREATE = "a_crear"

DEFAULT_VARIABLES = ("nombre", "producto")
NAME_MAX_CHARS = 60
GOAL_MAX_CHARS = 300
SILENCE_MAX_MINUTES = 30 * 24 * 60
DAY_MINUTES = 24 * 60
# La forma minima que la base mira en un item (_flow_change_items_are_valid).
DEFINITION_MAX_BYTES = 32 * 1024
ITEMS_MAX_BYTES = 256 * 1024
ITEMS_MAX = 10
CLIENT_OK_TEXT_MAX_CHARS = 120
# El esquema llega a unos siete niveles (pasos, condicion, rama, boton). Mas
# alla de esto la definicion se rechaza entera, antes de recorrerla.
MAX_DEPTH = 32
MAX_PATH_CHARS = 512
MAX_TEXT_CHARS = 300

_TOP_KEYS = (
    "esquema", "flujo", "producto", "nombre", "version", "estado", "dispara", "entra",
    "pasos", "termina", "horario", "topes", "prioridad",
)
_STEP_KEYS: Mapping[str, tuple[str, ...]] = {
    "esperar": ("esperar",),
    "plantilla": ("plantilla", "idioma", "variables", "boton"),
    "conversar": ("conversar",),
    "condicion": ("condicion", "entonces", "si_no"),
    "derivar": ("derivar",),
}
_UNIT_MINUTES = {"m": 1, "h": 60, "d": DAY_MINUTES}

KIND_FORM = "forma"
KIND_PROTECTION = "proteccion"
KIND_NOT_YET = "todavia_no"
KIND_CYCLE = "ciclo"
KIND_WARNING = "advertencia"
FINDING_KINDS = (KIND_FORM, KIND_PROTECTION, KIND_NOT_YET, KIND_CYCLE, KIND_WARNING)


# ---------------------------------------------------------------------------
# Los hallazgos.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    """Un bloqueo o una advertencia, con la forma que acepta la base.

    ``to_json`` da lo que acepta ``_flow_change_findings_are_valid``: las
    claves ``rule``, ``code``, ``kind`` y ``path`` siempre, y ``limit``,
    ``reason`` y ``alternative`` cuando hay texto.
    """

    rule: int | None
    code: str
    kind: str
    path: str
    limit: str | None = None
    reason: str | None = None
    alternative: str | None = None

    def __post_init__(self) -> None:
        if self.rule is not None and (not _is_int(self.rule) or not 1 <= self.rule <= 17):
            raise ValueError("finding_rule_invalid")
        if not isinstance(self.code, str) or not _CODE.fullmatch(self.code):
            raise ValueError("finding_code_invalid")
        if self.kind not in FINDING_KINDS:
            raise ValueError("finding_kind_invalid")
        if not isinstance(self.path, str) or len(self.path) > MAX_PATH_CHARS or not _POINTER.fullmatch(
            self.path
        ):
            raise ValueError("finding_path_invalid")
        for text in (self.limit, self.reason, self.alternative):
            if text is not None and not is_clean_text(text, MAX_TEXT_CHARS):
                raise ValueError("finding_text_invalid")

    def to_json(self) -> dict[str, object]:
        out: dict[str, object] = {
            "rule": self.rule, "code": self.code, "kind": self.kind, "path": self.path,
        }
        if self.limit is not None:
            out["limit"] = self.limit
        if self.reason is not None:
            out["reason"] = self.reason
        if self.alternative is not None:
            out["alternative"] = self.alternative
        return out

    @property
    def item_index(self) -> int | None:
        """El indice del item al que apunta la ruta, si apunta a uno."""
        parts = self.path.split("/")
        if len(parts) >= 2 and parts[1].isascii() and parts[1].isdigit():
            return int(parts[1])
        return None

    @property
    def definition_path(self) -> str | None:
        """La ruta adentro de la definicion del item (``/topes/bolsa``), o None."""
        parts = self.path.split("/")
        if len(parts) >= 3 and self.item_index is not None and parts[2] == "definicion":
            return "".join("/" + part for part in parts[3:])
        return None


@dataclass(frozen=True)
class CodeSpec:
    """Un codigo del catalogo cerrado, con su regla, su tipo y sus textos."""

    code: str
    rule: int | None
    kind: str
    reason: str
    limit: str | None = None
    alternative: str | None = None

    def at(self, path: str, *, reason: str | None = None) -> Finding:
        return Finding(
            rule=self.rule, code=self.code, kind=self.kind, path=path, limit=self.limit,
            reason=reason if reason is not None else self.reason, alternative=self.alternative,
        )


def _spec(code: str, rule: int | None, kind: str, reason: str, *,
          limit: str | None = None, alternative: str | None = None) -> CodeSpec:
    return CodeSpec(code=code, rule=rule, kind=kind, reason=reason, limit=limit,
                    alternative=alternative)


UNKNOWN_KEY = _spec(
    "unknown_key", 1, KIND_FORM,
    "Esa clave no está en el esquema setter-flujo/v1, y una clave desconocida rechaza la definición entera.",
    alternative="Sacar la clave, o escribirla con el nombre que tiene en el esquema.",
)
INVALID_VALUE = _spec(
    "invalid_value", 1, KIND_FORM,
    "El valor no tiene la forma que el esquema pide para esa clave.",
    alternative="Escribirlo con una de las formas de la definición formal del esquema.",
)
DUPLICATE_KEY = _spec(
    "duplicate_key", 1, KIND_FORM,
    "La misma clave aparece dos veces en un objeto: lo que se aprobaría no sería exactamente lo que se guarda.",
    alternative="Dejar la clave una sola vez.",
)
REQUIRED_KEY_MISSING = _spec(
    "required_key_missing", 1, KIND_FORM,
    "Falta una clave obligatoria, y no tiene un valor por defecto que se pueda completar.",
    alternative="Completarla.",
)
REQUIRED_KEY_MISSING_CYCLE = _spec(
    "required_key_missing", None, KIND_CYCLE,
    "Un flujo con versión recibe la definición completa, desde la versión publicada y sin valores por defecto.",
    alternative="Copiar la clave de la versión publicada.",
)
VERSION_SET_BY_CYCLE = _spec(
    "version_set_by_cycle", 1, KIND_FORM,
    "La versión y el estado los pone el ciclo al publicar, nunca la conversación.",
    alternative="Sacar version y dejar estado en borrador.",
)
DEFAULT_POOL_INVALID = _spec(
    "default_pool_invalid", 1, KIND_FORM,
    "La bolsa por defecto, <producto>-<flujo>, no cumple la forma de una bolsa: empieza con una letra y tiene hasta 40 caracteres.",
    alternative="Escribir topes.bolsa.",
)
IDENTIFIER_NOT_ALLOWED = _spec(
    "identifier_not_allowed", None, KIND_CYCLE,
    "El texto tiene un tramo con forma de teléfono o un mail, y en una definición no van datos de una persona.",
    alternative="Escribirlo sin el teléfono ni el mail.",
)

# Los codigos que emite este modulo. flow_validator suma los suyos y arma el
# catalogo cerrado.
DEFINITION_CODES: tuple[CodeSpec, ...] = (
    UNKNOWN_KEY, INVALID_VALUE, DUPLICATE_KEY, REQUIRED_KEY_MISSING, REQUIRED_KEY_MISSING_CYCLE,
    VERSION_SET_BY_CYCLE, DEFAULT_POOL_INVALID, IDENTIFIER_NOT_ALLOWED,
)


# ---------------------------------------------------------------------------
# Las formas sueltas.
# ---------------------------------------------------------------------------

def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_unresolved(value: object) -> bool:
    """``a_medir`` o ``a_decidir``: un valor que todavia no se sabe."""
    return isinstance(value, str) and value in UNRESOLVED_VALUES


def parse_duration(value: object) -> int | None:
    """Los minutos de una duracion (``0m``, ``60m``, ``24h``, ``1d``), o None."""
    if not isinstance(value, str):
        return None
    match = _DURATION.fullmatch(value)
    if match is None:
        return None
    return int(match.group(1)) * _UNIT_MINUTES[match.group(2)]


def parse_hour(value: object) -> int | None:
    """Los minutos desde la medianoche de una hora ``HH:MM``, o None."""
    if not isinstance(value, str):
        return None
    match = _HOUR.fullmatch(value)
    if match is None:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


# Lo que no entra en ninguna cadena de un item: los controles (Cc), los de
# formato y direccion (Cf), un sustituto UTF-16 suelto (Cs, de U+D800 a U+DFFF:
# no se puede escribir en UTF-8 ni guardar en un jsonb) y los separadores de
# linea y de parrafo (Zl y Zp, U+2028 y U+2029: el texto es de una linea).
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def has_control_or_format(text: str) -> bool:
    """Si el texto tiene un caracter de categoria Unicode Cc, Cf, Cs, Zl o Zp.

    Es mas amplio que la clase de la base (``_flow_store_free_text_is_valid``),
    a proposito: incluye U+200D, asi que un emoji compuesto no entra.
    """
    return any(unicodedata.category(character) in _FORBIDDEN_CATEGORIES for character in text)


def is_clean_text(text: object, max_chars: int) -> bool:
    """Texto de una linea, sin controles ni bidi, de 1 a ``max_chars`` caracteres."""
    return (
        isinstance(text, str)
        and bool(text.strip())
        and len(text) <= max_chars
        and not has_control_or_format(text)
    )


@functools.lru_cache(maxsize=1)
def _iana_zones() -> frozenset[str]:
    return frozenset(available_timezones())


def is_iana_zone(value: object) -> bool:
    return isinstance(value, str) and value in _iana_zones()


def is_flow_key(value: object) -> bool:
    return isinstance(value, str) and _FLOW_KEY.fullmatch(value) is not None


def is_product_ref(value: object) -> bool:
    return isinstance(value, str) and _PRODUCT.fullmatch(value) is not None


def is_pool(value: object) -> bool:
    return isinstance(value, str) and _POOL.fullmatch(value) is not None


def is_template_name(value: object) -> bool:
    return isinstance(value, str) and _TEMPLATE_NAME.fullmatch(value) is not None


def is_coupon_code(value: object) -> bool:
    return isinstance(value, str) and _COUPON.fullmatch(value) is not None


def is_personal_coupon(value: object) -> bool:
    """Si ``boton.cupon`` es el codigo por persona (``personal``), no un codigo fijo."""
    return value == PERSONAL_COUPON


def is_flow_id(value: object) -> bool:
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def parse_flow_ref(value: object, own_product: str | None) -> tuple[str, str] | None:
    """(producto, flujo) de una referencia: ``carrito`` o ``att2/carrito``.

    Sin producto propio, una referencia sin producto no se puede resolver.
    """
    if not isinstance(value, str):
        return None
    if "/" in value:
        product, _, key = value.partition("/")
        if is_product_ref(product) and is_flow_key(key):
            return product, key
        return None
    if is_flow_key(value) and own_product is not None:
        return own_product, value
    return None


def is_flow_ref_shape(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if "/" in value:
        product, _, key = value.partition("/")
        return is_product_ref(product) and is_flow_key(key)
    return is_flow_key(value)


def step_type(step: object) -> str | None:
    """El tipo de un paso: la unica clave de tipo que tiene, o None."""
    if not isinstance(step, Mapping):
        return None
    types = [kind for kind in STEP_TYPES if kind in step]
    return types[0] if len(types) == 1 else None


def depth_of(value: object) -> int:
    """Los niveles de anidamiento de un valor JSON, sin recursion."""
    deepest = 0
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, level = stack.pop()
        if isinstance(current, Mapping):
            children: Iterable[object] = current.values()
        elif isinstance(current, list):
            children = current
        else:
            deepest = max(deepest, level)
            continue
        deepest = max(deepest, level + 1)
        if level + 1 > MAX_DEPTH + 1:
            return level + 1
        stack.extend((child, level + 1) for child in children)
    return deepest


def json_size(value: object) -> int | None:
    """Los bytes del texto de un ``jsonb`` de Postgres (separadores con espacio, UTF-8).

    None si el valor no es JSON: un ``NaN``, un objeto de Python o un sustituto
    UTF-16 suelto, que no se puede escribir en UTF-8 (``UnicodeEncodeError``).
    """
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(", ", ": "), allow_nan=False)
        return len(text.encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        return None


# ---------------------------------------------------------------------------
# JSON Pointer.
# ---------------------------------------------------------------------------

Segment = str | int


def json_pointer(segments: Iterable[Segment]) -> str:
    """Un JSON Pointer (RFC 6901): ``~`` se escribe ``~0`` y ``/``, ``~1``."""
    return "".join(
        "/" + str(segment).replace("~", "~0").replace("/", "~1") for segment in segments
    )


def finding_path(segments: Sequence[Segment]) -> str:
    """La ruta de un hallazgo: hasta 512 caracteres y sin controles.

    Una clave desconocida puede traer cualquier cosa. Si la ruta no entra o
    tiene un control, el hallazgo apunta al padre.
    """
    current = list(segments)
    while current:
        pointer = json_pointer(current)
        if len(pointer) <= MAX_PATH_CHARS and not has_control_or_format(pointer):
            return pointer
        current.pop()
    return ""


_MISSING = object()


def _json_equal(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return left.keys() == right.keys() and all(_json_equal(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, (Mapping, list)) or isinstance(right, (Mapping, list)):
        return False
    return type(left) is type(right) and left == right


def json_equal(left: object, right: object) -> bool:
    """Igualdad de JSON: ``1`` no es ``true`` ni ``1.0``, y el orden de las claves no importa."""
    return _json_equal(left, right)


@dataclass(frozen=True)
class Change:
    """Una diferencia campo por campo: la ruta, el valor de antes y el de despues."""

    path: str
    before: object
    after: object


def diff(before: object, after: object, *, prefix: Sequence[Segment] = ()) -> tuple[Change, ...]:
    """Las diferencias entre dos valores JSON, por JSON Pointer.

    Una clave que falta de un lado va con ``None`` de ese lado y ``present``
    en la otra; una lista se compara posicion por posicion.
    """
    changes: list[Change] = []
    stack: list[tuple[tuple[Segment, ...], object, object]] = [(tuple(prefix), before, after)]
    while stack:
        segments, left, right = stack.pop()
        if isinstance(left, Mapping) and isinstance(right, Mapping):
            keys = list(left) + [key for key in right if key not in left]
            for key in reversed(keys):
                stack.append((segments + (key,), left.get(key, _MISSING), right.get(key, _MISSING)))
            continue
        if isinstance(left, list) and isinstance(right, list):
            for index in reversed(range(max(len(left), len(right)))):
                stack.append((
                    segments + (index,),
                    left[index] if index < len(left) else _MISSING,
                    right[index] if index < len(right) else _MISSING,
                ))
            continue
        if left is _MISSING and right is _MISSING:
            continue
        if left is _MISSING or right is _MISSING or not _json_equal(left, right):
            changes.append(Change(
                path=json_pointer(segments),
                before=None if left is _MISSING else left,
                after=None if right is _MISSING else right,
            ))
    return tuple(changes)


# ---------------------------------------------------------------------------
# El parseo estricto.
# ---------------------------------------------------------------------------

class StrictJSONError(ValueError):
    """El JSON no se puede leer (``invalid_json``) o repite una clave (``duplicate_key``)."""

    def __init__(self, code: str, path: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.path = path

    @property
    def finding(self) -> Finding | None:
        """El hallazgo de la regla 1 para una clave repetida, con su ruta en el documento."""
        if self.code != DUPLICATE_KEY.code:
            return None
        return DUPLICATE_KEY.at(self.path)


class _DuplicatedObject(dict):  # type: ignore[type-arg]
    duplicate_key: str = ""


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    duplicate: str | None = None
    for key, value in pairs:
        if key in seen and duplicate is None:
            duplicate = key
        seen[key] = value
    if duplicate is None:
        return seen
    marked = _DuplicatedObject(seen)
    marked.duplicate_key = duplicate
    return marked


def _reject_constant(name: str) -> object:
    raise ValueError(f"non_finite_number:{name}")


def _finite_float(text: str) -> float:
    value = float(text)
    if value in (float("inf"), float("-inf")) or value != value:
        raise ValueError("non_finite_number")
    return value


def _is_utf8_text(text: str) -> bool:
    """Si el texto se puede escribir en UTF-8: no tiene un sustituto UTF-16 suelto."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _has_lone_surrogate(value: object) -> bool:
    """Si alguna clave o cadena del valor JSON tiene un sustituto UTF-16 suelto."""
    stack: list[object] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            if not _is_utf8_text(current):
                return True
        elif isinstance(current, dict):
            if not all(_is_utf8_text(key) for key in current):
                return True
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return False


def loads_strict(text: str | bytes) -> Any:
    """Lee JSON sin perdonar: una clave repetida es ``duplicate_key``, con su ruta.

    ``NaN``, ``Infinity`` y un numero que no entra en un float son
    ``invalid_json``, y tambien un sustituto UTF-16 suelto en una clave o en
    una cadena (``\\ud83d`` sin su pareja): ``json.loads`` lo decodifica, pero
    no es texto, no se escribe en UTF-8 y un ``jsonb`` no lo guarda. Los
    enteros quedan ``int``; que un valor sea entero y no ``bool`` ni ``float``
    lo mira la regla de cada clave.
    """
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError as error:
            raise StrictJSONError("invalid_json") from error
    if not isinstance(text, str):
        raise StrictJSONError("invalid_json")
    try:
        value = json.loads(
            text, object_pairs_hook=_object_pairs, parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (ValueError, RecursionError) as error:
        raise StrictJSONError("invalid_json") from error
    # Un documento con un sustituto suelto no es texto: se rechaza entero, antes
    # que una clave repetida.
    if _has_lone_surrogate(value):
        raise StrictJSONError("invalid_json")
    # La primera clave repetida, en el orden del documento.
    stack: list[tuple[tuple[Segment, ...], object]] = [((), value)]
    while stack:
        segments, current = stack.pop()
        if isinstance(current, _DuplicatedObject):
            raise StrictJSONError(
                DUPLICATE_KEY.code, finding_path(segments + (current.duplicate_key,)),
            )
        if isinstance(current, dict):
            stack.extend((segments + (key,), child) for key, child in reversed(list(current.items())))
        elif isinstance(current, list):
            stack.extend((segments + (index,), child) for index, child in reversed(list(enumerate(current))))
    return value


# ---------------------------------------------------------------------------
# La instancia, el catalogo y el mundo.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InstanceView:
    """La instancia: el par (tenant, aliada), sus productos y la zona por defecto.

    No supone un solo producto. En esta pieza los productos los da quien
    llama; la fuente (los bindings activos o el manifiesto) es de la pieza 2.
    """

    tenant_ref: str
    ally_ref: str
    products: frozenset[str]
    time_zone: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_ref, str) or not _REF.fullmatch(self.tenant_ref):
            raise ValueError("instance_tenant_ref_invalid")
        if not isinstance(self.ally_ref, str) or not _REF.fullmatch(self.ally_ref):
            raise ValueError("instance_ally_ref_invalid")
        if not isinstance(self.products, frozenset) or not self.products or not all(
            is_product_ref(product) for product in self.products
        ):
            raise ValueError("instance_products_invalid")
        if self.time_zone is not None and not isinstance(self.time_zone, str):
            raise ValueError("instance_time_zone_invalid")

    @property
    def single_product(self) -> str | None:
        return next(iter(self.products)) if len(self.products) == 1 else None


@dataclass(frozen=True)
class TemplateCatalog:
    """El catalogo de plantillas de la bandeja, como lo publica Chatwoot.

    ``catalog_sha256`` es el sha256 del JSON canonico de ``message_templates``
    (claves ordenadas, sin espacios, UTF-8): la huella de lo que se miro.
    """

    templates: tuple[Mapping[str, Any], ...]
    catalog_sha256: str

    @classmethod
    def from_inbox_payload(cls, payload: object) -> TemplateCatalog:
        """Lee ``GET /inboxes/<id>`` de Chatwoot.

        Sin ``message_templates``, o con algo que no se puede escribir como JSON
        en UTF-8 (un sustituto UTF-16 suelto, un valor que no es JSON), no hay
        huella de lo que se miro: ``ValueError("template_catalog_unavailable")``.
        """
        if not isinstance(payload, Mapping):
            raise ValueError("template_catalog_unavailable")
        templates = payload.get("message_templates")
        if not isinstance(templates, list):
            raise ValueError("template_catalog_unavailable")
        try:
            canonical = json.dumps(templates, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        except (TypeError, ValueError, RecursionError) as error:
            raise ValueError("template_catalog_unavailable") from error
        return cls(
            templates=tuple(template for template in templates if isinstance(template, Mapping)),
            catalog_sha256=fingerprint,
        )

    def named(self, name: str) -> tuple[Mapping[str, Any], ...]:
        return tuple(template for template in self.templates if template.get("name") == name)

    def languages(self, name: str) -> tuple[str, ...]:
        """Los idiomas en que la plantilla esta en el catalogo, sin repetir."""
        found: list[str] = []
        for template in self.named(name):
            language = template.get("language")
            if isinstance(language, str) and language.strip() not in found:
                found.append(language.strip())
        return tuple(found)


@dataclass(frozen=True)
class WorldFlow:
    """Un flujo de la instancia en el almacen: su control y sus versiones publicadas.

    ``versions`` son las definiciones publicadas, normalizadas, con su numero.
    Tiene que traer al menos la activa, la mas vieja con un horario objeto (o
    ``declared_fixed_zone``) y el destino de un ``volver``.
    """

    flow_id: str
    product_ref: str
    flow_key: str
    state: str = "apagado"
    active_version: int | None = None
    generation: int = 0
    versions: Mapping[int, Mapping[str, Any]] = field(default_factory=dict)
    declared_fixed_zone: str | None = None

    def __post_init__(self) -> None:
        if not is_flow_id(self.flow_id):
            raise ValueError("world_flow_id_invalid")
        if not is_product_ref(self.product_ref) or not is_flow_key(self.flow_key):
            raise ValueError("world_flow_identity_invalid")
        if self.state not in FLOW_STATES:
            raise ValueError("world_flow_state_invalid")
        if not _is_int(self.generation) or self.generation < 0:
            raise ValueError("world_flow_generation_invalid")
        if not isinstance(self.versions, Mapping) or not all(
            _is_int(number) and number > 0 and isinstance(definition, Mapping)
            for number, definition in self.versions.items()
        ):
            raise ValueError("world_flow_versions_invalid")
        if self.active_version is None:
            if self.state != "apagado":
                raise ValueError("world_flow_on_without_version")
        elif not _is_int(self.active_version) or self.active_version not in self.versions:
            raise ValueError("world_flow_active_version_missing")
        computed = self._zone_from_versions()
        if computed is not None and self.declared_fixed_zone is not None and (
            computed != self.declared_fixed_zone
        ):
            raise ValueError("world_flow_fixed_zone_mismatch")

    def _zone_from_versions(self) -> str | None:
        for number in sorted(self.versions):
            horario = self.versions[number].get("horario")
            if isinstance(horario, Mapping) and isinstance(horario.get("zona"), str):
                return horario["zona"]
        return None

    @property
    def active_definition(self) -> Mapping[str, Any] | None:
        return None if self.active_version is None else self.versions[self.active_version]

    @property
    def fixed_zone(self) -> str | None:
        """La zona del flujo: la de su version mas vieja con un horario objeto.

        Es la del invariante 4 de la base: se elige al crear el flujo y despues
        no cambia, aunque en el medio haya versiones con ``siempre``.
        """
        computed = self._zone_from_versions()
        return computed if computed is not None else self.declared_fixed_zone


class FlowWorld:
    """La foto del almacen para una instancia: todos sus flujos, de todos sus productos."""

    def __init__(self, flows: Iterable[WorldFlow]) -> None:
        self._flows = tuple(flows)
        by_id: dict[str, WorldFlow] = {}
        by_key: dict[tuple[str, str], WorldFlow] = {}
        for flow in self._flows:
            if not isinstance(flow, WorldFlow):
                raise ValueError("world_flow_invalid")
            if flow.flow_id in by_id:
                raise ValueError("world_flow_id_repeated")
            if (flow.product_ref, flow.flow_key) in by_key:
                raise ValueError("world_flow_key_repeated")
            by_id[flow.flow_id] = flow
            by_key[(flow.product_ref, flow.flow_key)] = flow
        self._by_id = by_id
        self._by_key = by_key

    @property
    def flows(self) -> tuple[WorldFlow, ...]:
        return self._flows

    def flow(self, flow_id: str) -> WorldFlow | None:
        return self._by_id.get(flow_id)

    def find(self, product_ref: str, flow_key: str) -> WorldFlow | None:
        return self._by_key.get((product_ref, flow_key))

    def base_for(self, items: Sequence[ProposalItem]) -> dict[str, dict[str, int | None]]:
        """La base de la validacion, con la forma de ``_flow_store_base``.

        Todos los flujos de la instancia con su version activa; los de la
        propuesta, ademas con su generacion.
        """
        item_ids = {item.flow_id for item in items}
        base: dict[str, dict[str, int | None]] = {}
        for flow in self._flows:
            entry: dict[str, int | None] = {"active_version": flow.active_version}
            if flow.flow_id in item_ids:
                entry["generation"] = flow.generation
            base[flow.flow_id] = entry
        return base

    def draft_base_for(self, items: Sequence[ProposalItem]) -> dict[str, dict[str, int | None]]:
        """La version que lee quien propone: ``{flow_id: {active_version}}`` de los flujos de los items."""
        base: dict[str, dict[str, int | None]] = {}
        for item in items:
            flow = self._by_id.get(item.flow_id)
            if flow is not None:
                base[item.flow_id] = {"active_version": flow.active_version}
        return base


# ---------------------------------------------------------------------------
# Los items de una propuesta.
# ---------------------------------------------------------------------------

class ProposalItemsInvalid(ValueError):
    """Los items no tienen la forma minima del ciclo: ``flow_change_items_invalid``."""

    code = "flow_change_items_invalid"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}:{detail}")
        self.detail = detail


@dataclass(frozen=True)
class ProposalItem:
    """Un item de una propuesta: una version, volver, prender o apagar un flujo.

    ``to_json`` da lo que acepta ``_flow_change_items_are_valid`` cuando la
    definicion esta normalizada y ``en_curso`` es ``seguir``.
    """

    producto: str
    flujo: str
    flow_id: str
    tipo: str
    definicion: Mapping[str, Any] | None = None
    version: int | None = None
    en_curso: str | None = None
    ok_del_cliente: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _check_item_envelope(
            producto=self.producto, flujo=self.flujo, flow_id=self.flow_id, tipo=self.tipo,
            definicion=self.definicion, version=self.version, en_curso=self.en_curso,
            ok_del_cliente=self.ok_del_cliente,
        )

    @classmethod
    def from_json(cls, value: object) -> ProposalItem:
        if not isinstance(value, Mapping):
            raise ProposalItemsInvalid("item_not_object")
        tipo = value.get("tipo")
        if tipo not in ITEM_TYPES:
            raise ProposalItemsInvalid("tipo_invalid")
        keys = set(value)
        identity = {"producto", "flujo", "flow_id", "tipo"}
        if tipo == "version":
            required = identity | {"definicion", "en_curso"}
            allowed = required | {"ok_del_cliente"}
        elif tipo == "volver":
            required = allowed = identity | {"version", "en_curso"}
        else:
            required = allowed = identity
        if not required <= keys or not keys <= allowed:
            raise ProposalItemsInvalid("keys_invalid")
        return cls(
            producto=value["producto"], flujo=value["flujo"], flow_id=value["flow_id"], tipo=tipo,
            definicion=value.get("definicion"), version=value.get("version"),
            en_curso=value.get("en_curso"), ok_del_cliente=value.get("ok_del_cliente"),
        )

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "producto": self.producto, "flujo": self.flujo, "flow_id": self.flow_id,
            "tipo": self.tipo,
        }
        if self.tipo == "version":
            out["definicion"] = copy.deepcopy(dict(self.definicion or {}))
            out["en_curso"] = self.en_curso
            if self.ok_del_cliente is not None:
                out["ok_del_cliente"] = copy.deepcopy(dict(self.ok_del_cliente))
        elif self.tipo == "volver":
            out["version"] = self.version
            out["en_curso"] = self.en_curso
        return out

    @property
    def is_content(self) -> bool:
        return self.tipo in ("version", "volver")

    def with_definition(self, definition: Mapping[str, Any]) -> ProposalItem:
        return ProposalItem(
            producto=self.producto, flujo=self.flujo, flow_id=self.flow_id, tipo=self.tipo,
            definicion=definition, version=self.version, en_curso=self.en_curso,
            ok_del_cliente=self.ok_del_cliente,
        )


def _check_item_envelope(
    *, producto: object, flujo: object, flow_id: object, tipo: object, definicion: object,
    version: object, en_curso: object, ok_del_cliente: object,
) -> None:
    if tipo not in ITEM_TYPES:
        raise ProposalItemsInvalid("tipo_invalid")
    if not is_flow_id(flow_id):
        raise ProposalItemsInvalid("flow_id_invalid")
    if not is_product_ref(producto):
        raise ProposalItemsInvalid("producto_invalid")
    if not is_flow_key(flujo):
        raise ProposalItemsInvalid("flujo_invalid")
    if tipo == "version":
        if not isinstance(definicion, Mapping):
            raise ProposalItemsInvalid("definicion_invalid")
        if en_curso not in IN_FLIGHT_POLICIES:
            raise ProposalItemsInvalid("en_curso_invalid")
        if version is not None:
            raise ProposalItemsInvalid("keys_invalid")
        if ok_del_cliente is not None and not isinstance(ok_del_cliente, Mapping):
            raise ProposalItemsInvalid("ok_del_cliente_invalid")
    elif tipo == "volver":
        if not _is_int(version) or not 1 <= version <= 999_999_999:
            raise ProposalItemsInvalid("version_invalid")
        if en_curso not in IN_FLIGHT_POLICIES:
            raise ProposalItemsInvalid("en_curso_invalid")
        if definicion is not None or ok_del_cliente is not None:
            raise ProposalItemsInvalid("keys_invalid")
    elif definicion is not None or version is not None or en_curso is not None or (
        ok_del_cliente is not None
    ):
        raise ProposalItemsInvalid("keys_invalid")


def parse_items(value: object) -> tuple[ProposalItem, ...]:
    """Los items de una propuesta: de 1 a 10, y por flujo a lo sumo uno de contenido y uno de estado."""
    if not isinstance(value, list) or not 1 <= len(value) <= ITEMS_MAX:
        raise ProposalItemsInvalid("items_count_invalid")
    items = tuple(ProposalItem.from_json(item) for item in value)
    check_item_classes(items)
    return items


def check_item_classes(items: Sequence[ProposalItem]) -> None:
    """Por flujo, a lo sumo un item de contenido (version o volver) y uno de estado."""
    seen: set[tuple[str, bool]] = set()
    for item in items:
        mark = (item.flow_id, item.is_content)
        if mark in seen:
            raise ProposalItemsInvalid("item_class_repeated")
        seen.add(mark)


def as_draft(definition: Mapping[str, Any]) -> dict[str, Any]:
    """Una definicion publicada como punto de partida de un borrador: sin version y en borrador.

    Es como arranca el agente desde la version publicada (contrato del
    esquema, «Las cinco piezas»).
    """
    draft = {key: copy.deepcopy(value) for key, value in definition.items() if key != "version"}
    draft["estado"] = DRAFT
    return draft


def as_published(definition: Mapping[str, Any], version: int) -> dict[str, Any]:
    """Una definicion normalizada como la guarda la publicacion: con su numero y publicada.

    Es lo que escribe ``_flow_change_publish`` en ``flow_definitions`` (el
    invariante 1 de la base pide ``version`` y ``estado: publicada``).
    """
    if not _is_int(version) or version < 1:
        raise ValueError("published_version_invalid")
    published = _ordered({key: copy.deepcopy(value) for key, value in definition.items()})
    published["version"] = version
    published["estado"] = PUBLISHED
    return _ordered(published)


# ---------------------------------------------------------------------------
# La regla 1: la forma de una definicion.
# ---------------------------------------------------------------------------

class _Collector:
    def __init__(self, base: Sequence[Segment]) -> None:
        self.base = tuple(base)
        self.findings: list[Finding] = []

    def add(self, spec: CodeSpec, segments: Sequence[Segment], *, reason: str | None = None) -> None:
        self.findings.append(spec.at(finding_path(self.base + tuple(segments)), reason=reason))


def _check_text(out: _Collector, value: object, segments: tuple[Segment, ...],
                max_chars: int | None, what: str) -> None:
    if not isinstance(value, str) or not value.strip() or (
        max_chars is not None and len(value) > max_chars
    ):
        limit = f" de hasta {max_chars} caracteres" if max_chars is not None else ""
        out.add(INVALID_VALUE, segments, reason=f"{what} es un texto{limit}, que no puede quedar vacío.")


def _check_enum(out: _Collector, value: object, segments: tuple[Segment, ...],
                allowed: Sequence[str]) -> None:
    if is_unresolved(value):
        return
    if not isinstance(value, str) or value not in allowed:
        out.add(INVALID_VALUE, segments, reason="Vale uno de: " + ", ".join(allowed) + ".")


def _check_duration(out: _Collector, value: object, segments: tuple[Segment, ...],
                    *, extra: Sequence[str] = ()) -> None:
    if is_unresolved(value) or (isinstance(value, str) and value in extra):
        return
    if parse_duration(value) is None:
        accepted = " o ".join(("una duración (0m, 60m, 24h, 1d)", *extra))
        out.add(INVALID_VALUE, segments, reason=f"Vale {accepted}.")


def _check_list(out: _Collector, value: object, segments: tuple[Segment, ...],
                check: Any) -> None:
    if not isinstance(value, list):
        out.add(INVALID_VALUE, segments, reason="Va una lista.")
        return
    for index, element in enumerate(value):
        check(element, segments + (index,))


def _check_keys(out: _Collector, value: Mapping[str, Any], segments: tuple[Segment, ...],
                allowed: Sequence[str]) -> None:
    for key in value:
        if key not in allowed:
            out.add(UNKNOWN_KEY, segments + (key,))


def check_definition(definition: object, *, published: bool = False,
                     path: Sequence[Segment] = ()) -> tuple[Finding, ...]:
    """La regla 1 sobre una definicion ya normalizada, mas los textos sin identificadores.

    ``published`` acepta ``version`` y ``estado: publicada`` (el destino de un
    ``volver``); en un borrador son ``version_set_by_cycle``. Lo que tiene
    regla propia queda para esa regla: el disparador (2), las horas y la zona
    del horario (8) y los numeros de los topes (9).
    """
    out = _Collector(path)
    if not isinstance(definition, Mapping):
        out.add(INVALID_VALUE, (), reason="Una definición es un objeto.")
        return tuple(out.findings)
    if depth_of(definition) > MAX_DEPTH:
        out.add(INVALID_VALUE, (), reason="La definición tiene más niveles de los que admite el esquema.")
        return tuple(out.findings)
    # El tamano se mide primero. Lo que pasa de los 32 KiB no corre los chequeos
    # por cadena: el de identificadores de la 1a (looks_like_identifier) es
    # cuadratico en un tramo largo sin @ (63 s con 250 000 caracteres). Sin
    # tamano (no es JSON, por ejemplo con un sustituto suelto) se mira cada
    # cadena, que es lineal, para dar su ruta, pero no los identificadores.
    size = json_size(definition)
    if size is None:
        out.add(INVALID_VALUE, (), reason="La definición no es JSON.")
    elif size > DEFINITION_MAX_BYTES:
        out.add(INVALID_VALUE, (), reason=f"La definición pasa de {DEFINITION_MAX_BYTES} bytes.")

    _check_keys(out, definition, (), _TOP_KEYS)
    for key in ("flujo", "nombre", "dispara", "pasos"):
        if key not in definition:
            out.add(REQUIRED_KEY_MISSING, (key,))
    for key in ("esquema", "estado"):
        if key not in definition:
            out.add(REQUIRED_KEY_MISSING, (key,))

    if "esquema" in definition and definition["esquema"] != SCHEMA:
        out.add(INVALID_VALUE, ("esquema",), reason=f"El esquema es {SCHEMA}.")
    if "flujo" in definition and not is_flow_key(definition["flujo"]):
        out.add(INVALID_VALUE, ("flujo",), reason="La clave de un flujo empieza con una letra y lleva minúsculas, dígitos o _, hasta 40.")
    if "producto" in definition and not is_product_ref(definition["producto"]):
        out.add(INVALID_VALUE, ("producto",), reason="El producto tiene la forma del manifiesto: minúsculas, dígitos o -, hasta 128.")
    if "nombre" in definition:
        _check_text(out, definition["nombre"], ("nombre",), NAME_MAX_CHARS, "El nombre")
    if "version" in definition:
        if not published:
            out.add(VERSION_SET_BY_CYCLE, ("version",))
        elif not _is_int(definition["version"]) or definition["version"] < 1:
            out.add(INVALID_VALUE, ("version",), reason="La versión es un entero mayor que 0.")
    if "estado" in definition:
        estado = definition["estado"]
        expected = PUBLISHED if published else DRAFT
        if estado == PUBLISHED and not published:
            out.add(VERSION_SET_BY_CYCLE, ("estado",))
        elif estado != expected:
            out.add(INVALID_VALUE, ("estado",), reason=f"El estado es {expected}.")

    if "dispara" in definition:
        _check_trigger(out, definition["dispara"])
    if "entra" in definition:
        _check_entry(out, definition["entra"])
    if "pasos" in definition:
        steps = definition["pasos"]
        if isinstance(steps, list) and not steps:
            out.add(INVALID_VALUE, ("pasos",), reason="Un flujo tiene al menos un paso.")
        else:
            _check_steps(out, steps, ("pasos",))
    if "termina" in definition:
        _check_exits(out, definition["termina"])
    if "horario" in definition:
        _check_schedule(out, definition["horario"])
    if "topes" in definition:
        _check_caps(out, definition["topes"])
    if "prioridad" in definition:
        _check_priority(out, definition["prioridad"])

    if size is None or size <= DEFINITION_MAX_BYTES:
        _check_strings(out, definition, ())
    if size is not None and size <= DEFINITION_MAX_BYTES:
        _check_identifiers(out, definition)
    return tuple(out.findings)


def _check_trigger(out: _Collector, trigger: object) -> None:
    if not isinstance(trigger, Mapping):
        out.add(INVALID_VALUE, ("dispara",), reason="dispara es un objeto con evento o con silencio.")
        return
    _check_keys(out, trigger, ("dispara",), ("evento", "silencio"))
    if "evento" in trigger:
        _check_enum(out, trigger["evento"], ("dispara", "evento"), EVENTS)
    if "silencio" in trigger:
        silence = trigger["silencio"]
        segments = ("dispara", "silencio")
        if not isinstance(silence, Mapping):
            out.add(INVALID_VALUE, segments, reason="silencio es un objeto.")
            return
        _check_keys(out, silence, segments, ("lo_ultimo", "desde", "hasta", "conversaciones"))
        for key in ("lo_ultimo", "desde", "hasta"):
            if key not in silence:
                out.add(REQUIRED_KEY_MISSING, segments + (key,))
        if "lo_ultimo" in silence:
            _check_enum(out, silence["lo_ultimo"], segments + ("lo_ultimo",), LAST_SPEAKERS)
        for key in ("desde", "hasta"):
            if key in silence:
                _check_duration(out, silence[key], segments + (key,))
        if "conversaciones" in silence:
            _check_list(out, silence["conversaciones"], segments + ("conversaciones",),
                        lambda value, at: _check_enum(out, value, at, CONVERSATION_STATES))


def _check_flow_ref(out: _Collector, value: object, segments: tuple[Segment, ...]) -> None:
    if is_unresolved(value):
        return
    if not is_flow_ref_shape(value):
        out.add(INVALID_VALUE, segments, reason="Un flujo se nombra con su clave, o con <producto>/<flujo> si es de otro producto.")


def _check_entry(out: _Collector, entry: object) -> None:
    if not isinstance(entry, Mapping):
        out.add(INVALID_VALUE, ("entra",), reason="entra es un objeto.")
        return
    _check_keys(out, entry, ("entra",), ("permiso", "contesto_a"))
    if "permiso" in entry:
        _check_enum(out, entry["permiso"], ("entra", "permiso"), PERMISSIONS)
    if "contesto_a" in entry:
        _check_list(out, entry["contesto_a"], ("entra", "contesto_a"),
                    lambda value, at: _check_flow_ref(out, value, at))


def _check_steps(out: _Collector, steps: object, segments: tuple[Segment, ...]) -> None:
    if not isinstance(steps, list):
        out.add(INVALID_VALUE, segments, reason="Los pasos son una lista.")
        return
    for index, step in enumerate(steps):
        _check_step(out, step, segments + (index,))


def _check_step(out: _Collector, step: object, segments: tuple[Segment, ...]) -> None:
    if not isinstance(step, Mapping):
        out.add(INVALID_VALUE, segments, reason="Un paso es un objeto.")
        return
    kind = step_type(step)
    if kind is None:
        out.add(INVALID_VALUE, segments, reason="Un paso tiene exactamente un tipo: " + ", ".join(STEP_TYPES) + ".")
        return
    _check_keys(out, step, segments, _STEP_KEYS[kind])
    value = step[kind]
    if kind == "esperar":
        _check_duration(out, value, segments + ("esperar",))
    elif kind == "plantilla":
        _check_template_step(out, step, segments)
    elif kind == "conversar":
        _check_text(out, value, segments + ("conversar",), GOAL_MAX_CHARS, "El objetivo")
    elif kind == "derivar":
        _check_text(out, value, segments + ("derivar",), None, "La nota")
    else:
        _check_condition_step(out, step, segments)


def _check_template_step(out: _Collector, step: Mapping[str, Any], segments: tuple[Segment, ...]) -> None:
    template = step["plantilla"]
    if isinstance(template, Mapping):
        _check_keys(out, template, segments + ("plantilla",), (TO_CREATE,))
        if TO_CREATE not in template:
            out.add(REQUIRED_KEY_MISSING, segments + ("plantilla", TO_CREATE))
        elif not is_template_name(template[TO_CREATE]):
            out.add(INVALID_VALUE, segments + ("plantilla", TO_CREATE),
                    reason="El nombre propuesto tiene la forma de un nombre de plantilla de Meta.")
    elif not is_template_name(template):
        out.add(INVALID_VALUE, segments + ("plantilla",),
                reason="Una plantilla se nombra como en Meta (minúsculas, dígitos y _), o con {a_crear: <nombre>}.")
    if "idioma" in step:
        language = step["idioma"]
        if not is_unresolved(language) and (
            not isinstance(language, str) or not _LANGUAGE.fullmatch(language)
        ):
            out.add(INVALID_VALUE, segments + ("idioma",), reason="El idioma se escribe como en Meta: es_MX.")
    if "variables" in step:
        _check_list(out, step["variables"], segments + ("variables",),
                    lambda value, at: _check_enum(out, value, at, VARIABLES))
    if "boton" in step:
        button = step["boton"]
        at = segments + ("boton",)
        if not isinstance(button, Mapping):
            out.add(INVALID_VALUE, at, reason="boton es un objeto con link y, opcional, cupon.")
            return
        _check_keys(out, button, at, ("link", "cupon"))
        if "link" not in button:
            out.add(REQUIRED_KEY_MISSING, at + ("link",))
        else:
            _check_enum(out, button["link"], at + ("link",), LINKS)
        if "cupon" in button and not is_unresolved(button["cupon"]) and not is_coupon_code(button["cupon"]):
            out.add(INVALID_VALUE, at + ("cupon",), reason="El código del cupón lleva letras, dígitos, _ o -, hasta 64.")


def _check_condition_step(out: _Collector, step: Mapping[str, Any], segments: tuple[Segment, ...]) -> None:
    condition = step["condicion"]
    at = segments + ("condicion",)
    if not isinstance(condition, Mapping):
        out.add(INVALID_VALUE, at, reason="condicion es un objeto con dato y es.")
    else:
        _check_keys(out, condition, at, ("dato", "es"))
        for key in ("dato", "es"):
            if key not in condition:
                out.add(REQUIRED_KEY_MISSING, at + (key,))
        datum = condition.get("dato")
        if "dato" in condition:
            _check_enum(out, datum, at + ("dato",), CONDITION_DATA)
        if "es" in condition:
            def check_value(value: object, where: tuple[Segment, ...]) -> None:
                if is_unresolved(value):
                    return
                if datum == "ventana":
                    _check_enum(out, value, where, WINDOW_VALUES)
                elif datum == "medio_de_pago":
                    if not isinstance(value, str) or not _PAYMENT_CODE.fullmatch(value):
                        out.add(INVALID_VALUE, where, reason="Un medio de pago se escribe con el código de Hotmart, como CASHPAYMENT.")
                elif not isinstance(value, str):
                    out.add(INVALID_VALUE, where, reason="Los valores de una condición son textos.")
            _check_list(out, condition["es"], at + ("es",), check_value)
    if "entonces" not in step:
        out.add(REQUIRED_KEY_MISSING, segments + ("entonces",))
    else:
        _check_steps(out, step["entonces"], segments + ("entonces",))
    if "si_no" in step:
        _check_steps(out, step["si_no"], segments + ("si_no",))


def _check_exits(out: _Collector, exits: object) -> None:
    if not isinstance(exits, Mapping):
        out.add(INVALID_VALUE, ("termina",), reason="termina es un objeto con si y vence.")
        return
    _check_keys(out, exits, ("termina",), ("si", "vence"))
    if "si" in exits:
        _check_list(out, exits["si"], ("termina", "si"),
                    lambda value, at: _check_enum(out, value, at, EXITS))
    if "vence" in exits:
        _check_duration(out, exits["vence"], ("termina", "vence"))


def _check_schedule(out: _Collector, schedule: object) -> None:
    if schedule == ALWAYS or is_unresolved(schedule):
        return
    if not isinstance(schedule, Mapping):
        out.add(INVALID_VALUE, ("horario",), reason="El horario es un objeto con zona, desde y hasta, o la palabra siempre.")
        return
    _check_keys(out, schedule, ("horario",), ("zona", "desde", "hasta"))
    for key in ("zona", "desde", "hasta"):
        if key in schedule and not isinstance(schedule[key], str):
            out.add(INVALID_VALUE, ("horario", key), reason="Va un texto.")


def _check_caps(out: _Collector, caps: object) -> None:
    if not isinstance(caps, Mapping):
        out.add(INVALID_VALUE, ("topes",), reason="topes es un objeto.")
        return
    _check_keys(out, caps, ("topes",), ("bolsa", "por_dia", "total", "repeticion"))
    if "bolsa" in caps and not is_unresolved(caps["bolsa"]) and not is_pool(caps["bolsa"]):
        out.add(INVALID_VALUE, ("topes", "bolsa"), reason="La bolsa empieza con una letra y lleva minúsculas, dígitos, _ o -, hasta 40.")
    if "repeticion" in caps:
        repeat = caps["repeticion"]
        at = ("topes", "repeticion")
        if not isinstance(repeat, Mapping):
            out.add(INVALID_VALUE, at, reason="repeticion es un objeto con una_por y otra_despues_de.")
            return
        _check_keys(out, repeat, at, ("una_por", "otra_despues_de"))
        if "una_por" in repeat:
            _check_enum(out, repeat["una_por"], at + ("una_por",), REPEAT_UNITS)
        if "otra_despues_de" in repeat:
            _check_duration(out, repeat["otra_despues_de"], at + ("otra_despues_de",), extra=(NEVER,))


def _check_priority(out: _Collector, priority: object) -> None:
    if not isinstance(priority, Mapping):
        out.add(INVALID_VALUE, ("prioridad",), reason="prioridad es un objeto con le_gana_a y no_arranca_mientras.")
        return
    _check_keys(out, priority, ("prioridad",), ("le_gana_a", "no_arranca_mientras"))
    for key in ("le_gana_a", "no_arranca_mientras"):
        if key in priority:
            _check_list(out, priority[key], ("prioridad", key),
                        lambda value, at: _check_flow_ref(out, value, at))


def _check_strings(out: _Collector, value: object, segments: tuple[Segment, ...]) -> None:
    """Toda cadena con un caracter Cc, Cf, Cs, Zl o Zp es ``invalid_value``, con su ruta."""
    stack: list[tuple[tuple[Segment, ...], object]] = [(segments, value)]
    while stack:
        at, current = stack.pop()
        if isinstance(current, str):
            if has_control_or_format(current):
                out.add(INVALID_VALUE, at, reason="El texto tiene un carácter de control, de formato o de dirección, un separador de línea o un sustituto suelto (categorías Cc, Cf, Zl, Zp y Cs).")
        elif isinstance(current, Mapping):
            stack.extend((at + (key,), child) for key, child in reversed(list(current.items())))
        elif isinstance(current, list):
            stack.extend((at + (index,), child) for index, child in reversed(list(enumerate(current))))


def _iter_steps(steps: object, segments: tuple[Segment, ...]) -> Iterable[tuple[Mapping[str, Any], tuple[Segment, ...]]]:
    if not isinstance(steps, list):
        return
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            continue
        yield step, segments + (index,)
        if step_type(step) == "condicion":
            for branch in ("entonces", "si_no"):
                yield from _iter_steps(step.get(branch), segments + (index, branch))


def _check_identifiers(out: _Collector, definition: Mapping[str, Any]) -> None:
    """``nombre``, ``conversar`` y ``derivar`` sin un mail ni un tramo con forma de telefono.

    Es la regla de la 1a (automation-expert-read-v1, «Datos personales»), por
    tramo y no sumando los digitos del texto. Marca una fecha ISO como
    telefono: se resuelve donde entra texto libre largo (pieza 4).
    """
    name = definition.get("nombre")
    if isinstance(name, str) and looks_like_identifier(name):
        out.add(IDENTIFIER_NOT_ALLOWED, ("nombre",))
    for step, segments in _iter_steps(definition.get("pasos"), ("pasos",)):
        for key in ("conversar", "derivar"):
            text = step.get(key)
            if step_type(step) == key and isinstance(text, str) and looks_like_identifier(text):
                out.add(IDENTIFIER_NOT_ALLOWED, segments + (key,))


def check_client_ok(value: object, *, path: Sequence[Segment]) -> tuple[Finding, ...]:
    """``ok_del_cliente``: ``{quien, medio, fecha}`` exactos, como lo mira la base.

    ``quien`` y ``medio`` son texto libre de hasta 120 caracteres, sin
    controles; ``fecha`` es AAAA-MM-DD y una fecha que existe.
    """
    out = _Collector(path)
    if not isinstance(value, Mapping):
        out.add(INVALID_VALUE, (), reason="ok_del_cliente es un objeto con quien, medio y fecha.")
        return tuple(out.findings)
    _check_keys(out, value, (), ("quien", "medio", "fecha"))
    for key in ("quien", "medio", "fecha"):
        if key not in value:
            out.add(REQUIRED_KEY_MISSING, (key,))
    for key in ("quien", "medio"):
        if key in value:
            text = value[key]
            if not isinstance(text, str) or not text.strip() or len(text) > CLIENT_OK_TEXT_MAX_CHARS:
                out.add(INVALID_VALUE, (key,), reason=f"Es un texto de hasta {CLIENT_OK_TEXT_MAX_CHARS} caracteres.")
    if "fecha" in value and not _is_date(value["fecha"]):
        out.add(INVALID_VALUE, ("fecha",), reason="La fecha se escribe AAAA-MM-DD.")
    _check_strings(out, value, ())
    return tuple(out.findings)


def _is_date(value: object) -> bool:
    if not isinstance(value, str) or not _OK_DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# La normalizacion.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Trigger:
    """El disparador de una definicion: ``evento`` (con su valor si se sabe) o ``silencio``."""

    kind: str
    event: str | None = None

    @property
    def is_purchase(self) -> bool:
        return self.kind == "evento" and self.event == "compra"

    @property
    def is_recovery(self) -> bool:
        return self.kind == "evento" and self.event in RECOVERY_EVENTS

    @property
    def event_known(self) -> bool:
        return self.kind == "silencio" or self.event is not None


def trigger_of(definition: Mapping[str, Any]) -> Trigger | None:
    """El disparador, si hay exactamente uno. Un evento sin valor conocido va con ``event`` en None."""
    trigger = definition.get("dispara")
    if not isinstance(trigger, Mapping):
        return None
    present = [key for key in ("evento", "silencio") if key in trigger]
    if len(present) != 1:
        return None
    if present[0] == "silencio":
        return Trigger("silencio")
    event = trigger["evento"]
    return Trigger("evento", event if isinstance(event, str) and event in EVENTS else None)


@dataclass(frozen=True)
class NormalizedDefinition:
    definition: dict[str, Any]
    findings: tuple[Finding, ...]


class _NoDefault:
    def __init__(self, spec: CodeSpec = REQUIRED_KEY_MISSING) -> None:
        self.spec = spec


def default_schedule(*, time_zone: str | None, other_schedules: Sequence[object]) -> object:
    """El horario por defecto de un flujo nuevo, o ``_NoDefault``.

    La zona de la instancia y la franja de los otros flujos, si todos
    coinciden; si alguno es ``siempre``, todos tienen que serlo.
    """
    if not other_schedules:
        return _NoDefault()
    if other_schedules[0] == ALWAYS:
        return ALWAYS if all(schedule == ALWAYS for schedule in other_schedules) else _NoDefault()
    franja = _common_franja(other_schedules)
    if franja is None or time_zone is None:
        return _NoDefault()
    return {"zona": time_zone, "desde": franja[0], "hasta": franja[1]}


def _common_franja(other_schedules: Sequence[object]) -> tuple[str, str] | None:
    """La franja (desde, hasta) si todos los otros horarios son objetos con la misma, valida."""
    franjas: set[tuple[str, str]] = set()
    for schedule in other_schedules:
        if not isinstance(schedule, Mapping):
            return None
        since, until = schedule.get("desde"), schedule.get("hasta")
        if parse_hour(since) is None or parse_hour(until) is None:
            return None
        assert isinstance(since, str) and isinstance(until, str)
        franjas.add((since, until))
    if len(franjas) != 1:
        return None
    return franjas.pop()


class _Defaults:
    """Los valores por defecto de un flujo nuevo, cada uno de la linea del esquema que lo fija.

    En modo ``fill`` los completa (o deja ``required_key_missing`` de la
    regla 1 cuando no se pueden calcular); en modo ``report`` no completa
    nada y deja ``required_key_missing`` sin regla por cada clave que habria
    aparecido: un flujo con version recibe la definicion completa.
    """

    def __init__(self, definition: dict[str, Any], *, fill: bool, out: _Collector,
                 instance: InstanceView, other_schedules: Sequence[object],
                 catalog: TemplateCatalog | None) -> None:
        self.definition = definition
        self.fill = fill
        self.out = out
        self.instance = instance
        self.other_schedules = other_schedules
        self.catalog = catalog
        self.trigger = trigger_of(definition)

    def run(self) -> None:
        trigger = self.trigger
        definition = self.definition
        silence = definition.get("dispara", {}).get("silencio") if isinstance(definition.get("dispara"), Mapping) else None
        if isinstance(silence, dict):
            self._key(silence, ("dispara", "silencio"), "conversaciones", lambda: ["abierta"])

        entry = self._container(definition, (), "entra")
        if entry is not None:
            if trigger is not None:
                self._key(entry, ("entra",), "permiso",
                          lambda: "conversacion" if trigger.kind == "silencio" else "formulario")
            self._key(entry, ("entra",), "contesto_a", lambda: [])

        steps = definition.get("pasos")
        if isinstance(steps, list):
            self._steps(steps, ("pasos",))

        exits = self._container(definition, (), "termina")
        if exits is not None and trigger is not None:
            if trigger.event_known:
                self._key(exits, ("termina",), "si",
                          lambda: [] if trigger.is_purchase else ["compra"])
            if trigger.kind == "evento":
                self._key(exits, ("termina",), "vence", lambda: "1d")

        self._schedule()

        caps = self._container(definition, (), "topes")
        if caps is not None:
            self._pool(caps)
            if trigger is not None and trigger.event_known:
                limits = (10, 150) if trigger.is_recovery else (NO_CAP, NO_CAP)
                self._key(caps, ("topes",), "por_dia", lambda: limits[0])
                self._key(caps, ("topes",), "total", lambda: limits[1])
                repeat = self._container(caps, ("topes",), "repeticion")
                if repeat is not None:
                    if trigger.kind == "silencio":
                        pair = ("conversacion", NEVER)
                    elif trigger.is_purchase:
                        pair = ("persona", NEVER)
                    else:
                        pair = ("persona_y_oferta", "0m")
                    self._key(repeat, ("topes", "repeticion"), "una_por", lambda: pair[0])
                    self._key(repeat, ("topes", "repeticion"), "otra_despues_de", lambda: pair[1])

        priority = self._container(definition, (), "prioridad")
        if priority is not None:
            self._key(priority, ("prioridad",), "le_gana_a", lambda: [])
            self._key(priority, ("prioridad",), "no_arranca_mientras", lambda: [])

    def _container(self, parent: dict[str, Any], segments: tuple[Segment, ...],
                   key: str) -> dict[str, Any] | None:
        """El objeto ``parent[key]``: si falta, se crea (fill) o se reporta una vez (report)."""
        if key not in parent:
            if not self.fill:
                self.out.add(REQUIRED_KEY_MISSING_CYCLE, segments + (key,))
                return None
            parent[key] = {}
        value = parent[key]
        return value if isinstance(value, dict) else None

    def _key(self, parent: dict[str, Any], segments: tuple[Segment, ...], key: str,
             compute: Any) -> None:
        if key in parent:
            return
        if not self.fill:
            self.out.add(REQUIRED_KEY_MISSING_CYCLE, segments + (key,))
            return
        value = compute()
        if isinstance(value, _NoDefault):
            self.out.add(value.spec, segments + (key,))
            return
        parent[key] = value

    def _steps(self, steps: list[Any], segments: tuple[Segment, ...]) -> None:
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            at = segments + (index,)
            kind = step_type(step)
            if kind == "plantilla":
                self._key(step, at, "idioma", lambda step=step: self._language(step))
                self._key(step, at, "variables", lambda: list(DEFAULT_VARIABLES))
            elif kind == "condicion":
                self._key(step, at, "si_no", lambda: [])
                for branch in ("entonces", "si_no"):
                    if isinstance(step.get(branch), list):
                        self._steps(step[branch], at + (branch,))

    def _language(self, step: Mapping[str, Any]) -> object:
        name = step.get("plantilla")
        if self.catalog is None or not is_template_name(name) or is_unresolved(name):
            return _NoDefault()
        languages = self.catalog.languages(name)
        return languages[0] if len(languages) == 1 else _NoDefault()

    def _schedule(self) -> None:
        definition = self.definition
        if "horario" not in definition:
            if not self.fill:
                self.out.add(REQUIRED_KEY_MISSING_CYCLE, ("horario",))
                return
            value = default_schedule(time_zone=self.instance.time_zone,
                                     other_schedules=self.other_schedules)
            if isinstance(value, _NoDefault):
                self.out.add(REQUIRED_KEY_MISSING, ("horario",))
            else:
                definition["horario"] = value
            return
        schedule = definition["horario"]
        if not isinstance(schedule, dict):
            return
        self._key(schedule, ("horario",), "zona",
                  lambda: self.instance.time_zone if self.instance.time_zone is not None else _NoDefault())
        franja = _common_franja(self.other_schedules)
        self._key(schedule, ("horario",), "desde", lambda: franja[0] if franja else _NoDefault())
        self._key(schedule, ("horario",), "hasta", lambda: franja[1] if franja else _NoDefault())

    def _pool(self, caps: dict[str, Any]) -> None:
        def compute() -> object:
            product = self.definition.get("producto")
            key = self.definition.get("flujo")
            if not is_product_ref(product) or not is_flow_key(key):
                # Sin producto o sin clave valida ya hay otro bloqueo; la bolsa espera.
                return _SKIP
            pool = f"{product}-{key}"
            return pool if is_pool(pool) else _NoDefault(DEFAULT_POOL_INVALID)

        if "bolsa" in caps:
            return
        if not self.fill:
            self.out.add(REQUIRED_KEY_MISSING_CYCLE, ("topes", "bolsa"))
            return
        value = compute()
        if value is _SKIP:
            return
        if isinstance(value, _NoDefault):
            self.out.add(value.spec, ("topes", "bolsa"))
            return
        caps["bolsa"] = value


_SKIP = object()


_SUB_ORDER: Mapping[str, tuple[str, ...]] = {
    "dispara": ("evento", "silencio"),
    "entra": ("permiso", "contesto_a"),
    "termina": ("si", "vence"),
    "horario": ("zona", "desde", "hasta"),
    "topes": ("bolsa", "por_dia", "total", "repeticion"),
    "prioridad": ("le_gana_a", "no_arranca_mientras"),
}


def _reorder(value: object, order: Sequence[str]) -> object:
    """El objeto con sus claves en el orden del esquema; las demas, despues, como llegaron."""
    if not isinstance(value, Mapping):
        return value
    ordered = {key: value[key] for key in order if key in value}
    for key, child in value.items():
        if key not in ordered:
            ordered[key] = child
    return ordered


def _reorder_steps(steps: object, depth: int = 0) -> object:
    if not isinstance(steps, list) or depth > MAX_DEPTH:
        return steps
    ordered: list[object] = []
    for step in steps:
        kind = step_type(step)
        if kind is None:
            ordered.append(step)
            continue
        current = _reorder(step, _STEP_KEYS[kind])
        assert isinstance(current, dict)
        if kind == "plantilla" and isinstance(current.get("boton"), Mapping):
            current["boton"] = _reorder(current["boton"], ("link", "cupon"))
        if kind == "condicion":
            current["condicion"] = _reorder(current["condicion"], ("dato", "es"))
            for branch in ("entonces", "si_no"):
                if branch in current:
                    current[branch] = _reorder_steps(current[branch], depth + 1)
        ordered.append(current)
    return ordered


def _ordered(definition: Mapping[str, Any]) -> dict[str, Any]:
    """La definicion con las claves en el orden del esquema. No cambia ningun valor."""
    ordered = _reorder(definition, _TOP_KEYS)
    assert isinstance(ordered, dict)
    for key, order in _SUB_ORDER.items():
        if key in ordered:
            ordered[key] = _reorder(ordered[key], order)
    trigger = ordered.get("dispara")
    if isinstance(trigger, dict) and isinstance(trigger.get("silencio"), Mapping):
        trigger["silencio"] = _reorder(trigger["silencio"], ("lo_ultimo", "desde", "hasta", "conversaciones"))
    caps = ordered.get("topes")
    if isinstance(caps, dict) and isinstance(caps.get("repeticion"), Mapping):
        caps["repeticion"] = _reorder(caps["repeticion"], ("una_por", "otra_despues_de"))
    if "pasos" in ordered:
        ordered["pasos"] = _reorder_steps(ordered["pasos"])
    return ordered


def normalize_definition(
    definition: Mapping[str, Any],
    *,
    new_flow: bool,
    instance: InstanceView,
    other_schedules: Sequence[object] = (),
    catalog: TemplateCatalog | None = None,
    path: Sequence[Segment] = (),
) -> NormalizedDefinition:
    """Normaliza un borrador y le aplica la regla 1.

    Siempre completa ``esquema``, ``estado: borrador`` y ``producto`` (si la
    instancia tiene uno solo). Un flujo nuevo recibe los valores por defecto
    del esquema; uno con version, la definicion completa: cada clave que
    habria aparecido es ``required_key_missing`` sin regla, y no se completa.
    ``other_schedules`` son los horarios de las versiones activas de los
    otros flujos de la instancia (para el horario por defecto).
    """
    out = _Collector(path)
    if not isinstance(definition, Mapping):
        out.add(INVALID_VALUE, (), reason="Una definición es un objeto.")
        return NormalizedDefinition(definition={}, findings=tuple(out.findings))
    if depth_of(definition) > MAX_DEPTH:
        out.add(INVALID_VALUE, (), reason="La definición tiene más niveles de los que admite el esquema.")
        return NormalizedDefinition(definition=dict(definition), findings=tuple(out.findings))
    normalized: dict[str, Any] = copy.deepcopy(dict(definition))
    normalized.setdefault("esquema", SCHEMA)
    normalized.setdefault("estado", DRAFT)
    if "producto" not in normalized and instance.single_product is not None:
        normalized["producto"] = instance.single_product
    _Defaults(normalized, fill=new_flow, out=out, instance=instance,
              other_schedules=other_schedules, catalog=catalog).run()
    normalized = _ordered(normalized)
    findings = list(out.findings)
    findings.extend(check_definition(normalized, path=path))
    return NormalizedDefinition(definition=normalized, findings=tuple(_dedupe(findings)))


def _dedupe(findings: Iterable[Finding]) -> list[Finding]:
    seen: set[tuple[str, int | None, str]] = set()
    unique: list[Finding] = []
    for finding in findings:
        mark = (finding.code, finding.rule, finding.path)
        if mark not in seen:
            seen.add(mark)
            unique.append(finding)
    return unique


def dedupe_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Un hallazgo por codigo, regla y ruta, en el orden en que aparecieron."""
    return _dedupe(findings)
