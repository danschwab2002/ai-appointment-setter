"""La parte pura de la procedencia: digests y recorte de lo agregado.

Lo que estos tests protegen no es el formato sino dos propiedades. La primera:
el digest del contexto identifica lo que el agente vio, asi que no puede
depender del orden de las claves --- si dependiera, dos turnos identicos
parecerian distintos y la comparacion no serviria para nada. La segunda: el
recorte que se guarda en la capa durable NO puede llevar el texto de los
mensajes, porque la transcripcion vive en Chatwoot y duplicarla seria copiar
datos personales sin agregar nada.
"""

from __future__ import annotations

import json

from bridge.agent_provenance import (
    CONTEXT_BUILDER_VERSION,
    build_turn,
    context_added,
    context_digest,
    external_conversation_id,
    turn_digest,
)

_CONTEXTO = {
    "conversation_ref": "186",
    "human_handoff_confirmed": False,
    "known_fields": {
        "person_name": "Gustavo",
        "location": None,
        "role": None,
        "company_name": "",
    },
    "messages": [
        {"actor": "prospect", "text": "Los libros son fisicos?"},
        {"actor": "agent", "text": "Te cuento como es el programa"},
        {"actor": "prospect", "text": "Gracias"},
    ],
}


def test_the_context_digest_ignores_key_order() -> None:
    """Dos veces el mismo contexto da el mismo digest, escrito en otro orden.

    Si el digest dependiera del orden, no serviria para comparar un turno con
    una reproduccion posterior: cualquier diccionario reconstruido desde JSON
    podria salir con las claves en otra secuencia.
    """
    al_revés = dict(reversed(list(_CONTEXTO.items())))
    assert context_digest(_CONTEXTO) == context_digest(al_revés)
    assert len(context_digest(_CONTEXTO)) == 64


def test_the_context_digest_changes_with_the_content() -> None:
    otro = {**_CONTEXTO, "human_handoff_confirmed": True}
    assert context_digest(otro) != context_digest(_CONTEXTO)


def test_each_attempt_is_a_different_turn() -> None:
    """Dos intentos del mismo delivery son dos turnos contra el agente.

    El segundo puede haber corrido con otro prompt si el SOUL cambio en el
    medio, asi que no pueden colapsarse en uno. Lo que no puede pasar es que el
    MISMO intento se cuente dos veces, y de eso se encarga el unique de
    `agent_turn_provenance.turn_digest`.
    """
    digest = context_digest(_CONTEXTO)
    primero = turn_digest(delivery_id="d-1", attempt=1, context_digest_value=digest)
    segundo = turn_digest(delivery_id="d-1", attempt=2, context_digest_value=digest)
    repetido = turn_digest(delivery_id="d-1", attempt=1, context_digest_value=digest)
    assert primero != segundo
    assert primero == repetido
    assert len(primero) == 64


def test_the_conversation_id_is_read_from_the_reference() -> None:
    """`conversation_ref` es el display_id de Chatwoot, en texto.

    Es lo que permite juntar esto con la revision diaria, que indexa por
    `chatwoot_conversation_id`.
    """
    assert external_conversation_id(_CONTEXTO) == 186


def test_a_reference_that_is_not_a_positive_integer_is_not_invented() -> None:
    """Sin conversacion el turno se guarda igual, pero no se inventa el numero."""
    for referencia in ("", "abc", "0", "-3", "12.5", None, 186):
        assert external_conversation_id({"conversation_ref": referencia}) is None
    assert external_conversation_id({}) is None


def test_the_recorded_context_never_carries_the_message_text() -> None:
    """La propiedad que importa: ningun texto de mensaje entra a la capa durable.

    Se comprueba serializando el recorte completo y buscando los textos
    literales, no inspeccionando claves: asi el test sigue valiendo si manana se
    agrega un campo nuevo que por descuido los arrastre.
    """
    recorte = json.dumps(context_added(_CONTEXTO), ensure_ascii=False)
    for texto in (
        "Los libros son fisicos?",
        "Te cuento como es el programa",
        "Gracias",
    ):
        assert texto not in recorte


def test_the_recorded_context_keeps_what_the_bridge_added() -> None:
    recorte = context_added(_CONTEXTO)
    assert recorte["known_fields"] == _CONTEXTO["known_fields"]
    # Cuantos campos venian con valor: la medida de cuanto sabia el agente del
    # lead al contestar. `None` y la cadena vacia no cuentan.
    assert recorte["known_fields_present"] == ["person_name"]
    assert recorte["human_handoff_confirmed"] is False
    assert recorte["message_count"] == 3
    assert recorte["messages_by_actor"] == {"prospect": 2, "agent": 1}
    # El ultimo del fixture es del prospecto: el agente contesto y el lead
    # volvio a escribir.
    assert recorte["last_actor"] == "prospect"
    assert "messages" in recorte["context_keys"]


def test_an_unknown_actor_is_not_counted() -> None:
    """Un actor que el contrato no declara no infla los conteos."""
    recorte = context_added(
        {"messages": [{"actor": "marciano", "text": "hola"}, {"actor": "agent", "text": "hi"}]}
    )
    assert recorte["messages_by_actor"] == {"agent": 1}
    assert recorte["message_count"] == 2
    assert recorte["last_actor"] == "agent"


def test_a_context_without_messages_or_fields_does_not_explode() -> None:
    recorte = context_added({"conversation_ref": "9"})
    assert recorte["message_count"] == 0
    assert recorte["messages_by_actor"] == {}
    assert recorte["last_actor"] is None
    assert recorte["known_fields"] == {}
    assert recorte["known_fields_present"] == []


def test_building_a_turn_assembles_everything_the_rpc_needs() -> None:
    turno = build_turn(
        context=_CONTEXTO,
        delivery_id="delivery-7",
        attempt=1,
        outcome="completed",
        model_requested="agente-comercial",
        model_answered="glm-5.2",
    )
    assert turno.external_conversation_id == 186
    assert turno.context_digest == context_digest(_CONTEXTO)
    assert turno.turn_digest == turn_digest(
        delivery_id="delivery-7", attempt=1, context_digest_value=turno.context_digest
    )
    assert turno.model_answered == "glm-5.2"
    assert turno.settled is True


def test_a_failed_turn_is_still_a_turn() -> None:
    """Un turno que fallo tambien se registra.

    Si solo se anotaran los exitosos, un cambio de prompt que rompe la salida
    del agente no dejaria rastro en la procedencia --- y es justo el caso en el
    que hace falta saber que version estaba corriendo.
    """
    turno = build_turn(
        context=_CONTEXTO,
        delivery_id="delivery-8",
        attempt=3,
        outcome="failed",
        model_requested="agente-comercial",
        model_answered=None,
    )
    assert turno.outcome == "failed"
    assert turno.model_answered is None
    assert turno.settled is True


def test_the_context_builder_version_is_declared() -> None:
    """Se sube cuando cambia la forma del contexto, no el contenido.

    Un turno viejo con otra version no es comparable con uno nuevo, y sin el
    campo no habria forma de saberlo.
    """
    assert CONTEXT_BUILDER_VERSION == "shadow-context-v1"
