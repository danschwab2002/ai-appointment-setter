"""Un «no» claro de la persona: frena lo que nace de nosotros, sin darla de baja.

F-150 (decisiones de Dan del 2026-11-01, D3 y D4 de E-261): si la persona
escribio un rechazo claro despues de nuestra ultima plantilla, no le sale el
seguimiento con cupon (``lead_declined`` en ``followup_discount``) y se cancela
el recordatorio de la ficha que le toca (C3, ``att1_ficha_vence_hoy_01``, en el
despachador). No es una baja: el contacto sigue con su permiso, si vuelve a
escribir el agente la atiende, y lo que inicie ella (una ficha nueva, un pago
rechazado) se atiende igual. La baja la sigue reconociendo ``opt_out`` y es lo
unico que deja a alguien sin mensajes para siempre; esta lista no la toca ni la
reemplaza.

La lista es corta a proposito (D3): sacarle el seguimiento a alguien que iba a
comprar cuesta una venta, asi que solo entran frases que no dejan dudas, y se
mide 7 dias antes de sumar otras. Salen de conversaciones reales de ATT1 del
28/10 (enmascaradas en ``tests/test_lead_decline.py``):

* «Hola buenas tardes. Cambie de opinión. Ya no quiero el curso.. gracias»
  (#542): cuenta.
* «Gracias 🙏, será en otra ocasión Saludos» (#491): cuenta.
* «Mil gracias pero si es ahorita muy caro para mi saludos» (#491): NO cuenta.
  Es una objecion de precio, que el agente atiende (le ofrece meses sin
  intereses o efectivo).

La forma es la de ``opt_out``: el mensaje se parte en clausulas, y cuenta solo
si una clausula es el rechazo, afirmado (no preguntado ni citado), y todas las
demas son relleno (un saludo, un gracias, una disculpa). «No me interesa por
ahora, tal vez el mes que viene» no cuenta: tiene una clausula que no es
relleno, y ese «mas adelante» es de F-046 (el estado comercial), no de esta
lista.

«No gracias» solo cuenta como el primer mensaje de la persona despues de
nuestra plantilla: es la respuesta a la oferta. Mas adelante en la charla suele
contestar «¿tenes alguna otra duda?», y ahi no es un rechazo.

Lo reemplaza el estado comercial en cada turno (F-046), que es la correccion
decidida el 17/10.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

# Los mismos helpers que la baja, para que las dos listas lean el texto igual:
# minusculas, sin tildes, solo letras y numeros, partido en clausulas.
from bridge.opt_out import (
    MAX_MESSAGE_CHARS,
    _clauses,
    _is_question,
    _is_quoted_or_referenced,
    _normalize,
)

__all__ = [
    "LEAD_DECLINED",
    "LeadDeclineMatch",
    "detect_lead_decline",
]

# El motivo, el mismo en el seguimiento y en el recordatorio de la ficha: es lo
# que se cuenta en la medicion de 7 dias (D3).
LEAD_DECLINED = "lead_declined"


@dataclass(frozen=True, slots=True)
class LeadDeclineMatch:
    rule_key: str
    message_index: int


# Lo que puede rodear al rechazo en la misma clausula o en otra: el relleno de
# la baja mas «saludos», «bendiciones» y parecidos. Nunca una razon, una fecha
# ni una pregunta.
_FILLER_WORDS = (
    r"(?:hola|buenas|buenos dias|buen dia|buenas tardes|buenas noches|gracias"
    r"|muchas gracias|mil gracias|gracias de todos modos|gracias igual"
    r"|por favor|porfa|saludos|un saludo|disculpa|disculpe|disculpen|perdon"
    r"|bendiciones|que (?:tengas|tenga|tengan) (?:buen dia|buena tarde|buena noche|lindo dia)"
    r"|ok|okay|listo|bueno)"
)
_PREFIX = rf"(?:{_FILLER_WORDS} )*"
_SUFFIX = rf"(?: {_FILLER_WORDS})*"
_FILLER = re.compile(rf"(?:{_FILLER_WORDS}(?: {_FILLER_WORDS})*)?")

_OBJECT = r"(?:el|la|tu|su) (?:curso|programa|producto|inscripcion|compra)"

# (rule_key, patron, solo como primer mensaje despues de la plantilla)
_RULES: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    (
        "not_interested",
        re.compile(
            _PREFIX
            + r"(?:la verdad |pues )?(?:ya )?(?:no me interesa(?: mas)?(?: " + _OBJECT + r")?"
            + r"|no estoy interesad[oa](?: en " + _OBJECT + r")?)"
            + _SUFFIX
        ),
        False,
    ),
    (
        "no_longer_wants",
        re.compile(
            _PREFIX
            + r"(?:ya no (?:lo |la )?quiero(?: " + _OBJECT + r")?"
            + r"|no quiero " + _OBJECT + r")"
            + _SUFFIX
        ),
        False,
    ),
    (
        "changed_mind",
        re.compile(_PREFIX + r"(?:ya )?cambie de (?:opinion|idea|parecer)" + _SUFFIX),
        False,
    ),
    (
        "another_time",
        re.compile(
            _PREFIX
            + r"(?:sera|seria|mejor|lo dejo|lo dejamos|quizas|tal vez)? ?(?:en|para) otra ocasion"
            + _SUFFIX
        ),
        False,
    ),
    (
        "no_thanks",
        re.compile(_PREFIX + r"no gracias" + _SUFFIX),
        True,
    ),
)


def _match_rule(clause: str, *, first: bool) -> str | None:
    normalized = _normalize(clause)
    for rule_key, pattern, first_only in _RULES:
        if first_only and not first:
            continue
        if pattern.fullmatch(normalized):
            return rule_key
    return None


def _match_message(message: str, *, first: bool) -> str | None:
    found: str | None = None
    clauses = _clauses(message)
    # «No, gracias» se parte en dos clausulas: se lee entera antes de partirla.
    if first and _normalize(message) == "no gracias":
        return "no_thanks"
    for clause in clauses:
        if _is_quoted_or_referenced(clause):
            return None
        rule = None if _is_question(clause) else _match_rule(clause, first=first)
        if rule is not None:
            found = found or rule
        elif _FILLER.fullmatch(_normalize(clause)) is None:
            return None
    return found


def detect_lead_decline(messages: Sequence[object]) -> LeadDeclineMatch | None:
    """El primer rechazo claro entre los mensajes de la persona, o None.

    ``messages`` son los textos de la persona despues de nuestra ultima
    plantilla, en orden. Lo que no es texto, lo citado y lo demasiado largo no
    cuenta.
    """
    for index, message in enumerate(messages):
        if (
            not isinstance(message, str)
            or len(message) > MAX_MESSAGE_CHARS
            or _is_quoted_or_referenced(message)
        ):
            continue
        rule_key = _match_message(message, first=index == 0)
        if rule_key is not None:
            return LeadDeclineMatch(rule_key=rule_key, message_index=index)
    return None
