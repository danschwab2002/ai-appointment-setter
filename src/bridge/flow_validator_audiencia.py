"""Las reglas 2 y 3 del disparo por audiencia (E-484, H6), fuera de ``flow_validator``.

Contrato: ``docs/contracts/flow-audience-trigger-v1.md``.

Vive aparte a proposito: ``flow_validator.py`` lo esta cambiando el tramo E-483 (motor
nativo de varios toques) en paralelo. Este modulo trae los tres codigos nuevos y una
funcion pura que los produce; el enganche en ``flow_validator`` es de pocas lineas y se
hace al integrar (punto de choque anotado en el PR):

* sumar ``AUDIENCE_CODES`` a ``_VALIDATOR_CODES`` (el catalogo cerrado);
* en ``_rule2``: contar las claves con ``flow_definition.TRIGGER_KEYS`` en vez de
  ``("evento", "silencio")`` y emitir ``rule2_findings``;
* en ``_rule3``: con ``trigger.is_audience``, emitir ``rule3_findings`` y no mirar la
  tabla de permisos de evento y silencio;
* un caso negativo y uno positivo por codigo en ``CASES`` de ``test_flow_validator`` y
  regenerar ``tests/fixtures/flow_store/validator_results_cases.json``.

Hasta ese enganche, un flujo con disparo por audiencia ya pasa la regla 1 (la forma,
``flow_definition``) y vuelve con ``motor_nativo:disparador`` («todavia no») de la 14:
nadie lo puede publicar como corriendo.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .flow_audience import AUDIENCE_FORMS, PERMISSION_BY_STATUS
from .flow_definition import (
    KIND_FORM,
    PERMISSIONS,
    TRIGGER_KEYS,
    CodeSpec,
    Finding,
    Segment,
    finding_path,
    trigger_of,
)

AUDIENCE_REQUIRES_PARA = CodeSpec(
    code="audience_requires_para", rule=2, kind=KIND_FORM,
    reason="Un envío por fecha, cada cierto tiempo o manual dice a quién le llega, en para.",
    alternative="Sumar para con es: prospecta o clienta.",
)
PARA_REQUIRES_AUDIENCE_TRIGGER = CodeSpec(
    code="para_requires_audience_trigger", rule=2, kind=KIND_FORM,
    reason="para y ritmo van solo con un envío por fecha, cada cierto tiempo o manual.",
    alternative="Sacar para y ritmo; un evento o un silencio ya dicen a quién le llega.",
)
# D6: una difusion solo a quien dejo el formulario con permiso (prospecta) o a una clienta con el OK del
# cliente (compra); conversacion no alcanza: escribirnos una vez no es aceptar mensajes de marketing.
AUDIENCE_PERMISSION_MISMATCH = CodeSpec(
    code="audience_permission_mismatch", rule=3, kind=KIND_FORM,
    reason="Un envío a una audiencia va a prospectas con permiso formulario o a clientas con permiso compra; "
           "conversación no alcanza.",
    alternative="Usar permiso formulario con es: prospecta, o permiso compra con es: clienta.",
)
AUDIENCE_CODES: tuple[CodeSpec, ...] = (
    AUDIENCE_REQUIRES_PARA, PARA_REQUIRES_AUDIENCE_TRIGGER, AUDIENCE_PERMISSION_MISMATCH,
)


def rule2_findings(definition: Mapping[str, Any], base: tuple[Segment, ...] = ()) -> list[Finding]:
    """La 2 del disparo por audiencia: ``para`` con el disparo, y ``para``/``ritmo`` solo con el.

    Con mas de un disparador no dice nada: eso es ``trigger_not_exactly_one`` del validador.
    """
    trigger = definition.get("dispara")
    if not isinstance(trigger, Mapping):
        return []
    present = [key for key in TRIGGER_KEYS if key in trigger]
    if len(present) != 1:
        return []
    found: list[Finding] = []
    if present[0] in AUDIENCE_FORMS:
        if "para" not in definition:
            found.append(AUDIENCE_REQUIRES_PARA.at(finding_path(base + ("para",))))
        return found
    for key in ("para", "ritmo"):
        if key in definition:
            found.append(PARA_REQUIRES_AUDIENCE_TRIGGER.at(finding_path(base + (key,))))
    return found


def rule3_findings(definition: Mapping[str, Any], base: tuple[Segment, ...] = ()) -> list[Finding]:
    """La 3 del disparo por audiencia (D6): el permiso que corresponde a ``para.es``."""
    trigger = trigger_of(definition)
    if trigger is None or not trigger.is_audience:
        return []
    entry = definition.get("entra")
    permission = entry.get("permiso") if isinstance(entry, Mapping) else None
    if not (isinstance(permission, str) and permission in PERMISSIONS):
        return []
    para = definition.get("para")
    status = para.get("es") if isinstance(para, Mapping) else None
    expected = PERMISSION_BY_STATUS.get(status) if isinstance(status, str) else None
    if permission == "conversacion" or (expected is not None and permission != expected):
        return [AUDIENCE_PERMISSION_MISMATCH.at(finding_path(base + ("entra", "permiso")))]
    return []


def audience_findings(definition: Mapping[str, Any], base: tuple[Segment, ...] = ()) -> list[Finding]:
    """Las dos reglas juntas, en orden."""
    return rule2_findings(definition, base) + rule3_findings(definition, base)
