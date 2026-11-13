"""Abrir el recorrido nativo de una persona de una audiencia (E-484, el ``RunOpener``).

Programado contra la admision nativa del motor de varios mensajes (E-483,
``SupabaseClient.admit_flow_engine_event(instance, flow_event, *, priority_wins=False,
audience=False)`` sobre ``admit_flow_engine_event_v1``, con la fuente de audiencia de
20261112210600, flow-engine-v2 §34). La persona de una lista congelada entra como un
evento mas, con exactamente estas claves (la base rechaza cualquier otra forma con
22023 ``invalid_flow_engine_event``):

    {"fuente": "audiencia", "tipo": "fecha|cada|manual",
     "evento_id": "audiencia:<ocurrencia>:<contacto>", "flujo_id": ..., "version": <entero>,
     "ocurrencia_id": ..., "producto_ref": ..., "contacto_id": ...,
     "momento": <ahora>, "chatwoot": {"account_id", "inbox_id"}, "destino": <telefono>}

``audience=True`` suma ``native_audience`` a la instancia: sin el, la base rechaza el
evento (22023) como antes. Va con FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED, el mismo flag
con el que el validador deja publicar un flujo de audiencia; el armado
(``flow_audience_wiring``) solo construye este opener con ese flag prendido.

La base contesta una fila por flujo: ``motor_arranco`` (con caso, tambien la segunda
vez del mismo evento, ``created = false``), ``motor_no_arranca`` con su ``reason``, o
``sin_flujo`` con ``flow_id`` nulo (otra version activa, otro tipo, otro flujo). El
``evento_id`` es unico por persona y ocurrencia: abrir dos veces (la base vuelve a dar a
quien no se asento) devuelve el mismo recorrido. Desde ahi rige la puerta comun del
motor (horario, topes, repeticion, baja, conversacion viva, salida ``compra``).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from bridge.flow_audience_sweeper import (
    OPEN_NO_FLOW,
    OPEN_NOT_STARTED,
    OPEN_STARTED,
    AudienceFlow,
    AudienceOpenRetry,
    OccurrenceRow,
    ReleasedMember,
)
from bridge.supabase import FlowEngineAdmissionUnavailable, SupabaseError, SupabasePermanentError

logger = logging.getLogger(__name__)

EVENT_SOURCE = "audiencia"

# La firma de ``SupabaseClient.admit_flow_engine_event`` (E-483): instancia, evento y, con
# FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED, ``priority_wins`` y ``audience``.
Admit = Callable[..., Awaitable[Sequence[Any]]]


def audience_event(flow: AudienceFlow, occurrence: OccurrenceRow, member: ReleasedMember, *, now: datetime,
                   chatwoot_account_id: int, chatwoot_inbox_id: int) -> dict[str, object]:
    """El evento normalizado de una persona de la lista. Sin nombre ni mail."""
    return {
        "fuente": EVENT_SOURCE,
        "tipo": occurrence.form,
        "evento_id": f"{EVENT_SOURCE}:{occurrence.occurrence_id}:{member.contact_id}",
        "flujo_id": flow.flow_id,
        "version": flow.version,
        "ocurrencia_id": occurrence.occurrence_id,
        "producto_ref": flow.product_ref,
        "contacto_id": member.contact_id,
        "momento": now.isoformat(),
        "chatwoot": {"account_id": chatwoot_account_id, "inbox_id": chatwoot_inbox_id},
        "destino": member.phone,
    }


class NativeAudienceRunOpener:
    """El ``RunOpener`` del barredor: le pasa cada persona a la admision nativa."""

    def __init__(self, *, admit: Admit, instance: Mapping[str, object], chatwoot_account_id: int,
                 chatwoot_inbox_id: int, priority_wins: bool = False, audience: bool = False,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._admit = admit
        self._instance = dict(instance)
        self._account = chatwoot_account_id
        self._inbox = chatwoot_inbox_id
        self._priority_wins = priority_wins
        # E-483 (AUD, 20261112210600): la admision toma ``fuente: audiencia`` solo con esta clave.
        self._audience = audience
        self._clock = clock

    async def open(self, flow: AudienceFlow, occurrence: OccurrenceRow, member: ReleasedMember) -> str:
        event = audience_event(flow, occurrence, member, now=self._clock(), chatwoot_account_id=self._account,
                               chatwoot_inbox_id=self._inbox)
        kwargs: dict[str, bool] = {}
        if self._priority_wins:
            kwargs["priority_wins"] = True
        if self._audience:
            kwargs["audience"] = True
        try:
            rows = await self._admit(self._instance, event, **kwargs)
        except SupabasePermanentError as error:
            logger.warning("flow_audience_open_rejected flow_key=%s occurrence=%s reason=%s", flow.flow_key,
                           occurrence.occurrence_id, error.reason)
            return OPEN_NOT_STARTED
        except (FlowEngineAdmissionUnavailable, SupabaseError) as error:
            logger.warning("flow_audience_open_unavailable flow_key=%s occurrence=%s error=%s", flow.flow_key,
                           occurrence.occurrence_id, type(error).__name__)
            raise AudienceOpenRetry() from error
        mine = [row for row in rows if getattr(row, "flow_id", None) == flow.flow_id]
        for row in mine:
            logger.info("flow_audience_open flow_key=%s occurrence=%s outcome=%s reason=%s", flow.flow_key,
                        occurrence.occurrence_id, row.outcome, row.reason)
        if any(row.outcome == "motor_arranco" for row in mine):
            return OPEN_STARTED
        if any(row.outcome == "motor_no_arranca" for row in mine):
            return OPEN_NOT_STARTED
        return OPEN_NO_FLOW
