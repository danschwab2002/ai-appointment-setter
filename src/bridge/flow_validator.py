"""El validador de las definiciones de flujo: las reglas 1 a 13, 15 (con el limite de
cupones personales de la 1.1), 17 (la 5 suma las variables del cupon y del evento de la 1.1), las
siete advertencias y los chequeos del ciclo.

Contratos: docs/contracts/flow-definition-v1.md («Las reglas del validador») y
docs/contracts/flow-change-cycle-v1.md (la propuesta, la decision 4 y el OK
del cliente). La regla 16 va con el motor (pieza 2), y de la 14 esta pieza mira
solo, al publicar un flujo con ``legacy_kind`` (``WorldFlow.legacy_kind``), que
la definicion compile a la politica de su flujo del legado (T2.1); lo demas lo
mira ``flow_engine.not_supported``.

Corre una sola vez, aca, en Python. La base no lo repite: se queda con sus
cuatro invariantes y con la regla de que nadie publica sin una revalidacion
fresca. ``ValidationResult.to_json()`` da lo que acepta
``_flow_change_result_is_valid``; ``ValidationResult.validator_json()``, la
parte de ``p_validator`` que sabe el validador (el contrato, la version de las
reglas, la fase y el actor); ``FlowWorld.base_for`` da lo mismo que
``_flow_store_base``; ``ProposalItem.to_json()`` da lo que acepta
``_flow_change_items_are_valid``.

Es puro: no lee el entorno, no hace red ni toca bases.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .flow_definition import (
    ALWAYS,
    DEFINITION_CODES,
    FICHA_EVENTS,
    INVALID_VALUE,
    KIND_CYCLE,
    KIND_FORM,
    KIND_NOT_YET,
    KIND_PROTECTION,
    KIND_WARNING,
    COUPON_VARIABLES,
    EVENT_VARIABLES,
    MAX_DEPTH,
    NO_CAP,
    PERSONAL_COUPONS_PER_PATH,
    PERMISSIONS,
    SCHEMA,
    SILENCE_MAX_MINUTES,
    TO_CREATE,
    CodeSpec,
    Finding,
    FlowWorld,
    InstanceView,
    ProposalItem,
    ProposalItemsInvalid,
    Segment,
    TemplateCatalog,
    WorldFlow,
    check_client_ok,
    check_definition,
    check_item_classes,
    dedupe_findings,
    depth_of,
    diff,
    finding_path,
    is_flow_key,
    is_iana_zone,
    is_personal_coupon,
    is_product_ref,
    is_template_name,
    is_unresolved,
    json_equal,
    json_size,
    loads_strict,
    normalize_definition,
    parse_duration,
    parse_flow_ref,
    parse_hour,
    parse_items,
    step_type,
    trigger_of,
    ITEMS_MAX_BYTES,
)
from .flow_policy import PolicyCompileError, compile_policy

__all__ = [
    "ACTORS", "BLOCK_CODES", "CATALOG", "CHECKOUT_BUTTON_URL", "PHASES", "RULES_VERSION",
    "VALIDATOR_CONTRACT", "WARNING_CODES", "FlowValidationInputError", "ValidationResult",
    "Finding", "FlowWorld", "InstanceView", "ProposalItem", "ProposalItemsInvalid",
    "TemplateCatalog", "WorldFlow", "loads_strict", "parse_items", "validate",
]

# Lo que la base exige en la revalidacion (``_flow_change_validator_is_valid``):
# el contrato del esquema y la version de las reglas. Sube cuando las reglas
# cambian de forma incompatible, y una migracion sube la minima de la base.
VALIDATOR_CONTRACT = SCHEMA
RULES_VERSION = 1

ACTORS = ("automation_expert", "operador")
PHASES = ("draft", "publish")
# El boton del seguimiento con cupon: la URL dinamica sobre el checkout de
# Hotmart (followup_discount.FOLLOWUP_BUTTON_URL; una prueba fija que son la
# misma). Se escribe aca para no importar el modulo del barrido.
CHECKOUT_BUTTON_URL = "https://pay.hotmart.com/{{1}}"
# El resultado que acepta la base: hasta 200 hallazgos entre los dos, y hasta
# 61440 bytes de texto jsonb (_flow_change_result_is_valid; con la vista previa
# entra en la columna validation). Con los textos de la regla 13, 200 hallazgos
# llegan a 91 KB.
MAX_FINDINGS = 200
MAX_RESULT_BYTES = 61440

_PLACEHOLDER = re.compile(r"\{\{([0-9]+)\}\}")
_STATIC_COMPONENTS = ("HEADER", "FOOTER")


def _spec(code: str, rule: int | None, kind: str, reason: str, *,
          limit: str | None = None, alternative: str | None = None) -> CodeSpec:
    return CodeSpec(code=code, rule=rule, kind=kind, reason=reason, limit=limit,
                    alternative=alternative)


# ---------------------------------------------------------------------------
# El catalogo cerrado de codigos.
# ---------------------------------------------------------------------------

TRIGGER_NOT_EXACTLY_ONE = _spec(
    "trigger_not_exactly_one", 2, KIND_FORM,
    "Un flujo tiene exactamente un disparador: un evento o un silencio.",
    alternative="Dejar uno solo; un disparador distinto es otro flujo.",
)
SILENCE_WINDOW_INVALID = _spec(
    "silence_window_invalid", 2, KIND_FORM,
    "En un silencio, desde es menor que hasta, y hasta no pasa de 30 días.",
)
PERMISSION_TRIGGER_MISMATCH = _spec(
    "permission_trigger_mismatch", 3, KIND_FORM,
    "formulario va solo con un evento, conversacion solo con un silencio y compra solo con el disparador compra.",
)
CONTESTO_A_REQUIRES_CONVERSATION = _spec(
    "contesto_a_requires_conversation", 3, KIND_FORM,
    "contesto_a vale solo con permiso: conversacion.",
)
PURCHASE_EXIT_MISSING = _spec(
    "purchase_exit_missing", 4, KIND_PROTECTION,
    "sin la salida compra, el flujo le escribe a quien ya compró.",
    limit="La salida compra en todo flujo disparado por un evento que no es la compra",
    alternative="Sumar compra a termina.si.",
)
PURCHASE_EXIT_WITH_PURCHASE_TRIGGER = _spec(
    "purchase_exit_with_purchase_trigger", 4, KIND_FORM,
    "Un flujo disparado por la compra no termina con la compra.",
    alternative="Sacar compra de termina.si.",
)
FICHA_VENCIDA_REQUIRES_FICHA_TRIGGER = _spec(
    "ficha_vencida_requires_ficha_trigger", 5, KIND_FORM,
    "La salida ficha_vencida vale solo con el disparador ficha_impresa.",
)
MEDIO_DE_PAGO_REQUIRES_FICHA_TRIGGER = _spec(
    "medio_de_pago_requires_ficha_trigger", 5, KIND_FORM,
    "medio_de_pago, como variable o como dato de una condición, va solo con los disparadores de la ficha.",
)
FICHA_LINK_REQUIRES_FICHA_TRIGGER = _spec(
    "ficha_link_requires_ficha_trigger", 5, KIND_FORM,
    "El link ficha va solo con los disparadores de la ficha.",
)
COUPON_REQUIRES_CHECKOUT_LINK = _spec(
    "coupon_requires_checkout_link", 5, KIND_FORM,
    "Un cupón va solo con el link checkout.",
)
EVENT_REQUIRES_VENCE = _spec(
    "event_requires_vence", 6, KIND_FORM,
    "Un flujo disparado por un evento lleva vence, el plazo desde el disparo.",
    alternative="Escribir termina.vence.",
)
SILENCE_FORBIDS_VENCE = _spec(
    "silence_forbids_vence", 6, KIND_FORM,
    "Un silencio no lleva vence: el plazo lo marca hasta.",
    alternative="Sacar termina.vence.",
)
CONVERSAR_OUTSIDE_OPEN_WINDOW_BRANCH = _spec(
    "conversar_outside_open_window_branch", 7, KIND_FORM,
    "conversar va solo en la rama entonces de una condición con la ventana abierta.",
)
TERMINAL_STEP_NOT_LAST = _spec(
    "terminal_step_not_last", 7, KIND_FORM,
    "conversar y derivar son el último paso de su rama.",
)
NESTED_CONDITION = _spec(
    "nested_condition", 7, KIND_FORM,
    "Una condición no puede llevar otra adentro.",
)
HOUR_INVALID = _spec(
    "hour_invalid", 8, KIND_FORM,
    "Las horas se escriben HH:MM, de 00:00 a 23:59.",
)
WINDOW_NOT_INCREASING = _spec(
    "window_not_increasing", 8, KIND_FORM,
    "En el horario, desde es menor que hasta.",
)
ZONE_INVALID = _spec(
    "zone_invalid", 8, KIND_FORM,
    "La zona no es una zona IANA válida.",
)
ZONE_CHANGED = _spec(
    "zone_changed", 8, KIND_PROTECTION,
    "se elige al crear el flujo y después no cambia, porque el cupo del día se cuenta en el día de esa zona.",
    limit="La zona de un flujo",
    alternative="Cambiar la franja, desde y hasta, en la misma zona.",
)
POOL_ZONE_MISMATCH = _spec(
    "pool_zone_mismatch", 8, KIND_FORM,
    "Los flujos de una misma bolsa tienen la misma zona; siempre cuenta como sin zona, distinta de cualquier zona.",
)
CAP_INVALID = _spec(
    "cap_invalid", 9, KIND_FORM,
    "Un tope es un entero mayor que 0, o sin_tope.",
)
DAILY_CAP_EXCEEDS_TOTAL = _spec(
    "daily_cap_exceeds_total", 9, KIND_PROTECTION,
    "el tope por día de un flujo no puede pasar el total.",
    limit="El tope por día",
    alternative="Subir el total o bajar el tope por día.",
)
UNKNOWN_FLOW_REFERENCE = _spec(
    "unknown_flow_reference", 10, KIND_FORM,
    "Nombra un flujo que no existe en la instancia.",
)
MUTUAL_LE_GANA_A = _spec(
    "mutual_le_gana_a", 10, KIND_FORM,
    "Dos flujos no pueden ganarse uno al otro.",
)
PRODUCT_NOT_DECLARED = _spec(
    "product_not_declared", 10, KIND_FORM,
    "La instancia no declara ese producto.",
)
PRODUCT_REQUIRED = _spec(
    "product_required", 10, KIND_FORM,
    "La instancia tiene más de un producto: el flujo dice de cuál es.",
    alternative="Escribir producto.",
)
DUPLICATE_FLOW_KEY = _spec(
    "duplicate_flow_key", 10, KIND_FORM,
    "Ya hay otro flujo con esa clave en ese producto.",
)
UNRESOLVED_VALUE = _spec(
    "unresolved_value", 11, KIND_FORM,
    "Una versión con a_medir, a_decidir o a_crear no se publica.",
    alternative="Completar el dato antes de pedir el sí: medirlo, decidirlo o crear la plantilla.",
)
VENCE_NOT_AFTER_WAITS = _spec(
    "vence_not_after_waits", 12, KIND_FORM,
    "vence tiene que superar la suma de las esperas del camino más largo.",
)
_TEMPLATE_LIMIT = "Una plantilla APPROVED del catálogo de la bandeja, en su idioma, con sus variables y su botón"
_TEMPLATE_ALTERNATIVE = "Elegir una plantilla aprobada del catálogo, o crearla y esperar que Meta la apruebe."
TEMPLATE_NOT_IN_CATALOG = _spec(
    "template_not_in_catalog", 13, KIND_PROTECTION,
    "Meta solo deja mandar una plantilla aprobada, y esa no está en el catálogo de la bandeja.",
    limit=_TEMPLATE_LIMIT, alternative=_TEMPLATE_ALTERNATIVE,
)
TEMPLATE_NOT_APPROVED = _spec(
    "template_not_approved", 13, KIND_PROTECTION,
    "Meta solo deja mandar una plantilla aprobada, y esa no está APPROVED en el catálogo.",
    limit=_TEMPLATE_LIMIT, alternative=_TEMPLATE_ALTERNATIVE,
)
TEMPLATE_LANGUAGE_MISMATCH = _spec(
    "template_language_mismatch", 13, KIND_PROTECTION,
    "La plantilla no está en ese idioma en el catálogo, y Meta la busca por nombre e idioma.",
    limit=_TEMPLATE_LIMIT, alternative="Escribir el idioma con el que está aprobada.",
)
TEMPLATE_VARIABLE_COUNT_MISMATCH = _spec(
    "template_variable_count_mismatch", 13, KIND_PROTECTION,
    "La plantilla pide otra cantidad de variables, o parámetros que el flujo no manda, y Meta rechazaría el envío.",
    limit=_TEMPLATE_LIMIT, alternative="Escribir tantas variables como marcadores tiene la plantilla, en orden.",
)
TEMPLATE_BUTTON_MISMATCH = _spec(
    "template_button_mismatch", 13, KIND_PROTECTION,
    "El botón que declara el paso no es el que tiene la plantilla aprobada.",
    limit=_TEMPLATE_LIMIT, alternative="Declarar el botón que tiene la plantilla, o elegir otra.",
)
TEMPLATE_CATALOG_UNAVAILABLE = _spec(
    "template_catalog_unavailable", 13, KIND_PROTECTION,
    "No se pudo leer el catálogo de la bandeja, y sin él no se sabe si la plantilla sigue aprobada.",
    limit=_TEMPLATE_LIMIT, alternative="Volver a validar cuando el catálogo se pueda leer.",
)
COUPON_CHANGE_NOT_SUPPORTED = _spec(
    "coupon_change_not_supported", 15, KIND_NOT_YET,
    "El código del cupón vive también en Hotmart y en el conocimiento del agente, y en la v1 no se cambia conversando.",
    limit="El cambio del código del cupón",
    alternative="Dejar el cupón de la versión publicada; cambiarlo es un cambio con PR y aprobación.",
)
VARIABLE_NEEDS_PERSONAL_COUPON = _spec(
    "variable_needs_personal_coupon", 5, KIND_PROTECTION,
    "precio y vence hablan del cupón personal: el bridge solo los llena en un paso cuyo botón lleva cupon: personal.",
    limit="precio y vence, solo con cupon: personal",
    alternative="Sumar el botón con cupon: personal al paso, o sacar la variable.",
)
VARIABLE_NEEDS_EVENT_TRIGGER = _spec(
    "variable_needs_event_trigger", 5, KIND_PROTECTION,
    "cuando es la hora del evento que disparó el flujo: un silencio no la tiene.",
    limit="cuando, solo con un disparador de evento",
    alternative="Sacar la variable, o usar una plantilla sin ese marcador.",
)
# La regla 14 cuando el flujo tiene legacy_kind (T2.1): lo que se publica tiene que compilar a la
# politica de su flujo del legado (flow_policy.compile_policy). El detalle de
# flow_policy_not_compilable, sin el step_key, es el codigo; la ruta es el paso.
_SHAPE_LIMIT = "La forma de su flujo del legado: los toques, el cupón y las variables de cada uno"
_SHAPE_ALTERNATIVE = "Dejar los toques como están; agregar, sacar o cambiar uno es un cambio con PR y aprobación."
ENGINE_NOT_SUPPORTED_MORE_SENDS = _spec(
    "engine_not_supported:more_sends_than_steps", 14, KIND_NOT_YET,
    "El flujo tiene más envíos que los pasos de su flujo del legado.",
    limit=_SHAPE_LIMIT, alternative=_SHAPE_ALTERNATIVE,
)
ENGINE_NOT_SUPPORTED_COUPON_NOT_ALLOWED = _spec(
    "engine_not_supported:coupon_not_allowed", 14, KIND_NOT_YET,
    "Ese paso de su flujo del legado no lleva cupón personal.",
    limit=_SHAPE_LIMIT, alternative=_SHAPE_ALTERNATIVE,
)
ENGINE_NOT_SUPPORTED_COUPON_MISSING = _spec(
    "engine_not_supported:coupon_missing", 14, KIND_NOT_YET,
    "Ese paso de su flujo del legado sale solo con su cupón personal: sin él, cierra sin mensaje.",
    limit=_SHAPE_LIMIT, alternative="Dejar el botón con cupon: personal en ese paso.",
)
ENGINE_NOT_SUPPORTED_FIXED_COUPON = _spec(
    "engine_not_supported:fixed_coupon", 14, KIND_NOT_YET,
    "El flujo del legado no manda un cupón fijo en un paso: solo el personal.",
    limit=_SHAPE_LIMIT, alternative="Usar cupon: personal, o sacar el cupón del paso.",
)
ENGINE_NOT_SUPPORTED_VARIABLES_DIFFER = _spec(
    "engine_not_supported:variables_differ", 14, KIND_NOT_YET,
    "El bridge manda ese paso con sus variables fijas, en su orden: otras no llegan a la plantilla.",
    limit=_SHAPE_LIMIT, alternative="Escribir las variables del paso como las manda el bridge.",
)
ENGINE_NOT_SUPPORTED_WAIT_BETWEEN_SENDS = _spec(
    "engine_not_supported:espera_entre_envios", 14, KIND_NOT_YET,
    "Entre dos envíos tiene que haber al menos un minuto de espera.",
    limit="Al menos un minuto entre dos envíos", alternative="Sumar una espera entre los dos envíos.",
)
ENGINE_NOT_SUPPORTED_WAIT_AFTER_SEND = _spec(
    "engine_not_supported:espera_despues_del_envio", 14, KIND_NOT_YET,
    "Una espera después del último envío no lleva a nada, y el motor no la ejecuta.",
    limit="Las esperas, antes de un envío", alternative="Sacar la espera del final.",
)
_COMPILE_DETAIL_SPECS: Mapping[str, CodeSpec] = {
    "more_sends_than_steps": ENGINE_NOT_SUPPORTED_MORE_SENDS,
    "coupon_not_allowed": ENGINE_NOT_SUPPORTED_COUPON_NOT_ALLOWED,
    "coupon_missing": ENGINE_NOT_SUPPORTED_COUPON_MISSING,
    "fixed_coupon": ENGINE_NOT_SUPPORTED_FIXED_COUPON,
    "variables_differ": ENGINE_NOT_SUPPORTED_VARIABLES_DIFFER,
    "espera_entre_envios": ENGINE_NOT_SUPPORTED_WAIT_BETWEEN_SENDS,
}
COUPON_PERSONAL_LIMIT = _spec(
    "coupon_personal_limit", 15, KIND_PROTECTION,
    "Un camino lleva más de dos cupones personales, y a cada caso le tocan a lo sumo dos códigos.",
    limit="Dos cupones personales por camino",
    alternative="Dejar el cupón personal en dos envíos como mucho.",
)
PURCHASE_PERMISSION_REQUIRES_SINGLE_UTILITY_SEND = _spec(
    "purchase_permission_requires_single_utility_send", 17, KIND_PROTECTION,
    "la persona no dio permiso de WhatsApp: con la compra como permiso solo se le confirma la compra, una vez.",
    limit="Con permiso: compra, un solo envío por camino, y una plantilla de utilidad",
    alternative="Usar una sola plantilla de utilidad, o permiso: formulario.",
)
FLOW_CREATION_NOT_ENABLED = _spec(
    "flow_creation_not_enabled", None, KIND_CYCLE,
    "En la 1b el Automation Expert no crea flujos: crear un flujo es de la 1c.",
    alternative="Pedírselo a un operador.",
)
CHANGE_OUTSIDE_1B_SCOPE = _spec(
    "change_outside_1b_scope", None, KIND_CYCLE,
    "En la 1b el Automation Expert cambia las esperas y el plazo, la plantilla, los topes por día y total, el horario, y prende, pausa o apaga.",
    alternative="Pedírselo a un operador.",
)
TRIGGER_CHANGE_IS_ANOTHER_FLOW = _spec(
    "trigger_change_is_another_flow", None, KIND_CYCLE,
    "Un disparador distinto es otro flujo: otro evento, o un silencio con otro lo_ultimo.",
    alternative="Dejar el evento, o el lo_ultimo del silencio, de la versión publicada; o armar otro flujo.",
)
FLOW_CHANGE_STALE_BASE = _spec(
    "flow_change_stale_base", None, KIND_CYCLE,
    "La versión activa del flujo no es la que se leyó al armar el borrador.",
    alternative="Rehacer la propuesta sobre la versión activa.",
)
ROLLBACK_TARGET_NOT_FOUND = _spec(
    "rollback_target_not_found", None, KIND_CYCLE,
    "Esa versión no está publicada en el flujo.",
)
ROLLBACK_TARGET_IS_ACTIVE = _spec(
    "rollback_target_is_active", None, KIND_CYCLE,
    "Esa versión ya es la activa.",
)
STATE_TRANSITION_INVALID = _spec(
    "state_transition_invalid", None, KIND_CYCLE,
    "Un flujo apagado solo se prende; uno prendido se pausa o se apaga; uno pausado se prende o se apaga.",
)
FLOW_HAS_NO_VERSION = _spec(
    "flow_has_no_version", None, KIND_CYCLE,
    "Un flujo sin versión no se prende.",
    alternative="Publicar su primera versión en la misma propuesta.",
)
EN_CURSO_NOT_SUPPORTED = _spec(
    "en_curso_not_supported", None, KIND_CYCLE,
    "Hasta el motor, lo que está en curso sigue con la versión que lo empezó.",
    alternative="Usar en_curso: seguir.",
)
CLIENT_PURCHASE_OK_MISSING = _spec(
    "client_purchase_ok_missing", None, KIND_CYCLE,
    "Pasar a permiso: compra pide el OK del cliente: quién lo dio, por qué medio y la fecha.",
    alternative="Sumar ok_del_cliente al ítem.",
)

ALWAYS_WITH_MARKETING = _spec(
    "always_with_marketing", None, KIND_WARNING,
    "Sin horario, una plantilla que no es de utilidad, como una de marketing, puede salir de noche.",
)
RECOVERY_WITHOUT_CAP = _spec(
    "recovery_without_cap", None, KIND_WARNING,
    "Un flujo de recuperación sin tope.",
)
SILENCE_WITHOUT_PURCHASE_EXIT = _spec(
    "silence_without_purchase_exit", None, KIND_WARNING,
    "Un silencio sin la salida compra le puede llegar a quien ya compró.",
)
SAME_TRIGGER_UNRELATED = _spec(
    "same_trigger_unrelated", None, KIND_WARNING,
    "Otro flujo del mismo producto tiene el mismo disparador, y no hay relación entre los dos.",
)
WAIT_WITHOUT_CONTESTA = _spec(
    "wait_without_contesta", None, KIND_WARNING,
    "Mientras espera, si la persona contesta otra plantilla nuestra, el agente no le responde hasta que este mensaje salga o venza.",
    alternative="Sumar contesta a termina.si.",
)
SILENCE_WINDOW_SHORTER_THAN_CLOSED_GAP = _spec(
    "silence_window_shorter_than_closed_gap", None, KIND_WARNING,
    "La franja del silencio es más corta que el tramo cerrado del horario, y fuera de horario el barrido no mira nada: pierde conversaciones.",
)
EMPTY_BRANCH_WITH_LE_GANA_A = _spec(
    "empty_branch_with_le_gana_a", None, KIND_WARNING,
    "A quien cae en la rama vacía no le llega nada, y tampoco el flujo al que este le gana.",
)

_VALIDATOR_CODES: tuple[CodeSpec, ...] = (
    TRIGGER_NOT_EXACTLY_ONE, SILENCE_WINDOW_INVALID, PERMISSION_TRIGGER_MISMATCH,
    CONTESTO_A_REQUIRES_CONVERSATION, PURCHASE_EXIT_MISSING, PURCHASE_EXIT_WITH_PURCHASE_TRIGGER,
    FICHA_VENCIDA_REQUIRES_FICHA_TRIGGER, MEDIO_DE_PAGO_REQUIRES_FICHA_TRIGGER,
    FICHA_LINK_REQUIRES_FICHA_TRIGGER, COUPON_REQUIRES_CHECKOUT_LINK, EVENT_REQUIRES_VENCE,
    SILENCE_FORBIDS_VENCE, CONVERSAR_OUTSIDE_OPEN_WINDOW_BRANCH, TERMINAL_STEP_NOT_LAST,
    NESTED_CONDITION, HOUR_INVALID, WINDOW_NOT_INCREASING, ZONE_INVALID, ZONE_CHANGED,
    POOL_ZONE_MISMATCH, CAP_INVALID, DAILY_CAP_EXCEEDS_TOTAL, UNKNOWN_FLOW_REFERENCE,
    MUTUAL_LE_GANA_A, PRODUCT_NOT_DECLARED, PRODUCT_REQUIRED, DUPLICATE_FLOW_KEY,
    UNRESOLVED_VALUE, VENCE_NOT_AFTER_WAITS, TEMPLATE_NOT_IN_CATALOG, TEMPLATE_NOT_APPROVED,
    TEMPLATE_LANGUAGE_MISMATCH, TEMPLATE_VARIABLE_COUNT_MISMATCH, TEMPLATE_BUTTON_MISMATCH,
    TEMPLATE_CATALOG_UNAVAILABLE, COUPON_CHANGE_NOT_SUPPORTED, COUPON_PERSONAL_LIMIT,
    VARIABLE_NEEDS_PERSONAL_COUPON, VARIABLE_NEEDS_EVENT_TRIGGER,
    ENGINE_NOT_SUPPORTED_MORE_SENDS, ENGINE_NOT_SUPPORTED_COUPON_NOT_ALLOWED, ENGINE_NOT_SUPPORTED_COUPON_MISSING,
    ENGINE_NOT_SUPPORTED_FIXED_COUPON, ENGINE_NOT_SUPPORTED_VARIABLES_DIFFER,
    ENGINE_NOT_SUPPORTED_WAIT_BETWEEN_SENDS, ENGINE_NOT_SUPPORTED_WAIT_AFTER_SEND,
    PURCHASE_PERMISSION_REQUIRES_SINGLE_UTILITY_SEND, FLOW_CREATION_NOT_ENABLED,
    CHANGE_OUTSIDE_1B_SCOPE, TRIGGER_CHANGE_IS_ANOTHER_FLOW, FLOW_CHANGE_STALE_BASE,
    ROLLBACK_TARGET_NOT_FOUND, ROLLBACK_TARGET_IS_ACTIVE, STATE_TRANSITION_INVALID,
    FLOW_HAS_NO_VERSION, EN_CURSO_NOT_SUPPORTED, CLIENT_PURCHASE_OK_MISSING,
    ALWAYS_WITH_MARKETING, RECOVERY_WITHOUT_CAP, SILENCE_WITHOUT_PURCHASE_EXIT,
    SAME_TRIGGER_UNRELATED, WAIT_WITHOUT_CONTESTA, SILENCE_WINDOW_SHORTER_THAN_CLOSED_GAP,
    EMPTY_BRANCH_WITH_LE_GANA_A,
)

# El catalogo cerrado: cada codigo con su regla y su tipo. required_key_missing
# tiene dos: el de la regla 1 (una clave sin valor por defecto) y el del ciclo
# (un flujo con version recibe la definicion completa).
CATALOG: tuple[CodeSpec, ...] = DEFINITION_CODES + _VALIDATOR_CODES
BLOCK_CODES = frozenset(spec.code for spec in CATALOG if spec.kind != KIND_WARNING)
WARNING_CODES = frozenset(spec.code for spec in CATALOG if spec.kind == KIND_WARNING)
CODE_RULES: Mapping[str, frozenset[tuple[int | None, str]]] = {
    code: frozenset((spec.rule, spec.kind) for spec in CATALOG if spec.code == code)
    for code in BLOCK_CODES | WARNING_CODES
}


class FlowValidationInputError(ValueError):
    """Lo que llega al validador no es coherente: un error de quien llama, no un hallazgo.

    ``flow_not_found``: un item nombra un ``flow_id`` que no esta en el mundo,
    o cuyo producto y flujo no son los del control (como el ``P0002`` de la base).
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ValidationResult:
    """Lo que devuelve una validacion: bloqueos, advertencias y los items normalizados.

    Lo que se guarda en el borrador son ``normalized_items``. ``to_json`` es
    el ``p_result`` que acepta la base, y ``validator_json`` dice con que
    reglas, en que fase y con que actor corrio.
    """

    blocks: tuple[Finding, ...]
    warnings: tuple[Finding, ...]
    normalized_items: tuple[ProposalItem, ...]
    phase: str
    actor: str

    def __post_init__(self) -> None:
        if self.phase not in PHASES:
            raise ValueError("validator_phase_invalid")
        if self.actor not in ACTORS:
            raise ValueError("validator_actor_invalid")

    @property
    def ok(self) -> bool:
        return not self.blocks

    def validator_json(self) -> dict[str, object]:
        """Con que corrio la validacion: ``{contract, rules_version, phase, actor}``, exactamente.

        ``phase`` es la fase con la que se llamo a ``validate`` (``draft`` o
        ``publish``) y ``actor``, la clase de quien propone
        (``automation_expert`` u ``operador``). La base exige esas cuatro claves
        en ``p_validator``, con ``phase: publish`` y el ``actor`` de la
        propuesta, al congelar la validacion y al revalidar antes de publicar:
        una validacion de borrador, o con el actor de otra clase, no publica.
        ``product_version`` y ``git_sha`` no los sabe un modulo puro: los suma
        quien llama.
        """
        return {
            "contract": VALIDATOR_CONTRACT, "rules_version": RULES_VERSION,
            "phase": self.phase, "actor": self.actor,
        }

    def to_json(self) -> dict[str, list[dict[str, object]]]:
        return {
            "blocks": [finding.to_json() for finding in self.blocks],
            "warnings": [finding.to_json() for finding in self.warnings],
        }

    def items_json(self) -> list[dict[str, Any]]:
        """Los items normalizados como los guarda la base (``p_items``)."""
        return [item.to_json() for item in self.normalized_items]


# ---------------------------------------------------------------------------
# Ayudas para leer una definicion sin suponer su forma.
# ---------------------------------------------------------------------------

def _mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _get(definition: Mapping[str, Any] | None, *keys: str) -> object:
    current: object = definition
    for key in keys:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def _resolved_list(value: object) -> list[Any] | None:
    """La lista, si lo es y no tiene ningun valor sin resolver."""
    if not isinstance(value, list) or any(is_unresolved(element) for element in value):
        return None
    return value


def _iter_steps(steps: object, segments: tuple[Segment, ...], *, branch: str | None = None,
                condition: object = None, depth: int = 0
                ) -> Iterable[tuple[Mapping[str, Any], tuple[Segment, ...], str | None, object, int, bool]]:
    """Cada paso: el paso, su ruta, la rama, la condicion que lo contiene, la profundidad y si es el ultimo."""
    if not isinstance(steps, list) or depth > MAX_DEPTH:
        return
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            continue
        at = segments + (index,)
        yield step, at, branch, condition, depth, index == len(steps) - 1
        if step_type(step) == "condicion":
            for name in ("entonces", "si_no"):
                yield from _iter_steps(step.get(name), at + (name,), branch=name,
                                       condition=step.get("condicion"), depth=depth + 1)


def _is_open_window(condition: object) -> bool:
    values = _get(_mapping(condition), "es")
    return (
        _get(_mapping(condition), "dato") == "ventana"
        and isinstance(values, list) and bool(values)
        and all(value == "abierta" for value in values)
    )


def _max_waits(steps: object, depth: int = 0) -> int | None:
    """La suma de las esperas del camino mas largo; None si alguna no se puede leer."""
    if not isinstance(steps, list) or depth > MAX_DEPTH:
        return 0
    total = 0
    for step in steps:
        kind = step_type(step)
        if kind == "esperar":
            minutes = parse_duration(step["esperar"])
            if minutes is None:
                return None
            total += minutes
        elif kind == "condicion":
            branches = [_max_waits(step.get(name), depth + 1) for name in ("entonces", "si_no")]
            if any(branch is None for branch in branches):
                return None
            total += max(branch for branch in branches if branch is not None)
    return total


def _max_sends(steps: object, depth: int = 0) -> int:
    """Los envios del camino que mas tiene: cada plantilla y cada conversar."""
    if not isinstance(steps, list) or depth > MAX_DEPTH:
        return 0
    total = 0
    for step in steps:
        kind = step_type(step)
        if kind in ("plantilla", "conversar"):
            total += 1
        elif kind == "condicion":
            total += max(_max_sends(step.get(name), depth + 1) for name in ("entonces", "si_no"))
    return total


def _wait_before_first_send(steps: object) -> bool:
    """Si en algun camino una espera mayor que 0m va antes del primer envio.

    Una espera sin resolver no se cuenta: no se sabe si es mayor que 0m.
    """
    found = False

    def walk(steps: object, states: set[tuple[bool, bool]], depth: int) -> set[tuple[bool, bool]]:
        nonlocal found
        if not isinstance(steps, list) or depth > MAX_DEPTH:
            return states
        for step in steps:
            kind = step_type(step)
            if kind == "esperar":
                minutes = parse_duration(step["esperar"])
                if minutes is not None and minutes > 0:
                    states = {(sent, waited or not sent) for sent, waited in states}
            elif kind in ("plantilla", "conversar"):
                if any(not sent and waited for sent, waited in states):
                    found = True
                states = {(True, waited) for _, waited in states}
            elif kind == "condicion":
                states = walk(step.get("entonces"), set(states), depth + 1) | walk(
                    step.get("si_no"), set(states), depth + 1)
        return states

    walk(steps, {(False, False)}, 0)
    return found


def _personal_coupon(step: object) -> bool:
    """Si el paso es una plantilla con ``boton.cupon: personal``."""
    if step_type(step) != "plantilla":
        return False
    assert isinstance(step, Mapping)
    button = _mapping(step.get("boton"))
    return button is not None and is_personal_coupon(button.get("cupon"))


def _max_personal_coupons(steps: object, depth: int = 0) -> int:
    """Los cupones personales del camino que mas tiene, contados como ``_max_sends``."""
    if not isinstance(steps, list) or depth > MAX_DEPTH:
        return 0
    total = 0
    for step in steps:
        if _personal_coupon(step):
            total += 1
        elif step_type(step) == "condicion":
            total += max(_max_personal_coupons(step.get(name), depth + 1) for name in ("entonces", "si_no"))
    return total


def _coupons(definition: Mapping[str, Any]) -> frozenset[str] | None:
    """Los codigos de cupon de la definicion; None si alguno esta sin resolver."""
    codes: set[str] = set()
    for step, _, _, _, _, _ in _iter_steps(definition.get("pasos"), ("pasos",)):
        button = _mapping(step.get("boton")) if step_type(step) == "plantilla" else None
        if button is None or "cupon" not in button:
            continue
        coupon = button["cupon"]
        if not isinstance(coupon, str) or is_unresolved(coupon):
            return None
        codes.add(coupon)
    return frozenset(codes)


def _zone_token(definition: Mapping[str, Any] | None) -> str | None:
    """La zona de una definicion para comparar bolsas: la zona, ``siempre``, o None si no se sabe."""
    schedule = _get(definition, "horario")
    if schedule == ALWAYS:
        return ALWAYS
    zone = _get(_mapping(schedule), "zona")
    if isinstance(zone, str) and not is_unresolved(zone):
        return zone
    return None


def _pool_of(definition: Mapping[str, Any] | None) -> str | None:
    pool = _get(definition, "topes", "bolsa")
    return pool if isinstance(pool, str) and not is_unresolved(pool) else None


def _own_product(definition: Mapping[str, Any] | None, flow: WorldFlow) -> str:
    product = _get(definition, "producto")
    return product if is_product_ref(product) else flow.product_ref


def _refs(definition: Mapping[str, Any] | None, *keys: str) -> list[object]:
    value = _get(definition, *keys)
    return list(value) if isinstance(value, list) else []


def _scope_view(definition: Mapping[str, Any]) -> dict[str, Any]:
    """La definicion sin lo que el Automation Expert puede cambiar en la 1b (decision 4).

    Salen los pasos ``esperar`` (en cualquier rama); ``plantilla``, ``idioma``
    y ``variables`` de cada paso plantilla; ``/termina/vence``,
    ``/topes/por_dia``, ``/topes/total`` y ``/horario``; ``version`` y
    ``estado``; ``desde`` y ``hasta`` de un silencio, que son su plazo
    (flow-definition-v1.md:855 y :161); y la identidad del disparador (el
    evento, o el ``lo_ultimo`` del silencio) y ``boton.cupon``, que tienen su
    propio codigo. Lo que queda de ``/dispara`` es quien entra
    (``conversaciones``): eso no lo cambia el agente.
    """
    view = copy.deepcopy(dict(definition))
    for key in ("version", "estado", "horario"):
        view.pop(key, None)
    trigger = view.get("dispara")
    if isinstance(trigger, dict):
        trigger.pop("evento", None)
        silence = trigger.get("silencio")
        if isinstance(silence, dict):
            for key in ("lo_ultimo", "desde", "hasta"):
                silence.pop(key, None)
    exits = view.get("termina")
    if isinstance(exits, dict):
        exits.pop("vence", None)
    caps = view.get("topes")
    if isinstance(caps, dict):
        caps.pop("por_dia", None)
        caps.pop("total", None)
    if "pasos" in view:
        view["pasos"] = _scope_steps(view["pasos"], 0)
    return view


def _scope_steps(steps: object, depth: int) -> object:
    if not isinstance(steps, list) or depth > MAX_DEPTH:
        return steps
    kept: list[object] = []
    for step in steps:
        kind = step_type(step)
        if kind == "esperar":
            continue
        if kind == "plantilla":
            stripped = {key: value for key, value in step.items()
                        if key not in ("plantilla", "idioma", "variables")}
            if isinstance(stripped.get("boton"), dict):
                stripped["boton"] = {key: value for key, value in stripped["boton"].items()
                                     if key != "cupon"}
            kept.append(stripped)
        elif kind == "condicion":
            stripped = dict(step)
            for name in ("entonces", "si_no"):
                if name in stripped:
                    stripped[name] = _scope_steps(stripped[name], depth + 1)
            kept.append(stripped)
        else:
            kept.append(step)
    return kept


# ---------------------------------------------------------------------------
# La validacion.
# ---------------------------------------------------------------------------

class _Emitter:
    """Junta los hallazgos de una definicion, con las rutas prefijadas por su lugar."""

    def __init__(self, base: tuple[Segment, ...], collapse: str | None = None) -> None:
        self.base = base
        self.collapse = collapse
        self.findings: list[Finding] = []

    def add(self, spec: CodeSpec, segments: Sequence[Segment] = (), *, reason: str | None = None) -> None:
        path = self.collapse if self.collapse is not None else finding_path(self.base + tuple(segments))
        self.findings.append(spec.at(path, reason=reason))


@dataclass
class _Subject:
    """Una definicion que la propuesta deja activa: la de un item version o el destino de un volver."""

    index: int
    item: ProposalItem
    flow: WorldFlow
    definition: Mapping[str, Any]
    emitter: _Emitter
    is_rollback: bool


class _Run:
    def __init__(self, items: tuple[ProposalItem, ...], *, actor: str, world: FlowWorld,
                 instance: InstanceView, catalog: TemplateCatalog | None, phase: str,
                 draft_base: Mapping[str, Any] | None) -> None:
        self.items = items
        self.actor = actor
        self.world = world
        self.instance = instance
        self.catalog = catalog
        self.phase = phase
        self.publish = phase == "publish"
        self.draft_base = draft_base
        self.blocks: list[Finding] = []
        self.warnings: list[Finding] = []
        self.flows = [self._resolve(item) for item in items]
        self.normalized: list[ProposalItem] = []
        self.subjects: list[_Subject] = []
        self.effective: dict[str, Mapping[str, Any] | None] = {
            flow.flow_id: flow.active_definition for flow in world.flows
        }

    def _resolve(self, item: ProposalItem) -> WorldFlow:
        flow = self.world.flow(item.flow_id)
        if flow is None or flow.product_ref != item.producto or flow.flow_key != item.flujo:
            raise FlowValidationInputError("flow_not_found")
        return flow

    # -- El recorrido --------------------------------------------------------

    def run(self) -> ValidationResult:
        self._normalize()
        self._stale_base()
        for index, (item, flow) in enumerate(zip(self.items, self.flows)):
            if item.tipo in ("version", "volver") and item.en_curso != "seguir":
                self.blocks.append(EN_CURSO_NOT_SUPPORTED.at(finding_path((index, "en_curso"))))
            if item.tipo == "prender":
                self._switch_on(index, item, flow)
            elif item.tipo == "apagar":
                self._switch_off(index, item, flow)
        for subject in self.subjects:
            self._subject(subject)
        total = json_size([item.to_json() for item in self.normalized])
        if total is None or total > ITEMS_MAX_BYTES:
            self.blocks.append(INVALID_VALUE.at("", reason=(
                "Los ítems de la propuesta no se pueden escribir como JSON." if total is None
                else f"Los ítems de la propuesta pasan de {ITEMS_MAX_BYTES} bytes.")))
        blocks = dedupe_findings(self.blocks)
        warnings = dedupe_findings(self.warnings) if self.publish else []
        if len(blocks) + len(warnings) > MAX_FINDINGS:
            # Lo que importa del resultado es que bloquea: se quedan los primeros bloqueos.
            warnings = warnings[: max(0, MAX_FINDINGS - len(blocks))]
            blocks = blocks[:MAX_FINDINGS]
        # Lo mismo con los bytes: primero salen las advertencias y despues los
        # ultimos bloqueos, y siempre queda al menos uno.
        while (warnings or len(blocks) > 1) and _result_bytes(blocks, warnings) > MAX_RESULT_BYTES:
            if warnings:
                warnings.pop()
            else:
                blocks.pop()
        return ValidationResult(
            blocks=tuple(blocks), warnings=tuple(warnings), normalized_items=tuple(self.normalized),
            phase=self.phase, actor=self.actor,
        )

    def _normalize(self) -> None:
        for index, (item, flow) in enumerate(zip(self.items, self.flows)):
            if item.tipo == "version":
                others = [
                    other.active_definition.get("horario")
                    for other in self.world.flows
                    if other.flow_id != flow.flow_id and other.active_definition is not None
                ]
                result = normalize_definition(
                    item.definicion or {}, new_flow=flow.active_version is None,
                    instance=self.instance, other_schedules=others, catalog=self.catalog,
                    path=(index, "definicion"),
                )
                self.blocks.extend(result.findings)
                self.normalized.append(item.with_definition(result.definition))
                if item.ok_del_cliente is not None:
                    self.blocks.extend(check_client_ok(item.ok_del_cliente,
                                                       path=(index, "ok_del_cliente")))
                if depth_of(result.definition) <= MAX_DEPTH:
                    self.effective[flow.flow_id] = result.definition
                    self.subjects.append(_Subject(
                        index=index, item=item, flow=flow, definition=result.definition,
                        emitter=_Emitter((index, "definicion")), is_rollback=False,
                    ))
            elif item.tipo == "volver":
                self.normalized.append(item)
                target = flow.versions.get(item.version) if item.version is not None else None
                at = finding_path((index, "version"))
                if target is None:
                    self.blocks.append(ROLLBACK_TARGET_NOT_FOUND.at(at))
                elif item.version == flow.active_version:
                    self.blocks.append(ROLLBACK_TARGET_IS_ACTIVE.at(at))
                elif depth_of(target) > MAX_DEPTH:
                    self.blocks.append(INVALID_VALUE.at(at, reason="La versión de destino tiene más niveles de los que admite el esquema."))
                else:
                    self.effective[flow.flow_id] = target
                    # Los hallazgos del destino apuntan al item: su definicion no esta en la propuesta.
                    emitter = _Emitter((index, "version"), collapse=at)
                    self.blocks.extend(
                        finding_at(finding, at)
                        for finding in check_definition(target, published=True, path=(index, "version"))
                    )
                    self.subjects.append(_Subject(
                        index=index, item=item, flow=flow, definition=target, emitter=emitter,
                        is_rollback=True,
                    ))
            else:
                self.normalized.append(item)

    def _stale_base(self) -> None:
        if self.draft_base is None:
            return
        expected = self.world.draft_base_for(self.items)
        given = self.draft_base
        if not isinstance(given, Mapping):
            self.blocks.append(FLOW_CHANGE_STALE_BASE.at(""))
            return
        for index, item in enumerate(self.items):
            if not json_equal(given.get(item.flow_id), expected.get(item.flow_id)):
                self.blocks.append(FLOW_CHANGE_STALE_BASE.at(finding_path((index,))))
        if set(given) - set(expected):
            self.blocks.append(FLOW_CHANGE_STALE_BASE.at(""))

    # -- Prender y apagar ----------------------------------------------------

    def _switch_on(self, index: int, item: ProposalItem, flow: WorldFlow) -> None:
        at = finding_path((index,))
        if flow.state == "prendido":
            self.blocks.append(STATE_TRANSITION_INVALID.at(finding_path((index, "tipo"))))
        content = [other for other in self.items
                   if other.flow_id == item.flow_id and other.is_content]
        has_new_version = any(other.tipo == "version" for other in content)
        if flow.active_version is None and not has_new_version:
            self.blocks.append(FLOW_HAS_NO_VERSION.at(at))
        if not self.publish or content or flow.active_definition is None:
            return
        # Prender revalida la definicion activa con la 13 y la 17: la 17 depende
        # de la categoria viva del catalogo (y la 14 y la 16 llegan con el motor).
        active = flow.active_definition
        if depth_of(active) > MAX_DEPTH:
            return
        emitter = _Emitter((index,), collapse=at)
        categories = self._rule13(emitter, active)
        self._rule17(emitter, active, categories)
        self.blocks.extend(emitter.findings)

    def _switch_off(self, index: int, item: ProposalItem, flow: WorldFlow) -> None:
        if flow.state == "apagado":
            self.blocks.append(STATE_TRANSITION_INVALID.at(finding_path((index, "tipo"))))

    # -- Una definicion ------------------------------------------------------

    def _subject(self, subject: _Subject) -> None:
        definition = subject.definition
        emitter = subject.emitter
        flow = subject.flow
        active = flow.active_definition
        if not subject.is_rollback:
            self._identity(subject)
            if self.actor == "automation_expert" and active is None:
                self.blocks.append(FLOW_CREATION_NOT_ENABLED.at(finding_path((subject.index,))))
            if active is not None:
                mine, published = _trigger_identity(definition), _trigger_identity(active)
                if mine is not None and published is not None and not json_equal(list(mine), list(published)):
                    emitter.add(TRIGGER_CHANGE_IS_ANOTHER_FLOW, ("dispara",))
                if self.actor == "automation_expert":
                    self._scope(subject, active)
        self._rule2(emitter, definition)
        self._rule3(emitter, definition)
        self._rule4(emitter, definition)
        self._rule5(emitter, definition)
        self._rule6(emitter, definition)
        self._rule7(emitter, definition)
        self._rule8(emitter, definition, flow)
        self._rule9(emitter, definition)
        self._rule10(emitter, definition, flow)
        self._rule5_variables(emitter, definition)
        if self.publish:
            self._rule11(emitter, definition)
            self._rule12(emitter, definition)
            categories = self._rule13(emitter, definition)
            self._rule14_compiles(emitter, definition, flow)
            if active is not None:
                self._rule15(emitter, definition, active)
            self._rule15_personal(emitter, definition)
            self._rule17(emitter, definition, categories)
            if not subject.is_rollback:
                self._client_ok(subject, active)
            self._warnings(subject, categories)
        self.blocks.extend(emitter.findings)

    def _identity(self, subject: _Subject) -> None:
        """La clave y el producto de la definicion son los del item, y la clave es unica en su producto."""
        definition = subject.definition
        item = subject.item
        emitter = subject.emitter
        key = definition.get("flujo")
        product = definition.get("producto")
        lookup_product = product if is_product_ref(product) else item.producto
        if is_flow_key(key) and key != item.flujo:
            other = self.world.find(lookup_product, key)
            if other is not None and other.flow_id != item.flow_id:
                emitter.add(DUPLICATE_FLOW_KEY, ("flujo",))
            else:
                emitter.add(INVALID_VALUE, ("flujo",), reason="La clave no es la del flujo del ítem, y es igual en todas las versiones.")
        if is_product_ref(product) and product != item.producto:
            other = self.world.find(product, key) if is_flow_key(key) else None
            if other is not None and other.flow_id != item.flow_id:
                emitter.add(DUPLICATE_FLOW_KEY, ("flujo",))
            else:
                emitter.add(INVALID_VALUE, ("producto",), reason="El producto no es el del flujo del ítem.")

    def _scope(self, subject: _Subject, active: Mapping[str, Any]) -> None:
        """``change_outside_1b_scope``: lo que el Automation Expert cambia fuera de la decision 4."""
        before = _scope_view(active)
        after = _scope_view(subject.definition)
        keys: list[tuple[Segment, ...]] = []
        for change in diff(before, after):
            segments = _pointer_segments(change.path)
            if not segments:
                continue
            top = segments[0]
            if top == "pasos":
                key: tuple[Segment, ...] = ("pasos",)
            elif top == "dispara" and len(segments) >= 3:
                # /dispara/silencio/conversaciones: quien entra, no el silencio entero.
                key = tuple(segments[:3])
            elif len(segments) >= 2 and isinstance(before.get(top), Mapping) and isinstance(after.get(top), Mapping):
                key = (top, segments[1])
            else:
                key = (top,)
            if key not in keys:
                keys.append(key)
        for key in keys:
            subject.emitter.add(CHANGE_OUTSIDE_1B_SCOPE, key)

    # -- Las reglas de forma (de la 2 a la 10) -------------------------------

    def _rule2(self, emitter: _Emitter, definition: Mapping[str, Any]) -> None:
        trigger = _mapping(definition.get("dispara"))
        if trigger is None:
            return
        present = [key for key in ("evento", "silencio") if key in trigger]
        if len(present) != 1:
            emitter.add(TRIGGER_NOT_EXACTLY_ONE, ("dispara",))
            return
        silence = _mapping(trigger.get("silencio"))
        if silence is None:
            return
        since = parse_duration(silence.get("desde"))
        until = parse_duration(silence.get("hasta"))
        if until is not None and until > SILENCE_MAX_MINUTES:
            emitter.add(SILENCE_WINDOW_INVALID, ("dispara", "silencio", "hasta"))
        elif since is not None and until is not None and since >= until:
            emitter.add(SILENCE_WINDOW_INVALID, ("dispara", "silencio", "desde"))

    def _rule3(self, emitter: _Emitter, definition: Mapping[str, Any]) -> None:
        trigger = trigger_of(definition)
        entry = _mapping(definition.get("entra"))
        if entry is None:
            return
        permission = entry.get("permiso")
        known = isinstance(permission, str) and permission in PERMISSIONS
        if trigger is not None and known:
            if permission == "formulario":
                fits: bool | None = trigger.kind == "evento"
            elif permission == "conversacion":
                fits = trigger.kind == "silencio"
            elif trigger.kind == "evento" and trigger.event is None:
                fits = None
            else:
                fits = trigger.is_purchase
            if fits is False:
                emitter.add(PERMISSION_TRIGGER_MISMATCH, ("entra", "permiso"))
        replied_to = entry.get("contesto_a")
        if known and permission != "conversacion" and isinstance(replied_to, list) and replied_to:
            emitter.add(CONTESTO_A_REQUIRES_CONVERSATION, ("entra", "contesto_a"))

    def _rule4(self, emitter: _Emitter, definition: Mapping[str, Any]) -> None:
        trigger = trigger_of(definition)
        if trigger is None or trigger.kind != "evento" or trigger.event is None:
            return
        exits = definition.get("termina")
        if "termina" in definition and not isinstance(exits, Mapping):
            return
        reasons = _get(_mapping(exits), "si")
        if reasons is None:
            if not trigger.is_purchase:
                emitter.add(PURCHASE_EXIT_MISSING, ("termina", "si"))
            return
        listed = _resolved_list(reasons)
        if listed is None:
            return
        if trigger.is_purchase and "compra" in listed:
            emitter.add(PURCHASE_EXIT_WITH_PURCHASE_TRIGGER, ("termina", "si"))
        elif not trigger.is_purchase and "compra" not in listed:
            emitter.add(PURCHASE_EXIT_MISSING, ("termina", "si"))

    def _rule5(self, emitter: _Emitter, definition: Mapping[str, Any]) -> None:
        trigger = trigger_of(definition)
        if trigger is None or (trigger.kind == "evento" and trigger.event is None):
            ficha: bool | None = None
        else:
            ficha = trigger.kind == "evento" and trigger.event in FICHA_EVENTS
        reasons = _get(definition, "termina", "si")
        if isinstance(reasons, list) and ficha is not None:
            printed = trigger is not None and trigger.event == "ficha_impresa"
            for index, reason in enumerate(reasons):
                if reason == "ficha_vencida" and not printed:
                    emitter.add(FICHA_VENCIDA_REQUIRES_FICHA_TRIGGER, ("termina", "si", index))
        for step, at, _, _, _, _ in _iter_steps(definition.get("pasos"), ("pasos",)):
            kind = step_type(step)
            if kind == "plantilla":
                variables = step.get("variables")
                if isinstance(variables, list) and ficha is False:
                    for index, variable in enumerate(variables):
                        if variable == "medio_de_pago":
                            emitter.add(MEDIO_DE_PAGO_REQUIRES_FICHA_TRIGGER, at + ("variables", index))
                button = _mapping(step.get("boton"))
                if button is not None:
                    link = button.get("link")
                    if link == "ficha" and ficha is False:
                        emitter.add(FICHA_LINK_REQUIRES_FICHA_TRIGGER, at + ("boton", "link"))
                    if "cupon" in button and isinstance(link, str) and not is_unresolved(link) and link != "checkout":
                        emitter.add(COUPON_REQUIRES_CHECKOUT_LINK, at + ("boton", "cupon"))
                    elif "cupon" in button and "link" not in button:
                        emitter.add(COUPON_REQUIRES_CHECKOUT_LINK, at + ("boton", "cupon"))
            elif kind == "condicion":
                if _get(_mapping(step.get("condicion")), "dato") == "medio_de_pago" and ficha is False:
                    emitter.add(MEDIO_DE_PAGO_REQUIRES_FICHA_TRIGGER, at + ("condicion", "dato"))

    def _rule5_variables(self, emitter: _Emitter, definition: Mapping[str, Any]) -> None:
        """Las variables de la 1.1, en la regla 5: precio y vence solo con cupon personal, cuando solo
        con un evento. Van en la 5 y no en una 18 porque la base acepta reglas de 1 a 17
        (``_flow_change_result_is_valid``, ``20261016000100_flow_change_store_v1.sql:726``).

        Es lo que hace el bridge: llena precio y vence con el cupon del paso (A1, Z y el seguimiento
        del pago rechazado) y cuando con la hora del evento. Con el disparador sin resolver, cuando
        no se mira.
        """
        trigger = trigger_of(definition)
        event = None if trigger is None else trigger.kind == "evento"
        for step, at, _, _, _, _ in _iter_steps(definition.get("pasos"), ("pasos",)):
            if step_type(step) != "plantilla":
                continue
            variables = step.get("variables")
            if not isinstance(variables, list):
                continue
            personal = _personal_coupon(step)
            for index, variable in enumerate(variables):
                if variable in COUPON_VARIABLES and not personal:
                    emitter.add(VARIABLE_NEEDS_PERSONAL_COUPON, at + ("variables", index))
                elif variable in EVENT_VARIABLES and event is False:
                    emitter.add(VARIABLE_NEEDS_EVENT_TRIGGER, at + ("variables", index))

    def _rule6(self, emitter: _Emitter, definition: Mapping[str, Any]) -> None:
        trigger = trigger_of(definition)
        if trigger is None:
            return
        exits = definition.get("termina")
        if "termina" in definition and not isinstance(exits, Mapping):
            return
        has_deadline = isinstance(exits, Mapping) and "vence" in exits
        if trigger.kind == "evento" and not has_deadline:
            emitter.add(EVENT_REQUIRES_VENCE, ("termina", "vence"))
        elif trigger.kind == "silencio" and has_deadline:
            emitter.add(SILENCE_FORBIDS_VENCE, ("termina", "vence"))

    def _rule7(self, emitter: _Emitter, definition: Mapping[str, Any]) -> None:
        for step, at, branch, condition, depth, last in _iter_steps(definition.get("pasos"), ("pasos",)):
            kind = step_type(step)
            if kind == "conversar" and not (branch == "entonces" and _is_open_window(condition)):
                emitter.add(CONVERSAR_OUTSIDE_OPEN_WINDOW_BRANCH, at + ("conversar",))
            if kind in ("conversar", "derivar") and not last:
                emitter.add(TERMINAL_STEP_NOT_LAST, at + (kind,))
            if kind == "condicion" and depth > 0:
                emitter.add(NESTED_CONDITION, at + ("condicion",))

    def _rule8(self, emitter: _Emitter, definition: Mapping[str, Any], flow: WorldFlow) -> None:
        schedule = _mapping(definition.get("horario"))
        if schedule is not None:
            hours = {}
            for key in ("desde", "hasta"):
                if key in schedule and not is_unresolved(schedule[key]):
                    minutes = parse_hour(schedule[key])
                    if minutes is None:
                        emitter.add(HOUR_INVALID, ("horario", key))
                    hours[key] = minutes
            since, until = hours.get("desde"), hours.get("hasta")
            if since is not None and until is not None and since >= until:
                emitter.add(WINDOW_NOT_INCREASING, ("horario", "hasta"))
            zone = schedule.get("zona")
            if "zona" in schedule and not is_unresolved(zone):
                if not is_iana_zone(zone):
                    emitter.add(ZONE_INVALID, ("horario", "zona"))
                elif flow.fixed_zone is not None and zone != flow.fixed_zone:
                    emitter.add(ZONE_CHANGED, ("horario", "zona"))
        pool = _pool_of(definition)
        token = _zone_token(definition)
        if pool is None or token is None:
            return
        for other_id, other in self.effective.items():
            if other_id == flow.flow_id or other is None or _pool_of(other) != pool:
                continue
            other_token = _zone_token(other)
            if other_token is not None and other_token != token:
                emitter.add(POOL_ZONE_MISMATCH, ("horario",))
                return

    def _rule9(self, emitter: _Emitter, definition: Mapping[str, Any]) -> None:
        caps = _mapping(definition.get("topes"))
        if caps is None:
            return
        numbers: dict[str, int] = {}
        for key in ("por_dia", "total"):
            if key not in caps:
                continue
            value = caps[key]
            if is_unresolved(value) or value == NO_CAP:
                continue
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                numbers[key] = value
            else:
                emitter.add(CAP_INVALID, ("topes", key))
        if len(numbers) == 2 and numbers["por_dia"] > numbers["total"]:
            emitter.add(DAILY_CAP_EXCEEDS_TOTAL, ("topes", "por_dia"))

    def _rule10(self, emitter: _Emitter, definition: Mapping[str, Any], flow: WorldFlow) -> None:
        if "producto" not in definition:
            if len(self.instance.products) > 1:
                emitter.add(PRODUCT_REQUIRED, ("producto",))
        elif is_product_ref(definition["producto"]) and definition["producto"] not in self.instance.products:
            emitter.add(PRODUCT_NOT_DECLARED, ("producto",))
        own_product = _own_product(definition, flow)
        for keys in (("entra", "contesto_a"), ("prioridad", "le_gana_a"),
                     ("prioridad", "no_arranca_mientras")):
            for index, ref in enumerate(_refs(definition, *keys)):
                if is_unresolved(ref):
                    continue
                parsed = parse_flow_ref(ref, own_product)
                if parsed is not None and self.world.find(*parsed) is None:
                    emitter.add(UNKNOWN_FLOW_REFERENCE, keys + (index,))
        me = (flow.product_ref, flow.flow_key)
        for index, ref in enumerate(_refs(definition, "prioridad", "le_gana_a")):
            parsed = parse_flow_ref(ref, own_product) if not is_unresolved(ref) else None
            other = self.world.find(*parsed) if parsed is not None else None
            if other is None or other.flow_id == flow.flow_id:
                continue
            theirs = self.effective.get(other.flow_id)
            their_product = _own_product(theirs, other)
            for their_ref in _refs(theirs, "prioridad", "le_gana_a"):
                if not is_unresolved(their_ref) and parse_flow_ref(their_ref, their_product) == me:
                    emitter.add(MUTUAL_LE_GANA_A, ("prioridad", "le_gana_a", index))
                    break

    # -- Las de publicar (11, 12, 13, 15 y 17) -------------------------------

    def _rule11(self, emitter: _Emitter, definition: Mapping[str, Any]) -> None:
        """Nada sin resolver, en cualquier rama: el invariante 3 de la base, del mismo modo."""
        stack: list[tuple[tuple[Segment, ...], object]] = [((), definition)]
        while stack:
            at, current = stack.pop()
            if is_unresolved(current):
                emitter.add(UNRESOLVED_VALUE, at)
            elif isinstance(current, Mapping):
                if TO_CREATE in current:
                    emitter.add(UNRESOLVED_VALUE, at)
                stack.extend((at + (key,), child) for key, child in reversed(list(current.items())))
            elif isinstance(current, list):
                stack.extend((at + (index,), child) for index, child in reversed(list(enumerate(current))))

    def _rule12(self, emitter: _Emitter, definition: Mapping[str, Any]) -> None:
        trigger = trigger_of(definition)
        if trigger is None or trigger.kind != "evento":
            return
        deadline = parse_duration(_get(definition, "termina", "vence"))
        waits = _max_waits(definition.get("pasos"))
        if deadline is not None and waits is not None and deadline <= waits:
            emitter.add(VENCE_NOT_AFTER_WAITS, ("termina", "vence"))

    def _rule13(self, emitter: _Emitter, definition: Mapping[str, Any]) -> dict[tuple[Segment, ...], object]:
        """Las plantillas contra el catalogo. Devuelve la categoria de cada paso resuelto.

        Un paso resuelto es el que encontro en el catalogo una sola plantilla
        con su nombre y su idioma. Su categoria va tal cual la trae el
        catalogo, ``None`` si falta: la 17 y las advertencias la comparan con
        ``UTILITY`` en forma positiva, y una categoria nula, vacia o que no es
        un texto cuenta como que no es de utilidad.

        No mira una plantilla ``{a_crear: ...}``: no esta en el catalogo por
        definicion, y la bloquea la regla 11 con un mensaje mas claro.
        """
        categories: dict[tuple[Segment, ...], object] = {}
        for step, at, _, _, _, _ in _iter_steps(definition.get("pasos"), ("pasos",)):
            if step_type(step) != "plantilla":
                continue
            name = step.get("plantilla")
            if not is_template_name(name) or is_unresolved(name):
                continue
            if self.catalog is None:
                emitter.add(TEMPLATE_CATALOG_UNAVAILABLE, at + ("plantilla",))
                continue
            named = self.catalog.named(name)
            if not named:
                emitter.add(TEMPLATE_NOT_IN_CATALOG, at + ("plantilla",))
                continue
            language = step.get("idioma")
            if not isinstance(language, str) or is_unresolved(language):
                continue
            same = [template for template in named
                    if isinstance(template.get("language"), str)
                    and template["language"].strip() == language]
            if not same:
                emitter.add(TEMPLATE_LANGUAGE_MISMATCH, at + ("idioma",))
                continue
            if len(same) > 1:
                emitter.add(TEMPLATE_NOT_IN_CATALOG, at + ("plantilla",),
                            reason="Hay más de una plantilla con ese nombre y ese idioma en el catálogo.")
                continue
            template = same[0]
            categories[at] = template.get("category")
            if template.get("status") != "APPROVED":
                emitter.add(TEMPLATE_NOT_APPROVED, at + ("plantilla",))
                continue
            self._template_shape(emitter, step, at, template)
        return categories

    def _template_shape(self, emitter: _Emitter, step: Mapping[str, Any], at: tuple[Segment, ...],
                        template: Mapping[str, Any]) -> None:
        """Los marcadores {{1}}..{{n}} y los botones, con la forma de ``parse_approved_template``."""
        components = template.get("components")
        if not isinstance(components, list) or not all(isinstance(c, Mapping) for c in components):
            emitter.add(TEMPLATE_VARIABLE_COUNT_MISMATCH, at + ("plantilla",),
                        reason="La plantilla del catálogo no trae sus componentes.")
            return
        bodies = [component for component in components if component.get("type") == "BODY"]
        body = bodies[0].get("text") if len(bodies) == 1 else None
        variables = _resolved_list(step.get("variables"))
        if not isinstance(body, str) or not body.strip():
            emitter.add(TEMPLATE_VARIABLE_COUNT_MISMATCH, at + ("plantilla",),
                        reason="La plantilla del catálogo no tiene un cuerpo.")
        elif variables is not None:
            placeholders = sorted({int(number) for number in _PLACEHOLDER.findall(body)})
            if placeholders != list(range(1, len(variables) + 1)) or "{{" in _PLACEHOLDER.sub("", body):
                emitter.add(TEMPLATE_VARIABLE_COUNT_MISMATCH, at + ("variables",))
        buttons: list[object] = []
        groups = 0
        for component in components:
            kind = component.get("type")
            if kind == "BODY":
                continue
            if kind == "BUTTONS":
                groups += 1
                listed = component.get("buttons")
                buttons.extend(listed if isinstance(listed, list) else [None])
                continue
            if kind in _STATIC_COMPONENTS:
                text = component.get("text")
                if (kind == "HEADER" and component.get("format", "TEXT") != "TEXT") or (
                    isinstance(text, str) and "{{" in text
                ):
                    emitter.add(TEMPLATE_VARIABLE_COUNT_MISMATCH, at + ("plantilla",),
                                reason="El encabezado o el pie de la plantilla piden parámetros que el flujo no manda.")
                continue
            emitter.add(TEMPLATE_VARIABLE_COUNT_MISMATCH, at + ("plantilla",),
                        reason="La plantilla tiene un componente que el flujo no sabe llenar.")
        button = _mapping(step.get("boton"))
        link = button.get("link") if button is not None else None
        if button is not None and (not isinstance(link, str) or is_unresolved(link)):
            return
        def quick_replies(rest: list[object]) -> bool:
            return all(isinstance(b, Mapping) and b.get("type") == "QUICK_REPLY" for b in rest)

        # The link opens the group and quick replies may follow it («No más
        # mensajes» after the checkout, 18/10), the same order that
        # ``parse_approved_template(checkout_button=True)`` accepts.
        if link == "checkout":
            fits = groups == 1 and bool(buttons) and _is_url_button(buttons[0]) and (
                buttons[0].get("url") == CHECKOUT_BUTTON_URL) and quick_replies(buttons[1:])
        elif link == "ficha":
            fits = groups == 1 and bool(buttons) and _is_url_button(buttons[0]) and (
                _is_dynamic_url(buttons[0].get("url"))) and quick_replies(buttons[1:])
        else:
            fits = quick_replies(buttons)
        if not fits:
            emitter.add(TEMPLATE_BUTTON_MISMATCH, at + (("boton",) if button is not None else ("plantilla",)))

    def _rule15(self, emitter: _Emitter, definition: Mapping[str, Any], active: Mapping[str, Any]) -> None:
        new = _coupons(definition)
        old = _coupons(active)
        if new is None or old is None or new == old:
            return
        for step, at, _, _, _, _ in _iter_steps(definition.get("pasos"), ("pasos",)):
            button = _mapping(step.get("boton")) if step_type(step) == "plantilla" else None
            if button is not None and button.get("cupon") not in old and "cupon" in button:
                emitter.add(COUPON_CHANGE_NOT_SUPPORTED, at + ("boton", "cupon"))
                return
        emitter.add(COUPON_CHANGE_NOT_SUPPORTED, ("pasos",))

    def _rule14_compiles(self, emitter: _Emitter, definition: Mapping[str, Any], flow: WorldFlow) -> None:
        """Con ``legacy_kind``, lo que se publica compila a la politica de su flujo del legado (T2.1).

        Solo se convierte lo que es de la forma (``flow_policy_not_compilable``) y la
        espera despues del ultimo envio. Lo demas que no compila (el horario, el vence,
        un tipo de paso) lo miran otras reglas.
        """
        if flow.legacy_kind is None:
            return
        try:
            compile_policy(definition, flow_id=flow.flow_id, version=1, legacy_kind=flow.legacy_kind,
                           fixed_zone=flow.fixed_zone)
        except PolicyCompileError as error:
            code = str(error)
            steps = definition.get("pasos")
            sends = [index for index, step in enumerate(steps) if step_type(step) == "plantilla"] if (
                isinstance(steps, list)) else []
            if code.startswith("flow_policy_not_compilable:"):
                detail = code.split(":")[1]
                spec = _COMPILE_DETAIL_SPECS.get(detail)
                if spec is None:
                    return
                index = error.send_index
                at: tuple[Segment, ...] = ("pasos", sends[index]) if index is not None and index < len(sends) else (
                    "pasos",)
                if spec is ENGINE_NOT_SUPPORTED_VARIABLES_DIFFER and len(at) == 2:
                    at = at + ("variables",)
                emitter.add(spec, at)
            elif code == "compile_policy_step_after_send":
                emitter.add(ENGINE_NOT_SUPPORTED_WAIT_AFTER_SEND, ("pasos", len(steps) - 1) if isinstance(
                    steps, list) and steps else ("pasos",))

    def _rule15_personal(self, emitter: _Emitter, definition: Mapping[str, Any]) -> None:
        """Mas de dos ``cupon: personal`` en un camino (contrato 1.1): en el tercero, o en ``pasos`` con ramas."""
        steps = definition.get("pasos")
        if _max_personal_coupons(steps) <= PERSONAL_COUPONS_PER_PATH:
            return
        if isinstance(steps, list) and not any(step_type(step) == "condicion" for step in steps):
            seen = 0
            for index, step in enumerate(steps):
                if _personal_coupon(step):
                    seen += 1
                    if seen > PERSONAL_COUPONS_PER_PATH:
                        emitter.add(COUPON_PERSONAL_LIMIT, ("pasos", index, "boton", "cupon"))
                        return
        emitter.add(COUPON_PERSONAL_LIMIT, ("pasos",))

    def _rule17(self, emitter: _Emitter, definition: Mapping[str, Any],
                categories: Mapping[tuple[Segment, ...], object]) -> None:
        """Con ``permiso: compra``, un solo envio por camino y solo plantillas de utilidad.

        En forma positiva: cada paso ``plantilla`` resuelto bloquea salvo que
        su categoria sea exactamente ``UTILITY``, tambien si falta, es nula,
        vacia o no es un texto. Uno que no se resolvio ya lo bloquea la 13 (o
        la 11, si es ``a_crear``).
        """
        if _get(definition, "entra", "permiso") != "compra":
            return
        steps = definition.get("pasos")
        if _max_sends(steps) > 1:
            emitter.add(PURCHASE_PERMISSION_REQUIRES_SINGLE_UTILITY_SEND, ("pasos",))
        for step, at, _, _, _, _ in _iter_steps(steps, ("pasos",)):
            kind = step_type(step)
            if kind == "conversar":
                emitter.add(PURCHASE_PERMISSION_REQUIRES_SINGLE_UTILITY_SEND, at + ("conversar",))
            elif kind == "plantilla":
                if at in categories and categories[at] != "UTILITY":
                    emitter.add(PURCHASE_PERMISSION_REQUIRES_SINGLE_UTILITY_SEND, at + ("plantilla",))

    def _client_ok(self, subject: _Subject, active: Mapping[str, Any] | None) -> None:
        """El OK del cliente cuando el permiso pasa a compra (contrato del ciclo, elecciones tecnicas)."""
        if _get(subject.definition, "entra", "permiso") != "compra":
            return
        if active is not None and _get(active, "entra", "permiso") == "compra":
            return
        if subject.item.ok_del_cliente is None:
            self.blocks.append(CLIENT_PURCHASE_OK_MISSING.at(
                finding_path((subject.index, "ok_del_cliente"))))

    # -- Las advertencias ----------------------------------------------------

    def _warnings(self, subject: _Subject, categories: Mapping[tuple[Segment, ...], object]) -> None:
        definition = subject.definition
        base = subject.emitter.base if subject.emitter.collapse is None else None
        collapse = subject.emitter.collapse
        warn = _Emitter(base or (), collapse=collapse)
        trigger = trigger_of(definition)
        schedule = definition.get("horario")
        reasons = _resolved_list(_get(definition, "termina", "si"))

        # En forma positiva: toda plantilla resuelta que no es exactamente UTILITY.
        if schedule == ALWAYS and any(category != "UTILITY" for category in categories.values()):
            warn.add(ALWAYS_WITH_MARKETING, ("horario",))
        caps = _mapping(definition.get("topes"))
        if trigger is not None and trigger.is_recovery and caps is not None and (
            caps.get("por_dia") == NO_CAP or caps.get("total") == NO_CAP
        ):
            warn.add(RECOVERY_WITHOUT_CAP, ("topes",))
        if trigger is not None and trigger.kind == "silencio" and reasons is not None and "compra" not in reasons:
            warn.add(SILENCE_WITHOUT_PURCHASE_EXIT, ("termina", "si"))
        if trigger is not None and self._same_trigger_unrelated(subject):
            warn.add(SAME_TRIGGER_UNRELATED, ("dispara",))
        if reasons is not None and "contesta" not in reasons and _wait_before_first_send(definition.get("pasos")):
            warn.add(WAIT_WITHOUT_CONTESTA, ("pasos",))
        if trigger is not None and trigger.kind == "silencio" and self._silence_too_short(definition):
            warn.add(SILENCE_WINDOW_SHORTER_THAN_CLOSED_GAP, ("dispara", "silencio"))
        if _refs(definition, "prioridad", "le_gana_a"):
            for step, at, _, _, _, _ in _iter_steps(definition.get("pasos"), ("pasos",)):
                if step_type(step) == "condicion" and any(
                    isinstance(step.get(name), list) and not step[name] for name in ("entonces", "si_no")
                ):
                    warn.add(EMPTY_BRANCH_WITH_LE_GANA_A, at)
        self.warnings.extend(warn.findings)

    def _same_trigger_unrelated(self, subject: _Subject) -> bool:
        definition = subject.definition
        flow = subject.flow
        mine = _trigger_signature(definition)
        if mine is None:
            return False
        me = (flow.product_ref, flow.flow_key)
        my_refs = _relations(definition, _own_product(definition, flow))
        for other in self.world.flows:
            if other.flow_id == flow.flow_id or other.product_ref != flow.product_ref:
                continue
            theirs = self.effective.get(other.flow_id)
            if theirs is None or _trigger_signature(theirs) != mine:
                continue
            their_refs = _relations(theirs, _own_product(theirs, other))
            if (other.product_ref, other.flow_key) not in my_refs and me not in their_refs:
                return True
        return False

    def _silence_too_short(self, definition: Mapping[str, Any]) -> bool:
        since = parse_duration(_get(definition, "dispara", "silencio", "desde"))
        until = parse_duration(_get(definition, "dispara", "silencio", "hasta"))
        schedule = definition.get("horario")
        if since is None or until is None or not isinstance(schedule, Mapping):
            return False
        opens = parse_hour(schedule.get("desde"))
        closes = parse_hour(schedule.get("hasta"))
        if opens is None or closes is None or opens >= closes:
            return False
        # La franja abierta cuenta el minuto de cierre, como el motor.
        closed = 24 * 60 - (closes - opens + 1)
        return until - since < closed


def _result_bytes(blocks: Sequence[Finding], warnings: Sequence[Finding]) -> int:
    """Los bytes del resultado como texto jsonb, los que mide la base."""
    size = json_size({"blocks": [finding.to_json() for finding in blocks],
                      "warnings": [finding.to_json() for finding in warnings]})
    # Los textos de un hallazgo son limpios y cortos: siempre es JSON.
    assert size is not None
    return size


def _trigger_identity(definition: Mapping[str, Any]) -> tuple[str, object] | None:
    """La identidad del disparador: la clase y el evento, o la clase y el ``lo_ultimo`` del silencio.

    Un disparador distinto es otro flujo (flow-definition-v1.md:46), y el
    cupon (lo_ultimo: agente) y la reactivacion (persona) son flujos
    distintos (:58-61 y :844-856). ``desde`` y ``hasta`` de un silencio no
    son la identidad: son su plazo (:855 y :161), y cambiarlos es una version
    nueva del mismo flujo. None si el disparador no tiene una forma que se
    pueda comparar (lo frenan la regla 1 o la 2).
    """
    trigger = _mapping(definition.get("dispara"))
    if trigger is None:
        return None
    present = [key for key in ("evento", "silencio") if key in trigger]
    if len(present) != 1:
        return None
    if present[0] == "evento":
        return ("evento", trigger["evento"])
    silence = _mapping(trigger["silencio"])
    return ("silencio", silence.get("lo_ultimo") if silence is not None else None)


def _trigger_signature(definition: Mapping[str, Any]) -> tuple[str, str] | None:
    """El disparador para comparar flujos: el evento, o el silencio con su ``lo_ultimo``."""
    trigger = trigger_of(definition)
    if trigger is None:
        return None
    if trigger.kind == "evento":
        return ("evento", trigger.event) if trigger.event is not None else None
    speaker = _get(definition, "dispara", "silencio", "lo_ultimo")
    if not isinstance(speaker, str) or is_unresolved(speaker):
        return None
    return ("silencio", speaker)


def _relations(definition: Mapping[str, Any], own_product: str) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for key in ("le_gana_a", "no_arranca_mientras"):
        for ref in _refs(definition, "prioridad", key):
            parsed = parse_flow_ref(ref, own_product) if not is_unresolved(ref) else None
            if parsed is not None:
                found.add(parsed)
    return found


def _is_url_button(button: object) -> bool:
    return isinstance(button, Mapping) and button.get("type") == "URL"


def _is_dynamic_url(url: object) -> bool:
    return isinstance(url, str) and url.startswith("https://") and url.endswith("{{1}}") and (
        _PLACEHOLDER.findall(url) == ["1"])


def _pointer_segments(pointer: str) -> tuple[str, ...]:
    if not pointer:
        return ()
    return tuple(
        part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]
    )


def finding_at(finding: Finding, path: str) -> Finding:
    """El mismo hallazgo con otra ruta (los del destino de un volver apuntan al item)."""
    return Finding(rule=finding.rule, code=finding.code, kind=finding.kind, path=path,
                   limit=finding.limit, reason=finding.reason, alternative=finding.alternative)


def validate(
    items: Sequence[ProposalItem],
    *,
    actor: str,
    world: FlowWorld,
    instance: InstanceView,
    catalog: TemplateCatalog | None,
    phase: str,
    draft_base: Mapping[str, Any] | None = None,
) -> ValidationResult:
    """Valida los items de una propuesta.

    * ``actor``: ``automation_expert`` u ``operador``, la clase de quien
      propone. De ella dependen ``flow_creation_not_enabled`` y
      ``change_outside_1b_scope``.
    * ``world``: todos los flujos de la instancia, con sus versiones.
    * ``catalog``: el catalogo de la bandeja; ``None`` si no se pudo leer.
    * ``phase``: ``draft`` (reglas 1 a 10 y el ciclo) o ``publish`` (todas,
      mas las advertencias).
    * ``draft_base``: la version que leyo quien propone,
      ``{flow_id: {active_version}}``; sin ella no se mira
      ``flow_change_stale_base``.

    Un item cuyo flujo no esta en el mundo es ``FlowValidationInputError``
    (``flow_not_found``); items con la forma minima rota,
    ``ProposalItemsInvalid``.
    """
    if actor not in ACTORS:
        raise ValueError("validator_actor_invalid")
    if phase not in PHASES:
        raise ValueError("validator_phase_invalid")
    if not isinstance(world, FlowWorld) or not isinstance(instance, InstanceView):
        raise ValueError("validator_context_invalid")
    if catalog is not None and not isinstance(catalog, TemplateCatalog):
        raise ValueError("validator_catalog_invalid")
    items = tuple(items)
    if not 1 <= len(items) <= 10 or not all(isinstance(item, ProposalItem) for item in items):
        raise ProposalItemsInvalid("items_count_invalid")
    check_item_classes(items)
    return _Run(items, actor=actor, world=world, instance=instance, catalog=catalog, phase=phase,
                draft_base=draft_base).run()
