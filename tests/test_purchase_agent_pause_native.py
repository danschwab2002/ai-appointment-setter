"""E-165: con un flujo nativo de compra, la pausa al comprar se saltea solo para quien el flujo tomo.

Hasta la 1.20.5, con un flujo nativo de compra en ``cohorte`` o ``prendido``, la
pausa del agente al comprar (``PURCHASE_AGENT_PAUSE_ENABLED``, E-100) se salteaba
para todo comprador del producto (``reason=native_flow_on``). Con la bienvenida
en cohorte (cinco telefonos de prueba), el resto de los compradores seguia
atendido por el agente. Desde E-165 la pausa va despues de la admision nativa y
se saltea solo si un flujo arranco para ese comprador (``motor_arranco``).

Datos:

* La compra es la captura de ATT1
  ``tests/fixtures/hotmart_cash_payment_approved_att1_20261012.json``, la misma
  de ``test_flow_engine_native_bridge`` (de donde salen la base doble y el
  flujo en cohorte, ``native_bridge_rpc_rows.json`` de PGlite).
* Las filas de la admision tienen la forma que devuelve la base real:
  ``motor_arranco`` con su caso, y ``motor_no_arranca`` con
  ``fuera_de_cohorte`` o ``ya_disparado`` (``validate_flow_engine_native.mjs``,
  ``validate_flow_engine_native_bridge.mjs``), y ``sin_flujo``.
* La pausa es la de ``test_purchase_agent_pause`` (la conversacion 40).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from test_flow_engine_native_bridge import (
    ALLY,
    EVENT_ID,
    FLOW_ID,
    INSTANCE,
    _Base,
    _native_flow,
)
from test_purchase_agent_pause import _pause, _PauseChatwoot

from bridge.flow_engine_runtime import FlowEngineRuntime
from bridge.native_flows import NATIVE_PURCHASE_NOT_STARTED, NATIVE_PURCHASE_STARTED
from bridge.supabase import (
    FlowEngineAdmissionUnavailable,
    FlowEngineNativeAdmissionRow,
    PurchaseAgentPauseLookup,
)
from bridge.worker import ResolutionWorker

STARTED = FlowEngineNativeAdmissionRow(FLOW_ID, "motor_arranco", None, "case-1", "action-1", True)


def _not_started(reason: str) -> FlowEngineNativeAdmissionRow:
    return FlowEngineNativeAdmissionRow(FLOW_ID, "motor_no_arranca", reason, None, None, None)


SIN_FLUJO = FlowEngineNativeAdmissionRow(None, "sin_flujo", None, None, None, None)


class _PauseBase(_Base):
    """La base del worker nativo, con la lectura de la pausa y la fila de la admision elegida."""

    def __init__(self, rows: tuple[FlowEngineNativeAdmissionRow, ...], **kwargs: Any) -> None:
        super().__init__(native=(_native_flow(),), **kwargs)
        self.rows = rows
        self.reads: list[dict[str, object]] = []

    async def admit_flow_engine_event(self, instance: Any, flow_event: dict[str, Any]) -> tuple[Any, ...]:
        self.calls.append("admit")
        self.flow_events.append(flow_event)
        if self.admission == "unavailable":
            raise FlowEngineAdmissionUnavailable("admit_flow_engine_event_v1_unavailable: HTTP 503")
        return self.rows

    async def read_purchase_agent_pause_conversations(self, **kwargs: object) -> PurchaseAgentPauseLookup:
        self.calls.append("pause_read")
        self.reads.append(kwargs)
        return PurchaseAgentPauseLookup("found", (40,))


def _worker(base: _PauseBase, chatwoot: _PauseChatwoot) -> tuple[ResolutionWorker, FlowEngineRuntime]:
    runtime = FlowEngineRuntime(supabase=base, instance=INSTANCE)  # type: ignore[arg-type]
    worker = ResolutionWorker(
        supabase=base,  # type: ignore[arg-type]
        purchase_worker_enabled=True,
        commercial_ally_config=ALLY,
        chatwoot_account_id=ALLY.chatwoot_account_id,
        chatwoot_inbox_id=ALLY.chatwoot_inbox_id,
        flow_engine=runtime,
        purchase_agent_pause=_pause(base, chatwoot),
    )
    return worker, runtime


def _batch(worker: ResolutionWorker) -> None:
    asyncio.run(worker._process_batch())


def test_the_buyer_the_native_flow_took_is_not_paused(caplog: pytest.LogCaptureFixture) -> None:
    # La bienvenida va a la misma conversacion: pausada, no saldria.
    base, chatwoot = _PauseBase((STARTED,)), _PauseChatwoot()
    worker, _ = _worker(base, chatwoot)
    with caplog.at_level(logging.INFO, logger="bridge.worker"):
        _batch(worker)

    assert base.calls.count("apply") == 1 and base.calls.count("admit") == 1
    assert "pause_read" not in base.calls and chatwoot.paused == []
    assert "reason=native_flow_started" in caplog.text
    assert "native_flow_on" not in caplog.text


@pytest.mark.parametrize(
    "rows",
    [(_not_started("fuera_de_cohorte"),), (_not_started("ya_disparado"),), (SIN_FLUJO,)],
    ids=["fuera_de_cohorte", "ya_disparado", "sin_flujo"],
)
def test_a_buyer_the_native_flow_did_not_take_is_paused_as_without_the_flow(
    rows: tuple[FlowEngineNativeAdmissionRow, ...],
) -> None:
    # Maribel con la bienvenida en cohorte: el comprador que no es de la cohorte deja de ser
    # atendido por el agente, como con la pausa sin flujo nativo (E-100).
    base, chatwoot = _PauseBase(rows), _PauseChatwoot()
    worker, _ = _worker(base, chatwoot)
    _batch(worker)

    # Primero la compra, despues la admision y recien despues la pausa.
    assert base.calls.index("apply") < base.calls.index("admit") < base.calls.index("pause_read")
    assert [call["conversation_id"] for call in chatwoot.paused] == [40]
    assert base.finishes == [{"webhook_event_id": EVENT_ID, "done": True}]


def test_a_buyer_the_admission_can_never_take_is_paused() -> None:
    # Una compra sin telefono no puede ir a la bienvenida (descartada:sin_telefono): se pausa por el mail.
    base = _PauseBase((STARTED,))
    base.payload["data"]["buyer"].pop("checkout_phone", None)
    base.payload["data"]["buyer"].pop("phone", None)
    chatwoot = _PauseChatwoot()
    worker, _ = _worker(base, chatwoot)
    _batch(worker)

    assert "admit" not in base.calls
    assert [call["conversation_id"] for call in chatwoot.paused] == [40]


def test_an_unanswered_admission_waits_and_the_queue_pauses_when_no_flow_started(
    caplog: pytest.LogCaptureFixture,
) -> None:
    base = _PauseBase((_not_started("fuera_de_cohorte"),), admission="unavailable")
    chatwoot = _PauseChatwoot()
    worker, runtime = _worker(base, chatwoot)
    with caplog.at_level(logging.INFO, logger="bridge.worker"):
        _batch(worker)
    # Sin respuesta no se sabe si el flujo lo toma: no se pausa todavia.
    assert chatwoot.paused == [] and base.queue == [EVENT_ID]
    assert "reason=native_admission_pending" in caplog.text

    base.admission = "ok"
    worker._native_queue_read_at = None  # type: ignore[attr-defined]
    _batch(worker)
    assert base.calls.count("apply") == 1 and base.calls.count("admit") == 2
    assert [call["conversation_id"] for call in chatwoot.paused] == [40]
    assert base.queue == []
    assert runtime.readiness()["flow_engine_native_queue_pending"] == "1"


def test_an_unanswered_admission_that_then_starts_the_flow_never_pauses() -> None:
    base = _PauseBase((STARTED,), admission="unavailable")
    chatwoot = _PauseChatwoot()
    worker, _ = _worker(base, chatwoot)
    _batch(worker)
    base.admission = "ok"
    worker._native_queue_read_at = None  # type: ignore[attr-defined]
    _batch(worker)

    assert base.calls.count("admit") == 2
    assert "pause_read" not in base.calls and chatwoot.paused == []


def test_the_outcomes_are_the_ones_the_worker_compares() -> None:
    assert (NATIVE_PURCHASE_STARTED, NATIVE_PURCHASE_NOT_STARTED) == ("admitida", "sin_arranque")
