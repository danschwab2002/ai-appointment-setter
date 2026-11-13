"""El disparo por audiencia en el bridge (E-484): la base, la fuente, el motor, el aviso y la prueba entera.

Contrato: docs/contracts/flow-audience-trigger-v1.md; la base, 20261112220000 (lo que hace de
verdad lo prueba PGlite: tests/sql/followup_engine/validate_flow_audience_triggers.mjs). Aca:

* ``SupabaseAudienceStore`` contra PostgREST emulado con la forma que contestan esas funciones;
* la fuente con las etiquetas de Chatwoot (la forma del listado de conversaciones);
* ``NativeAudienceRunOpener`` contra un doble de la admision nativa **tal como esta en E-483**
  (``admit_flow_engine_event(instance, flow_event, *, priority_wins=False)`` y sus filas);
* el aviso del freno (``SYS-007``) y el armado con el flag;
* la prueba de punta a punta: una fecha que vence, la lista, el ritmo, el motor y el freno
  por salud, con la tienda real contra la base emulada.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from bridge import flow_audience_sweeper as fs
from bridge.flow_audience import Candidate, Occurrence, audience_spec
from bridge.flow_audience_opener import EVENT_SOURCE, NativeAudienceRunOpener, audience_event
from bridge.flow_audience_store import (
    AudienceLabelsUnavailable,
    AudienceStoreRejected,
    AudienceStoreUnavailable,
    BaseAudienceSource,
    ChatwootLabelIndex,
    SupabaseAudienceStore,
)
from bridge.flow_audience_wiring import (
    BRAKE_EVENT_CODE,
    FlowAudienceConfigError,
    SlackBrakeNotifier,
    brake_notice,
    build_flow_audience,
)
from bridge.supabase import (
    FlowEngineAdmissionUnavailable,
    FlowEngineNativeAdmissionRow,
    SupabaseClient,
    SupabasePermanentError,
)
from slack_correlation.catalog import EVENT_TEMPLATES

from test_flow_audience import difusion

NOW = datetime(2026, 12, 1, 16, 5, tzinfo=timezone.utc)
INSTANCE = {"tenant_ref": "lancemos", "ally_ref": "att1"}
FLOW_ID = "0000000b-0000-4000-8000-00000000d1f0"
GREEN = {"calidad": "GREEN", "leido_en": (NOW - timedelta(hours=2)).isoformat()}


def contact(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def phone(n: int) -> str:
    return f"52155500{n:05d}"


# ---------------------------------------------------------------------------
# La base emulada: las funciones del bridge con las reglas que importan aca.
# ---------------------------------------------------------------------------

class AudienceBase:
    """PostgREST con las funciones de 20261112220000 del lado de service_role."""

    def __init__(self, definition: Mapping[str, Any], candidates: list[dict[str, Any]]) -> None:
        self.definition = dict(definition)
        self.candidates = candidates
        self.occurrences: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.down: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer service-key"
        name = request.url.path.rsplit("/", 1)[1]
        body = json.loads(request.content)
        self.calls.append((name, body))
        if name in self.down:
            return httpx.Response(503, json={"message": "caida"})
        return getattr(self, name)(body)

    def read_flow_audience_flows_v1(self, body: dict[str, Any]) -> httpx.Response:
        assert body == {"p_instance": INSTANCE}
        return httpx.Response(200, json=[{"flow_id": FLOW_ID, "flow_key": "difusion", "product_ref": "att1",
                                          "state": "prendido", "version": 1, "definition": self.definition}])

    def read_flow_audience_candidates_v1(self, body: dict[str, Any]) -> httpx.Response:
        assert body["p_flow_id"] == FLOW_ID and body["p_version"] == 1
        return httpx.Response(200, json={"candidates": self.candidates, "sin_contacto": 1, "ambiguas": 0})

    def materialize_flow_audience_occurrence_v1(self, body: dict[str, Any]) -> httpx.Response:
        key = body["p_occurrence"]["key"]
        for occurrence_id, row in self.occurrences.items():
            if row["key"] == key:
                return httpx.Response(200, json={"ok": True, "created": False, "occurrence_id": occurrence_id,
                                                 "state": row["state"], "member_count": len(row["members"])})
        occurrence_id = str(uuid.uuid4())
        members = [{"contact_id": m["contact_id"], "phone": m["phone"], "state": "pendiente", "settled": False}
                   for m in body["p_members"]]
        self.occurrences[occurrence_id] = {"key": key, "form": key.split(":")[0], "members": members,
                                           "state": "materializada" if key.startswith("manual:") else "confirmada",
                                           "per_hour": self.definition.get("ritmo", {}).get("por_hora", 200),
                                           "by": body["p_occurrence"]["materialized_by"]}
        return httpx.Response(200, json={"ok": True, "created": True, "occurrence_id": occurrence_id,
                                         "state": self.occurrences[occurrence_id]["state"],
                                         "member_count": len(members), "omitted_count": 0})

    def read_flow_audience_occurrences_v1(self, body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, json=[{"occurrence_id": oid, "flow_id": FLOW_ID, "flow_key": "difusion",
                                          "flow_version": 1, "form": row["form"], "state": row["state"],
                                          "member_count": len(row["members"])}
                                         for oid, row in self.occurrences.items()])

    def brake_flow_audience_occurrence_v1(self, body: dict[str, Any]) -> httpx.Response:
        row = self.occurrences[body["p_occurrence_id"]]
        if row["state"] not in ("materializada", "confirmada", "enviando"):
            return httpx.Response(200, json={"ok": False, "reason_code": "not_brakeable"})
        row["state"], row["brake"] = "frenada", body["p_reason"]
        return httpx.Response(200, json={"ok": True})

    def release_flow_audience_members_v1(self, body: dict[str, Any]) -> httpx.Response:
        row = self.occurrences[body["p_occurrence_id"]]
        if row["state"] not in ("confirmada", "enviando"):
            return httpx.Response(200, json={"ok": False, "reason_code": "not_releasable", "state": row["state"]})
        released = []
        for member in row["members"]:
            if member["state"] == "pendiente" and len(released) < row["per_hour"]:
                member["state"] = "liberada"
                released.append({"contact_id": member["contact_id"], "phone": member["phone"], "again": False})
        row["state"] = "enviando"
        pending = sum(m["state"] == "pendiente" for m in row["members"])
        return httpx.Response(200, json={"ok": True, "released": released, "pending": pending,
                                         "state": row["state"], "per_hour_left": 0})

    def settle_flow_audience_member_v1(self, body: dict[str, Any]) -> httpx.Response:
        row = self.occurrences[body["p_occurrence_id"]]
        assert body["p_outcome"] in ("arranco", "no_arranca", "sin_flujo")
        for member in row["members"]:
            if member["contact_id"] == body["p_contact_id"]:
                member["settled"] = body["p_outcome"]
        if all(m["state"] == "liberada" and m["settled"] for m in row["members"]):
            row["state"] = "terminada"
        return httpx.Response(200, json={"ok": True, "settled": True, "state": row["state"]})


def store_for(base: AudienceBase) -> SupabaseAudienceStore:
    return SupabaseAudienceStore(base_url="https://att1-rest.internal", service_role_key="service-key",
                                 instance=INSTANCE, transport=httpx.MockTransport(base.handler))


def candidate_row(n: int, **changes: Any) -> dict[str, Any]:
    return {"contact_id": contact(n), "phone": phone(n), "form_at": (NOW - timedelta(days=2)).isoformat(),
            "opted_out": False, "customer_products": [], **changes}


# ---------------------------------------------------------------------------
# La tienda.
# ---------------------------------------------------------------------------

def test_the_store_reads_the_flows_and_the_candidates_without_names() -> None:
    base = AudienceBase(difusion(), [candidate_row(1), candidate_row(2, opted_out=True, form_at=None,
                                                                        customer_products=["att1"])])
    store = store_for(base)
    (flow,) = asyncio.run(store.read_audience_flows())
    assert (flow.flow_id, flow.version, flow.product_ref, flow.state) == (FLOW_ID, 1, "att1", "prendido")
    read = asyncio.run(store.read_candidates(flow))
    assert [c.contact_id for c in read.candidates] == [contact(1), contact(2)]
    assert read.candidates[1].opted_out and read.candidates[1].form_at is None
    assert read.candidates[1].customer_products == frozenset({"att1"}) and read.without_contact == 1


def test_a_row_out_of_shape_or_a_5xx_is_unavailable_and_a_22023_is_rejected() -> None:
    base = AudienceBase(difusion(), [candidate_row(1, phone="+52 1 55")])
    store = store_for(base)
    (flow,) = asyncio.run(store.read_audience_flows())
    with pytest.raises(AudienceStoreUnavailable):
        asyncio.run(store.read_candidates(flow))
    base.down.add("read_flow_audience_flows_v1")
    with pytest.raises(AudienceStoreUnavailable):
        asyncio.run(store.read_audience_flows())

    def rejecting(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": "22023", "message": "flow_audience_over_cap"})

    rejected = SupabaseAudienceStore(base_url="https://x", service_role_key="k", instance=INSTANCE,
                                     transport=httpx.MockTransport(rejecting))
    with pytest.raises(AudienceStoreRejected) as error:
        asyncio.run(rejected.brake(contact(9), "meta_salud:red"))
    assert error.value.reason == "flow_audience_over_cap"
    with pytest.raises(ValueError):
        asyncio.run(store.settle(contact(9), contact(1), "lo_que_sea"))


def test_the_store_sends_the_occurrence_and_only_contact_and_phone_of_each_member() -> None:
    base = AudienceBase(difusion(), [])
    store = store_for(base)
    (flow,) = asyncio.run(store.read_audience_flows())
    members = [Candidate(contact(1), phone(1), frozenset({"black"}), False, NOW, frozenset())]
    asyncio.run(store.materialize(flow, Occurrence("fecha:2026-12-01T16:00Z", NOW), members, "a" * 64))
    (_, body), = [call for call in base.calls if call[0] == "materialize_flow_audience_occurrence_v1"]
    assert body["p_occurrence"] == {"key": "fecha:2026-12-01T16:00Z", "scheduled_at": NOW.isoformat(),
                                    "fingerprint": "a" * 64, "materialized_by": "barredor"}
    assert body["p_members"] == [{"contact_id": contact(1), "phone": phone(1)}]


# ---------------------------------------------------------------------------
# La fuente y las etiquetas.
# ---------------------------------------------------------------------------

class FakeChatwoot:
    """El listado de conversaciones (list_recent_conversations_with_messages, include_messages=False)."""

    def __init__(self, items: list[dict[str, Any]], *, failure: Exception | None = None) -> None:
        self.items = items
        self.failure = failure
        self.asked: list[dict[str, Any]] = []

    async def list_recent_conversations_with_messages(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.asked.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return self.items


def conversation(labels: list[str], number: str | None) -> dict[str, Any]:
    sender = {"phone_number": number} if number is not None else {}
    return {"conversation": {"id": 1, "inbox_id": 11, "labels": labels, "meta": {"sender": sender}}}


def test_labels_come_from_chatwoot_by_phone_with_the_521_equivalence() -> None:
    # La conversacion trae el 52 sin el 1 movil; la base, el 521.
    chatwoot = FakeChatwoot([conversation(["black-2026"], "+5255500" + "00001"),
                             conversation(["black-2026", "vip"], "+5215550000002"), conversation([], None)])
    base = AudienceBase(difusion(para={"es": "prospecta", "etiquetas": ["black-2026"], "excluye": ["vip"]}),
                        [candidate_row(1), candidate_row(2), candidate_row(3)])
    store = store_for(base)
    source = BaseAudienceSource(store=store, labels=ChatwootLabelIndex(chatwoot=chatwoot, inbox_id=11,
                                                                       clock=lambda: NOW))
    (flow,) = asyncio.run(store.read_audience_flows())
    spec = audience_spec(flow.definition)
    found = {c.contact_id: c.labels for c in asyncio.run(source.candidates(flow, spec))}
    assert found == {contact(1): {"black-2026"}, contact(2): {"black-2026", "vip"}, contact(3): frozenset()}
    assert chatwoot.asked[0]["include_messages"] is False and chatwoot.asked[0]["expected_inbox_id"] == 11
    assert chatwoot.asked[0]["active_since_epoch"] == int((NOW - timedelta(days=30)).timestamp())


def test_labels_that_cannot_be_read_whole_stop_the_audience() -> None:
    base = AudienceBase(difusion(para={"es": "prospecta", "etiquetas": ["black-2026"]}), [candidate_row(1)])
    store = store_for(base)
    (flow,) = asyncio.run(store.read_audience_flows())
    spec = audience_spec(flow.definition)
    for chatwoot in (FakeChatwoot([], failure=RuntimeError("conversation_scan_incomplete")),
                     FakeChatwoot([conversation(["black-2026"], None)])):
        source = BaseAudienceSource(store=store, labels=ChatwootLabelIndex(chatwoot=chatwoot, inbox_id=11))
        with pytest.raises(AudienceLabelsUnavailable):
            asyncio.run(source.candidates(flow, spec))
    with pytest.raises(AudienceLabelsUnavailable):
        asyncio.run(BaseAudienceSource(store=store, labels=None).candidates(flow, spec))


def test_without_labels_chatwoot_is_not_asked() -> None:
    chatwoot = FakeChatwoot([], failure=AssertionError("no se pide"))
    base = AudienceBase(difusion(), [candidate_row(1)])
    store = store_for(base)
    (flow,) = asyncio.run(store.read_audience_flows())
    source = BaseAudienceSource(store=store, labels=ChatwootLabelIndex(chatwoot=chatwoot, inbox_id=11))
    assert len(list(asyncio.run(source.candidates(flow, audience_spec(flow.definition))))) == 1


# ---------------------------------------------------------------------------
# El RunOpener contra la interfaz de la admision nativa de E-483.
# ---------------------------------------------------------------------------

def row(outcome: str, *, flow_id: str = FLOW_ID, reason: str | None = None) -> FlowEngineNativeAdmissionRow:
    return FlowEngineNativeAdmissionRow(flow_id=flow_id, outcome=outcome, reason=reason,
                                        recovery_case_id=contact(77) if outcome == "motor_arranco" else None,
                                        scheduled_action_id=None, created=True)


class AdmitDouble:
    """El doble de ``SupabaseClient.admit_flow_engine_event`` (E-483): instancia, evento, priority_wins."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []

    async def __call__(self, instance: Mapping[str, object], flow_event: Mapping[str, object],
                       **kwargs: Any) -> tuple[FlowEngineNativeAdmissionRow, ...]:
        self.calls.append((dict(instance), dict(flow_event), kwargs))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


FLOW = fs.AudienceFlow(FLOW_ID, 1, "difusion", difusion(), product_ref="att1", state="prendido")
OCCURRENCE = fs.OccurrenceRow(contact(50), FLOW_ID, "enviando", "fecha", 1)
MEMBER = fs.ReleasedMember(contact(1), phone(1))


def opener(answer: Any, *, priority_wins: bool = False,
           audience: bool = True) -> tuple[NativeAudienceRunOpener, AdmitDouble]:
    double = AdmitDouble(answer)
    return NativeAudienceRunOpener(admit=double, instance={**INSTANCE, "funnel_ref": "att1"}, chatwoot_account_id=1,
                                   chatwoot_inbox_id=11, priority_wins=priority_wins, audience=audience,
                                   clock=lambda: NOW), double


def test_the_real_admission_takes_an_instance_and_an_event() -> None:
    # Si E-483 cambia la firma, esto avisa antes que produccion.
    parameters = list(inspect.signature(SupabaseClient.admit_flow_engine_event).parameters)
    assert parameters[:3] == ["self", "instance", "flow_event"]


def test_the_event_of_a_person_of_the_list_has_a_unique_id_and_no_name() -> None:
    event = audience_event(FLOW, OCCURRENCE, MEMBER, now=NOW, chatwoot_account_id=1, chatwoot_inbox_id=11)
    assert event == {"fuente": EVENT_SOURCE, "tipo": "fecha", "evento_id": f"audiencia:{contact(50)}:{contact(1)}",
                     "flujo_id": FLOW_ID, "version": 1, "ocurrencia_id": contact(50), "producto_ref": "att1",
                     "contacto_id": contact(1), "momento": NOW.isoformat(),
                     "chatwoot": {"account_id": 1, "inbox_id": 11}, "destino": phone(1)}


@pytest.mark.parametrize(("rows", "expected"), [
    ((row("motor_arranco"),), fs.OPEN_STARTED),
    ((row("motor_no_arranca", reason="ya_disparado"),), fs.OPEN_NOT_STARTED),
    ((row("sin_flujo", flow_id=None),), fs.OPEN_NO_FLOW),
    ((row("motor_arranco", flow_id=contact(99)),), fs.OPEN_NO_FLOW),  # arranco otro flujo, no este
])
def test_what_the_engine_answered_is_what_gets_settled(rows: tuple[Any, ...], expected: str) -> None:
    run, double = opener(rows)
    assert asyncio.run(run.open(FLOW, OCCURRENCE, MEMBER)) == expected
    assert double.calls[0][2] == {"audience": True}  # sin le_gana_a; la clave de la fuente audiencia (AUD)


def test_a_rejection_is_not_started_and_no_answer_is_a_retry() -> None:
    run, _ = opener(SupabasePermanentError("admit_flow_engine_event_v1_rejected", reason="flow_event_invalid"))
    assert asyncio.run(run.open(FLOW, OCCURRENCE, MEMBER)) == fs.OPEN_NOT_STARTED
    run, _ = opener(FlowEngineAdmissionUnavailable("admit_flow_engine_event_v1_unavailable"))
    with pytest.raises(fs.AudienceOpenRetry):
        asyncio.run(run.open(FLOW, OCCURRENCE, MEMBER))


def test_priority_wins_travels_only_with_the_multitouch_flag() -> None:
    run, double = opener((row("motor_arranco"),), priority_wins=True)
    asyncio.run(run.open(FLOW, OCCURRENCE, MEMBER))
    assert double.calls[0][2] == {"priority_wins": True, "audience": True}


def test_the_real_admission_takes_the_audience_key_of_e483() -> None:
    # E-483 (AUD, 20261112210600): sin ``audience`` la base rechaza ``fuente: audiencia`` con 22023.
    parameter = inspect.signature(SupabaseClient.admit_flow_engine_event).parameters["audience"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.default is False
    run, double = opener((row("motor_arranco"),), audience=False)
    asyncio.run(run.open(FLOW, OCCURRENCE, MEMBER))
    assert "audience" not in double.calls[0][2]


def test_the_event_has_exactly_the_keys_the_native_admission_of_e483_accepts() -> None:
    # Las claves que mira _flow_engine_admit_native_audience_v1, leidas de la migracion (no copiadas).
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[1] / "supabase" / "migrations"
           / "20261112210600_e483_native_audience_source_v1.sql").read_text(encoding="utf-8")
    body = sql.split("create function public._flow_engine_admit_native_audience_v1", 1)[1]
    top = body.split("is distinct from array[", 1)[1].split("]", 1)[0]
    chatwoot = body.split("is distinct from array[", 2)[2].split("]", 1)[0]
    keys = sorted(part.strip().strip("'") for part in top.replace("\n", " ").split(","))
    event = audience_event(FLOW, OCCURRENCE, MEMBER, now=NOW, chatwoot_account_id=1, chatwoot_inbox_id=11)
    assert sorted(event) == keys
    assert sorted(event["chatwoot"]) == sorted(part.strip().strip("'") for part in chatwoot.split(","))
    assert type(event["version"]) is int and str(event["destino"]).isdigit() and 6 <= len(event["destino"]) <= 20
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})", event["momento"])


# ---------------------------------------------------------------------------
# El aviso y el armado.
# ---------------------------------------------------------------------------

class FakeProducer:
    def __init__(self) -> None:
        self.commands: list[Any] = []

    async def admit(self, command: Any) -> None:
        self.commands.append(command)


def test_the_brake_notice_is_a_sys_007_with_the_flow_and_the_reason_once_per_occurrence() -> None:
    first = brake_notice(flow_key="difusion", occurrence_id=contact(50), reason="meta_salud:red", occurred_at=NOW)
    again = brake_notice(flow_key="difusion", occurrence_id=contact(50), reason="meta_salud:red", occurred_at=NOW)
    assert first.event_code == BRAKE_EVENT_CODE == "SYS-007" and EVENT_TEMPLATES["SYS-007"].severity == "p2"
    assert (first.component, first.reason_code) == ("flow_audience:difusion", "meta_salud:red")
    assert first.event_id == again.event_id and first.dedupe_key == again.dedupe_key
    producer = FakeProducer()
    asyncio.run(SlackBrakeNotifier(producer=producer, clock=lambda: NOW).braked("difusion", contact(50),
                                                                                "meta_salud:yellow"))
    assert producer.commands[0].reason_code == "meta_salud:yellow"


def _build(environ: Mapping[str, str] | None, **changes: Any) -> Any:
    values: dict[str, Any] = dict(environ=environ, supabase_base_url="https://att1-rest.internal",
                                  supabase_service_role_key="service-key", tenant_ref="lancemos", ally_ref="att1",
                                  chatwoot_account_id=1, chatwoot_inbox_id=11, chatwoot=None,
                                  flow_engine_runtime=None, supabase=None, producer=None)
    values.update(changes)
    return build_flow_audience(**values)


class Untouchable:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"con el flag apagado no se toca {name}")


def test_with_the_flag_off_nothing_is_built_or_touched() -> None:
    assert _build({}, supabase=Untouchable(), chatwoot=Untouchable(), producer=Untouchable()) is None
    assert _build({fs.FLAG: "1"}, supabase=Untouchable()) is None


def test_with_the_flag_on_it_does_not_start_without_the_health_reading_or_the_manifest() -> None:
    with pytest.raises(FlowAudienceConfigError, match=fs.HEALTH_PATH_VARIABLE):
        _build({fs.FLAG: "true"})
    with pytest.raises(FlowAudienceConfigError, match="tenant_ref"):
        _build({fs.FLAG: "true", fs.HEALTH_PATH_VARIABLE: "/salud/estado.json"}, tenant_ref=None)


def test_without_the_engine_it_builds_and_says_sin_motor() -> None:
    built = _build({fs.FLAG: "true", fs.HEALTH_PATH_VARIABLE: "/salud/estado.json"})
    assert built is not None and built.readiness()["flow_audience"] == fs.STATE_NO_ENGINE


class RuntimeDouble:
    def __init__(self, *, native_audience: bool) -> None:
        self.instance = dict(INSTANCE)
        self.native_priority_wins = native_audience
        self.native_audience = native_audience


class SupabaseDouble:
    def __init__(self) -> None:
        self.admit_flow_engine_event = AdmitDouble((row("motor_arranco"),))


def test_the_opener_is_built_only_with_the_multitouch_flag_of_the_engine() -> None:
    # Sin FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED (native_audience del runtime) la base rechazaria a cada
    # persona: no hay opener y nadie se suelta; con el, el opener pasa audience=True.
    environ = {fs.FLAG: "true", fs.HEALTH_PATH_VARIABLE: "/salud/estado.json"}
    off = _build(environ, flow_engine_runtime=RuntimeDouble(native_audience=False), supabase=SupabaseDouble())
    assert off.readiness()["flow_audience"] == fs.STATE_NO_ENGINE
    supabase = SupabaseDouble()
    on = _build(environ, flow_engine_runtime=RuntimeDouble(native_audience=True), supabase=supabase)
    assert on.readiness()["flow_audience"] != fs.STATE_NO_ENGINE
    opener_built = on.sweeper._opener
    asyncio.run(opener_built.open(FLOW, OCCURRENCE, MEMBER))
    assert supabase.admit_flow_engine_event.calls[0][2] == {"priority_wins": True, "audience": True}


# ---------------------------------------------------------------------------
# La prueba de punta a punta (con la base emulada, el doble del motor y Slack falso).
# ---------------------------------------------------------------------------

def _end_to_end(health: Mapping[str, Any] | None) -> tuple[fs.AudienceSweeper, AudienceBase, AdmitDouble,
                                                           FakeProducer]:
    base = AudienceBase(difusion(ritmo={"por_hora": 2, "por_ocurrencia": 10}),
                        [candidate_row(1), candidate_row(2), candidate_row(3), candidate_row(4, opted_out=True)])
    store = store_for(base)
    run, double = opener((row("motor_arranco"),))
    producer = FakeProducer()
    clock = {"now": NOW}
    sweeper = fs.AudienceSweeper(store=store, source=BaseAudienceSource(store=store, labels=None),
                                 health=lambda: health, opener=run,
                                 notifier=SlackBrakeNotifier(producer=producer, clock=lambda: clock["now"]),
                                 clock=lambda: clock["now"])
    return sweeper, base, double, producer


def test_end_to_end_a_date_comes_due_goes_out_at_its_pace_and_finishes() -> None:
    sweeper, base, double, producer = _end_to_end(GREEN)
    first = asyncio.run(sweeper.sweep_once())
    assert (first.materialized, first.released, first.started) == (1, 2, 2)
    (occurrence,) = base.occurrences.values()
    assert len(occurrence["members"]) == 3  # la baja quedo afuera en el bridge
    events = [call[1] for call in double.calls]
    assert {e["contacto_id"] for e in events} == {contact(1), contact(2)}
    assert all(e["fuente"] == "audiencia" and e["destino"].startswith("521") for e in events)
    # La pasada siguiente (la base lleva la cuenta por hora; el emulador suelta por_hora por pasada).
    second = asyncio.run(sweeper.sweep_once())
    assert (second.materialized, second.released) == (0, 1)
    assert occurrence["state"] == "terminada" and producer.commands == []
    assert all(phone(n) not in json.dumps(sweeper.readiness()) for n in (1, 2, 3))


def test_end_to_end_a_red_number_brakes_before_anyone_goes_out_and_tells_slack() -> None:
    sweeper, base, double, producer = _end_to_end({"calidad": "RED", "leido_en": GREEN["leido_en"]})
    report = asyncio.run(sweeper.sweep_once())
    assert (report.materialized, report.braked, report.released) == (1, 1, 0)
    assert double.calls == []
    (occurrence,) = base.occurrences.values()
    assert (occurrence["state"], occurrence["brake"]) == ("frenada", "meta_salud:red")
    (command,) = producer.commands
    assert command.event_code == "SYS-007" and command.component == "flow_audience:difusion"
    assert asyncio.run(sweeper.sweep_once()).released == 0  # una frenada no sale mas aunque vuelva el verde


def test_the_skill_of_the_ae_says_the_numbers_of_the_code_and_that_a_person_confirms() -> None:
    from pathlib import Path

    from bridge import flow_audience as fa
    from bridge.flow_audience_page import PATH, TITLE

    skill = (Path(__file__).resolve().parents[1] / "profiles" / "automation-expert" / "skills" / "flujos"
             / "programar-un-envio" / "SKILL.md").read_text(encoding="utf-8")
    assert f"{fa.DEFAULT_PER_HOUR} personas por hora" in skill
    assert f"{fa.DEFAULT_PER_OCCURRENCE:,}".replace(",", ".") + " por envío" in skill
    assert f"{fa.MAX_PER_OCCURRENCE:,}".replace(",", ".") in skill and "7 días" in skill and "30" in skill
    assert TITLE in skill and PATH.rsplit("/", 1)[1] in skill
    assert "Vos nunca lo lanzás" in skill and "amarillo o en rojo" in skill
