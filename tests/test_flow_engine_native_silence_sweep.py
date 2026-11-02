"""El barrido nativo del seguimiento a 24 h con cupon (P22-3 de M16, E-269, flow-engine-v2 §26).

El diseno es el del seguimiento y la ficha nativos (2026-11-01, en el vault), §1.2 (c) y (d). La base
que lee es la 20261102000300 (``read_flow_engine_native_silences_v1``) y la reserva que llama, la de
P22-2 (``claim_flow_engine_silence_v1``, 20261101000200, con los frenos de 20261101000300).

Datos capturados:

* ``chatwoot_inbox_11_message_templates_20261010.json``: el catalogo de la bandeja 11 de ATT1 (Chatwoot,
  2026-10-10), con ``att1_seguimiento_descuento_01`` aprobada (es_MX, MARKETING, un solo boton al
  checkout). La de Maribel no esta capturada: la definicion de Maribel se ejecuta con la de ATT1, que
  tiene la misma forma.
* ``chatwoot_followup_candidate_inbox_11_20261010.json``: la conversacion 21 de ATT1 (contesto la
  plantilla del carrito, recibio el link del agente y se callo), con las paginas del inbox 11.
* El link del agente de la 21 es el que trae su ultimo mensaje (capturado): es el que emite la reserva.

Lo fabricado, declarado: las respuestas de Supabase son dobles (``SimpleNamespace`` con la forma de
``ConversationFollowupClaim``), el ``flow_id`` y el sha256 son sinteticos, y la fila de
``read_flow_engine_native_silences_v1`` (``_row``) esta escrita con la forma que devuelve la base en
``validate_flow_engine_native_silence_sweep.mjs``.

Lo que fijan:

* la configuracion del barredor sale de la definicion publicada, entera, y una definicion que no se
  puede ejecutar no arma barredor (con su motivo);
* sin un silencio publicado no se arma ningun barredor ni se manda nada;
* el barredor nativo reserva por ``claim_flow_engine_silence_v1`` con el flujo, la version y el
  sha256 leidos, manda la plantilla con el cupon de la definicion y el producto del manifiesto, y en
  cohorte solo relee y reserva las conversaciones de la cohorte;
* despues de Z (``blocked_after_last_touch``, lo que E-267 vuelve mas frecuente) no manda, lo cuenta y
  lo loguea con su prefijo;
* el horario es la franja de la definicion (el minuto de fin entra entero);
* el refresco arma, rearma y para los barredores, y una lectura caida no para los que corren;
* el cableado: lo prende ``FLOW_ENGINE_ENABLED`` (no ``CONVERSATION_FOLLOWUP_ENABLED``), con las
  opciones del manifiesto, y /ready lo publica sin leer la base;
* la simulacion de un silencio cuenta con ``p_trigger = silencio`` (el bridge y el operador);
* la regla 14 frena un silencio fuera de la ventana de 24 a 72 h.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from test_flow_engine_native_silence import FOLLOWUP

from bridge.flow_change_simulation import build_new_flow_simulation, native_flow_counts_request, simulation_trigger
from bridge.flow_definition import parse_items
from bridge.flow_engine import native_not_supported
from bridge.flow_schedule import Franja
from bridge.followup_discount import HELD_OUT_OF_COHORT, ConversationFollowupSweeper
from bridge.native_silence import (
    REFUSED_CONVERSATIONS,
    REFUSED_OTHER_PRODUCT,
    REFUSED_WINDOW,
    NativeSilenceRefused,
    NativeSilenceSpec,
    NativeSilenceSweepers,
    native_silence_spec,
)
from bridge.phones import canonical_whatsapp_phone
from bridge.supabase import (
    FOLLOWUP_CLAIM_OUTCOMES,
    FlowEngineNativeSilence,
    NativeSilenceRun,
    SupabaseClient,
    SupabaseError,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
CATALOG = json.loads((FIXTURES / "chatwoot_inbox_11_message_templates_20261010.json").read_text(encoding="utf-8"))
CANDIDATE = json.loads((FIXTURES / "chatwoot_followup_candidate_inbox_11_20261010.json").read_text(encoding="utf-8"))
MEXICO_CITY = ZoneInfo("America/Mexico_City")
HOUR = 3_600
FLOW_ID = "0b5c9e7e-6c55-4d0e-9a51-6c0f7a6b2c11"
TEMPLATE = "att1_seguimiento_descuento_01"
PRODUCT = "Alimenta tu Tiroides"
# El seguimiento de ATT1 escrito como nativo: el de Maribel (test_flow_engine_native_silence) con el
# producto y el cupon de ATT1 (TIROIDES10, decision de Dan).
ATT1_FOLLOWUP: dict[str, Any] = copy.deepcopy(FOLLOWUP)
ATT1_FOLLOWUP["pasos"][0]["boton"]["cupon"] = "TIROIDES10"
SHA = hashlib.sha256(json.dumps(ATT1_FOLLOWUP, sort_keys=True).encode()).hexdigest()
LEAD_REPLIED_AT = max(
    message["created_at"]
    for message in CANDIDATE["conversation"]["messages"]
    if message.get("message_type") == 0
)
AGENT_LINK = next(
    word for word in CANDIDATE["conversation"]["messages"][-1]["content"].split()
    if word.startswith("https://pay.hotmart.com/")
)


def _local_hour(epoch: float) -> int:
    return datetime.fromtimestamp(epoch, MEXICO_CITY).hour


# Un instante dentro de la ventana (24 a 72 h despues de la respuesta) y dentro del horario 09:00-20:59
# de CDMX, y otro dentro de la ventana pero a las 21 h (fuera del horario).
OPEN_NOW = next(
    LEAD_REPLIED_AT + h * HOUR for h in range(25, 70) if 10 <= _local_hour(LEAD_REPLIED_AT + h * HOUR) <= 19
)
CLOSED_NOW = next(
    LEAD_REPLIED_AT + h * HOUR for h in range(25, 70) if _local_hour(LEAD_REPLIED_AT + h * HOUR) == 21
)


def _silence(definition: dict[str, Any] | None = None, *, state: str = "cohorte",
             cohort: frozenset[int] | None = frozenset({21}), version: int = 1,
             product: str = "att1") -> FlowEngineNativeSilence:
    return FlowEngineNativeSilence(
        flow_id=FLOW_ID, product_ref=product, flow_key="seguimiento_cupon", state=state,
        active_version=version, definition_sha256=SHA,
        definition=copy.deepcopy(ATT1_FOLLOWUP if definition is None else definition),
        cohort_conversation_ids=cohort if state == "cohorte" else None,
    )


# ---------------------------------------------------------------------------
# La configuracion sale de la definicion publicada.
# ---------------------------------------------------------------------------

def test_the_spec_is_the_published_definition() -> None:
    spec = native_silence_spec(_silence(), product_ref="att1")

    assert spec.run == NativeSilenceRun(flow_id=FLOW_ID, version=1, definition_sha256=SHA)
    assert (spec.template_name, spec.template_language, spec.coupon_code) == (TEMPLATE, "es_MX", "TIROIDES10")
    assert (spec.min_age_seconds, spec.max_age_seconds) == (86_400, 259_200)
    assert spec.conversation_statuses == ("open", "resolved")
    assert spec.window == Franja(always=False, zone="America/Mexico_City", start_minute=9 * 60,
                                 end_minute=20 * 60 + 59)
    assert spec.cohort_conversation_ids == frozenset({21})
    # Prendido es todo el inbox.
    assert native_silence_spec(_silence(state="prendido"), product_ref="att1").cohort_conversation_ids is None


def _changed(path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    definition = copy.deepcopy(ATT1_FOLLOWUP)
    target = definition
    for key in path[:-1]:
        target = target[key]
    if value is KeyError:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return definition


@pytest.mark.parametrize(
    ("definition", "reason"),
    [
        # Menos de 24 h o mas de 72 h, y una ventana al reves (la regla 14 lo frena al publicar).
        (_changed(("dispara", "silencio", "desde"), "23h"), "no_ejecutable:engine_not_supported:silencio_ventana"),
        (_changed(("dispara", "silencio", "hasta"), "96h"), "no_ejecutable:engine_not_supported:silencio_ventana"),
        (_changed(("dispara", "silencio", "conversaciones"), KeyError),
         "no_ejecutable:engine_not_supported:silencio_ventana"),
        (_changed(("pasos", 0, "boton", "cupon"), "personal"), "no_ejecutable:engine_not_supported:cupon_personal"),
        (_changed(("dispara", "silencio", "lo_ultimo"), "persona"), "no_ejecutable"),
        (_changed(("horario",), {"zona": "Marte/Olympus", "desde": "09:00", "hasta": "20:59"}), "horario"),
    ],
)
def test_a_definition_it_cannot_run_gets_no_sweeper(definition: dict[str, Any], reason: str) -> None:
    with pytest.raises(NativeSilenceRefused) as refusal:
        native_silence_spec(_silence(definition), product_ref="att1")
    assert refusal.value.reason == reason


def test_another_product_is_not_this_instance() -> None:
    # D-P22-4: por instancia; un silencio de otro producto no tiene el producto del manifiesto.
    with pytest.raises(NativeSilenceRefused) as refusal:
        native_silence_spec(_silence(product="otro"), product_ref="att1")
    assert refusal.value.reason == REFUSED_OTHER_PRODUCT


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        (("dispara", "silencio", "desde"), "23h", REFUSED_WINDOW),
        (("dispara", "silencio", "hasta"), "96h", REFUSED_WINDOW),
        (("dispara", "silencio", "conversaciones"), [], REFUSED_CONVERSATIONS),
        (("dispara", "silencio", "conversaciones"), ["abierta", "abierta"], REFUSED_CONVERSATIONS),
    ],
)
def test_the_second_layer_refuses_a_window_even_if_rule_14_let_it_pass(
    monkeypatch: pytest.MonkeyPatch, path: tuple[Any, ...], value: Any, reason: str
) -> None:
    # La regla 14 lo frena al publicar; el barredor lo vuelve a mirar por si una definicion vieja paso.
    monkeypatch.setattr("bridge.native_silence.native_not_supported", lambda definition: ())
    with pytest.raises(NativeSilenceRefused) as refusal:
        native_silence_spec(_silence(_changed(path, value)), product_ref="att1")
    assert refusal.value.reason == reason


def test_rule_14_stops_a_silence_outside_24_to_72_hours() -> None:
    assert native_not_supported(ATT1_FOLLOWUP) == ()
    for since, until in (("23h", "72h"), ("24h", "73h"), ("72h", "24h"), ("48h", "48h")):
        definition = _changed(("dispara", "silencio", "desde"), since)
        definition["dispara"]["silencio"]["hasta"] = until
        assert "engine_not_supported:silencio_ventana" in native_not_supported(definition), (since, until)
    assert native_not_supported(_changed(("dispara", "silencio", "desde"), "1d")) == ()


# ---------------------------------------------------------------------------
# El barredor nativo, contra la 21 capturada de ATT1.
# ---------------------------------------------------------------------------

class _Chatwoot:
    def __init__(self) -> None:
        self.entries = [copy.deepcopy(CANDIDATE["conversation"])]
        self.sent: list[dict[str, Any]] = []
        self.listings: list[dict[str, Any]] = []
        self.inbox_reads = 0
        self.rereads: list[int] = []

    async def get_inbox(self, *, inbox_id: int) -> dict[str, Any]:
        self.inbox_reads += 1
        return copy.deepcopy(CATALOG)

    async def list_recent_conversations_with_messages(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.listings.append(kwargs)
        return copy.deepcopy(self.entries)

    async def get_conversation_messages(self, *, conversation_id: int, limit: int) -> list[dict[str, Any]]:
        self.rereads.append(conversation_id)
        return copy.deepcopy(self.entries[0]["messages"])

    async def send_followup_template(self, **kwargs: Any) -> dict[str, Any]:
        self.sent.append(kwargs)
        return {"status": "sent", "message_id": 9001}


class _Supabase:
    def __init__(self, outcome: str = "claimed") -> None:
        self.outcome = outcome
        self.claims: list[dict[str, Any]] = []
        self.settled: list[dict[str, Any]] = []
        self.finalized: list[dict[str, Any]] = []

    async def get_lead_first_name_inference(self, name_key: str) -> None:
        return None

    async def claim_conversation_followup(self, **kwargs: Any) -> SimpleNamespace:
        self.claims.append(kwargs)
        claimed = self.outcome == "claimed"
        return SimpleNamespace(
            outcome=self.outcome,
            followup_event_id="evt" if claimed else None,
            checkout_issuance_id="iss-21" if claimed else None,
            checkout_url_final=AGENT_LINK if claimed else None,
            sck_value="hermes|v1|01K5ABCDEFX2VYB4M6X9CDPTF1" if claimed else None,
        )

    async def authorize_chatwoot_checkout_issuance_v2(self, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(outcome="request_started")

    async def finalize_chatwoot_checkout_issuance_v2(self, **kwargs: Any) -> None:
        self.finalized.append(kwargs)

    async def settle_conversation_followup(self, **kwargs: Any) -> None:
        self.settled.append(kwargs)


async def _stored_identity(wa_id: str, *, conversation_id: object) -> str:
    # El formulario guardo 52 + 10; Chatwoot trae el wa_id 521 + 10 (la funcion real).
    stored = canonical_whatsapp_phone(wa_id)
    assert stored is not None
    return stored


def _native_sweeper(spec: NativeSilenceSpec, chatwoot: _Chatwoot, supabase: _Supabase, *,
                    now: float) -> ConversationFollowupSweeper:
    """El barredor con las opciones con que lo arma el bridge (app.py, build_native_silence_sweeper)."""
    return ConversationFollowupSweeper(
        chatwoot=chatwoot, supabase=supabase, account_id=2, inbox_id=11,
        template_name=spec.template_name, coupon_code=spec.coupon_code, product_name=PRODUCT,
        expected_template_language=spec.template_language,
        min_inbound_age_seconds=spec.min_age_seconds, max_inbound_age_seconds=spec.max_age_seconds,
        refuse_unsafe_greeting=True, phone_equivalence=True, external_user_id_resolver=_stored_identity,
        native_run=spec.run, send_window=spec.window, conversation_statuses=spec.conversation_statuses,
        only_conversation_ids=spec.cohort_conversation_ids, log_prefix="flow_engine_silence",
        clock=lambda: now, ulid_factory=lambda: "01K5ABCDEFX2VYB4M6X9CDPTF1",
    )


def test_in_its_cohort_the_native_sweeper_reserves_with_the_flow_and_sends_the_coupon() -> None:
    chatwoot, supabase = _Chatwoot(), _Supabase()
    spec = native_silence_spec(_silence(), product_ref="att1")
    sweeper = _native_sweeper(spec, chatwoot, supabase, now=OPEN_NOW)

    assert asyncio.run(sweeper.run_once()) == 1
    [listing] = chatwoot.listings
    assert listing["statuses"] == ("open", "resolved")
    [claim] = supabase.claims
    # La reserva nativa, con lo que leyo de la base, y los mismos argumentos que la portable.
    assert claim["native_run"] == NativeSilenceRun(flow_id=FLOW_ID, version=1, definition_sha256=SHA)
    assert claim["phone_equivalence"] is True
    assert claim["external_user_id"] == "525500000021"
    assert claim["command_key"] == "followup:21:2766"
    assert (claim["template_name"], claim["template_language"], claim["coupon_code"]) == (
        TEMPLATE, "es_MX", "TIROIDES10")
    [sent] = chatwoot.sent
    assert sent["template_params"]["processed_params"] == {
        "body": {"1": "Lucia", "2": PRODUCT},
        "buttons": [{"type": "url", "parameter": AGENT_LINK.removeprefix("https://pay.hotmart.com/")
                     + "&offDiscount=TIROIDES10"}],
    }
    assert [s["status"] for s in supabase.settled] == ["sent"]


def test_it_lists_only_the_conversation_states_of_the_definition() -> None:
    chatwoot, supabase = _Chatwoot(), _Supabase()
    spec = native_silence_spec(_silence(_changed(("dispara", "silencio", "conversaciones"), ["abierta"])),
                               product_ref="att1")
    assert spec.conversation_statuses == ("open",)
    asyncio.run(_native_sweeper(spec, chatwoot, supabase, now=OPEN_NOW).run_once())
    assert [listing["statuses"] for listing in chatwoot.listings] == [("open",)]


def test_the_sent_line_carries_the_native_prefix(caplog: pytest.LogCaptureFixture) -> None:
    chatwoot, supabase = _Chatwoot(), _Supabase()
    sweeper = _native_sweeper(native_silence_spec(_silence(), product_ref="att1"), chatwoot, supabase, now=OPEN_NOW)
    with caplog.at_level(logging.INFO, logger="bridge.followup_discount"):
        assert asyncio.run(sweeper.run_once()) == 1
    messages = [record.getMessage() for record in caplog.records]
    assert any(m.startswith("flow_engine_silence_sent conversation=21 ") for m in messages)
    assert any(m.startswith("flow_engine_silence_scan ") for m in messages)
    assert not any(m.startswith("conversation_followup_") for m in messages)
    # El filtro del log de produccion (event_log) mira la primera palabra del molde: es literal.
    assert all(not str(record.msg).startswith("%s") for record in caplog.records)


def test_the_held_out_of_cohort_count_is_always_in_the_summary() -> None:
    from collections import Counter

    from bridge.followup_discount import _followup_scan_summary

    motivos = Counter({"a": 9, "b": 8, "c": 7, "d": 6, "e": 5, HELD_OUT_OF_COHORT: 1})
    summary = _followup_scan_summary(scanned=36, sent=0, motivos=motivos)
    assert summary.endswith(f" {HELD_OUT_OF_COHORT}=1")


def test_a_draining_stop_lets_the_send_in_flight_finish() -> None:
    # Al parar por otra version o por la pausa, el barrido en curso termina: cancelar entre la reserva y
    # el envio dejaria la reserva tomada y a la persona sin cupon.
    class _SlowChatwoot(_Chatwoot):
        def __init__(self) -> None:
            super().__init__()
            self.sending = asyncio.Event()
            self.release = asyncio.Event()

        async def send_followup_template(self, **kwargs: Any) -> dict[str, Any]:
            self.sending.set()
            await self.release.wait()
            return await super().send_followup_template(**kwargs)

    async def scenario() -> tuple[_SlowChatwoot, _Supabase]:
        chatwoot, supabase = _SlowChatwoot(), _Supabase()
        sweeper = _native_sweeper(native_silence_spec(_silence(), product_ref="att1"), chatwoot, supabase,
                                  now=OPEN_NOW)
        await sweeper.start()
        await asyncio.wait_for(chatwoot.sending.wait(), timeout=5)
        stopping = asyncio.create_task(sweeper.stop(drain_seconds=5))
        await asyncio.sleep(0)
        chatwoot.release.set()
        await stopping
        return chatwoot, supabase

    chatwoot, supabase = asyncio.run(scenario())
    assert len(chatwoot.sent) == 1
    assert [s["status"] for s in supabase.settled] == ["sent"]


def test_outside_its_cohort_it_does_not_reread_nor_reserve() -> None:
    chatwoot, supabase = _Chatwoot(), _Supabase()
    spec = native_silence_spec(_silence(cohort=frozenset({999})), product_ref="att1")
    sweeper = _native_sweeper(spec, chatwoot, supabase, now=OPEN_NOW)

    assert asyncio.run(sweeper.run_once()) == 0
    assert chatwoot.rereads == [] and supabase.claims == [] and chatwoot.sent == []
    assert f"{HELD_OUT_OF_COHORT}=1" in sweeper.last_scan_summary


def test_after_z_it_does_not_send_and_logs_the_stop_with_its_prefix(caplog: pytest.LogCaptureFixture) -> None:
    # E-267: la respuesta escrita a A2 adelanta Z, asi que habra mas personas con Z en 30 dias, y la
    # reserva nativa las frena con blocked_after_last_touch (20261101000300).
    assert "blocked_after_last_touch" in FOLLOWUP_CLAIM_OUTCOMES
    chatwoot, supabase = _Chatwoot(), _Supabase("blocked_after_last_touch")
    sweeper = _native_sweeper(native_silence_spec(_silence(), product_ref="att1"), chatwoot, supabase, now=OPEN_NOW)

    with caplog.at_level(logging.INFO, logger="bridge.followup_discount"):
        assert asyncio.run(sweeper.run_once()) == 0
    assert chatwoot.sent == [] and supabase.settled == []
    assert "blocked_after_last_touch=1" in sweeper.last_scan_summary
    assert sweeper.last_scan_state == "healthy"
    messages = [record.getMessage() for record in caplog.records]
    assert "flow_engine_silence_stopped reason=blocked_after_last_touch conversation=21" in messages
    assert not any(message.startswith("conversation_followup_") for message in messages)


@pytest.mark.parametrize("outcome", ["blocked_out_of_cohort", "blocked_flow_version", "blocked_flow_not_running",
                                     "blocked_flow_cap", "blocked_pending_payment"])
def test_the_native_barriers_never_send(outcome: str) -> None:
    chatwoot, supabase = _Chatwoot(), _Supabase(outcome)
    sweeper = _native_sweeper(native_silence_spec(_silence(), product_ref="att1"), chatwoot, supabase, now=OPEN_NOW)
    assert asyncio.run(sweeper.run_once()) == 0
    assert chatwoot.sent == [] and sweeper.last_scan_state == "healthy"


def test_its_schedule_is_the_definition_one() -> None:
    chatwoot, supabase = _Chatwoot(), _Supabase()
    sweeper = _native_sweeper(native_silence_spec(_silence(), product_ref="att1"), chatwoot, supabase,
                              now=CLOSED_NOW)
    assert asyncio.run(sweeper.run_once()) == 0
    assert chatwoot.inbox_reads == 0 and chatwoot.listings == []
    assert sweeper.last_scan_summary == "outside_send_hours"


def test_the_native_options_without_a_manifest_or_with_hours_do_not_build() -> None:
    spec = native_silence_spec(_silence(), product_ref="att1")
    with pytest.raises(ValueError, match="invalid conversation followup configuration"):
        ConversationFollowupSweeper(
            chatwoot=_Chatwoot(), supabase=_Supabase(), account_id=2, inbox_id=11, template_name=TEMPLATE,
            coupon_code="TIROIDES10", product_name=PRODUCT, native_run=spec.run)
    with pytest.raises(ValueError, match="invalid conversation followup configuration"):
        ConversationFollowupSweeper(
            chatwoot=_Chatwoot(), supabase=_Supabase(), account_id=2, inbox_id=11, template_name=TEMPLATE,
            coupon_code="TIROIDES10", product_name=PRODUCT, phone_equivalence=True,
            send_window=spec.window, send_hours=(9, 21), time_zone=MEXICO_CITY)


# ---------------------------------------------------------------------------
# El refresco: arma, rearma y para.
# ---------------------------------------------------------------------------

class _Reads:
    def __init__(self, *flows: FlowEngineNativeSilence) -> None:
        self.flows = list(flows)
        self.fail = False
        self.calls = 0

    async def read_flow_engine_native_silences(self, instance: object) -> tuple[FlowEngineNativeSilence, ...]:
        self.calls += 1
        if self.fail:
            raise SupabaseError("read_flow_engine_native_silences_v1_failed: HTTP 503")
        return tuple(self.flows)


class _Built:
    def __init__(self, spec: NativeSilenceSpec) -> None:
        self.spec = spec
        self.started = 0
        self.stopped = 0
        self.drains: list[float | None] = []
        self.cohorts: list[frozenset[int]] = []
        self.last_scan_state = "never"
        self.last_scan_summary = "never"

    async def start(self) -> None:
        self.started += 1

    async def stop(self, *, drain_seconds: float | None = None) -> None:
        self.stopped += 1
        self.drains.append(drain_seconds)

    def set_only_conversation_ids(self, ids: frozenset[int]) -> None:
        self.cohorts.append(ids)


def _sweepers(reads: _Reads) -> tuple[NativeSilenceSweepers, list[_Built]]:
    built: list[_Built] = []

    def build(spec: NativeSilenceSpec) -> Any:
        sweeper = _Built(spec)
        built.append(sweeper)
        return sweeper

    return NativeSilenceSweepers(supabase=reads, instance={"tenant_ref": "lancemos", "ally_ref": "att1"},
                                 product_ref="att1", build=build), built


def test_without_a_published_silence_nothing_is_built() -> None:
    sweepers, built = _sweepers(_Reads())
    asyncio.run(sweepers.refresh_once())
    assert built == [] and sweepers.sweepers == ()
    assert sweepers.readiness() == {
        "flow_engine_native_silence_read": "ok", "flow_engine_native_silence_running": "0",
        "flow_engine_native_silence_cohort": "0", "flow_engine_native_silence_scan": "none",
        "flow_engine_native_silence_last_scan": "none", "flow_engine_native_silence_refused": "none",
    }


def test_the_refresh_builds_rebuilds_and_stops() -> None:
    reads = _Reads(_silence())
    sweepers, built = _sweepers(reads)

    async def scenario() -> None:
        await sweepers.refresh_once()
        assert len(built) == 1 and built[0].started == 1
        await sweepers.refresh_once()                  # sin cambios: el mismo
        assert len(built) == 1
        reads.flows = [_silence(cohort=frozenset({21, 22}))]   # un miembro nuevo: el mismo, sin parar
        await sweepers.refresh_once()
        assert len(built) == 1 and built[0].stopped == 0 and built[0].cohorts == [frozenset({21, 22})]
        reads.flows = [_silence(cohort=frozenset({21, 22}), version=2)]   # otra version: se rearma, drenando
        await sweepers.refresh_once()
        assert len(built) == 2 and built[0].stopped == 1 and built[0].drains == [120.0]
        reads.flows = [_silence(state="prendido", version=2)]   # a todos: se rearma, sin cohorte
        await sweepers.refresh_once()
        assert len(built) == 3 and built[1].stopped == 1 and built[2].spec.cohort_conversation_ids is None
        reads.fail = True                              # la lectura cae: el que corre sigue
        await sweepers.refresh_once()
        assert sweepers.state == "failing" and built[2].stopped == 0 and len(sweepers.sweepers) == 1
        reads.fail = False
        reads.flows = []                               # pausado o apagado: se para
        await sweepers.refresh_once()
        assert built[2].stopped == 1 and sweepers.sweepers == ()

    asyncio.run(scenario())


def test_a_sweeper_that_does_not_build_or_start_does_not_stop_the_others() -> None:
    other = FlowEngineNativeSilence(
        flow_id="1c2d3e4f-0000-4000-8000-000000000002", product_ref="att1", flow_key="seguimiento_b",
        state="cohorte", active_version=1, definition_sha256=SHA, definition=copy.deepcopy(ATT1_FOLLOWUP),
        cohort_conversation_ids=frozenset())
    reads = _Reads(_silence(), other)
    built: list[_Built] = []
    attempts: list[str] = []

    def build(spec: NativeSilenceSpec) -> Any:
        attempts.append(spec.flow_key)
        if spec.flow_key == "seguimiento_cupon" and attempts.count("seguimiento_cupon") == 1:
            raise RuntimeError("chatwoot down")
        sweeper = _Built(spec)
        built.append(sweeper)
        return sweeper

    sweepers = NativeSilenceSweepers(supabase=reads, instance={"tenant_ref": "lancemos", "ally_ref": "att1"},
                                     product_ref="att1", build=build)
    asyncio.run(sweepers.refresh_once())
    assert [b.spec.flow_key for b in built] == ["seguimiento_b"]
    assert sweepers.refused == {"att1/seguimiento_cupon": "configuracion"}
    asyncio.run(sweepers.refresh_once())   # el siguiente refresco lo reintenta
    assert sorted(b.spec.flow_key for b in built) == ["seguimiento_b", "seguimiento_cupon"]
    assert sweepers.refused == {}


def test_a_silence_it_cannot_run_is_published_with_its_reason() -> None:
    reads = _Reads(_silence(_changed(("dispara", "silencio", "desde"), "1h")))
    sweepers, built = _sweepers(reads)
    asyncio.run(sweepers.refresh_once())
    assert built == []
    assert sweepers.readiness()["flow_engine_native_silence_refused"] == (
        "att1/seguimiento_cupon=no_ejecutable:engine_not_supported:silencio_ventana")


# ---------------------------------------------------------------------------
# El cliente de la base.
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, payload: Any) -> None:
        self.status_code = 200
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def _client(payload: Any, calls: list[tuple[str, dict[str, Any]]]) -> SupabaseClient:
    client = SupabaseClient.__new__(SupabaseClient)

    async def request(method: str, path: str, *, content: str) -> _Response:
        calls.append((path, json.loads(content)))
        return _Response(payload)

    client._request = request  # type: ignore[method-assign]
    return client


def _row(**overrides: Any) -> dict[str, Any]:
    row = {"flow_id": FLOW_ID, "product_ref": "att1", "flow_key": "seguimiento_cupon", "state": "cohorte",
           "active_version": 1, "definition_sha256": SHA, "definition": ATT1_FOLLOWUP,
           "cohort_conversation_ids": [21]}
    row.update(overrides)
    return row


def test_the_client_reads_the_silences_and_fails_closed_on_any_other_shape() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    [flow] = asyncio.run(_client({"contract": "flow-engine-v2", "flows": [_row()]}, calls)
                         .read_flow_engine_native_silences({"tenant_ref": "lancemos", "ally_ref": "att1"}))
    assert calls == [("/rest/v1/rpc/read_flow_engine_native_silences_v1",
                      {"p_instance": {"tenant_ref": "lancemos", "ally_ref": "att1"}})]
    assert flow == _silence()
    for bad in (_row(state="pausado"), _row(state="pausado", cohort_conversation_ids=None),
                _row(cohort_conversation_ids=None), _row(state="prendido"),
                _row(definition_sha256="x"), _row(active_version=0), _row(cohort_conversation_ids=[True])):
        with pytest.raises(SupabaseError):
            asyncio.run(_client({"contract": "flow-engine-v2", "flows": [bad]}, [])
                        .read_flow_engine_native_silences({"tenant_ref": "lancemos", "ally_ref": "att1"}))
    with pytest.raises(SupabaseError):
        asyncio.run(_client({"flows": []}, []).read_flow_engine_native_silences({}))


def test_the_client_claims_by_the_native_reservation_with_the_flow_first() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    client = _client([{"outcome": "blocked_after_last_touch", "followup_event_id": None,
                       "checkout_issuance_id": None, "checkout_url_final": None, "sck_value": None}], calls)
    args = dict(external_conversation_id=21, chatwoot_account_id=2, chatwoot_inbox_id=11,
                external_user_id="525500000021", contact_email=None, command_key="followup:21:2766",
                regime="link_sent_no_purchase", template_name=TEMPLATE, template_language="es_MX",
                coupon_code="TIROIDES10", last_inbound_message_id=2765, last_outbound_message_id=2766,
                inbound_age_seconds=90_000, issuance_ulid="01K5ABCDEFX2VYB4M6X9CDPTF1")
    run = NativeSilenceRun(flow_id=FLOW_ID, version=1, definition_sha256=SHA)
    claim = asyncio.run(client.claim_conversation_followup(**args, phone_equivalence=True, native_run=run))
    assert claim.outcome == "blocked_after_last_touch"
    [(path, body)] = calls
    assert path == "/rest/v1/rpc/claim_flow_engine_silence_v1"
    assert list(body)[:3] == ["p_flow_id", "p_version", "p_definition_sha256"]
    assert (body["p_flow_id"], body["p_version"], body["p_definition_sha256"]) == (FLOW_ID, 1, SHA)
    with pytest.raises(ValueError, match="native_run requires phone_equivalence"):
        asyncio.run(client.claim_conversation_followup(**args, native_run=run))


# ---------------------------------------------------------------------------
# La simulacion de un silencio.
# ---------------------------------------------------------------------------

def _operator() -> Any:
    spec = importlib.util.spec_from_file_location("flow_change_operator_e269", ROOT / "scripts" / "flow_change_operator.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_simulation_of_a_silence_counts_with_silencio_on_both_sides() -> None:
    assert simulation_trigger(ATT1_FOLLOWUP) == "silencio"
    assert simulation_trigger({"dispara": {"evento": "carrito"}}) == "carrito"
    assert simulation_trigger({"dispara": {"silencio": {"lo_ultimo": "persona"}}}) is None
    operator = _operator()
    for definition in (ATT1_FOLLOWUP, {"dispara": {"evento": "compra"}}, {"dispara": {"silencio": {"lo_ultimo": "persona"}}}):
        assert operator._simulation_trigger(definition) == simulation_trigger(definition)
    # El prender de un silencio en cohorte pide los conteos del silencio.
    store = {"flows": [{"flow_id": FLOW_ID, "native": True, "state": "cohorte", "active_version": 1,
                        "versions": {"1": ATT1_FOLLOWUP}}]}
    items = [{"tipo": "prender", "flow_id": FLOW_ID, "producto": "att1", "flujo": "seguimiento_cupon"}]
    assert native_flow_counts_request(store, items) == {"product_ref": "att1", "trigger": "silencio"}
    assert operator._native_flow_counts_request(store, items) == {"product_ref": "att1", "trigger": "silencio"}


def test_a_new_silence_flow_builds_its_simulation() -> None:
    [item] = parse_items([{"tipo": "nuevo", "producto": "att1", "flujo": "seguimiento_cupon",
                           "definicion": ATT1_FOLLOWUP, "prender": "cohorte"}])
    counts = {"contract": "flow-engine-v2", "trigger": "silencio", "product_ref": "att1",
              "since": "2026-10-26T00:00:00Z", "until": "2026-11-02T00:00:00Z", "events": 6, "people": 6}
    result = build_new_flow_simulation(counts, [item])
    assert result["status"] == "built"
    assert result["summary"]["disparador"] == "silencio"
    assert result["summary"]["mensajes_si_fuera_para_todos"] == 6
