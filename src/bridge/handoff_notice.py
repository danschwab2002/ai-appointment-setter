"""F-034: el aviso corto a la persona cuando el agente deriva.

Hasta F-034, al derivar el bridge descartaba la respuesta del agente y a la
persona no le llegaba nada (decision del 15/10, F-033). En ATT1 la mitad de
las derivaciones espero mas de 6 h a que alguien del equipo escribiera (33
derivaciones en 30 dias, leidas el 24/10 en att1-db y Chatwoot).

Dan, 24/10 (decisiones D5, D6 y D7): un aviso corto que nombra al equipo, sin
plazo, sin nombre de persona, asertivo, y que no dice que escribe una IA. Sin
vuelta automatica al agente.

El texto es fijo y lo manda el bridge, no el modelo: no depende de que el
agente se acuerde de una regla y no gasta caracteres del SOUL comun (le
quedan 74 hasta el tope de 20.000 de Hermes).


E-486 (v1.37.0, D1 aprobada por Dan el 12/11): el aviso ya no promete que
"te responden por este mismo chat" (el conocimiento de las dos aliadas prohibe
anunciar que alguien va a responder). Dice que la consulta pasa al equipo
"para que la revise" y, si la derivacion cae fuera del horario de atencion de
la instancia, suma una segunda frase fija con ese horario. El horario sale de
`[atencion]` del manifiesto; sin el bloque, el de por defecto de las aliadas
(09:00 a 20:00, lunes a viernes, en la zona de la instancia). Sin manifiesto
(Johanna), sin horario: solo la primera frase.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

HANDOFF_NOTICE_TEXT = "Le paso tu consulta al equipo para que la revise."

DAY_NAMES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")

# El horario de por defecto de las aliadas (Dan, 12/11): 09:00 a 20:00. Los
# dias no los dijo: lunes a viernes, marcado para revisar (E-486).
DEFAULT_SERVICE_DAYS: tuple[int, ...] = (0, 1, 2, 3, 4)
DEFAULT_SERVICE_START = time(9, 0)
DEFAULT_SERVICE_END = time(20, 0)

# La ciudad que se nombra por zona; otra zona usa su ultimo tramo.
_CITY_BY_ZONE = {
    "America/Mexico_City": "Ciudad de México",
    "America/Argentina/Buenos_Aires": "Buenos Aires",
    "America/Bogota": "Bogotá",
    "America/Lima": "Lima",
    "America/Santiago": "Santiago",
    "America/Guatemala": "Guatemala",
    "Europe/Madrid": "Madrid",
}


def _plain(word: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", word.strip().lower())
        if unicodedata.category(char) != "Mn"
    )


_DAY_BY_PLAIN_NAME = {_plain(name): index for index, name in enumerate(DAY_NAMES)}


def parse_day(name: str) -> int | None:
    """El dia (0 = lunes) de un nombre en espanol, con o sin tilde."""
    return _DAY_BY_PLAIN_NAME.get(_plain(name))


def city_of_zone(time_zone: str) -> str:
    return _CITY_BY_ZONE.get(time_zone, time_zone.rsplit("/", 1)[-1].replace("_", " "))


@dataclass(frozen=True)
class ServiceHours:
    """Cuando atiende el equipo de una instancia ([atencion], E-486)."""

    days: tuple[int, ...]
    start: time
    end: time
    time_zone: str
    city: str

    def is_open(self, now: datetime) -> bool:
        local = now.astimezone(ZoneInfo(self.time_zone))
        return local.weekday() in self.days and self.start <= local.time() < self.end

    def describe_days(self) -> str:
        days = sorted(set(self.days))
        if len(days) == 7:
            return "todos los días"
        if len(days) == 1:
            return f"los {DAY_NAMES[days[0]]}"
        if days == list(range(days[0], days[-1] + 1)):
            return f"de {DAY_NAMES[days[0]]} a {DAY_NAMES[days[-1]]}"
        names = [DAY_NAMES[day] for day in days]
        return "los " + ", ".join(names[:-1]) + " y " + names[-1]

    def sentence(self) -> str:
        return (
            f"El equipo atiende {self.describe_days()} de "
            f"{self.start:%H:%M} a {self.end:%H:%M} (hora de {self.city})."
        )


def default_service_hours(time_zone: str) -> ServiceHours:
    return ServiceHours(
        days=DEFAULT_SERVICE_DAYS,
        start=DEFAULT_SERVICE_START,
        end=DEFAULT_SERVICE_END,
        time_zone=time_zone,
        city=city_of_zone(time_zone),
    )


def current_time() -> datetime:
    """El reloj del aviso. Aparte para que un test lo pueda fijar."""
    return datetime.now(UTC)


def handoff_notice_text(hours: ServiceHours | None, now: datetime) -> str:
    """El aviso: la frase fija y, fuera de horario, cuando atiende el equipo."""
    if hours is None or hours.is_open(now):
        return HANDOFF_NOTICE_TEXT
    return f"{HANDOFF_NOTICE_TEXT} {hours.sentence()}"

# La marca del mensaje en Chatwoot (``appointment_setter_decision``): la
# misma que lee la revision diaria para saber por que salio cada mensaje.
HANDOFF_NOTICE_DECISION = "handoff"


def handoff_notice_already_sent(messages: Iterable[object]) -> bool:
    """Si la conversacion ya tiene un aviso de derivacion del agente.

    El aviso sale una sola vez por conversacion: si la persona vuelve con el
    agente (reanudacion, F-037) y se deriva otra vez, no se le repite. Se
    reconoce por la marca que el bridge le pone al mandarlo; ningun otro
    mensaje del agente sale con la decision ``handoff``.
    """
    for message in messages:
        if not isinstance(message, dict):
            continue
        sender = message.get("sender")
        attributes = message.get("content_attributes")
        if (
            message.get("private") is False
            and message.get("message_type") == 1
            and isinstance(sender, dict)
            and sender.get("type") == "agent_bot"
            and isinstance(attributes, dict)
            and attributes.get("appointment_setter_decision")
            == HANDOFF_NOTICE_DECISION
        ):
            return True
    return False
