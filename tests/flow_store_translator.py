"""El traductor de la configuracion de ATT1 de hoy a definiciones setter-flujo/v1.

Lo usan las pruebas (``tests/test_flow_validator_att1.py``) y, despues, la
migracion de ATT1 (pieza 5). Se carga con ``importlib.util.spec_from_file_location``,
como ``tests/test_supabase_release_readiness.py`` carga un script.

De donde sale cada campo (diseno del almacen, §7.5):

* del dato real (att1-db), para el carrito y el pago fallido (el scope activo
  ``att1-recuperacion`` con su politica) y el primer contacto
  (``att1-primer-contacto`` con la suya): ``por_dia`` y ``total``, la primera
  espera desde la gracia, ``vence`` desde ``expires_after``, el horario desde
  ``business_windows`` y ``timezone``, la bolsa desde ``scope_key``, el
  disparador desde ``source_event_type`` y ``additional_source_event_types``, y
  ``permiso: formulario`` desde ``audience_mode: consented_intent``;
* del manifiesto, la plantilla y su idioma (``[plantillas]``);
* del codigo, escrito en el contrato (docs/contracts/flow-definition-v1.md:481-590):
  las variables, ``termina.si``, la repeticion, ``le_gana_a``,
  ``no_arranca_mientras`` y el nombre;
* de los bloques del esquema, enteros: el cupon y la reactivacion como esta
  construida (:592-662), que no estan en la base: viven en variables y en el
  manifiesto.

Lo que el carrito y el pago fallido comparten (el scope, la politica y el
control) se parte en dos flujos con los mismos valores
(docs/contracts/flow-change-cycle-v1.md:202). Una forma que el traductor no
conoce (otra ventana, dias que no son los siete, un intervalo con otro
formato, otra audiencia, otro evento) es ``TranslationError``: falla en voz
alta, nunca inventa un valor.

**Varios toques (T2.1, la captura del 2026-10-21).** Desde el 19/10 el carrito
corre ``att1-recuperacion`` v7 con la politica de tres pasos, y el pago fallido
tiene su propio scope, ``att1-pago-rechazado`` (v2, dos pasos). Si la
configuracion tiene el control de un scope propio para un evento
(``DEDICATED_SCOPES``), ese evento sale de ahi y no del scope compartido: el
bridge toma el pago fallido de ``LANCEMOS_PILOT_PAYMENT_FAILURE_SCOPE_*``
(``src/bridge/app.py:254-257``), aunque ``att1-recuperacion`` v7 siga nombrando
``PURCHASE_CANCELED``. De cada politica se toman los pasos de su flujo (los
``step_key`` de ``bridge.flow_policy.STEPS_BY_LEGACY_KIND``): la espera de cada
uno sale de su ``delay`` (desde el primer envio, la regla del legado) y el cupon
personal, de ``coupon_ordinal``. Las plantillas de los toques no estan en el
manifiesto (son del runtime): entran como el parametro ``touches``, con su
origen escrito (``ATT1_TOUCHES_20261021``). La captura del 16/10 (un paso por
flujo y sin scope propio) se traduce igual que antes.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from bridge.flow_definition import PERSONAL_COUPON
from bridge.flow_policy import STEPS_BY_LEGACY_KIND

# El evento de Hotmart o de la landing, en el esquema: (evento, flujo).
EVENTS: Mapping[str, tuple[str, str]] = {
    "PURCHASE_OUT_OF_SHOPPING_CART": ("carrito", "carrito"),
    "PURCHASE_CANCELED": ("pago_fallido", "pago_fallido"),
    "PRECHECKOUT_FORM_SUBMITTED": ("intencion", "precheckout"),
}
# La audiencia de hoy, en el permiso del esquema (flow-definition-v1.md:90).
AUDIENCES: Mapping[str, str] = {"consented_intent": "formulario"}

# Lo que vive en el codigo de hoy y el contrato escribe en cada bloque
# (flow-definition-v1.md:481-590).
CODE: Mapping[str, Mapping[str, Any]] = {
    "carrito": {
        "nombre": "Carrito abandonado",
        "variables": ["nombre", "producto"],
        "si": ["compra"],
        "repeticion": {"una_por": "persona_y_oferta", "otra_despues_de": "0m"},
        "prioridad": {"le_gana_a": ["precheckout"], "no_arranca_mientras": ["pago_fallido"]},
    },
    "pago_fallido": {
        "nombre": "Pago fallido",
        "variables": ["nombre", "producto"],
        "si": ["compra"],
        "repeticion": {"una_por": "persona_y_oferta", "otra_despues_de": "0m"},
        "prioridad": {"le_gana_a": ["precheckout"], "no_arranca_mientras": []},
    },
    "precheckout": {
        "nombre": "Primer contacto tras el formulario",
        "variables": ["nombre", "producto"],
        "si": ["compra"],
        "repeticion": {"una_por": "persona", "otra_despues_de": "24h"},
        "prioridad": {"le_gana_a": [], "no_arranca_mientras": []},
    },
}
# El scope propio de un evento, si la configuracion tiene su control: el evento
# sale de ahi y no del scope compartido (src/bridge/app.py:254-257).
DEDICATED_SCOPES: Mapping[str, str] = {"PURCHASE_CANCELED": "att1-pago-rechazado"}


@dataclass(frozen=True)
class Touch:
    """La plantilla de un toque que no esta en el manifiesto: su nombre, su idioma, si lleva el cupon
    personal y sus variables, con la constante del bridge de la que salen.

    El cupon personal va con el boton del checkout (``{link: checkout, cupon:
    personal}``): el link de recuperacion con ``offDiscount``.
    """

    template: str
    language: str
    personal_coupon: bool
    variables: tuple[str, ...]
    variables_from: str


SOURCE_BRIDGE = "codigo del bridge"
# Las variables de cada toque, en el orden {{1}}, {{2}}...: las constantes con las que el bridge
# arma los parametros (y que exige al leer la plantilla del catalogo).
A1_VARIABLES = ("nombre", "producto", "cuando", "precio", "vence")
A1_VARIABLES_FROM = f"{SOURCE_BRIDGE}: cart_first_touch.CART_DISCOUNT_VARIABLES (src/bridge/cart_first_touch.py:33)"
A2_VARIABLES = ("nombre", "producto")
A2_VARIABLES_FROM = f"{SOURCE_BRIDGE}: cart_followups.CART_SECOND_TOUCH_VARIABLES (src/bridge/cart_followups.py:67)"
LAST_TOUCH_VARIABLES = ("nombre", "producto", "vence")
Z_VARIABLES_FROM = f"{SOURCE_BRIDGE}: cart_followups.CART_LAST_TOUCH_VARIABLES (src/bridge/cart_followups.py:68)"
B2_VARIABLES_FROM = (f"{SOURCE_BRIDGE}: payment_failure_followups.PAYMENT_FAILURE_FOLLOWUP_VARIABLES "
                     "(src/bridge/payment_failure_followups.py:65)")


# Las plantillas de los toques de ATT1 al 2026-10-21, por flujo y en el orden de
# sus pasos; None es «la del manifiesto». Salen de despliegue/secretos/generar.py
# de la instancia ATT1 (setter-instancia-att1) en origin/main, d9fa3ee
# (2026-10-21), todas en es_MX (WABA_TEMPLATE_LANGUAGE, :271):
# * carrito, A1: att1_carrito_descuento_01 (RECOVERY_DISCOUNT_TEMPLATE_NAME, :320),
#   con el cupon personal. Si el cupon no llega, el bridge manda la del
#   manifiesto, att1_carrito_abandonado_01 (excepcion a1_espera_al_cupon);
# * carrito, A2: att1_carrito_dudas_01 (RECOVERY_CART_SECOND_TOUCH_TEMPLATE_NAME, :329);
# * carrito, Z: att1_ultima_renovacion_01 (RECOVERY_CART_LAST_TOUCH_TEMPLATE_NAME, :336);
# * pago fallido, B1: la del manifiesto, att1_compra_fallida_01 (las variantes
#   de B1 no se migran en la Etapa 1);
# * pago fallido, el seguimiento: B2, att1_pago_rechazado_descuento_01
#   (WABA_PAYMENT_FAILURE_TEMPLATE_SECOND_TOUCH, :407), el que sigue a un B1 sin
#   cupon (portable-payment-failure-followup-v1.md, «Proposito y limite»).
ATT1_TOUCHES_20261021: Mapping[str, tuple[Touch | None, ...]] = {
    "carrito": (
        Touch("att1_carrito_descuento_01", "es_MX", True, A1_VARIABLES, A1_VARIABLES_FROM),
        Touch("att1_carrito_dudas_01", "es_MX", False, A2_VARIABLES, A2_VARIABLES_FROM),
        Touch("att1_ultima_renovacion_01", "es_MX", True, LAST_TOUCH_VARIABLES, Z_VARIABLES_FROM),
    ),
    "pago_fallido": (None, Touch("att1_pago_rechazado_descuento_01", "es_MX", True, LAST_TOUCH_VARIABLES,
                                 B2_VARIABLES_FROM)),
}
TOUCHES_SOURCE = ("parametro del traductor: ATT1_TOUCHES_20261021 "
                  "(despliegue/secretos/generar.py de la instancia ATT1, origin/main d9fa3ee)")

# Los flujos que salen enteros de un bloque del esquema: (bloque, flujo).
FROM_SCHEMA_BLOCKS = (("4", "descuento"), ("5a", "reactivacion"))
# El orden de la propuesta de primeras versiones.
ORDER = ("carrito", "pago_fallido", "precheckout", "descuento", "reactivacion")

SOURCE_REAL = "dato real"
SOURCE_MANIFEST = "manifiesto"
SOURCE_CODE = "codigo (contrato :481-590)"
SOURCE_SCHEMA = "bloque del esquema"

_HMS = re.compile(r"([0-9]{2}):([0-9]{2}):([0-9]{2})")
_DELAY = re.compile(r"([1-9][0-9]{0,5}) (minute|hour|day)s?")
_DELAY_MINUTES = {"minute": 1, "hour": 60, "day": 24 * 60}
_DAYS = re.compile(r"([1-9][0-9]{0,2}) days?")
_HOUR = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")


class TranslationError(ValueError):
    """La configuracion trae una forma que el traductor no conoce."""


@dataclass(frozen=True)
class Translated:
    """Una definicion traducida y la fuente de cada campo (JSON Pointer -> fuente)."""

    definition: dict[str, Any]
    sources: dict[str, str]


def _grace_minutes(value: object) -> int:
    match = _HMS.fullmatch(value) if isinstance(value, str) else None
    if match is None or match.group(3) != "00":
        raise TranslationError(f"grace_period_shape:{value!r}")
    return int(match.group(1)) * 60 + int(match.group(2))


def _delay_minutes(value: object) -> int:
    match = _DELAY.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise TranslationError(f"step_delay_shape:{value!r}")
    return int(match.group(1)) * _DELAY_MINUTES[match.group(2)]


def _duration(minutes: int) -> str:
    """Una espera del esquema: en horas si son horas justas, si no en minutos."""
    return f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}m"


def _flow_steps(policy: Mapping[str, Any], flow_key: str) -> list[Mapping[str, Any]]:
    """Los pasos de la politica que son de este flujo, en orden, y que son los primeros de su forma."""
    shape = [step.step_key for step in STEPS_BY_LEGACY_KIND[flow_key]]
    steps = policy.get("steps")
    if not isinstance(steps, list) or not all(isinstance(step, Mapping) for step in steps):
        raise TranslationError("policy_steps_shape")
    mine = [step for step in steps if step.get("step_key") in shape]
    if [step.get("step_key") for step in mine] != shape[:len(mine)] or not mine:
        raise TranslationError(f"policy_steps_not_the_shape:{flow_key}")
    for index, step in enumerate(mine):
        expected = {"mode", "step_key"} if index == 0 else {"mode", "step_key", "delay", "approved_template"}
        keys = set(step) - {"coupon_ordinal"} if index else set(step)
        if keys != expected or step.get("mode") != "freeform" or (index and step.get("approved_template") is not True):
            raise TranslationError(f"policy_step_shape:{step.get('step_key')}")
    return mine


def _deadline(value: object) -> str:
    match = _DAYS.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise TranslationError(f"expires_after_shape:{value!r}")
    return f"{int(match.group(1))}d"


def _window(policy: Mapping[str, Any]) -> tuple[str, str]:
    windows = policy.get("business_windows")
    if not isinstance(windows, list) or len(windows) != 1 or not isinstance(windows[0], Mapping):
        raise TranslationError("business_windows_not_one_window")
    window = windows[0]
    if set(window) != {"start", "end", "days"} or window["days"] != [1, 2, 3, 4, 5, 6, 7]:
        raise TranslationError("business_window_not_every_day")
    start, end = window["start"], window["end"]
    if not (isinstance(start, str) and _HOUR.fullmatch(start) and isinstance(end, str) and _HOUR.fullmatch(end)):
        raise TranslationError("business_window_hour_shape")
    return start, end


def _active_scope(config: Mapping[str, Any], scope_key: str) -> Mapping[str, Any]:
    controls = [c for c in config.get("pilot_runtime_controls") or [] if c.get("scope_key") == scope_key]
    if len(controls) != 1:
        raise TranslationError(f"control_not_one:{scope_key}")
    version = controls[0].get("scope_version")
    scopes = [s for s in config.get("pilot_scope_versions") or []
              if s.get("scope_key") == scope_key and s.get("version") == version]
    if len(scopes) != 1:
        raise TranslationError(f"active_scope_missing:{scope_key}:{version}")
    return scopes[0]


def _policy_of(config: Mapping[str, Any], scope: Mapping[str, Any]) -> Mapping[str, Any]:
    policies = [p for p in config.get("followup_policy_versions") or []
                if p.get("policy_key") == scope.get("policy_key") and p.get("version") == scope.get("policy_version")]
    if len(policies) != 1:
        raise TranslationError(f"policy_missing:{scope.get('policy_key')}:{scope.get('policy_version')}")
    return policies[0]


def _template(manifest: Mapping[str, Any], flow_key: str) -> tuple[str, str]:
    entry = (manifest.get("plantillas") or {}).get(flow_key)
    if not isinstance(entry, Mapping) or not isinstance(entry.get("nombre"), str) or not isinstance(
        entry.get("idioma"), str
    ):
        raise TranslationError(f"manifest_template_missing:{flow_key}")
    return entry["nombre"], entry["idioma"]


def _from_scope(scope: Mapping[str, Any], policy: Mapping[str, Any], event_type: str,
                manifest: Mapping[str, Any],
                touches: Mapping[str, Sequence[Touch | None]] | None = None) -> Translated:
    if event_type not in EVENTS:
        raise TranslationError(f"event_type_unknown:{event_type}")
    event, flow_key = EVENTS[event_type]
    audience = scope.get("audience_mode")
    if audience not in AUDIENCES:
        raise TranslationError(f"audience_mode_unknown:{audience}")
    if scope.get("timezone") != policy.get("timezone"):
        raise TranslationError("scope_and_policy_time_zones_differ")
    policy_steps = _flow_steps(policy, flow_key)
    if policy.get("max_automatic_messages") != len(policy_steps):
        raise TranslationError("automatic_messages_and_steps_differ")
    flow_touches = (touches or {}).get(flow_key)
    if len(policy_steps) > 1 and (flow_touches is None or len(flow_touches) != len(policy_steps)):
        raise TranslationError(f"touch_templates_missing:{flow_key}")
    for key in ("max_outbound_request_starts_per_day", "max_outbound_request_starts_total"):
        value = scope.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise TranslationError(f"cap_shape:{key}")
    code = CODE[flow_key]
    template, language = _template(manifest, flow_key)
    grace = _grace_minutes(policy.get("grace_period"))
    start, end = _window(policy)
    sources: dict[str, str] = {}
    steps: list[dict[str, Any]] = []
    if grace:
        steps.append({"esperar": f"{grace}m"})
        sources["/pasos/0/esperar"] = f"{SOURCE_REAL}: followup_policy_versions.grace_period"
    previous_delay = 0
    for index, policy_step in enumerate(policy_steps):
        if index:
            delay = _delay_minutes(policy_step.get("delay"))
            if delay <= previous_delay:
                raise TranslationError(f"step_delay_not_increasing:{policy_step.get('step_key')}")
            steps.append({"esperar": _duration(delay - previous_delay)})
            sources[f"/pasos/{len(steps) - 1}/esperar"] = (
                f"{SOURCE_REAL}: followup_policy_versions.steps[{policy_step.get('step_key')}].delay")
            previous_delay = delay
        touch = flow_touches[index] if flow_touches is not None else None
        step_at = f"/pasos/{len(steps)}"
        if touch is None:
            step = {"plantilla": template, "idioma": language, "variables": list(code["variables"])}
            sources[f"{step_at}/plantilla"] = f"{SOURCE_MANIFEST}: [plantillas].{flow_key}.nombre"
            sources[f"{step_at}/idioma"] = f"{SOURCE_MANIFEST}: [plantillas].{flow_key}.idioma"
        else:
            step = {"plantilla": touch.template, "idioma": touch.language, "variables": list(touch.variables)}
            sources[f"{step_at}/plantilla"] = TOUCHES_SOURCE
            sources[f"{step_at}/idioma"] = TOUCHES_SOURCE
        sources[f"{step_at}/variables"] = SOURCE_CODE if touch is None else touch.variables_from
        personal = touch is not None and touch.personal_coupon
        marked = "coupon_ordinal" in policy_step
        # El primer toque no lleva marca: su cupon lo gobierna el flag (a1_cupon_por_flag).
        if index and personal != marked:
            raise TranslationError(f"touch_coupon_and_policy_differ:{policy_step.get('step_key')}")
        if personal:
            step["boton"] = {"link": "checkout", "cupon": PERSONAL_COUPON}
            sources[f"{step_at}/boton/link"] = TOUCHES_SOURCE
            sources[f"{step_at}/boton/cupon"] = (
                f"{SOURCE_REAL}: followup_policy_versions.steps[{policy_step.get('step_key')}].coupon_ordinal"
                if index else TOUCHES_SOURCE)
        steps.append(step)
    definition: dict[str, Any] = {
        "flujo": flow_key,
        "nombre": code["nombre"],
        "dispara": {"evento": event},
        "entra": {"permiso": AUDIENCES[audience]},
        "pasos": steps,
        "termina": {"si": list(code["si"]), "vence": _deadline(policy.get("expires_after"))},
        "horario": {"zona": scope["timezone"], "desde": start, "hasta": end},
        "topes": {
            "bolsa": scope["scope_key"],
            "por_dia": scope["max_outbound_request_starts_per_day"],
            "total": scope["max_outbound_request_starts_total"],
            "repeticion": copy.deepcopy(dict(code["repeticion"])),
        },
        "prioridad": copy.deepcopy(dict(code["prioridad"])),
    }
    trigger_column = ("source_event_type" if event_type == scope.get("source_event_type")
                      else "additional_source_event_types")
    sources.update({
        "/flujo": f"{SOURCE_REAL}: pilot_scope_versions.{trigger_column} (el flujo de ese evento)",
        "/nombre": SOURCE_CODE,
        "/dispara/evento": f"{SOURCE_REAL}: pilot_scope_versions.{trigger_column}",
        "/entra/permiso": f"{SOURCE_REAL}: pilot_scope_versions.audience_mode",
        "/termina/si": SOURCE_CODE,
        "/termina/vence": f"{SOURCE_REAL}: followup_policy_versions.expires_after",
        "/horario/zona": f"{SOURCE_REAL}: pilot_scope_versions.timezone",
        "/horario/desde": f"{SOURCE_REAL}: followup_policy_versions.business_windows",
        "/horario/hasta": f"{SOURCE_REAL}: followup_policy_versions.business_windows",
        "/topes/bolsa": f"{SOURCE_REAL}: pilot_scope_versions.scope_key",
        "/topes/por_dia": f"{SOURCE_REAL}: pilot_scope_versions.max_outbound_request_starts_per_day",
        "/topes/total": f"{SOURCE_REAL}: pilot_scope_versions.max_outbound_request_starts_total",
        "/topes/repeticion/una_por": SOURCE_CODE,
        "/topes/repeticion/otra_despues_de": SOURCE_CODE,
        "/prioridad/le_gana_a": SOURCE_CODE,
        "/prioridad/no_arranca_mientras": SOURCE_CODE,
    })
    return Translated(definition=definition, sources=sources)


def _has_control(config: Mapping[str, Any], scope_key: str) -> bool:
    return any(c.get("scope_key") == scope_key for c in config.get("pilot_runtime_controls") or [])


def translate_att1(config: Mapping[str, Any], manifest: Mapping[str, Any],
                   schema_blocks: Sequence[Mapping[str, Any]],
                   touches: Mapping[str, Sequence[Touch | None]] | None = None) -> dict[str, Translated]:
    """Los cinco flujos de ATT1, en el orden de la propuesta, con la fuente de cada campo.

    ``touches`` son las plantillas de los toques que no estan en el manifiesto
    (``ATT1_TOUCHES_20261021``); hacen falta solo si una politica tiene mas de
    un paso para su flujo.
    """
    out: dict[str, Translated] = {}
    dedicated = {event: scope_key for event, scope_key in DEDICATED_SCOPES.items() if _has_control(config, scope_key)}
    scope_keys = ["att1-recuperacion", *sorted(set(dedicated.values())), "att1-primer-contacto"]
    for scope_key in scope_keys:
        scope = _active_scope(config, scope_key)
        policy = _policy_of(config, scope)
        events = [scope.get("source_event_type"), *(scope.get("additional_source_event_types") or [])]
        for event_type in events:
            if dedicated.get(event_type, scope_key) != scope_key:
                continue
            translated = _from_scope(scope, policy, event_type, manifest, touches)
            flow_key = translated.definition["flujo"]
            if flow_key in out:
                raise TranslationError(f"flow_repeated:{flow_key}")
            out[flow_key] = translated
    by_block = {block.get("block"): block for block in schema_blocks}
    for block_id, flow_key in FROM_SCHEMA_BLOCKS:
        block = by_block.get(block_id)
        if block is None or block.get("flujo") != flow_key:
            raise TranslationError(f"schema_block_missing:{block_id}")
        out[flow_key] = Translated(
            definition=copy.deepcopy(dict(block["definition"])),
            sources={"": f"{SOURCE_SCHEMA} {block_id} (docs/contracts/flow-definition-v1.md:{block['contract_lines']})"},
        )
    if set(out) != set(ORDER):
        raise TranslationError(f"flows_mismatch:{sorted(out)}")
    return {flow_key: out[flow_key] for flow_key in ORDER}


def att1_definitions_from_config(config: Mapping[str, Any], manifest: Mapping[str, Any],
                                 schema_blocks: Sequence[Mapping[str, Any]],
                                 touches: Mapping[str, Sequence[Touch | None]] | None = None
                                 ) -> dict[str, dict[str, Any]]:
    """Las cinco definiciones de ATT1, sin normalizar, en el orden de la propuesta."""
    return {flow_key: translated.definition
            for flow_key, translated in translate_att1(config, manifest, schema_blocks, touches).items()}
