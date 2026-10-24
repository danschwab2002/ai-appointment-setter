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
"""

from __future__ import annotations

from collections.abc import Iterable

HANDOFF_NOTICE_TEXT = (
    "Le paso tu consulta al equipo y te responden por este mismo chat."
)

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
