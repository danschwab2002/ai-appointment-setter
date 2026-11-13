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
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .flow_definition import (
    ALWAYS,
    ANY_TEMPLATE,
    COHORT,
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
    InstanceRuntime,
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
    CONDITION_CHAIN_MAX_LEVELS,
    ITEMS_MAX_BYTES,
    ROUTE_MOTOR,
    ROUTE_NOT_READ,
)
from .flow_engine import (
    NATIVE_MULTITOUCH_TRIGGERS,
    NATIVE_SILENCE_TRIGGER,
    NATIVE_UNSUPPORTED_PERMISSION,
    NATIVE_UNSUPPORTED_PRIORITY,
    NATIVE_UNSUPPORTED_REPEAT,
    NATIVE_UNSUPPORTED_TRIGGER,
    NOT_SUPPORTED,
    TOO_MANY_SENDS,
    native_not_supported,
    native_trigger_key,
)
from .flow_policy import PolicyCompileError, compile_policy
# Las claves de los flujos del legado (E-207, D1 del mapa de M16): la lectura del AE descarta todos los
# nativos si uno usa una (automation_expert_read.build_native_flows). Esa lectura no importa nada del bridge.
from .automation_expert_read import FLOW_KEYS as LEGACY_FLOW_KEYS

__all__ = [
    "ACTORS", "BLOCK_CODES", "CATALOG", "CHECKOUT_BUTTON_URL", "PHASES", "RULES_VERSION",
    "VALIDATOR_CONTRACT", "WARNING_CODES", "FlowValidationInputError", "ValidationResult",
    "Finding", "FlowWorld", "InstanceView", "ProposalItem", "ProposalItemsInvalid",
    "TemplateCatalog", "WorldFlow", "loads_strict", "parse_items", "structural_changes", "validate",
]

# Lo que la base exige en la revalidacion (``_flow_change_validator_is_valid``):
# el contrato del esquema y la version de las reglas. Sube cuando las reglas
# cambian de forma incompatible, y una migracion sube la minima de la base.
VALIDATOR_CONTRACT = SCHEMA
RULES_VERSION = 1

ACTORS = ("automation_expert", "operador")
PHASES = ("draft", "publish")
# El disparador del pago rechazado: el unico con sus datos de condicion y la cadena (1.4, E-306).
PAYMENT_FAILURE_EVENT = "pago_fallido"
# El boton del seguimiento con cupon: la URL dinamica sobre el checkout de
# Hotmart (followup_discount.FOLLOWUP_BUTTON_URL; una prueba fija que son la
# misma). Se escribe aca para no importar el modulo del barrido.
CHECKOUT_BUTTON_URL = "https://pay.hotmart.com/{{1}}"
# La respuesta rapida de la baja que el seguimiento nativo acepta despues del checkout
# (followup_discount.FOLLOWUP_OPT_OUT_BUTTON, E-272; una prueba fija que son el mismo texto).
FOLLOWUP_OPT_OUT_BUTTON = "No más mensajes"
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
# Los datos del pago rechazado (1.4, E-306): como medio_de_pago con la ficha, solo con su disparador.
TIPO_DE_RECHAZO_REQUIRES_PAYMENT_FAILURE_TRIGGER = _spec(
    "tipo_de_rechazo_requires_payment_failure_trigger", 5, KIND_FORM,
    "tipo_de_rechazo, como dato de una condición, va solo con el disparador pago_fallido.",
)
RECHAZOS_REQUIRES_PAYMENT_FAILURE_TRIGGER = _spec(
    "rechazos_requires_payment_failure_trigger", 5, KIND_FORM,
    "rechazos, como dato de una condición, va solo con el disparador pago_fallido.",
)
DESCUENTO_PREVIO_REQUIRES_PAYMENT_FAILURE_TRIGGER = _spec(
    "descuento_previo_requires_payment_failure_trigger", 5, KIND_FORM,
    "descuento_previo, como dato de una condición, va solo con el disparador pago_fallido.",
)
PAYMENT_FAILURE_DATUM_SPECS = {
    "tipo_de_rechazo": TIPO_DE_RECHAZO_REQUIRES_PAYMENT_FAILURE_TRIGGER,
    "rechazos": RECHAZOS_REQUIRES_PAYMENT_FAILURE_TRIGGER,
    "descuento_previo": DESCUENTO_PREVIO_REQUIRES_PAYMENT_FAILURE_TRIGGER,
}
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
    "Una condición no puede llevar otra adentro. Con el disparador pago_fallido, sí como único paso del "
    "si_no de otra, hasta tres niveles (la cadena del pago rechazado).",
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
    "El Automation Expert no escribe la primera versión de un flujo registrado sin versión: ese es el "
    "camino de los flujos del legado, que registra el operador.",
    alternative="Para un flujo nuevo, usar un ítem nuevo: nace en el motor y se publica para la cohorte.",
)
# El item nuevo (ciclo 1.7.0, flow-engine-v2 §7).
FLOW_ALREADY_REGISTERED = _spec(
    "flow_already_registered", None, KIND_CYCLE,
    "Ya hay un flujo con esa clave en ese producto.",
    alternative="Elegir otra clave, o cambiar el flujo que ya existe con una versión nueva.",
)
# E-207 (D1 del mapa de M16): un flujo nativo no usa la clave de un flujo del legado. La base lo frena
# igual al publicar (_flow_change_new_flow_blocker, 20261030000200).
FLOW_KEY_RESERVED_FOR_LEGACY = _spec(
    "flow_key_reserved_for_legacy", None, KIND_CYCLE,
    "Esa clave es de un flujo del legado (precheckout, carrito, pago_fallido, reactivacion o descuento): "
    "un flujo nativo con esa clave deja al Automation Expert sin ver ningún flujo nativo.",
    alternative="Elegir una clave nativa propia, por ejemplo carrito_nativo o pago_rechazado.",
)
# Ciclo 1.8.0: volver a la cohorte.
PRENDER_A_COHORT_NEEDS_PAUSED_NATIVE = _spec(
    "prender_a_cohorte_needs_paused_native", None, KIND_CYCLE,
    "Volver a la cohorte de prueba (prender con a: cohorte) solo sirve para un flujo nativo pausado.",
    alternative="Para un flujo del legado, prender sin a; para uno en cohorte o prendido, pausarlo primero.",
)
NEW_FLOW_MUST_START_IN_COHORT = _spec(
    "new_flow_must_start_in_cohort", None, KIND_CYCLE,
    "Un flujo nuevo se publica primero para la cohorte de prueba; pasarlo a todos es otra propuesta.",
    alternative="Usar prender: cohorte.",
)
# El paso etiquetar (esquema 1.2): en la 5 y en la 7, como el resto de sus reglas.
ETIQUETAR_REQUIRES_PURCHASE_TRIGGER = _spec(
    "etiquetar_requires_purchase_trigger", 5, KIND_PROTECTION,
    "etiquetar marca a la persona como clienta: va solo en un flujo disparado por la compra.",
    limit="etiquetar, solo con el disparador compra",
    alternative="Sacar el paso, o disparar el flujo con la compra.",
)
ETIQUETAR_NOT_AFTER_TEMPLATE = _spec(
    "etiquetar_not_after_template", 7, KIND_FORM,
    "etiquetar va después de una plantilla, y hay uno solo por camino.",
    alternative="Ponerlo después del envío, una sola vez.",
)
# La etiqueta de un segmento (E-316, PR-C, segment-labels-v1). La pone el motor antes del primer envio,
# para que la conversacion ya la tenga cuando la persona contesta: con cualquier disparador (la 5 de arriba
# es solo de cliente), arriba de todo y antes de la plantilla (7), y dentro de la vigencia del segmento (5).
# Van en la 5 y la 7 porque la base acepta reglas de 1 a 17.
ETIQUETAR_SEGMENT_NOT_BEFORE_TEMPLATE = _spec(
    "etiquetar_segment_not_before_template", 7, KIND_FORM,
    "La etiqueta de un segmento la pone el motor antes del primer envío: va arriba de todo, antes de la "
    "plantilla y fuera de las condiciones, y hay una sola etiqueta por camino.",
    alternative="Poner etiquetar con el segmento como primer paso, o antes de la plantilla, una sola vez.",
)
SEGMENT_LABEL_OUTSIDE_WINDOW = _spec(
    "segment_label_outside_window", 5, KIND_PROTECTION,
    "Ese segmento ya venció: el motor no le pone su etiqueta a nadie, y el agente ya no usa su conocimiento.",
    limit="etiquetar con un segmento, solo mientras el segmento no venció",
    alternative="Sacar el paso, o cargar una versión nueva del segmento con otra vigencia.",
)
# La regla 14 de un flujo nativo (flow-engine-v2 §9): lo que la base no abre ni admite.
_NATIVE_LIMIT = ("Un flujo nativo, hoy: lo dispara la compra, el carrito, el pago rechazado o el formulario, "
                 "y manda una plantilla sin botón declarado; o lo dispara el silencio con lo último del agente "
                 "(permiso: conversacion, contesto_a: cualquier_plantilla), y manda una plantilla con el checkout "
                 "y su cupón")
ENGINE_NOT_SUPPORTED_NATIVE_TRIGGER = _spec(
    NATIVE_UNSUPPORTED_TRIGGER, 14, KIND_NOT_YET,
    "El motor nativo de esta versión solo arranca con la compra, con el carrito, con el pago rechazado, con "
    "el formulario o con el silencio en que lo último lo dijo el agente.",
    limit=_NATIVE_LIMIT,
    alternative=("Disparar con dispara: {evento: compra}, {evento: carrito}, {evento: pago_fallido}, "
                 "{evento: intencion} o {silencio: {lo_ultimo: agente, ...}}. Otro disparador es de un flujo "
                 "del legado."),
)
ENGINE_NOT_SUPPORTED_NATIVE_PERMISSION = _spec(
    NATIVE_UNSUPPORTED_PERMISSION, 14, KIND_NOT_YET,
    "El motor nativo de esta versión entra con permiso: compra si lo dispara la compra, con permiso: "
    "formulario si lo dispara el carrito, el pago rechazado o el formulario, y con permiso: conversacion si "
    "lo dispara el silencio.", limit=_NATIVE_LIMIT,
    alternative=("Con la compra, permiso: compra y el OK del cliente; con el carrito, el pago rechazado o el "
                 "formulario, permiso: formulario; con el silencio, permiso: conversacion."),
)
ENGINE_NOT_SUPPORTED_NATIVE_REPEAT = _spec(
    NATIVE_UNSUPPORTED_REPEAT, 14, KIND_NOT_YET,
    "El motor nativo repite por persona o por persona y oferta, con nunca o una duración; con el silencio, "
    "una vez por conversación y nunca otra.", limit=_NATIVE_LIMIT,
    alternative=("Usar repeticion: {una_por: persona, otra_despues_de: nunca}; con el silencio, "
                 "{una_por: conversacion, otra_despues_de: nunca}."),
)
ENGINE_NOT_SUPPORTED_NATIVE_PRIORITY = _spec(
    NATIVE_UNSUPPORTED_PRIORITY, 14, KIND_NOT_YET,
    "no_arranca_mientras es una lista de flujos.", limit=_NATIVE_LIMIT,
    alternative="Escribir no_arranca_mientras como una lista, vacía si no hay ninguno.",
)
ENGINE_NOT_SUPPORTED_NATIVE_SHAPE = _spec(
    "motor_nativo:forma", 14, KIND_NOT_YET,
    "El motor nativo de esta versión no ejecuta esa forma de pasos.", limit=_NATIVE_LIMIT,
    alternative=("Dejar una sola plantilla con variables de entre nombre y producto, sin botón o con botones "
                 "de URL fija, y etiquetar después si hace falta."),
)
ENGINE_NOT_SUPPORTED_ETIQUETAR = _spec(
    "engine_not_supported:etiquetar", 14, KIND_NOT_YET,
    "etiquetar lo ejecuta solo el motor nativo: un flujo del legado no lo hace.",
    limit="etiquetar, solo en un flujo nativo",
    alternative="Sacar el paso, o crear el flujo con un ítem nuevo.",
)
_NATIVE_SPECS: Mapping[str, CodeSpec] = {
    spec.code: spec for spec in (
        ENGINE_NOT_SUPPORTED_NATIVE_TRIGGER, ENGINE_NOT_SUPPORTED_NATIVE_PERMISSION,
        ENGINE_NOT_SUPPORTED_NATIVE_REPEAT, ENGINE_NOT_SUPPORTED_NATIVE_PRIORITY,
    )
}
_NATIVE_PATHS: Mapping[str, tuple[Segment, ...]] = {
    NATIVE_UNSUPPORTED_TRIGGER: ("dispara",),
    NATIVE_UNSUPPORTED_PERMISSION: ("entra", "permiso"),
    NATIVE_UNSUPPORTED_REPEAT: ("topes", "repeticion"),
    NATIVE_UNSUPPORTED_PRIORITY: ("prioridad", "no_arranca_mientras"),
}
CHANGE_OUTSIDE_1B_SCOPE = _spec(
    "change_outside_1b_scope", None, KIND_CYCLE,
    "En la 1b el Automation Expert cambia las esperas y el plazo, la plantilla, los topes por día y total, el horario, y prende, pausa o apaga.",
    alternative="Pedírselo a un operador.",
)
# Lo que el AE no cambia de la estructura de un flujo nativo (E-357, P28 del 30/10): lo que el motor nativo no
# ejecuta, y por eso se publicaria sin cambiar nada. Son bloqueos: una version «estructural» que no cambia ningun
# envio es lo que E-353 ya frena. El motivo y la alternativa son lo que el AE le dice al dueno: en lenguaje de
# negocio, sin campos ni la palabra operador, porque el pedido es que el dueno no dependa de un operador.
_DROP_THAT_CHANGE = "Sacá ese cambio de la propuesta (lo demás sigue) y decile al dueño, con este motivo, que hoy no se puede. "
NATIVE_CHANGE_EXITS = _spec(
    "native_change_exits", None, KIND_CYCLE,
    "Este flujo siempre corta cuando la persona compra, y por ahora esa es su única salida: que conteste, por ejemplo, no lo corta.",
    alternative=_DROP_THAT_CHANGE + "Lo que sí se puede cambiar es cuánto tiempo sigue vigente el mensaje y cuánto se espera antes de mandarlo.",
)
NATIVE_CHANGE_REPEAT_SILENCE = _spec(
    "native_change_repeat_silence", None, KIND_CYCLE,
    "El seguimiento le llega una sola vez a cada conversación, y eso no se puede cambiar.",
    alternative=_DROP_THAT_CHANGE + "Lo que sí se puede cambiar es a qué altura del silencio sale, entre las 24 y las 72 horas, y su mensaje.",
)
NATIVE_CHANGE_PRIORITY_SILENCE = _spec(
    "native_change_priority_silence", None, KIND_CYCLE,
    "El seguimiento no mira si la persona está en otro flujo: sale igual.",
    alternative=_DROP_THAT_CHANGE + "Para que no salga, se puede pausar o apagar.",
)
NATIVE_CHANGE_PRIORITY_WINS = _spec(
    "native_change_priority_wins", None, KIND_CYCLE,
    "Hoy un flujo no le puede ganar a otro. Lo que sí se puede decir es que este flujo no arranque mientras la persona está en otro.",
    alternative="Cambiá el pedido a «que no arranque mientras está en ese otro flujo», en el flujo que tiene que esperar, y confirmalo con el dueño.",
)
# E-483 (C7, migracion 20261112210200): con FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED la admision nativa corta los
# recorridos en curso de la misma persona de los flujos de le_gana_a. Solo nombra flujos del mismo producto que
# corren en el sistema nuevo: el carrito de hoy (el del legado, tambien con la ruta motor) no se toca.
NATIVE_CHANGE_PRIORITY_WINS_REF = _spec(
    "native_change_priority_wins_ref", None, KIND_CYCLE,
    "Un flujo solo puede cortar a otro del mismo producto que corra en el sistema nuevo: a este no lo cortaría.",
    alternative=_DROP_THAT_CHANGE + "Si el dueño quiere cortar un flujo que corre en el sistema anterior, como el "
                "carrito de hoy, el equipo técnico tiene que pasarlo antes al sistema nuevo.",
)
# Solo cortan los flujos que arrancan con la admision de un evento de Hotmart (admit_flow_engine_event_v1).
NATIVE_PRIORITY_WINS_TRIGGERS = frozenset({"compra", "carrito", "pago_fallido"})
_NATIVE_PRIORITY_WINS_TRIGGER = ("Solo el carrito, el pago rechazado y la bienvenida pueden cortar a otro flujo: "
                                 "este no lo cortaría.")
NATIVE_CHANGE_PRIORITY_REF = _spec(
    "native_change_priority_ref", None, KIND_CYCLE,
    "Un flujo solo puede esperar a otro del mismo producto y que corra en el sistema nuevo: con este no cambiaría nada.",
    alternative=_DROP_THAT_CHANGE + "Si el dueño quiere esperar a un flujo que corre en el sistema anterior, el equipo técnico tiene que pasarlo antes al sistema nuevo.",
)
# El seguimiento corre en el sistema nuevo, pero nadie queda «adentro»: el texto general diria lo contrario.
_NATIVE_PRIORITY_REF_SILENCE = "El seguimiento manda su mensaje sin dejar a la persona adentro de un flujo, así que no se lo puede esperar: con este no cambiaría nada."
NATIVE_CHANGE_ENTRY = _spec(
    "native_change_entry", None, KIND_CYCLE,
    "Quién entra a este flujo lo fija lo que lo dispara: no se puede cambiar por separado.",
    alternative=_DROP_THAT_CHANGE + "Si el dueño quiere otra gente, es otro flujo, con otro disparador.",
)
NATIVE_CHANGE_POOL = _spec(
    "native_change_pool", None, KIND_CYCLE,
    "El cupo de mensajes de un flujo es siempre el mismo: si se cambiara, la cuenta de lo enviado volvería a cero.",
    alternative=_DROP_THAT_CHANGE + "Para mandar más o menos, se cambia el tope por día o el total.",
)
NATIVE_CHANGE_OTHER = _spec(
    "native_change_other", None, KIND_CYCLE,
    "Eso, en este flujo, hoy no se puede cambiar.",
    alternative=_DROP_THAT_CHANGE + "Sí se pueden cambiar las esperas, el mensaje, el plazo, los topes, el horario y, en casi todos, cada cuánto se repite.",
)
# La salida de un flujo nativo, dicha por lo que lo dispara (native_trigger_key).
_NATIVE_EXITS_TEXT: Mapping[str, str] = MappingProxyType({
    "compra": "La bienvenida no tiene salidas: manda su mensaje y termina.",
    NATIVE_SILENCE_TRIGGER: "El seguimiento siempre corta si la persona compra o contesta, y esas son sus únicas salidas.",
    "intencion": ("El primer contacto corta cuando la persona compra, abandona el carrito, se le rechaza el pago, "
                  "se da de baja o la pasan a una persona, y esas son sus únicas salidas: no se pueden sumar otras."),
})
def _exits_of(definition: Mapping[str, Any] | None) -> frozenset[str] | None:
    ends = definition.get("termina") if isinstance(definition, Mapping) else None
    exits = ends.get("si") if isinstance(ends, Mapping) else None
    if not isinstance(exits, list) or not all(isinstance(item, str) for item in exits):
        return None
    return frozenset(exits)


def _only_reply_exit_changed(active: Mapping[str, Any] | None, proposed: Mapping[str, Any]) -> bool:
    """Si de las salidas solo cambia ``contesta`` (E-483): es la unica que el nativo de varios toques suma."""
    before, after = _exits_of(active), _exits_of(proposed)
    return before is not None and after is not None and before - {"contesta"} == after - {"contesta"}


def _native_shape_for_the_owner(what: str) -> tuple[str, str]:
    """El motivo y la alternativa de una forma que el motor nativo no ejecuta, para el AE (E-357).

    ``what`` es el codigo cerrado de ``native_not_supported``; el dueno nunca lo lee. Un codigo sin texto cae en
    el general, que tampoco lo muestra.
    """
    drop = "Sacá eso de la propuesta y decile al dueño que hoy no se puede."
    fix = "Corregilo en la propuesta."
    if what == TOO_MANY_SENDS or what == "conversar":
        return ("El carrito, el primer contacto y el pago rechazado mandan hasta tres mensajes por persona; la "
                "bienvenida y el seguimiento, uno solo.",
                drop + " Si quiere más mensajes de los que ese flujo manda, decile que esa capacidad todavía no está.")
    # E-483: lo que exige un flujo nativo de varios mensajes (20261112210000).
    if what == "varios_envios_sin_contesta":
        return ("Con más de un mensaje, el flujo corta cuando la persona contesta: si contestó, no le llega el siguiente.",
                "Sumá la salida de que conteste. " + fix)
    if what == "boton_despues_del_primer_envio":
        return ("Solo el primer mensaje puede llevar el botón de pago; los siguientes, todavía no.", fix)
    if what == "condicion_despues_de_un_envio":
        # E-483 (C3, 20261112210300): la variante se elige una sola vez, antes del primer mensaje; cada una
        # puede seguir con sus mensajes.
        return ("El mensaje se elige según el caso una sola vez, antes del primero: después de mandar uno no se "
                "puede volver a elegir. Lo que sí se puede es que cada variante del pago rechazado siga con sus "
                "propios mensajes.",
                "Poné la elección al principio y, adentro de cada variante, su mensaje y los que siguen. " + fix)
    if what == "espera_entre_envios":
        return ("Entre un mensaje y el siguiente tiene que haber una espera de al menos un minuto.",
                "Poné una espera entre los dos mensajes. " + fix)
    if what == "espera_despues_del_envio":
        return ("Una espera después del último mensaje no hace nada: el flujo termina cuando lo manda.",
                "Sacá la espera del final o poné un mensaje después. " + fix)
    if what == "condicion":
        return ("Solo el pago rechazado elige el mensaje según el caso: este flujo manda siempre el mismo.", drop)
    if what == "condicion_dato":
        return ("Las variantes del pago rechazado solo pueden mirar el tipo de rechazo, cuántas veces se rechazó o "
                "si tuvo descuento antes.", fix)
    if what in ("condicion_no_al_final", "rama_de_un_envio"):
        return ("Las variantes van al final del flujo: cada una empieza con su mensaje y puede seguir con hasta dos "
                "más, con una espera antes de cada uno.", fix)
    if what == "ramas_con_otras_esperas":
        return ("Todas las variantes del pago rechazado tienen que esperar lo mismo entre un mensaje y el "
                "siguiente (por ejemplo, 24 horas en todas).", "Igualá las esperas de las variantes. " + fix)
    if what == "etiquetar":
        return ("El seguimiento solo manda su mensaje: no pone etiquetas.", drop)
    if what == "boton_sin_cupon":
        return ("El seguimiento siempre lleva el botón de pago con su cupón.", fix)
    if what == "cupon_personal":
        # E-483 (C2, 20261112210500): el cupon propio va en el carrito y en el pago rechazado sin variantes.
        return ("El cupón distinto para cada persona solo va en el carrito y en el pago rechazado sin variantes, "
                "en el botón de pago: en este flujo, o en las variantes del pago rechazado, todavía no.",
                drop + " Un mismo cupón para todos sí se puede, si ya estaba en el flujo.")
    if what == "cupones_personales_de_mas":
        return ("Cada persona recibe como mucho dos cupones propios en un flujo: el primero y una renovación.",
                "Dejá el cupón propio en dos mensajes como mucho. " + fix)
    if what == "boton_sin_cupon_personal":
        return ("En el carrito, el botón de pago va solo con el cupón propio de cada persona.",
                "Poné el cupón propio en ese botón o sacá el botón. " + fix)
    if what.startswith("boton_"):
        return ("Este flujo no lleva un botón propio: usa los botones que ya trae la plantilla aprobada.", drop)
    if what == "variables_seguimiento":
        return ("El seguimiento siempre usa el nombre de la persona y el producto, en ese orden.", fix)
    if what.startswith("variable") or what == "variables_differ":
        # E-483 (C2): con el cupon propio, tambien el precio y el vencimiento, y cuando en el primero.
        return ("Este mensaje solo puede usar el nombre de la persona y el producto; con el cupón propio, también "
                "el precio y hasta cuándo vale, y en el primer mensaje cuándo dejó la compra.", fix)
    if what == "contesto_a":
        return ("El seguimiento solo le escribe a quien ya contestó alguna plantilla: eso no se cambia.", fix)
    if what == "silencio_ventana":
        return ("El seguimiento solo corre entre las 24 y las 72 horas de silencio.", fix)
    if what == "salida_compra_y_contesta":
        # Aca falta una salida obligatoria; el texto de abajo es el de sumar una que no existe.
        return ("El seguimiento siempre corta si la persona compra o contesta: el flujo tiene que tener esas dos salidas.",
                "Poné las dos salidas, que compre y que conteste. " + fix)
    if what.startswith("salida_") or what.startswith("contesta_"):
        return (NATIVE_CHANGE_EXITS.reason, NATIVE_CHANGE_EXITS.alternative or drop)
    return ("Eso, en este flujo, hoy no se puede hacer.", drop)


# Un cambio que se publicaria sin cambiar ningun envio (E-353). Son bloqueos y no advertencias: una
# advertencia el modelo la puede pasar por alto. El motivo y la alternativa son lo que el AE le dice al
# dueno: no se arregla tocando la propuesta, y por eso la alternativa lo dice (crear-un-flujo.md le pide
# corregir solo lo que vuelve frenado). Solo para el AE: el operador puede preparar un flujo antes de
# prender lo que falta.
CHANGE_WITHOUT_EFFECT_LEGACY_ROUTE = _spec(
    "change_without_effect_legacy_route", None, KIND_CYCLE,
    "Este flujo todavía lo manda el sistema anterior: una versión nueva quedaría guardada, pero los mensajes que salen no cambiarían.",
    alternative="No se arregla cambiando la propuesta. Decile al dueño que, para cambiar estos mensajes, el equipo técnico tiene que pasar antes este flujo al sistema nuevo de mensajes.",
)
NATIVE_ENGINE_NOT_RUNNING = _spec(
    "native_engine_not_running", None, KIND_CYCLE,
    "En esta cuenta el sistema nuevo de mensajes está apagado: este flujo se publicaría y no mandaría ningún mensaje.",
    alternative="No se arregla cambiando la propuesta. Decile al dueño que el equipo técnico tiene que prender el sistema nuevo de mensajes en su cuenta antes de publicarlo.",
)
NATIVE_FIRST_CONTACT_NOT_RUNNING = _spec(
    "native_first_contact_not_running", None, KIND_CYCLE,
    "Esta cuenta todavía no tiene prendido el primer contacto nuevo: los formularios seguirían saliendo como hoy y este flujo no mandaría ningún mensaje.",
    alternative="No se arregla cambiando la propuesta. Decile al dueño que el equipo técnico tiene que prender el primer contacto nuevo en su cuenta antes de publicarlo.",
)
NATIVE_SILENCE_NOT_RUNNING = _spec(
    "native_silence_not_running", None, KIND_CYCLE,
    "Esta cuenta no tiene en marcha el seguimiento nuevo por silencio: si se publica, el seguimiento de hoy dejaría de salir y este no lo reemplazaría.",
    alternative="No se arregla cambiando la propuesta. Decile al dueño que el equipo técnico tiene que poner en marcha el seguimiento nuevo en su cuenta antes de publicarlo.",
)
NATIVE_TRIGGER_OTHER_PRODUCT = _spec(
    "native_trigger_other_product", None, KIND_CYCLE,
    "El seguimiento nuevo por silencio corre solo en el producto principal de la cuenta: en este producto no mandaría ningún mensaje.",
    alternative="No se arregla cambiando los mensajes. Decile al dueño que hoy esto solo se puede publicar en el producto principal.",
)
@dataclass(frozen=True)
class NativeRuntime(InstanceRuntime):
    """``InstanceRuntime`` con los toques siguientes de un nativo de varios (E-483).

    ``native_multitouch`` es ``FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED`` del bridge. Va aca y no como campo de
    ``InstanceRuntime`` para no tocar ``flow_definition.py`` (lo reserva E-484); al integrar las dos ramas
    se puede subir. Un ``InstanceRuntime`` a secas es «apagado», que es lo que corre si nadie lo prendio.
    """

    native_multitouch: bool = False
    # E-483 (C2): FLOW_ENGINE_NATIVE_PERSONAL_COUPON_ENABLED, el toque nativo con el cupon propio.
    native_personal_coupon: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.native_multitouch, bool) or not isinstance(self.native_personal_coupon, bool):
            raise ValueError("instance_runtime_invalid")

    def to_json(self) -> dict[str, Any]:
        return {**super().to_json(), "native_multitouch_enabled": self.native_multitouch,
                "native_personal_coupon_enabled": self.native_personal_coupon}


def runtime_runs_multitouch(runtime: InstanceRuntime | None) -> bool:
    """Si el bridge manda los toques siguientes de un nativo (E-483); un runtime sin el dato, no."""
    return isinstance(runtime, NativeRuntime) and runtime.native_multitouch


def runtime_runs_personal_coupon(runtime: InstanceRuntime | None) -> bool:
    """Si el bridge arma el toque nativo con el cupon propio (E-483, C2); un runtime sin el dato, no."""
    return isinstance(runtime, NativeRuntime) and runtime.native_personal_coupon


def _personal_coupon_sends(steps: object) -> int:
    """Los envios con el boton del cupon propio (``{link: checkout, cupon: personal}``), ramas incluidas."""
    if not isinstance(steps, list):
        return 0
    total = 0
    for step in steps:
        if step_type(step) == "plantilla" and isinstance(step, Mapping):
            button = step.get("boton")
            total += 1 if isinstance(button, Mapping) and is_personal_coupon(button.get("cupon")) else 0
        elif step_type(step) == "condicion" and isinstance(step, Mapping):
            total += sum(_personal_coupon_sends(step.get(name)) for name in ("entonces", "si_no"))
    return total


# E-483: un nativo de varios mensajes con FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED apagado. La base lo abre y
# lo publica, pero el bridge cierra el segundo y el tercero sin mandarlos
# (flow_engine_native_multitouch_disabled): sale solo el primero. Bloqueo para el AE, como los otros
# cinco; al operador se le avisa (puede preparar el flujo antes de que se prenda).
NATIVE_MULTITOUCH_NOT_RUNNING = _spec(
    "native_multitouch_not_running", None, KIND_CYCLE,
    "Esta cuenta todavía no manda más de un mensaje por flujo en el sistema nuevo: si se publica, le llegaría "
    "solo el primero y los siguientes no saldrían.",
    alternative="No se arregla cambiando la propuesta, salvo dejando un solo mensaje. Decile al dueño que el "
                "equipo técnico tiene que prender los mensajes siguientes en su cuenta antes de publicar uno de "
                "varios.",
)
NATIVE_MULTITOUCH_NOT_RUNNING_WARNING = _spec(
    "native_multitouch_not_running_warning", None, KIND_WARNING,
    "Esta cuenta todavía no manda más de un mensaje por flujo en el sistema nuevo: publicado así, sale solo el "
    "primero hasta que se prenda FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED.",
)
# E-483 (C2, 20261112210500): un nativo con el cupon propio con FLOW_ENGINE_NATIVE_PERSONAL_COUPON_ENABLED
# apagado. La base lo publica y el trabajador del cupon crearia el codigo en Hotmart, pero el bridge cierra el
# toque sin mandarlo (flow_engine_native_personal_coupon_disabled). Bloqueo para el AE; al operador se le avisa.
NATIVE_PERSONAL_COUPON_NOT_RUNNING = _spec(
    "native_personal_coupon_not_running", None, KIND_CYCLE,
    "Esta cuenta todavía no manda el cupón propio de cada persona en el sistema nuevo: si se publica, los mensajes "
    "con ese cupón no saldrían.",
    alternative="No se arregla cambiando la propuesta, salvo sacando el cupón propio de los mensajes. Decile al "
                "dueño que el equipo técnico tiene que prender el cupón propio en su cuenta antes de publicarlo.",
)
NATIVE_PERSONAL_COUPON_NOT_RUNNING_WARNING = _spec(
    "native_personal_coupon_not_running_warning", None, KIND_WARNING,
    "Esta cuenta todavía no manda el cupón propio de cada persona en el sistema nuevo: publicado así, los mensajes "
    "con ese cupón no salen hasta que se prenda FLOW_ENGINE_NATIVE_PERSONAL_COUPON_ENABLED.",
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
# E-483 (C8): cancelar lo que espera se aplica solo en un flujo nativo (la base lo hace al publicar,
# 20261112210100). En el legado (con o sin la ruta motor) lo que esta en curso sigue con su version, y migrar
# no existe en ninguno: los dos siguen con este codigo.
EN_CURSO_NOT_SUPPORTED = _spec(
    "en_curso_not_supported", None, KIND_CYCLE,
    "Cortar a la gente que está a mitad de los mensajes solo se puede en un flujo del sistema nuevo de mensajes. "
    "En este flujo, lo que ya empezó sigue con la versión que lo empezó; pasarlo a la versión nueva todavía no "
    "existe en ningún flujo.",
    alternative="Usar en_curso: seguir. Si el dueño necesita cortar a quienes esperan en este flujo, el equipo "
                "técnico tiene que pasarlo antes al sistema nuevo de mensajes.",
)
# Lo que esta en curso: seguir en cualquier flujo, cancelar solo en uno nativo (E-483, C8).
IN_FLIGHT_CANCEL = "cancelar"
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
# Dos flujos nativos con el mismo evento (E-139). El motor abre un solo caso por
# evento (recovery_cases.abandonment_event_id es unico) y admite los flujos en
# orden de flow_id (admit_flow_engine_event_v1, 20261024000200): gana el de
# flow_id menor, que para un flujo nuevo es un UUID al azar, y el otro queda con
# evento_con_otro_flujo. No mira prioridad para el mismo evento.
# Decision de Dan del 2026-10-27: es bloqueo tambien en la cohorte, porque ahi la
# persona igual recibe uno solo, elegido al azar.
SAME_EVENT_NATIVE_FLOWS = _spec(
    "same_event_native_flows", None, KIND_CYCLE,
    "Otro flujo nativo del mismo producto, prendido o en la cohorte, arranca con el mismo evento. El "
    "motor abre un solo caso por evento y elige el flujo por su identificador, no por prioridad: a quien "
    "le toquen los dos le llega uno solo, al azar, y el otro no arranca.",
    alternative="Pausar o apagar el otro flujo en la misma propuesta, o disparar este con otro evento.",
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
    TIPO_DE_RECHAZO_REQUIRES_PAYMENT_FAILURE_TRIGGER, RECHAZOS_REQUIRES_PAYMENT_FAILURE_TRIGGER,
    DESCUENTO_PREVIO_REQUIRES_PAYMENT_FAILURE_TRIGGER,
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
    FLOW_ALREADY_REGISTERED, FLOW_KEY_RESERVED_FOR_LEGACY, NEW_FLOW_MUST_START_IN_COHORT,
    PRENDER_A_COHORT_NEEDS_PAUSED_NATIVE,
    ETIQUETAR_REQUIRES_PURCHASE_TRIGGER,
    ETIQUETAR_NOT_AFTER_TEMPLATE, ETIQUETAR_SEGMENT_NOT_BEFORE_TEMPLATE, SEGMENT_LABEL_OUTSIDE_WINDOW,
    ENGINE_NOT_SUPPORTED_NATIVE_TRIGGER, ENGINE_NOT_SUPPORTED_NATIVE_PERMISSION,
    ENGINE_NOT_SUPPORTED_NATIVE_REPEAT, ENGINE_NOT_SUPPORTED_NATIVE_PRIORITY, ENGINE_NOT_SUPPORTED_NATIVE_SHAPE,
    ENGINE_NOT_SUPPORTED_ETIQUETAR, CHANGE_OUTSIDE_1B_SCOPE, TRIGGER_CHANGE_IS_ANOTHER_FLOW, FLOW_CHANGE_STALE_BASE,
    NATIVE_CHANGE_EXITS, NATIVE_CHANGE_REPEAT_SILENCE, NATIVE_CHANGE_PRIORITY_SILENCE, NATIVE_CHANGE_PRIORITY_WINS,
    NATIVE_CHANGE_PRIORITY_WINS_REF, NATIVE_CHANGE_PRIORITY_REF, NATIVE_CHANGE_ENTRY, NATIVE_CHANGE_POOL, NATIVE_CHANGE_OTHER,
    CHANGE_WITHOUT_EFFECT_LEGACY_ROUTE, NATIVE_ENGINE_NOT_RUNNING, NATIVE_FIRST_CONTACT_NOT_RUNNING,
    NATIVE_SILENCE_NOT_RUNNING, NATIVE_TRIGGER_OTHER_PRODUCT, NATIVE_MULTITOUCH_NOT_RUNNING,
    NATIVE_MULTITOUCH_NOT_RUNNING_WARNING, NATIVE_PERSONAL_COUPON_NOT_RUNNING,
    NATIVE_PERSONAL_COUPON_NOT_RUNNING_WARNING,
    ROLLBACK_TARGET_NOT_FOUND, ROLLBACK_TARGET_IS_ACTIVE, STATE_TRANSITION_INVALID,
    FLOW_HAS_NO_VERSION, EN_CURSO_NOT_SUPPORTED, CLIENT_PURCHASE_OK_MISSING,
    ALWAYS_WITH_MARKETING, RECOVERY_WITHOUT_CAP, SILENCE_WITHOUT_PURCHASE_EXIT,
    SAME_TRIGGER_UNRELATED, WAIT_WITHOUT_CONTESTA, SILENCE_WINDOW_SHORTER_THAN_CLOSED_GAP,
    EMPTY_BRANCH_WITH_LE_GANA_A, SAME_EVENT_NATIVE_FLOWS,
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


def _scope_keys(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[tuple[Segment, ...]]:
    """Lo que difiere entre dos ``_scope_view``, por campo y sin repetidos."""
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
    return keys


# Lo que no cambia ningun envio: renombrar un flujo no es un cambio de estructura para la pagina.
_NOT_A_STRUCTURE_CHANGE = frozenset({("nombre",)})


def structural_changes(active: Mapping[str, Any], draft: Mapping[str, Any]) -> tuple[str, ...]:
    """Lo que cambia ``draft`` de la estructura de ``active``: los campos, separados por ``/``.

    Es la misma cuenta que ``_scope``, para que la pagina y el segundo paso digan lo mismo que el validador
    (``pasos``, ``topes/repeticion``, ``prioridad/no_arranca_mientras``…). Vacio si la version solo cambia lo
    que el AE cambia en cualquier flujo, o solo el nombre.
    """
    return tuple("/".join(str(part) for part in key)
                 for key in _scope_keys(_scope_view(active), _scope_view(draft))
                 if key not in _NOT_A_STRUCTURE_CHANGE)


# ---------------------------------------------------------------------------
# La validacion.
# ---------------------------------------------------------------------------

class _Emitter:
    """Junta los hallazgos de una definicion, con las rutas prefijadas por su lugar."""

    def __init__(self, base: tuple[Segment, ...], collapse: str | None = None) -> None:
        self.base = base
        self.collapse = collapse
        self.findings: list[Finding] = []

    def add(self, spec: CodeSpec, segments: Sequence[Segment] = (), *, reason: str | None = None,
            alternative: str | None = None) -> None:
        path = self.collapse if self.collapse is not None else finding_path(self.base + tuple(segments))
        self.findings.append(spec.at(path, reason=reason, alternative=alternative))


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
        # Los items nuevos cuya clave ya existe en su producto (flow_already_registered).
        self.taken: set[int] = set()
        # Los items nuevos con la clave de un flujo del legado (flow_key_reserved_for_legacy, E-207).
        self.reserved: set[int] = set()
        self.flows = [self._resolve(index, item) for index, item in enumerate(items)]
        self.normalized: list[ProposalItem] = []
        self.subjects: list[_Subject] = []
        self.effective: dict[str, Mapping[str, Any] | None] = {
            flow.flow_id: flow.active_definition for flow in world.flows
        }

    def _resolve(self, index: int, item: ProposalItem) -> WorldFlow:
        if item.is_new_flow:
            # Un flujo que todavia no existe: un flujo nativo apagado y sin version, con un id
            # que sale de su clave y no esta en la foto. Si la clave ya existe, es un bloqueo.
            if self.world.find(item.producto, item.flujo) is not None:
                self.taken.add(index)
            if item.flujo in LEGACY_FLOW_KEYS:
                self.reserved.add(index)
            return WorldFlow(
                flow_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"setter-flujo-nuevo:{item.producto}/{item.flujo}")),
                product_ref=item.producto, flow_key=item.flujo, native=True)
        assert item.flow_id is not None
        flow = self.world.flow(item.flow_id)
        if flow is None or flow.product_ref != item.producto or flow.flow_key != item.flujo:
            raise FlowValidationInputError("flow_not_found")
        return flow

    # -- El recorrido --------------------------------------------------------

    def run(self) -> ValidationResult:
        self._normalize()
        self._stale_base()
        for index, (item, flow) in enumerate(zip(self.items, self.flows)):
            if item.tipo in ("version", "volver") and not (
                    item.en_curso == "seguir" or (item.en_curso == IN_FLIGHT_CANCEL and flow.is_native)):
                self.blocks.append(EN_CURSO_NOT_SUPPORTED.at(finding_path((index, "en_curso"))))
            if item.is_new_flow:
                if item.prender != COHORT:
                    self.blocks.append(NEW_FLOW_MUST_START_IN_COHORT.at(finding_path((index, "prender"))))
                if index in self.taken:
                    self.blocks.append(FLOW_ALREADY_REGISTERED.at(finding_path((index, "flujo"))))
                if index in self.reserved:
                    self.blocks.append(FLOW_KEY_RESERVED_FOR_LEGACY.at(finding_path((index, "flujo"))))
            if item.tipo == "prender":
                self._switch_on(index, item, flow)
            elif item.tipo == "apagar":
                self._switch_off(index, item, flow)
        for subject in self.subjects:
            self._subject(subject)
        self._same_event_native()
        self._without_effect()
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
            if item.tipo == "version" or item.is_new_flow:
                others = [
                    other.active_definition.get("horario")
                    for other in self.world.flows
                    if other.flow_id != flow.flow_id and other.active_definition is not None
                ]
                written: Mapping[str, Any] = item.definicion or {}
                if item.is_new_flow and isinstance(written, Mapping) and "flujo" not in written:
                    # La clave de un flujo nuevo es la del item: la definicion la repite (la base la exige).
                    written = {**written, "flujo": item.flujo}
                result = normalize_definition(
                    written, new_flow=flow.active_version is None,
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
                        for finding in check_definition(target, published=True, path=(index, "version"),
                                                         labels=self.instance.labels)
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
        if item.a is not None:
            if not flow.native or flow.state != "pausado":
                self.blocks.append(PRENDER_A_COHORT_NEEDS_PAUSED_NATIVE.at(finding_path((index, "a"))))
        elif flow.state == "prendido":
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
        categories = self._rule13(emitter, active, flow)
        self._rule17(emitter, active, categories)
        self.blocks.extend(emitter.findings)

    # -- Lo que se publicaria sin efecto (E-353) -----------------------------

    def _without_effect(self) -> None:
        """Lo que el AE publicaria sin cambiar ningun envio: la ruta del flujo y lo que corre la instancia.

        Mira lo que promete mensajes (``nuevo``, ``version``, ``volver`` y ``prender``), nunca lo que los
        saca: apagar un flujo del legado frena al legado, y eso si tiene efecto.

        * Un flujo del legado solo cambia lo que sale con la ruta en ``motor``; con ``legado``, o sin ruta,
          una version queda guardada y nadie la lee (el caso de E-198). Tampoco con la ruta en ``motor`` si
          este bridge lo manda al legado: la unica ruta del legado que va a ``motor`` es la del carrito
          (``flow_route_kind_not_supported``), y sin el motor o sin sus toques (``resolution.py``,
          ``p_force_legacy``) sale por el legado. ``prender`` no se mira: sobre una ruta en legado abre lo
          que la guarda del legado frena.
        * De un flujo nativo, lo que la instancia corre (``InstanceRuntime``): el motor, el primer contacto
          con su flag y el silencio con su barredor, en el producto principal (el primer contacto corre
          tambien en los otros productos desde E-350). Un silencio
          sin barredor es peor que no mandar: la barrera del legado (``blocked_native_flow``) le saca la
          conversacion al seguimiento de hoy.
        """
        self._multitouch_without_effect()
        self._personal_coupon_without_effect()
        if self.actor != "automation_expert":
            return
        runtime = self.instance.runtime
        for index, (item, flow) in enumerate(zip(self.items, self.flows)):
            if item.tipo not in ("version", "volver", "prender") and not item.is_new_flow:
                continue
            at = finding_path((index,))
            if not flow.is_native:
                engine_sends_it = flow.route_executor == ROUTE_MOTOR and (
                    runtime is None or (runtime.engine_enabled and runtime.cart_touches))
                if item.tipo != "prender" and flow.active_version is not None \
                        and flow.route_executor != ROUTE_NOT_READ and not engine_sends_it:
                    self.blocks.append(CHANGE_WITHOUT_EFFECT_LEGACY_ROUTE.at(at))
                continue
            if runtime is None:
                continue
            if not runtime.engine_enabled:
                self.blocks.append(NATIVE_ENGINE_NOT_RUNNING.at(at))
                continue
            definition = self.effective.get(flow.flow_id)
            trigger = native_trigger_key(definition) if isinstance(definition, Mapping) else None
            if trigger == "intencion" and not runtime.native_intent_enabled:
                self.blocks.append(NATIVE_FIRST_CONTACT_NOT_RUNNING.at(at))
            elif trigger == NATIVE_SILENCE_TRIGGER and not runtime.native_silence_sweeper:
                self.blocks.append(NATIVE_SILENCE_NOT_RUNNING.at(at))
            elif trigger == NATIVE_SILENCE_TRIGGER and flow.product_ref != runtime.main_product:
                self.blocks.append(NATIVE_TRIGGER_OTHER_PRODUCT.at(at))

    def _multitouch_without_effect(self) -> None:
        """Un nativo de varios mensajes con los toques siguientes apagados en el bridge (E-483).

        La base publica hasta tres envios (20261112210000) y el bridge manda el segundo y el tercero solo con
        ``FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED``; apagado, los cierra sin mandar. Mira lo mismo que
        ``_without_effect`` (``nuevo``, ``version``, ``volver`` y ``prender``) y solo si la instancia dice lo
        que corre: el AE no lo publica; al operador se le avisa. Con el motor apagado ya bloquea
        ``native_engine_not_running``.
        """
        runtime = self.instance.runtime
        if runtime is None or runtime_runs_multitouch(runtime) or not runtime.engine_enabled:
            return
        for index, (item, flow) in enumerate(zip(self.items, self.flows)):
            if not flow.is_native or (item.tipo not in ("version", "volver", "prender") and not item.is_new_flow):
                continue
            definition = self.effective.get(flow.flow_id)
            if not isinstance(definition, Mapping) or _max_sends(definition.get("pasos")) <= 1:
                continue
            at = finding_path((index,))
            if self.actor == "automation_expert":
                self.blocks.append(NATIVE_MULTITOUCH_NOT_RUNNING.at(at))
            else:
                self.warnings.append(NATIVE_MULTITOUCH_NOT_RUNNING_WARNING.at(at))

    def _personal_coupon_without_effect(self) -> None:
        """Un nativo con el cupon propio con ese toque apagado en el bridge (E-483, C2).

        La base lo publica (20261112210500) y el bridge arma el toque con el cupon solo con
        ``FLOW_ENGINE_NATIVE_PERSONAL_COUPON_ENABLED``; apagado, lo cierra sin mandarlo. El mismo molde que
        ``_multitouch_without_effect``: solo si la instancia dice lo que corre; el AE no lo publica, al operador
        se le avisa.
        """
        runtime = self.instance.runtime
        if runtime is None or runtime_runs_personal_coupon(runtime) or not runtime.engine_enabled:
            return
        for index, (item, flow) in enumerate(zip(self.items, self.flows)):
            if not flow.is_native or (item.tipo not in ("version", "volver", "prender") and not item.is_new_flow):
                continue
            definition = self.effective.get(flow.flow_id)
            if not isinstance(definition, Mapping) or not _personal_coupon_sends(definition.get("pasos")):
                continue
            at = finding_path((index,))
            if self.actor == "automation_expert":
                self.blocks.append(NATIVE_PERSONAL_COUPON_NOT_RUNNING.at(at))
            else:
                self.warnings.append(NATIVE_PERSONAL_COUPON_NOT_RUNNING_WARNING.at(at))

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
            # El agente crea un flujo solo con un item nuevo (nativo); la primera version de un
            # flujo registrado sin version sigue siendo del operador (ciclo 1.7.0).
            if self.actor == "automation_expert" and active is None and subject.item.tipo == "version":
                self.blocks.append(FLOW_CREATION_NOT_ENABLED.at(finding_path((subject.index,))))
            if active is not None:
                mine, published = _trigger_identity(definition), _trigger_identity(active)
                if mine is not None and published is not None and not json_equal(list(mine), list(published)):
                    emitter.add(TRIGGER_CHANGE_IS_ANOTHER_FLOW, ("dispara",))
                if self.actor == "automation_expert":
                    self._scope(subject, active)
            elif self.actor == "automation_expert" and subject.item.is_new_flow:
                # E-483 (C7): el flujo nuevo con le_gana_a, con la misma regla que una version.
                self._native_priority_wins(subject)
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
            categories = self._rule13(emitter, definition, flow)
            self._rule14_compiles(emitter, definition, flow)
            self._rule14_native(emitter, definition, flow)
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
        """Lo que el Automation Expert cambia fuera de la decision 4 (``change_outside_1b_scope``).

        En un flujo nativo (P28, E-357) la estructura se abre hasta donde el motor nativo la ejecuta:
        ``_native_scope``. El legado y la ruta del motor siguen igual: su estructura la cambia el operador.
        """
        for key in _scope_keys(_scope_view(active), _scope_view(subject.definition)):
            if subject.flow.is_native:
                self._native_scope(subject, key, active)
            else:
                subject.emitter.add(CHANGE_OUTSIDE_1B_SCOPE, key)

    def _native_scope(self, subject: _Subject, key: tuple[Segment, ...],
                      active: Mapping[str, Any] | None = None) -> None:
        """Si el cambio de ``key`` en un flujo nativo se publica, o por que no (flow-change-cycle-v1 1.12.0).

        Se abre solo lo que el motor lee: ``pasos`` (dentro de la regla 14, que en fase publish mira cada
        forma), ``topes.repeticion``, ``prioridad.no_arranca_mientras`` y ``nombre``. Lo que el motor no lee
        se bloquea: publicarlo no cambiaria nada. El silencio corre por su propio barrido, que no mira ni la
        repeticion (una por conversacion, indice unico) ni la prioridad.
        """
        trigger = native_trigger_key(subject.definition)
        silence = trigger == NATIVE_SILENCE_TRIGGER
        emitter = subject.emitter
        if key in (("pasos",), ("nombre",)):
            return
        if key == ("topes", "repeticion"):
            if silence:
                emitter.add(NATIVE_CHANGE_REPEAT_SILENCE, key)
        elif key == ("prioridad", "no_arranca_mientras"):
            if silence:
                emitter.add(NATIVE_CHANGE_PRIORITY_SILENCE, key)
            else:
                self._native_priority_refs(subject)
        elif key == ("prioridad", "le_gana_a"):
            self._native_priority_wins(subject)
        elif key == ("termina", "si"):
            if trigger in NATIVE_MULTITOUCH_TRIGGERS and _only_reply_exit_changed(active, subject.definition):
                # E-483: que conteste ahora lo lee el motor (corta el toque siguiente, 20261112210000).
                return
            emitter.add(NATIVE_CHANGE_EXITS, key, reason=_NATIVE_EXITS_TEXT.get(trigger or ""))
        elif key in (("entra", "contesto_a"), ("entra", "permiso")):
            emitter.add(NATIVE_CHANGE_ENTRY, key)
        elif key == ("topes", "bolsa"):
            emitter.add(NATIVE_CHANGE_POOL, key)
        else:
            emitter.add(NATIVE_CHANGE_OTHER, key)

    def _native_priority_wins(self, subject: _Subject) -> None:
        """``prioridad.le_gana_a`` de un flujo nativo que propone el AE (E-483, C7, 20261112210200).

        La admision nativa la lee solo si el bridge corre los toques siguientes
        (``FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED``: ``runtime_runs_multitouch``), y solo en los flujos que
        arrancan con un evento de Hotmart (``NATIVE_PRIORITY_WINS_TRIGGERS``): sin eso, ``native_change_priority_wins``
        como antes. Con eso, cada flujo de la lista tiene que ser un nativo del mismo producto, por su clave, que
        no sea el seguimiento (su corrida no es un recorrido): si no, ``native_change_priority_wins_ref``. Sacar
        la lista entera siempre se puede.
        """
        definition = subject.definition
        emitter = subject.emitter
        refs = _refs(definition, "prioridad", "le_gana_a")
        if not refs:
            return
        at = ("prioridad", "le_gana_a")
        if not runtime_runs_multitouch(self.instance.runtime):
            emitter.add(NATIVE_CHANGE_PRIORITY_WINS, at)
            return
        if native_trigger_key(definition) not in NATIVE_PRIORITY_WINS_TRIGGERS:
            emitter.add(NATIVE_CHANGE_PRIORITY_WINS, at, reason=_NATIVE_PRIORITY_WINS_TRIGGER)
            return
        flow = subject.flow
        own_product = _own_product(definition, flow)
        for index, ref in enumerate(refs):
            if is_unresolved(ref):
                continue
            parsed = parse_flow_ref(ref, own_product)
            other = self.world.find(*parsed) if parsed is not None else None
            if other is None:
                continue  # unknown_flow_reference, de la regla 10
            active = other.active_definition
            silence = active is not None and native_trigger_key(active) == NATIVE_SILENCE_TRIGGER
            if ref != other.flow_key or other.product_ref != own_product or other.flow_id == flow.flow_id \
                    or not other.is_native or silence:
                emitter.add(NATIVE_CHANGE_PRIORITY_WINS_REF, at + (index,))

    def _native_priority_refs(self, subject: _Subject) -> None:
        """Cada flujo de ``no_arranca_mientras`` lo puede leer la base (``_flow_engine_admit_native_flow``).

        La base compara ``flow_engine_runs.flow_key`` con el texto de la lista, en el mismo producto: una
        referencia con producto, un flujo de otro producto o uno sin recorridos del motor no frenarian nunca.
        El seguimiento con cupon es nativo pero tampoco tiene recorridos: su corrida es una fila de
        ``conversation_followup_events``, sin caso (D-P22-1, migracion 20261101000200).
        """
        flow = subject.flow
        own_product = _own_product(subject.definition, flow)
        for index, ref in enumerate(_refs(subject.definition, "prioridad", "no_arranca_mientras")):
            if is_unresolved(ref):
                continue
            parsed = parse_flow_ref(ref, own_product)
            other = self.world.find(*parsed) if parsed is not None else None
            if other is None:
                continue  # unknown_flow_reference, de la regla 10
            at = ("prioridad", "no_arranca_mientras", index)
            active = other.active_definition
            if active is not None and native_trigger_key(active) == NATIVE_SILENCE_TRIGGER:
                subject.emitter.add(NATIVE_CHANGE_PRIORITY_REF, at, reason=_NATIVE_PRIORITY_REF_SILENCE)
                continue
            runs_in_engine = other.is_native or other.route_executor in (ROUTE_MOTOR, ROUTE_NOT_READ)
            if ref != other.flow_key or other.product_ref != own_product or other.flow_id == flow.flow_id \
                    or not runs_in_engine:
                subject.emitter.add(NATIVE_CHANGE_PRIORITY_REF, at)

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
        if known and permission != "conversacion" and (
                (isinstance(replied_to, list) and replied_to) or replied_to == ANY_TEMPLATE):
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
        payment_failure = None if ficha is None else (trigger is not None and trigger.event == PAYMENT_FAILURE_EVENT)
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
                datum = _get(_mapping(step.get("condicion")), "dato")
                if datum == "medio_de_pago" and ficha is False:
                    emitter.add(MEDIO_DE_PAGO_REQUIRES_FICHA_TRIGGER, at + ("condicion", "dato"))
                elif isinstance(datum, str) and datum in PAYMENT_FAILURE_DATUM_SPECS and payment_failure is False:
                    emitter.add(PAYMENT_FAILURE_DATUM_SPECS[datum], at + ("condicion", "dato"))
            elif kind == "etiquetar" and step.get("etiquetar") in self.instance.segment_label_states:
                # E-316: la etiqueta de un segmento va con cualquier disparador, mientras no vencio.
                if self.instance.segment_label_states[step["etiquetar"]] == "vencido":
                    emitter.add(SEGMENT_LABEL_OUTSIDE_WINDOW, at + ("etiquetar",))
            elif kind == "etiquetar" and trigger is not None and not (
                    trigger.kind == "evento" and trigger.event is None) and not trigger.is_purchase:
                emitter.add(ETIQUETAR_REQUIRES_PURCHASE_TRIGGER, at + ("etiquetar",))

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
        trigger = trigger_of(definition)
        chain = trigger is not None and trigger.kind == "evento" and trigger.event == PAYMENT_FAILURE_EVENT
        for step, at, branch, condition, depth, last in _iter_steps(definition.get("pasos"), ("pasos",)):
            kind = step_type(step)
            if kind == "conversar" and not (branch == "entonces" and _is_open_window(condition)):
                emitter.add(CONVERSAR_OUTSIDE_OPEN_WINDOW_BRANCH, at + ("conversar",))
            if kind in ("conversar", "derivar") and not last:
                emitter.add(TERMINAL_STEP_NOT_LAST, at + (kind,))
            if kind == "condicion" and depth > 0 and not (
                    chain and branch == "si_no" and last and at[-1] == 0 and depth < CONDITION_CHAIN_MAX_LEVELS):
                # La cadena del pago rechazado (1.4): otra condicion como unico paso del si_no, hasta tres niveles.
                emitter.add(NESTED_CONDITION, at + ("condicion",))
        self._rule7_etiquetar(emitter, definition.get("pasos"), ("pasos",), sent=False, labeled=False)

    def _rule7_etiquetar(self, emitter: _Emitter, steps: object, segments: tuple[Segment, ...], *,
                         sent: bool, labeled: bool, depth: int = 0) -> None:
        """Un ``etiquetar`` va despues de una plantilla de su camino, y hay uno solo por camino.

        El de un segmento (E-316) es al reves: arriba de todo y antes de la plantilla, porque el motor lo
        ejecuta antes del primer envio.
        """
        if not isinstance(steps, list) or depth > MAX_DEPTH:
            return
        for index, step in enumerate(steps):
            kind = step_type(step)
            at = segments + (index,)
            if kind == "plantilla":
                sent = True
            elif kind == "etiquetar" and isinstance(step, Mapping) \
                    and step.get("etiquetar") in self.instance.segment_label_states:
                if sent or labeled or depth > 0:
                    emitter.add(ETIQUETAR_SEGMENT_NOT_BEFORE_TEMPLATE, at + ("etiquetar",))
                labeled = True
            elif kind == "etiquetar":
                if not sent or labeled:
                    emitter.add(ETIQUETAR_NOT_AFTER_TEMPLATE, at + ("etiquetar",))
                labeled = True
            elif kind == "condicion" and isinstance(step, Mapping):
                for name in ("entonces", "si_no"):
                    self._rule7_etiquetar(emitter, step.get(name), at + (name,), sent=sent, labeled=labeled,
                                          depth=depth + 1)
                return

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

    def _rule13(self, emitter: _Emitter, definition: Mapping[str, Any],
                flow: WorldFlow | None = None) -> dict[tuple[Segment, ...], object]:
        """Las plantillas contra el catalogo. Devuelve la categoria de cada paso resuelto.

        En un flujo nativo (``flow.is_native``) los botones son los que manda el
        envio nativo (``parse_approved_template(parameterless_buttons_only=True)``):
        ninguno, de URL fija (E-115) o de respuesta rapida (flow-engine-v2 2.6.0),
        sin parametro. La categoria la mira la 17: con ``permiso: compra``, solo
        ``UTILITY``; con otro permiso, tambien ``MARKETING``.

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
            self._template_shape(emitter, step, at, template,
                                 native=flow is not None and flow.is_native,
                                 silence=native_trigger_key(definition) == NATIVE_SILENCE_TRIGGER)
        return categories

    def _template_shape(self, emitter: _Emitter, step: Mapping[str, Any], at: tuple[Segment, ...],
                        template: Mapping[str, Any], *, native: bool = False, silence: bool = False) -> None:
        """Los marcadores {{1}}..{{n}} y los botones, con la forma de ``parse_approved_template``.

        ``native``: el paso de un flujo nativo. Sus variables las declara el paso, una por
        marcador y en su orden, y la razon del bloqueo dice cuantos marcadores tiene la
        plantilla. Sus botones, si no declara ``boton``, solo pueden ser de URL fija o de
        respuesta rapida. Y solo el envio nativo manda un encabezado de imagen, el de la
        plantilla declarada en la instancia; ``silence``: el barrido del silencio no lo manda.
        """
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
                emitter.add(TEMPLATE_VARIABLE_COUNT_MISMATCH, at + ("variables",),
                            reason=_native_count_reason(placeholders, variables) if native else None)
        buttons: list[object] = []
        groups = 0
        # E-370: solo un nativo que no sea el silencio manda la imagen que declara la instancia.
        sends_image = native and not silence
        image_declared = step.get("plantilla") in self.instance.template_images
        image_headers = 0
        for component in components:
            kind = component.get("type")
            if kind == "BODY":
                continue
            if kind == "HEADER" and component.get("format") == "IMAGE" and sends_image and image_headers == 0:
                image_headers += 1
                if not image_declared:
                    emitter.add(TEMPLATE_VARIABLE_COUNT_MISMATCH, at + ("plantilla",),
                                reason="La plantilla pide una imagen en el encabezado y la instancia no tiene declarada cuál mandar.",
                                alternative=("Usar una plantilla sin imagen, o decirle al dueño que el equipo técnico "
                                             "tiene que declarar la imagen de esa plantilla en la instancia."))
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
        if sends_image and image_declared and image_headers == 0:
            # El envio la frenaria siempre con header_media_missing.
            emitter.add(TEMPLATE_VARIABLE_COUNT_MISMATCH, at + ("plantilla",),
                        reason="La instancia declara una imagen para esta plantilla, pero en el catálogo no tiene encabezado de imagen.",
                        alternative="Usar la plantilla que tiene la imagen, o pedirle al equipo técnico que corrija la declaración.")
        button = _mapping(step.get("boton"))
        link = button.get("link") if button is not None else None
        if button is not None and (not isinstance(link, str) or is_unresolved(link)):
            return
        def quick_replies(rest: list[object]) -> bool:
            return all(isinstance(b, Mapping) and b.get("type") == "QUICK_REPLY" for b in rest)

        # The link opens the group and quick replies may follow it («No más
        # mensajes» after the checkout, 18/10), the same order that
        # ``parse_approved_template(checkout_button=True)`` accepts.
        if native and button is None:
            # El envio nativo no manda parametros de boton: solo pasa una URL fija.
            wrong = _native_button_problem(buttons, groups)
            if wrong is not None:
                emitter.add(TEMPLATE_BUTTON_MISMATCH, at + ("plantilla",), reason=wrong)
            return
        if link == "checkout" and native:
            # El seguimiento nativo manda con parse_followup_template: el checkout primero y, despues, nada
            # (la forma vieja, las _01) o la respuesta rapida «No más mensajes» y nada mas (las _02, E-272).
            # Otra respuesta rapida fallaria en cada barrido (P22-1 de M16).
            opt_out = (len(buttons) == 2 and isinstance(buttons[1], Mapping)
                       and buttons[1].get("type") == "QUICK_REPLY"
                       and buttons[1].get("text") == FOLLOWUP_OPT_OUT_BUTTON)
            fits = groups == 1 and (len(buttons) == 1 or opt_out) and _is_url_button(buttons[0]) and (
                buttons[0].get("url") == CHECKOUT_BUTTON_URL)
        elif link == "checkout":
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

    def _rule14_native(self, emitter: _Emitter, definition: Mapping[str, Any], flow: WorldFlow) -> None:
        """La 14 de un flujo nativo, con lo que la base abre y admite (flow-engine-v2 §9).

        Un flujo del legado no ejecuta ``etiquetar``: ahi es ``engine_not_supported:etiquetar``.
        """
        steps = definition.get("pasos")
        if not flow.is_native:
            for step, at, *_ in _iter_steps(steps, ("pasos",)):
                if step_type(step) == "etiquetar":
                    emitter.add(ENGINE_NOT_SUPPORTED_ETIQUETAR, at + ("etiquetar",))
            return
        codes = native_not_supported(definition)
        if self.actor == "automation_expert" and f"{NOT_SUPPORTED}:{TOO_MANY_SENDS}" in codes:
            # Dos mensajes seguidos son dos problemas para el motor (la espera en el medio y el segundo envio) y
            # uno solo para el dueno: el del mensaje de mas. Los dos caen en el mismo camino y se contaria solo el primero.
            codes = tuple(code for code in codes if not code.endswith(("espera_despues_del_envio", "espera_entre_envios")))
        for code in codes:
            spec = _NATIVE_SPECS.get(code)
            if spec is not None:
                emitter.add(spec, _NATIVE_PATHS[code])
                continue
            if not code.startswith(NOT_SUPPORTED + ":"):
                continue
            # Un codigo cerrado (motor_nativo:forma), con lo que no ejecuta en la razon.
            what = code.split(":", 1)[1]
            at: tuple[Segment, ...] = ("termina", "si") if what.startswith(("salida_", "contesta_")) \
                else ("dispara",) if what.startswith("disparo_") \
                else ("dispara", "silencio") if what.startswith("silencio_") \
                else ("entra", "contesto_a") if what == "contesto_a" else ("pasos",)
            if self.actor == "automation_expert":
                if what.startswith(("salida_", "contesta_")) \
                        and any(found.code == NATIVE_CHANGE_EXITS.code for found in emitter.findings):
                    continue  # el cambio de salidas ya volvio frenado, con su motivo (``_native_scope``)
                reason, alternative = _native_shape_for_the_owner(what)
                emitter.add(ENGINE_NOT_SUPPORTED_NATIVE_SHAPE, at, reason=reason, alternative=alternative)
                continue
            emitter.add(ENGINE_NOT_SUPPORTED_NATIVE_SHAPE, at,
                        reason=f"El motor nativo de esta versión no ejecuta eso ({what}).")

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

    # -- Dos flujos nativos con el mismo evento (E-139) --------------------

    def _final_states(self) -> dict[str, str]:
        """El estado en que la propuesta deja cada flujo: el de la foto, con los items aplicados."""
        states = {flow.flow_id: flow.state for flow in self.world.flows}
        for item, flow in zip(self.items, self.flows):
            if item.is_new_flow:
                states[flow.flow_id] = COHORT
            elif item.tipo == "prender":
                states[flow.flow_id] = COHORT if item.a is not None else "prendido"
            elif item.tipo == "apagar":
                states[flow.flow_id] = "apagado"
        return states

    def _same_event_native(self) -> None:
        """Un flujo nativo que la propuesta toca y deja con el mismo evento que otro nativo del producto.

        Los dos tienen que quedar abiertos al evento (``prendido`` o ``cohorte``;
        ``pausado`` no abre caso y ``apagado`` no se admite). Es un bloqueo para todos
        y para la cohorte: a quien le toquen los dos le llega uno solo, al azar.
        Una ``prioridad`` no lo levanta: el motor no la mira para el mismo evento.
        """
        states = self._final_states()
        flows: dict[str, WorldFlow] = {flow.flow_id: flow for flow in self.world.flows}
        for flow in self.flows:
            flows.setdefault(flow.flow_id, flow)
        first: dict[str, int] = {}
        for index, flow in enumerate(self.flows):
            first.setdefault(flow.flow_id, index)
        open_states = ("prendido", COHORT)
        for flow_id, index in first.items():
            flow = flows[flow_id]
            mine = _native_event(self.effective.get(flow_id))
            if not flow.native or states.get(flow_id) not in open_states or mine is None:
                continue
            item = self.items[index]
            at = finding_path((index, "definicion", "dispara") if item.definicion is not None else (index,))
            for other in flows.values():
                if other.flow_id == flow_id or other.product_ref != flow.product_ref or not other.native:
                    continue
                if states.get(other.flow_id) not in open_states:
                    continue
                if _native_event(self.effective.get(other.flow_id)) != mine:
                    continue
                self.blocks.append(SAME_EVENT_NATIVE_FLOWS.at(at))

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
            if flow.native and other.native:
                # Dos nativos: lo mira _same_event_native, que sabe que la prioridad no los separa.
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


def _native_event(definition: Mapping[str, Any] | None) -> str | None:
    """El evento con el que el motor admite un flujo nativo (``dispara.evento``), o ``None``."""
    event = _get(definition, "dispara", "evento") if isinstance(definition, Mapping) else None
    return event if isinstance(event, str) and not is_unresolved(event) else None


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


def _is_static_url(url: object) -> bool:
    """Una URL fija de un boton (sin ``{{n}}``): el envio no la llena. Como ``approved_templates.is_static_url``."""
    return isinstance(url, str) and url.startswith("https://") and "{{" not in url and "}}" not in url \
        and not any(char.isspace() for char in url)


def _native_button_problem(buttons: list[object], groups: int) -> str | None:
    """Por que el envio nativo no manda los botones de esta plantilla, o ``None`` si los manda.

    Manda los que no llevan parametro: los de URL fija (E-115) y los de respuesta rapida
    (flow-engine-v2 2.6.0), en cualquier orden, como ``parse_approved_template(
    parameterless_buttons_only=True)``.
    """
    if groups > 1:
        return "La plantilla tiene más de un grupo de botones, y el motor nativo no sabe mandarla."
    for button in buttons:
        if not isinstance(button, Mapping):
            return "La plantilla tiene un botón que no se puede leer."
        if button.get("type") == "QUICK_REPLY":
            continue
        if button.get("type") != "URL":
            return ("La plantilla tiene un botón de otro tipo, y el motor nativo solo manda botones de URL fija "
                    "y de respuesta rápida.")
        if not _is_static_url(button.get("url")):
            return ("La plantilla tiene un botón de URL con una variable, y el motor nativo no la llena: "
                    "solo manda botones de URL fija y de respuesta rápida.")
    return None


def _native_count_reason(placeholders: list[int], variables: list[object]) -> str:
    """La razon de un conteo distinto en un paso nativo: cuantos marcadores tiene la plantilla."""
    count = len(placeholders)
    marks = "ningún marcador" if not count else "un marcador ({{1}})" if count == 1 \
        else f"{count} marcadores ({{{{1}}}} a {{{{{count}}}}})"
    return (f"La plantilla tiene {marks} y el paso manda {len(variables)} variables: "
            "escribí en variables una por marcador, en su orden, de entre nombre y producto.")


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
