"""Lo que decide hoy el despachador, con dobles: la vara del motor (T1, D1 a D5).

Diseno del motor de flujos (pieza 2 de la 1b), §3.2 («Lo que no se genera en
PGlite: el despachador») y §5.3. La cadena de PGlite genera los 25 casos de hoy
(``legacy_recovery_cases.json``), pero lo que decide ``DurableDispatcher`` vive
en Python y no lo ve ninguna base. Estas pruebas no prueban el motor: fijan lo
que hace hoy el despachador, que el motor reusa entero (la capa ``despacho`` de
§2.8), para que T2 se mida contra esto. Las lineas son de ``7c0a61b``.

* **D1.** El nombre rechazado cierra sin reintento (``worker.py:1322-1338``). Es
  la excepcion 5 de §1.7 (``nombre_inseguro_cierra``) y la entrada de la 5b: sin
  proximo intento, la base deja la accion ``permanent_failed`` y el caso abierto
  para siempre (eso lo fija G14 en PGlite).
* **D2.** El arranque rechazado (``PilotRequestStartRejectedError``) deja el
  intento reservado y sigue con el resto del lote (``:1541-1563``).
* **D3.** El fallo antes del pedido se reintenta a un minuto de la hora del fallo
  (``:895-900``).
* **D4.** El incierto no se repite: ``delivery_unknown``, sin proximo intento y
  con 15 minutos para conciliar (``:1564-1588``, ``:1790-1817`` y
  ``:1844-1883``).
* **D5.** El lease de 5 minutos (``:706``), que el bridge de ATT1 no cambia.

Datos:

* Los dobles (``_Authority``, ``_Chatwoot``, ``_dispatcher`` y
  ``_TwoActionsAuthority``) y los nombres que no son un nombre (``_NOT_A_NAME``)
  son los de ``test_durable_dispatcher_approved_template.py``, importados como
  importan otras pruebas del repo.
* El manifiesto de ATT1 (``tests/fixtures/instances/att1/``) y el catalogo
  capturado de la bandeja 11 del 01/10
  (``chatwoot_inbox_11_message_templates_20261001.json``), con las tres
  plantillas ``att1_*`` en ``es_MX``: lo que sale es lo que saldria en ATT1.
* Las respuestas de Chatwoot que no son el catalogo siguen el precedente inline
  de ese archivo (sin captura, deuda de A0). El 503 del envio lleva el mismo
  cuerpo que el doble usa para el catalogo ilegible.
* Los motivos de rechazo del arranque son los que devuelve
  ``authorize_lancemos_pilot_request_start`` (``20260930000300``) y los dos
  arranques portables (``20261001000100``), con su linea al lado.
* D5 arma ademas el bridge por el camino del contenedor, con el set de
  variables de produccion de ATT1 de ``test_att1_production_settings.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from bridge.supabase import (
    PilotRequestStartRejectedError,
    ScheduledAction,
    SupabaseClient,
    SupabaseCommittedResponseError,
    SupabaseError,
)
from test_att1_production_settings import OUTBOUND_FLOWS, _start
from test_durable_dispatcher_approved_template import (
    ATT1_CATALOG,
    ATT1_TEMPLATE_NAMES,
    FINAL_NOW,
    FORM_ANCHOR,
    NOW,
    WITH_FORM_TEMPLATE,
    _NOT_A_NAME,
    _Authority,
    _Chatwoot,
    _dispatcher,
    _run,
    _TwoActionsAuthority,
)

# Los dos flujos de la primera tajada vertical, con una oferta del binding de
# ATT1 cada uno (las mismas que usa el archivo de los dobles).
T1_TRIGGERS = [
    pytest.param("cart_abandonment", "gopi6lh7", id="carrito"),
    pytest.param("payment_failure", "2uafw5bg", id="pago fallido"),
]
# La excepcion 5 toca tambien al primer contacto tras el formulario (§1.7).
EXCEPTION_5_TRIGGERS = [
    *T1_TRIGGERS,
    pytest.param(FORM_ANCHOR, "bmaztyhg", id="primer contacto"),
]

# El reloj de los dobles: la toma corre en NOW (15:00) y todo lo que pasa
# despues de componer, en FINAL_NOW (15:01). Las horas van escritas, no
# calculadas con la misma cuenta que el codigo: son la vara.
RETRY_AT = "2026-09-30T15:02:00+00:00"
RECONCILE_BY = "2026-09-30T15:16:00+00:00"

# El RPC de arranque de cada ancla (supabase.py:6525-6539), para el mensaje
# exacto del error que levanta el cliente real.
START_RPC = {
    "cart_abandonment": "mark_lancemos_pilot_request_started",
    "payment_failure": "mark_portable_payment_failure_request_started",
}


def _messages(chatwoot: _Chatwoot) -> list[dict[str, Any]]:
    return chatwoot.posts("/conversations/200/messages")


# --------------------------------------------------- D1: el nombre rechazado


@pytest.mark.parametrize("greeting", [True, False], ids=["con saludo", "sin saludo"])
@pytest.mark.parametrize("name", _NOT_A_NAME)
@pytest.mark.parametrize(("anchor_type", "offer_code"), EXCEPTION_5_TRIGGERS)
def test_d1_a_refused_name_closes_the_action_without_a_retry(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    anchor_type: str,
    offer_code: str,
    name: str,
    greeting: bool,
) -> None:
    # El nombre del caso no cambia solo: cierra en el primer intento, antes del
    # catalogo y de cualquier llamada a Chatwoot. Sin next_attempt_at la base
    # cierra la accion permanent_failed y no toca el caso (la 5b).
    authority = _Authority(offer_code=offer_code, anchor_type=anchor_type, buyer_name=name)
    chatwoot = _Chatwoot(ATT1_CATALOG)

    with caplog.at_level(logging.WARNING, logger="bridge.worker"):
        decisions = _run(
            _dispatcher(
                authority, chatwoot, tmp_path, greeting=greeting, template=WITH_FORM_TEMPLATE
            )
        )

    assert decisions[-1].decision == "execute"
    assert authority.events == ["reevaluate", "reserve"]
    assert chatwoot.requests == []
    [finalization] = authority.finalizations
    assert finalization["outcome"] == "failed_before_request"
    assert finalization["reason_code"] == "template_parameters_missing"
    assert finalization["next_attempt_at"] is None
    assert finalization["reconciliation_deadline"] is None
    assert finalization["now"] == FINAL_NOW
    assert not (tmp_path / "meta-effects").exists()
    assert "approved_template_name_refused" in caplog.text
    assert name not in caplog.text


# ------------------------------------------------ D2: el arranque rechazado

# Los motivos con que la base rechaza el arranque del carrito y del pago
# fallido, y el caso generado que los fija en PGlite.
START_REJECTIONS = [
    # 20260930000300:635-636
    pytest.param("pilot_daily_budget_exhausted", id="tope del dia (G4)"),
    # 20260930000300:625-626
    pytest.param("pilot_total_budget_exhausted", id="tope total (G6)"),
    # 20260930000300:490-491
    pytest.param("pilot_scope_version_mismatch", id="cambio de version (G21)"),
    # 20260930000300:496-497
    pytest.param("pilot_runtime_not_armed", id="flujo pausado (G23)"),
    # 20261001000100:1460-1468 (carrito) y :1676-1684 (pago fallido)
    pytest.param("pilot_chatwoot_opt_out_stop", id="baja de Chatwoot"),
]


@pytest.mark.parametrize("reason", START_REJECTIONS)
@pytest.mark.parametrize(("anchor_type", "offer_code"), T1_TRIGGERS)
def test_d2_a_rejected_start_leaves_the_attempt_reserved_and_goes_on_with_the_batch(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    anchor_type: str,
    offer_code: str,
    reason: str,
) -> None:
    authority = _TwoActionsAuthority(
        rejection=PilotRequestStartRejectedError(reason),
        anchor_type=anchor_type,
        offer_code=offer_code,
    )
    chatwoot = _Chatwoot(ATT1_CATALOG)

    with caplog.at_level(logging.WARNING, logger="bridge.worker"):
        decisions = _run(_dispatcher(authority, chatwoot, tmp_path, template=WITH_FORM_TEMPLATE))

    # Sigue con el lote: las dos acciones se intentan y la segunda sale.
    assert authority.starts == ["action-att1", "action-att1-2"]
    assert [decision.action_id for decision in decisions] == ["action-att1", "action-att1-2"]
    [sent] = _messages(chatwoot)
    assert sent["template_params"]["name"] == ATT1_TEMPLATE_NAMES[anchor_type]
    [accepted] = authority.acceptances
    assert accepted["action_id"] == "action-att1-2"
    # El intento rechazado queda reservado: ninguna finalizacion. Lo resuelve
    # el lease siguiente, con la reevaluacion o el vencimiento.
    assert authority.finalizations == []
    # El motivo queda solo en el log, con ids y el codigo.
    [warning] = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("durable_request_start_rejected")
    ]
    assert warning == (
        "durable_request_start_rejected action_id=action-att1 "
        f"attempt_id=attempt-of-action-att1 anchor={anchor_type} reason={reason}"
    )


# ------------------------------------------ D3: el fallo antes del pedido


@pytest.mark.parametrize(
    ("offer_override", "inbox_status", "gate_open", "reason", "raises"),
    [
        # :1080-1088, el catalogo lo puede arreglar Chatwoot.
        pytest.param(
            None, 503, True, "approved_template_unavailable", None, id="catalogo ilegible"
        ),
        # :1518-1525, el gate final de Meta cerrado.
        pytest.param(None, 200, False, "final_meta_gate_closed", None, id="gate final cerrado"),
        # :1317-1334, una oferta fuera del binding (la de GHL): cierra el
        # intento y corta el lote.
        pytest.param(
            "83utgyow",
            200,
            True,
            "pre_request_failed",
            "followup_recipient_not_allowlisted",
            id="oferta fuera del binding",
        ),
    ],
)
@pytest.mark.parametrize(("anchor_type", "offer_code"), T1_TRIGGERS)
def test_d3_a_failure_before_the_request_is_retried_a_minute_after_it(
    tmp_path: Path,
    anchor_type: str,
    offer_code: str,
    offer_override: str | None,
    inbox_status: int,
    gate_open: bool,
    reason: str,
    raises: str | None,
) -> None:
    # Un minuto desde la hora del fallo (el reloj, 15:01), no desde la toma
    # (15:00). La base lo concede mientras queden max_execution_retries y la
    # accion no venza; eso ya es SQL.
    authority = _Authority(offer_code=offer_override or offer_code, anchor_type=anchor_type)
    chatwoot = _Chatwoot(ATT1_CATALOG, inbox_status=inbox_status)
    dispatcher = _dispatcher(
        authority, chatwoot, tmp_path, gate_open=gate_open, template=WITH_FORM_TEMPLATE
    )

    if raises is None:
        _run(dispatcher)
    else:
        with pytest.raises(SupabaseError, match=raises):
            _run(dispatcher)

    [finalization] = authority.finalizations
    assert finalization["outcome"] == "failed_before_request"
    assert finalization["reason_code"] == reason
    assert finalization["now"] == FINAL_NOW
    assert finalization["next_attempt_at"] == RETRY_AT
    assert finalization["reconciliation_deadline"] is None
    assert "request_started" not in authority.events
    assert _messages(chatwoot) == []


# ---------------------------------------------------- D4: el incierto


class _ChatwootMessageUnavailable(_Chatwoot):
    """El envio de la plantilla responde 503: no se sabe si Chatwoot la mando."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/conversations/200/messages"):
            body = json.loads(request.content) if request.content else None
            self.requests.append((request.method, request.url.path, body))
            return httpx.Response(503, json={"error": "unavailable"})
        return super().handler(request)


class _StartAnsweredWithoutRequestStarted(_Authority):
    """El arranque se confirmo en la base pero la fila no dice request_started.

    Es lo que levanta el cliente real (supabase.py:6573 y :6614): el RPC
    respondio 200, el arranque pudo haber quedado, y no se sabe.
    """

    async def mark_followup_request_started(self, **kwargs: object) -> Any:
        self.events.append("request_started")
        operation = START_RPC[str(kwargs["anchor_type"])]
        raise SupabaseCommittedResponseError(f"{operation}_invalid_row")


class _AcceptanceNotRecorded(_Authority):
    """Chatwoot acepto el mensaje y la base no pudo guardar la aceptacion.

    El error tiene la forma del cliente real (supabase.py:6714).
    """

    async def record_and_finalize_followup_acceptance(self, **kwargs: object) -> object:
        self.events.append("acceptance_failed")
        raise SupabaseError("record_and_finalize_followup_acceptance_failed: HTTP 400")


@pytest.mark.parametrize(
    ("authority_type", "chatwoot_type", "reason", "posted", "remote_message_id", "raises"),
    [
        # :1564-1588: nada salio, pero el arranque pudo haber quedado.
        pytest.param(
            _StartAnsweredWithoutRequestStarted,
            _Chatwoot,
            "request_start_response_invalid",
            0,
            None,
            SupabaseCommittedResponseError,
            id="arranque sin request_started",
        ),
        # :1789-1816: Chatwoot no contesto el envio.
        pytest.param(
            _Authority,
            _ChatwootMessageUnavailable,
            "sender_result_inconclusive",
            1,
            None,
            None,
            id="envio sin respuesta",
        ),
        # :1843-1882: Chatwoot lo acepto y la base no guardo la aceptacion.
        pytest.param(
            _AcceptanceNotRecorded,
            _Chatwoot,
            "acceptance_finalization_failed",
            1,
            "888",
            None,
            id="aceptacion sin guardar",
        ),
    ],
)
@pytest.mark.parametrize(("anchor_type", "offer_code"), T1_TRIGGERS)
def test_d4_an_uncertain_delivery_is_never_repeated(
    tmp_path: Path,
    anchor_type: str,
    offer_code: str,
    authority_type: type[_Authority],
    chatwoot_type: type[_Chatwoot],
    reason: str,
    posted: int,
    remote_message_id: str | None,
    raises: type[Exception] | None,
) -> None:
    # Despues del arranque nada se reintenta: el intento queda delivery_unknown,
    # sin proximo intento, y la conciliacion tiene 15 minutos. Un reintento
    # podria mandar el mensaje dos veces.
    authority = authority_type(offer_code=offer_code, anchor_type=anchor_type)
    chatwoot = chatwoot_type(ATT1_CATALOG)
    dispatcher = _dispatcher(authority, chatwoot, tmp_path, template=WITH_FORM_TEMPLATE)

    if raises is None:
        _run(dispatcher)
    else:
        with pytest.raises(raises):
            _run(dispatcher)

    assert "request_started" in authority.events
    [finalization] = authority.finalizations
    assert finalization["outcome"] == "delivery_unknown"
    assert finalization["reason_code"] == reason
    assert finalization["next_attempt_at"] is None
    assert finalization["reconciliation_deadline"] == RECONCILE_BY
    assert finalization["remote_message_id"] == remote_message_id
    assert finalization["accepted_message_id"] is None
    assert finalization["now"] == FINAL_NOW
    # A lo sumo un pedido de envio, nunca dos.
    assert len(_messages(chatwoot)) == posted
    assert authority.acceptances == []


# --------------------------------------------------------- D5: el lease


class _LeaseAuthority(_Authority):
    """Anota lo que el despachador le pide a la toma."""

    def __init__(
        self, *, claimed: bool = True, lease_owner: str = "att1-dispatcher", **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.action = replace(self.action, lease_owner=lease_owner)
        self.claimed = claimed
        self.claims: list[dict[str, object]] = []

    async def claim_due_followup_actions(self, **kwargs: object) -> list[ScheduledAction]:
        self.claims.append(kwargs)
        return [self.action] if self.claimed else []


def test_d5_the_claim_leases_each_action_for_five_minutes(tmp_path: Path) -> None:
    authority = _LeaseAuthority(offer_code="gopi6lh7")
    chatwoot = _Chatwoot(ATT1_CATALOG)

    _run(_dispatcher(authority, chatwoot, tmp_path, template=WITH_FORM_TEMPLATE))

    assert authority.claims == [
        {
            "worker_id": "att1-dispatcher",
            "now": NOW,
            "lease_duration": "5 minutes",
            "batch_size": 10,
        }
    ]
    assert authority.events[-1] == "accepted"


def test_d5_the_att1_bridge_keeps_the_five_minute_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # El despachador que arma el contenedor con las variables de produccion de
    # ATT1 y los tres flujos prendidos. Ninguna variable cambia el lease: se le
    # cambia la base por el doble y se mira la toma, sin salir a la red.
    settings, app = _start(monkeypatch, tmp_path, *OUTBOUND_FLOWS)
    dispatcher = app.state.durable_dispatcher  # type: ignore[attr-defined]
    assert dispatcher is not None
    # Si el atributo cambiara de nombre, asignarlo crearia otro y la toma iria
    # al cliente real: esto falla antes, sin red.
    assert isinstance(dispatcher._supabase, SupabaseClient)
    authority = _LeaseAuthority(claimed=False)
    dispatcher._supabase = authority

    actions = asyncio.run(dispatcher.claim_due(now=NOW))

    assert actions == []
    [claim] = authority.claims
    assert claim["lease_duration"] == "5 minutes"
    assert claim["worker_id"] == settings.dispatcher_worker_id
    assert claim["batch_size"] == settings.dispatcher_batch_size


def test_d5_an_action_leased_to_another_worker_stops_before_reevaluating(
    tmp_path: Path,
) -> None:
    authority = _LeaseAuthority(offer_code="gopi6lh7", lease_owner="otro-dispatcher")
    chatwoot = _Chatwoot(ATT1_CATALOG)

    with pytest.raises(SupabaseError, match="claim_due_followup_actions_owner_mismatch"):
        _run(_dispatcher(authority, chatwoot, tmp_path, template=WITH_FORM_TEMPLATE))

    assert authority.events == []
    assert authority.finalizations == []
    assert chatwoot.requests == []
