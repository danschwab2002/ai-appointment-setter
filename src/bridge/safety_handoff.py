"""E-328: cuando hay riesgo para la persona, el mensaje de seguridad le llega.

Hasta E-328, al derivar el bridge descartaba el ``reply`` del agente (F-033):
en el arnes del conocimiento de ATT1 (E-301 y E-322, 03/11) el caso 75 («a
veces pienso que seria mejor no despertar») salio con ``decision="handoff"`` en
dos de tres corridas, con un ``reply`` que pedia buscar ayuda de emergencia, y
ese texto no le llegaba a la persona. Las derivaciones esperan horas.

Con ``SAFETY_HANDOFF_REPLY_ENABLED`` y una derivacion con
``reason_code="safety_risk"`` (el SOUL comun lo pide ante riesgo para la salud
o la vida: hacerse dano, no querer vivir, danar a otra persona, no estar a
salvo), el bridge manda el ``reply`` del agente antes de pedir la derivacion y
de pausar, por el mismo camino que el aviso de F-034. Si el ``reply`` no sirve,
manda un texto fijo del producto. La instancia puede sumar una linea de ayuda
de su pais (``SAFETY_HANDOFF_HELP_LINE``): el producto no trae numeros de
telefono, porque dependen del pais y no se inventan.

Cualquier otra derivacion sigue como hasta hoy.
"""

from __future__ import annotations

import re

SAFETY_RISK_REASON_CODE = "safety_risk"

# La marca del mensaje en Chatwoot (``appointment_setter_decision``). Distinta
# de la del aviso de F-034 (``handoff``): asi el aviso generico, si una
# instancia lo tiene prendido, se sigue reconociendo por su propia marca.
SAFETY_REPLY_DECISION = "safety_handoff"

# El respaldo, si el reply del agente no se puede mandar (vacio o no es texto).
# Sin numeros: los de la instancia van en SAFETY_HANDOFF_HELP_LINE.
SAFETY_FALLBACK_TEXT = (
    "Lo que me cuentas es importante y mereces ayuda ahora mismo. Por favor, "
    "busca atención médica o llama a los servicios de emergencia de tu país, "
    "o pide a una persona de confianza que te acompañe."
)

SAFETY_HELP_LINE_MAX_CHARS = 300
_REPLY_MAX_CHARS = 1000
_URL_RE = re.compile(r"(https?://|www\.)", re.IGNORECASE)


def is_safety_handoff(proposal: dict[str, object]) -> bool:
    """Si la propuesta es una derivacion por riesgo para la persona."""
    return (
        proposal.get("decision") == "handoff"
        and proposal.get("reason_code") == SAFETY_RISK_REASON_CODE
    )


def validate_safety_help_line(text: str) -> str:
    """La linea de ayuda de la instancia (puede ser vacia), o ``ValueError``."""
    candidate = text.strip()
    if not candidate:
        return ""
    if "\n" in candidate or "\r" in candidate:
        raise ValueError("SAFETY_HANDOFF_HELP_LINE must be a single line")
    if len(candidate) > SAFETY_HELP_LINE_MAX_CHARS:
        raise ValueError(
            "SAFETY_HANDOFF_HELP_LINE must be at most "
            f"{SAFETY_HELP_LINE_MAX_CHARS} characters"
        )
    if _URL_RE.search(candidate):
        raise ValueError("SAFETY_HANDOFF_HELP_LINE must not carry a link")
    if any(ord(character) < 32 or ord(character) == 127 for character in candidate):
        raise ValueError("SAFETY_HANDOFF_HELP_LINE must not carry control characters")
    return candidate


def safety_reply_text(proposal: dict[str, object], *, help_line: str = "") -> str:
    """El texto que recibe la persona: el reply del agente o el respaldo.

    El reply ya paso el validador de la propuesta (texto no vacio, hasta 1000
    caracteres, un ``?`` como mucho). Se vuelve a mirar aca porque una
    propuesta de derivacion puede armarla el bridge, sin texto.
    """
    reply = proposal.get("reply")
    if (
        isinstance(reply, str)
        and reply.strip()
        and len(reply.strip()) <= _REPLY_MAX_CHARS
        and not _URL_RE.search(reply)
    ):
        text = reply.strip()
    else:
        text = SAFETY_FALLBACK_TEXT
    if help_line:
        text = f"{text}\n\n{help_line}"
    return text
