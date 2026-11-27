"""El flujo que arranca con la palabra clave de una regla (Z2 3.9, segunda parte de ``keyword-rules-v1``).

Contrato: ``docs/contracts/keyword-rules-v1.md`` §5. Migracion ``20261127400000_flow_engine_keyword_trigger_v1``.

La regla v1 (``keyword_rules``) pone la etiqueta y la nota del turno cuando el primer mensaje de una
conversacion que abrio la persona trae su palabra. Con esto, ademas, si hay un flujo nativo publicado con
``dispara: {palabra_clave: <clave de la regla>}``, el motor lo admite:

1. el turno ya aplico la regla (``KeywordTurn.rule``) sobre el primer mensaje (``KeywordTurn.message_ref``);
2. el bridge mira los frenos de afuera de la base: la salud de Meta de la instancia (el mismo
   ``estado.json`` y el mismo veredicto que la audiencia, ``flow_audience_sweeper.health_verdict``) y el
   candado del portfolio del masivo (``bulk_send_worker.PortfolioLock``: un masivo en curso en otro bridge
   del portfolio, o un freno de Meta de un masivo en las ultimas 24 h, ``start_after_meta_brake_verdict``);
3. le pasa a la admision nativa (``admit_flow_engine_event_v1`` con ``native_keyword``) la conversacion,
   el mensaje, la regla y el numero de WhatsApp de quien escribio (el contacto de la bandeja, el mismo
   del webhook). La base resuelve el contacto desde la identidad de WhatsApp de esa cuenta, elige los
   flujos de la regla y les aplica la puerta comun: la baja, la cohorte, la repeticion, la bolsa, la
   conversacion derivada y la compra. Nadie le pide un dato a la persona.

Despues, cada toque sale por el despacho nativo de siempre: la baja del contacto y la de Chatwoot de ese
movil antes de cada envio, el tope por persona (``FLOW_ENGINE_NATIVE_PERSON_CAP_ENABLED``) y, con
``META_ENFORCEMENT_BRAKES_ENABLED`` (F1), el catalogo con las plantillas retiradas o la instancia
congelada. Nada de eso se rodea aca.

Flags: ``KEYWORD_FLOW_TRIGGER_ENABLED`` (este), con ``KEYWORD_RULES_ENABLED`` y ``FLOW_ENGINE_ENABLED``.
Con cualquiera apagado, ``keyword_flow_trigger_setup`` devuelve None y la regla sigue como hoy (etiqueta y
nota). Prendido falla cerrado al arrancar si falta algo que lo hace seguro.

Decisiones (con su default, tambien en el reporte):

* **D-Z2.1** El flujo nombra la regla (``dispara.palabra_clave``); la regla no nombra al flujo (D-39.1 sigue).
* **D-Z2.8** Si la base no contesta o rechaza, el turno sigue igual (el agente contesta, la etiqueta y la
  nota ya estan): el flujo no arranca para ese mensaje y queda en el log. No se reintenta el turno: un
  reintento repetiria la respuesta del agente por un mensaje automatico.
* **D-Z2.9** La salud de Meta y el candado del portfolio son obligatorios para prenderlo
  (``FLOW_AUDIENCE_META_HEALTH_PATH`` y ``BULK_SEND_PORTFOLIO_LOCK_DIR``), como para la audiencia y el masivo:
  sin ellos no se sabe si se puede mandar y no se manda (fail-closed al arrancar). Un freno no deja fila en la
  base: queda en el log (``keyword_flow_braked``).

Los logs llevan la conversacion, la clave de la regla, el flujo y el motivo; nunca el telefono ni el texto.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bulk_send_brakes import start_after_meta_brake_verdict
from .flow_audience_sweeper import HEALTH_KEY_VARIABLE, HEALTH_PATH_VARIABLE, file_health_reader, health_verdict

__all__ = [
    "EVENT_SOURCE",
    "FLAG",
    "KeywordFlowConfigError",
    "KeywordFlowTrigger",
    "keyword_event",
    "keyword_flow_trigger_setup",
    "portfolio_brake",
]

logger = logging.getLogger(__name__)

CONTRACT = "keyword-rules-v1"
FLAG = "KEYWORD_FLOW_TRIGGER_ENABLED"
LOCK_DIR_VARIABLE = "BULK_SEND_PORTFOLIO_LOCK_DIR"
EVENT_SOURCE = "palabra_clave"
_DESTINATION = re.compile(r"[0-9]{6,20}")

OUTCOME_STARTED = "arranco"
OUTCOME_NOT_STARTED = "no_arranco"
OUTCOME_NO_FLOW = "sin_flujo"
OUTCOME_BRAKED = "frenado"
OUTCOME_FAILED = "fallo"

BRAKE_PORTFOLIO_BUSY = "masivo_en_curso"
BRAKE_PORTFOLIO_META = "freno_meta_24h"
BRAKE_PORTFOLIO_UNREADABLE = "candado_ilegible"
BRAKE_NO_DESTINATION = "sin_destino"


class KeywordFlowConfigError(ValueError):
    """El flag esta prendido y falta algo que lo hace seguro."""


def keyword_event(*, rule_id: str, rule_key: str, conversation_id: int, message_id: int, destination: str,
                  now: datetime, account_id: int, inbox_id: int) -> dict[str, object]:
    """El evento normalizado de la palabra clave. Sin nombre ni mail; el numero es el de quien escribio."""
    return {
        "fuente": EVENT_SOURCE,
        "evento_id": f"{EVENT_SOURCE}:{conversation_id}:{message_id}",
        "regla_id": rule_id,
        "regla_clave": rule_key,
        "momento": now.isoformat(),
        "chatwoot": {"account_id": account_id, "inbox_id": inbox_id, "conversation_id": conversation_id,
                     "message_id": message_id},
        "destino": destination,
    }


def portfolio_brake(lock: Any, *, now: datetime) -> str | None:
    """Por que el candado del portfolio frena un envio nuevo; None si no frena. Nunca lanza (falla cerrado)."""
    try:
        if lock.busy_elsewhere():
            return BRAKE_PORTFOLIO_BUSY
        verdict = start_after_meta_brake_verdict(lock.read_brakes(), now=now)
    except Exception:  # noqa: BLE001 - un candado que no se lee frena (fail-closed)
        return BRAKE_PORTFOLIO_UNREADABLE
    return None if verdict.allows else BRAKE_PORTFOLIO_META


@dataclass
class _Counts:
    started: int = 0
    not_started: int = 0
    no_flow: int = 0
    braked: int = 0
    failed: int = 0


class KeywordFlowTrigger:
    """Le pasa a la admision nativa la conversacion cuyo primer mensaje trajo la palabra de una regla."""

    def __init__(self, *, admit: Callable[..., Awaitable[Sequence[Any]]], instance: Mapping[str, str],
                 account_id: int, inbox_id: int, health: Callable[[], Mapping[str, Any] | None],
                 portfolio: Any, priority_wins: bool = False,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._admit = admit
        self._instance = dict(instance)
        self._account = account_id
        self._inbox = inbox_id
        self._health = health
        self._portfolio = portfolio
        self._priority_wins = priority_wins
        self._clock = clock
        self._counts = _Counts()

    def _brake(self, destination: str, now: datetime) -> str | None:
        if _DESTINATION.fullmatch(destination) is None:
            return BRAKE_NO_DESTINATION
        verdict = health_verdict(self._health(), now=now)
        if not verdict.allows:
            return verdict.brake_reason or "meta_salud:sin_lectura"
        return portfolio_brake(self._portfolio, now=now)

    async def start(self, *, rule: Any, conversation_id: int, message_ref: object, destination: str) -> str:
        """Admite el flujo de la regla para esta conversacion, si lo hay. Nunca lanza."""
        try:
            return await self._start(rule=rule, conversation_id=conversation_id, message_ref=message_ref,
                                     destination=destination)
        except Exception as error:  # noqa: BLE001 - D-Z2.8: el turno sigue
            self._counts.failed += 1
            logger.warning("keyword_flow_failed conversation=%s rule=%s error=%s", conversation_id,
                           getattr(rule, "key", None), type(error).__name__)
            return OUTCOME_FAILED

    async def _start(self, *, rule: Any, conversation_id: int, message_ref: object, destination: str) -> str:
        from .supabase import FlowEngineAdmissionUnavailable, SupabaseError, SupabasePermanentError

        now = self._clock()
        message_id = int(str(message_ref)) if str(message_ref).isdigit() else 0
        brake = self._brake(destination, now) if message_id > 0 else BRAKE_NO_DESTINATION
        if brake is not None:
            self._counts.braked += 1
            logger.info("keyword_flow_braked conversation=%s rule=%s reason=%s", conversation_id, rule.key, brake)
            return OUTCOME_BRAKED
        event = keyword_event(rule_id=rule.rule_id, rule_key=rule.key, conversation_id=conversation_id,
                              message_id=message_id, destination=destination, now=now,
                              account_id=self._account, inbox_id=self._inbox)
        kwargs: dict[str, bool] = {"keyword": True}
        if self._priority_wins:
            kwargs["priority_wins"] = True
        try:
            rows = await self._admit(self._instance, event, **kwargs)
        except SupabasePermanentError as error:
            self._counts.failed += 1
            logger.warning("keyword_flow_rejected conversation=%s rule=%s reason=%s", conversation_id, rule.key,
                           error.reason)
            return OUTCOME_FAILED
        except (FlowEngineAdmissionUnavailable, SupabaseError) as error:
            self._counts.failed += 1
            logger.warning("keyword_flow_unavailable conversation=%s rule=%s error=%s", conversation_id, rule.key,
                           type(error).__name__)
            return OUTCOME_FAILED
        started = False
        not_started = False
        for row in rows:
            logger.info("keyword_flow_admitted conversation=%s rule=%s flow_id=%s outcome=%s reason=%s",
                        conversation_id, rule.key, getattr(row, "flow_id", None), getattr(row, "outcome", None),
                        getattr(row, "reason", None))
            started = started or getattr(row, "outcome", None) == "motor_arranco"
            not_started = not_started or getattr(row, "outcome", None) == "motor_no_arranca"
        if started:
            self._counts.started += 1
            return OUTCOME_STARTED
        if not_started:
            self._counts.not_started += 1
            return OUTCOME_NOT_STARTED
        self._counts.no_flow += 1
        return OUTCOME_NO_FLOW

    def status_line(self) -> str:
        """Para ``/ready``: lo que paso desde que arranco el bridge."""
        c = self._counts
        return (f"enabled,started={c.started},not_started={c.not_started},no_flow={c.no_flow},"
                f"braked={c.braked},failed={c.failed}")


def keyword_flow_trigger_setup(settings: Any, *, keyword_rules: Any, flow_engine_runtime: Any, supabase: Any,
                               environ: Mapping[str, str] | None = None) -> KeywordFlowTrigger | None:
    """El disparador, o None si falta cualquiera de los tres flags. Prendido, falla cerrado.

    ``KEYWORD_FLOW_TRIGGER_ENABLED`` solo hace algo con ``KEYWORD_RULES_ENABLED`` (``keyword_rules`` no es
    None) y ``FLOW_ENGINE_ENABLED`` (``flow_engine_runtime`` no es None); sin cualquiera de los dos la regla
    sigue como hoy. Prendidos los tres, pide la cuenta y la bandeja de Chatwoot, la base, la salud de Meta y
    el candado del portfolio (D-Z2.9).
    """
    if not getattr(settings, "keyword_flow_trigger_enabled", False):
        return None
    if keyword_rules is None or flow_engine_runtime is None:
        logger.warning("keyword_flow_trigger_inactive reason=%s",
                       "keyword_rules_disabled" if keyword_rules is None else "flow_engine_disabled")
        return None
    if environ is None:
        environ = {name: value for name in (HEALTH_PATH_VARIABLE, HEALTH_KEY_VARIABLE, LOCK_DIR_VARIABLE)
                   if (value := os.getenv(name)) is not None}
    account = getattr(settings, "chatwoot_account_id", None)
    inbox = getattr(settings, "chatwoot_inbox_id", None)
    missing = [name for name, value in (
        ("CHATWOOT_ACCOUNT_ID", type(account) is int), ("CHATWOOT_INBOX_ID", type(inbox) is int),
        ("Supabase", supabase is not None and hasattr(supabase, "admit_flow_engine_event")),
        (HEALTH_PATH_VARIABLE, bool(environ.get(HEALTH_PATH_VARIABLE))),
        (LOCK_DIR_VARIABLE, bool(environ.get(LOCK_DIR_VARIABLE))),
    ) if not value]
    if missing:
        raise KeywordFlowConfigError(f"{FLAG} requires {', '.join(missing)}")
    lock_dir = Path(str(environ[LOCK_DIR_VARIABLE]))
    if not lock_dir.is_dir():
        # Como el masivo: una carpeta creada adentro del contenedor seria un candado de un solo bridge.
        raise KeywordFlowConfigError(f"{FLAG} requires {LOCK_DIR_VARIABLE} to exist")
    from .bulk_send_worker import PortfolioLock

    return KeywordFlowTrigger(
        admit=supabase.admit_flow_engine_event,
        instance=flow_engine_runtime.instance,
        account_id=account,
        inbox_id=inbox,
        health=file_health_reader(Path(str(environ[HEALTH_PATH_VARIABLE])),
                                  instance_key=environ.get(HEALTH_KEY_VARIABLE) or None),
        portfolio=PortfolioLock(lock_dir),
        priority_wins=bool(getattr(flow_engine_runtime, "native_priority_wins", False)),
    )
