"""El armado del disparo por audiencia en el bridge (E-484): flag, salud, aviso y motor.

Todo lo que ``app.py`` necesita para el disparo por audiencia vive aca, para que
``app.py`` solo lo llame (crear, arrancar, parar, ``/ready`` y la seccion de la
pagina):

* el aviso del freno al canal de alertas de la aliada (``SlackBrakeNotifier``): el
  ``SYS-007`` del catalogo («Automatizacion pausada automaticamente»), con el flujo
  en ``component`` y el motivo (``meta_salud:red``...) en ``reason_code``; uno por
  ocurrencia, sin telefonos;
* la lectura de la salud de Meta de la instancia (P22): el JSON que deja el timer de
  la salud (``meta_salud.py``), montado en el contenedor, en
  ``FLOW_AUDIENCE_META_HEALTH_PATH``, y la clave de la instancia en ese archivo en
  ``FLOW_AUDIENCE_META_HEALTH_KEY`` (``att1``, ``maribel``);
* el ``RunOpener`` del motor nativo (``flow_audience_opener``), solo si el motor
  corre en la instancia (``FLOW_ENGINE_ENABLED``) y con
  ``FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED`` (la admision de la fuente audiencia de
  E-483, 20261112210600); sin los dos, el barredor arma y frena pero no suelta a
  nadie (``/ready``: ``sin_motor``).

Con ``FLOW_AUDIENCE_TRIGGERS_ENABLED`` apagado, ``build_flow_audience`` devuelve
None y nada de esto existe.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bridge.flow_audience_opener import NativeAudienceRunOpener
from bridge.flow_audience_store import BaseAudienceSource, ChatwootLabelIndex, SupabaseAudienceStore
from bridge.flow_audience_sweeper import (
    FLAG,
    HEALTH_KEY_VARIABLE,
    HEALTH_PATH_VARIABLE,
    AudienceSweeper,
    enabled,
    file_health_reader,
)
from slack_correlation.catalog import NotificationCommand

BRAKE_EVENT_CODE = "SYS-007"
_NOTICE_NAMESPACE = b"flow-audience-brake-v1\0"


def brake_notice(*, flow_key: str, occurrence_id: str, reason: str, occurred_at: datetime) -> NotificationCommand:
    """El aviso de una ocurrencia frenada: el flujo y el motivo, sin personas. Uno por ocurrencia."""
    digest = hashlib.sha256(_NOTICE_NAMESPACE + occurrence_id.encode("ascii")).hexdigest()
    return NotificationCommand(
        event_id=str(uuid.UUID(hex=digest[:32])),
        event_code=BRAKE_EVENT_CODE,
        dedupe_key=digest,
        occurred_at=occurred_at,
        reason_code=reason[:80],
        component=f"flow_audience:{flow_key}"[:80],
        state="pending",
        count=1,
    )


class SlackBrakeNotifier:
    """El ``BrakeNotifier`` del barredor, por el productor del conector de Slack de la aliada."""

    def __init__(self, *, producer: Any, clock: Any = None) -> None:
        self._producer = producer
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def braked(self, flow_key: str, occurrence_id: str, reason: str) -> None:
        await self._producer.admit(brake_notice(flow_key=flow_key, occurrence_id=occurrence_id, reason=reason,
                                                occurred_at=self._clock()))


class FlowAudienceConfigError(ValueError):
    """El flag esta prendido y falta algo que lo hace seguro."""


@dataclass
class FlowAudience:
    """Lo que ``app.py`` guarda: el barredor y la tienda (la pagina usa los dos)."""

    sweeper: AudienceSweeper
    store: SupabaseAudienceStore

    async def start(self) -> None:
        await self.sweeper.start()

    async def stop(self) -> None:
        await self.sweeper.stop()

    def readiness(self) -> dict[str, object]:
        return self.sweeper.readiness()


def build_flow_audience(*, environ: Mapping[str, str] | None, supabase_base_url: str | None,
                        supabase_service_role_key: str | None, tenant_ref: str | None, ally_ref: str | None,
                        chatwoot_account_id: int | None, chatwoot_inbox_id: int | None, chatwoot: Any,
                        flow_engine_runtime: Any, supabase: Any, producer: Any) -> FlowAudience | None:
    """El disparo por audiencia de la instancia, o None con el flag apagado.

    ``environ`` None es el entorno del proceso (las tres variables del modulo).
    Prendido exige la base, el manifiesto (tenant, aliada, cuenta y bandeja) y la
    lectura de la salud: sin ella frenaria todo (fail-closed), asi que se corta al
    arrancar en vez de prender algo que nunca manda.
    """
    if environ is None:
        environ = {name: value for name in (FLAG, HEALTH_PATH_VARIABLE, HEALTH_KEY_VARIABLE)
                   if (value := os.getenv(name)) is not None}
    if not enabled(environ):
        return None
    missing = [name for name, value in (
        ("SUPABASE_BASE_URL", supabase_base_url), ("SUPABASE_SERVICE_ROLE_KEY", supabase_service_role_key),
        ("manifiesto: tenant_ref", tenant_ref), ("manifiesto: ally_ref", ally_ref),
        ("manifiesto: chatwoot account", chatwoot_account_id), ("manifiesto: chatwoot inbox", chatwoot_inbox_id),
        (HEALTH_PATH_VARIABLE, environ.get(HEALTH_PATH_VARIABLE)),
    ) if not value]
    if missing:
        raise FlowAudienceConfigError(f"{FLAG} requires {', '.join(missing)}")
    assert supabase_base_url and supabase_service_role_key and tenant_ref and ally_ref
    assert chatwoot_account_id is not None and chatwoot_inbox_id is not None
    store = SupabaseAudienceStore(base_url=supabase_base_url, service_role_key=supabase_service_role_key,
                                  instance={"tenant_ref": tenant_ref, "ally_ref": ally_ref})
    # Las etiquetas salen del listado de conversaciones del cliente de control de Chatwoot.
    lists = chatwoot is not None and callable(getattr(chatwoot, "list_recent_conversations_with_messages", None))
    labels = ChatwootLabelIndex(chatwoot=chatwoot, inbox_id=chatwoot_inbox_id) if lists else None
    opener = None
    # La admision nativa del motor (E-483, AUD: admit_flow_engine_event(..., audience=True)) solo con
    # FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED (``native_audience`` del runtime). Sin el, la base rechazaria
    # cada persona (22023): no hay opener, nadie se suelta y /ready dice sin_motor.
    if flow_engine_runtime is not None and supabase is not None \
            and bool(getattr(flow_engine_runtime, "native_audience", False)):
        opener = NativeAudienceRunOpener(
            admit=supabase.admit_flow_engine_event, instance=flow_engine_runtime.instance, chatwoot_account_id=chatwoot_account_id,
            chatwoot_inbox_id=chatwoot_inbox_id,
            priority_wins=bool(getattr(flow_engine_runtime, "native_priority_wins", False)), audience=True)
    health = file_health_reader(Path(str(environ[HEALTH_PATH_VARIABLE])),
                                instance_key=environ.get(HEALTH_KEY_VARIABLE) or None)
    sweeper = AudienceSweeper(store=store, source=BaseAudienceSource(store=store, labels=labels), health=health,
                              opener=opener,
                              notifier=SlackBrakeNotifier(producer=producer) if producer is not None else None)
    return FlowAudience(sweeper=sweeper, store=store)
