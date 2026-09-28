"""Con que prompt contesto el agente, en cada turno.

Hasta el 2026-09-28 nada lo guardaba: `daily_feedback_items.release_id` existia
en el esquema desde 20260910000100 y el exportador lo escribia como el literal
'release_lineage_unavailable' con version 0. Del feedback de una revision se
sabia que se dijo y de que dia, no sobre que version del agente.

Este modulo es la parte pura: arma el digest del contexto, el del turno y el
recorte de las informaciones agregadas. Lo impuro --- llamar a Supabase --- vive
en `bridge.supabase`, y el cableado en `bridge.app`.

LO QUE NO ENTRA. El recorte NO lleva el texto de los mensajes: la transcripcion
vive en Chatwoot y duplicarla en la capa durable copiaria datos personales sin
agregar nada. Lo que se guarda es lo que el bridge AGREGO al prompt
(`known_fields`, la bandera de derivacion) y la forma del historial, mas el
digest del JSON completo, que es lo que despues permite probar si una
reproduccion coincide con lo que el agente realmente vio.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

# Se sube cuando cambia la FORMA del contexto que se le manda al agente, o sea
# cuando `_shadow_context` o el enriquecido de `app.py` dejan de producir el
# mismo conjunto de claves. Un turno viejo con otra version no es comparable.
CONTEXT_BUILDER_VERSION = "shadow-context-v1"

_ACTORES = ("prospect", "agent", "team", "system")


def _canonico(valor: object) -> bytes:
    return json.dumps(
        valor, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def context_digest(context: dict[str, object]) -> str:
    """sha256 del contexto exacto que se le mando al agente."""
    return hashlib.sha256(_canonico(context)).hexdigest()


def turn_digest(*, delivery_id: str, attempt: int, context_digest_value: str) -> str:
    """Identidad del turno, estable ante reintentos del mismo intento.

    Lleva el numero de intento a proposito: dos intentos del mismo delivery son
    dos turnos distintos contra el agente, y el segundo puede haber corrido con
    otro prompt si el SOUL cambio en el medio. Lo que no puede pasar es que el
    MISMO intento se cuente dos veces, y de eso se encarga el unique de
    `agent_turn_provenance.turn_digest`.
    """
    return hashlib.sha256(
        _canonico(
            {
                "delivery_id": delivery_id,
                "attempt": attempt,
                "context": context_digest_value,
            }
        )
    ).hexdigest()


def external_conversation_id(context: dict[str, object]) -> int | None:
    """El `conversation_ref` del contexto es el display_id de Chatwoot, en texto.

    Es lo que permite juntar esto con la revision diaria, que indexa por
    `chatwoot_conversation_id`. Si no es un entero positivo se devuelve None: un
    turno sin conversacion se guarda igual, pero no se inventa el numero.
    """
    referencia = context.get("conversation_ref")
    if not isinstance(referencia, str) or not referencia.isdigit():
        return None
    valor = int(referencia)
    return valor if valor > 0 else None


def context_added(context: dict[str, object]) -> dict[str, object]:
    """Lo que el bridge le AGREGO al prompt, sin el texto de los mensajes."""
    mensajes = context.get("messages")
    mensajes = mensajes if isinstance(mensajes, list) else []

    por_actor: dict[str, int] = {}
    ultimo: str | None = None
    for mensaje in mensajes:
        if not isinstance(mensaje, dict):
            continue
        actor = mensaje.get("actor")
        if isinstance(actor, str) and actor in _ACTORES:
            por_actor[actor] = por_actor.get(actor, 0) + 1
            ultimo = actor

    conocidos = context.get("known_fields")
    conocidos = conocidos if isinstance(conocidos, dict) else {}

    return {
        "known_fields": conocidos,
        # Cuantos de esos campos venian con valor: es la medida de cuanto sabia
        # el agente del lead al contestar.
        "known_fields_present": sorted(
            clave for clave, valor in conocidos.items() if valor not in (None, "")
        ),
        "human_handoff_confirmed": bool(context.get("human_handoff_confirmed")),
        "message_count": len(mensajes),
        "messages_by_actor": por_actor,
        "last_actor": ultimo,
        "context_keys": sorted(str(clave) for clave in context),
    }


@dataclass(frozen=True)
class AgentTurn:
    """Un turno del agente, listo para grabarse."""

    external_conversation_id: int | None
    turn_digest: str
    context_digest: str
    context_added: dict[str, object]
    outcome: str
    model_requested: str
    model_answered: str | None

    @property
    def settled(self) -> bool:
        return self.outcome in {"completed", "failed"}


def build_turn(
    *,
    context: dict[str, object],
    delivery_id: str,
    attempt: int,
    outcome: str,
    model_requested: str,
    model_answered: str | None,
) -> AgentTurn:
    digest = context_digest(context)
    return AgentTurn(
        external_conversation_id=external_conversation_id(context),
        turn_digest=turn_digest(
            delivery_id=delivery_id, attempt=attempt, context_digest_value=digest
        ),
        context_digest=digest,
        context_added=context_added(context),
        outcome=outcome,
        model_requested=model_requested,
        model_answered=model_answered,
    )
