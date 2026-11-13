"""El barredor del disparo por audiencia (E-484, P19): la salud frena, el ritmo lo lleva la base.

Contrato: docs/contracts/flow-audience-trigger-v1.md. Los fakes imitan la forma de lo que
contestan las funciones de 20261112220000 (su validador PGlite las prueba contra la base:
tests/sql/followup_engine/validate_flow_audience_triggers.mjs).
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from bridge import flow_audience as fa
from bridge import flow_audience_sweeper as fs

from test_flow_audience import difusion, person

NOW = datetime(2026, 12, 1, 16, 5, tzinfo=timezone.utc)
GREEN = {"calidad": "GREEN", "leido_en": (NOW - timedelta(hours=2)).isoformat()}


class FakeStore:
    def __init__(self, definition: Mapping[str, Any], *, version: int = 1) -> None:
        self.flow = fs.AudienceFlow("f-1", version, "difusion", definition, product_ref="att1", state="prendido")
        self.occurrences: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.settled: list[tuple[str, str]] = []

    async def read_audience_flows(self) -> Sequence[fs.AudienceFlow]:
        return (self.flow,)

    async def read_occurrences(self) -> Sequence[fs.OccurrenceRow]:
        return tuple(fs.OccurrenceRow(key, "f-1", row["state"], row["form"], row["version"])
                     for key, row in self.occurrences.items())

    async def materialize(self, flow: fs.AudienceFlow, occurrence: fa.Occurrence, members: Sequence[fa.Candidate],
                          fingerprint: str, *, materialized_by: str = "barredor") -> Mapping[str, Any]:
        self.calls.append(f"materialize:{occurrence.key}:{materialized_by}")
        if occurrence.key in self.occurrences:
            return {"ok": True, "created": False}
        manual = occurrence.key.startswith("manual:")
        self.occurrences[occurrence.key] = {"state": "materializada" if manual else "confirmada",
                                            "form": occurrence.key.split(":")[0], "version": flow.version,
                                            "pending": [m for m in members], "unsettled": []}
        return {"ok": True, "created": True, "occurrence_id": occurrence.key, "member_count": len(members),
                "omitted_count": 0}

    async def brake(self, occurrence_id: str, reason: str) -> Mapping[str, Any]:
        self.calls.append(f"brake:{reason}")
        self.occurrences[occurrence_id]["state"] = "frenada"
        return {"ok": True}

    async def release(self, occurrence_id: str, *, now: datetime, limit: int) -> Mapping[str, Any]:
        row = self.occurrences[occurrence_id]
        again = [fs.ReleasedMember(m.contact_id, m.phone, again=True) for m in row["unsettled"]]
        chosen, row["pending"] = row["pending"][:limit], row["pending"][limit:]
        row["unsettled"] = row["unsettled"] + chosen
        row["state"] = "enviando"
        return {"ok": True, "released": again + [fs.ReleasedMember(m.contact_id, m.phone) for m in chosen],
                "pending": len(row["pending"])}

    async def settle(self, occurrence_id: str, contact_id: str, outcome: str) -> Mapping[str, Any]:
        row = self.occurrences[occurrence_id]
        row["unsettled"] = [m for m in row["unsettled"] if m.contact_id != contact_id]
        self.settled.append((contact_id[-1], outcome))
        if not row["pending"] and not row["unsettled"]:
            row["state"] = "terminada"
        return {"ok": True, "settled": True, "state": row["state"]}


class FakeSource:
    def __init__(self, candidates: list[fa.Candidate]) -> None:
        self.list = candidates

    async def candidates(self, flow: fs.AudienceFlow, spec: fa.AudienceSpec) -> list[fa.Candidate]:
        return self.list


class FakeOpener:
    """El RunOpener: contesta lo de la lista, o no contesta (AudienceOpenRetry) para quien este en ``down``."""

    def __init__(self, *, down: set[str] | None = None, outcome: str = fs.OPEN_STARTED) -> None:
        self.opened: list[str] = []
        self.down = down or set()
        self.outcome = outcome

    async def open(self, flow: fs.AudienceFlow, occurrence: fs.OccurrenceRow, member: fs.ReleasedMember) -> str:
        if member.contact_id in self.down:
            raise fs.AudienceOpenRetry()
        self.opened.append(member.contact_id)
        return self.outcome


class FakeNotifier:
    def __init__(self) -> None:
        self.notices: list[str] = []

    async def braked(self, flow_key: str, occurrence_id: str, reason: str) -> None:
        self.notices.append(reason)


def sweeper(health: Mapping[str, Any] | None, *, opener: FakeOpener | None = None,
            notifier: FakeNotifier | None = None, definition: Mapping[str, Any] | None = None
            ) -> tuple[fs.AudienceSweeper, FakeStore]:
    store = FakeStore(definition or difusion())
    source = FakeSource([person(1), person(2), person(3, opted_out=True)])
    return fs.AudienceSweeper(store=store, source=source, health=lambda: health, opener=opener,
                              notifier=notifier, clock=lambda: NOW), store


def test_the_flag_is_off_unless_it_says_true() -> None:
    assert not fs.enabled({})
    assert not fs.enabled({fs.FLAG: "1"})
    assert fs.enabled({fs.FLAG: "true"})


def test_a_due_date_materializes_once_releases_to_the_engine_and_settles() -> None:
    opener = FakeOpener()
    sweep, store = sweeper(GREEN, opener=opener)
    first = asyncio.run(sweep.sweep_once())
    second = asyncio.run(sweep.sweep_once())
    assert (first.materialized, first.released, first.started) == (1, 2, 2)
    assert (second.materialized, second.released) == (0, 0)
    assert len(opener.opened) == 2
    assert store.settled == [("1", "arranco"), ("2", "arranco")]
    assert store.calls.count("materialize:fecha:2026-12-01T16:00Z:barredor") == 2  # la base contesta created: false
    assert all(row["state"] == "terminada" for row in store.occurrences.values())


def test_an_engine_that_does_not_answer_is_not_settled_and_comes_back() -> None:
    down = {person(2).contact_id}
    opener = FakeOpener(down=down)
    sweep, store = sweeper(GREEN, opener=opener)
    first = asyncio.run(sweep.sweep_once())
    assert (first.released, first.retry, store.settled) == (1, 1, [("1", "arranco")])
    (row,) = store.occurrences.values()
    assert row["state"] == "enviando"  # no termina con alguien sin asentar
    opener.down.clear()
    second = asyncio.run(sweep.sweep_once())
    assert (second.released, second.retry) == (0, 0)  # la re-entrega no cuenta otra vez
    assert store.settled[-1] == ("2", "arranco") and row["state"] == "terminada"


def test_a_refusal_of_the_engine_is_settled_as_not_started() -> None:
    opener = FakeOpener(outcome=fs.OPEN_NOT_STARTED)
    sweep, store = sweeper(GREEN, opener=opener)
    report = asyncio.run(sweep.sweep_once())
    assert (report.released, report.started) == (2, 0)
    assert store.settled == [("1", "no_arranca"), ("2", "no_arranca")]


def test_red_or_yellow_or_blocked_or_no_reading_brakes_and_notifies() -> None:
    for health, reason in [
        ({"calidad": "RED", "leido_en": GREEN["leido_en"]}, "meta_salud:red"),
        ({"calidad": "YELLOW", "leido_en": GREEN["leido_en"]}, "meta_salud:yellow"),
        ({**GREEN, "puede_enviar": "BLOCKED"}, "meta_salud:blocked"),
        (None, fs.BRAKE_NO_READING),
        ({"calidad": "GREEN", "leido_en": (NOW - timedelta(hours=27)).isoformat()}, fs.BRAKE_STALE),
        ({"calidad": "GREEN", "leido_en": "2026-12-01T10:00"}, fs.BRAKE_NO_READING),
        ({"calidad": "UNKNOWN", "leido_en": GREEN["leido_en"]}, fs.BRAKE_NO_READING),
    ]:
        opener, notifier = FakeOpener(), FakeNotifier()
        sweep, store = sweeper(health, opener=opener, notifier=notifier)
        report = asyncio.run(sweep.sweep_once())
        assert (report.braked, report.released, opener.opened) == (1, 0, []), reason
        assert notifier.notices == [reason]
        assert asyncio.run(sweep.sweep_once()).released == 0


def test_a_notice_that_fails_does_not_undo_the_brake() -> None:
    class Broken:
        async def braked(self, flow_key: str, occurrence_id: str, reason: str) -> None:
            raise RuntimeError("slack caido")

    store = FakeStore(difusion())
    sweep = fs.AudienceSweeper(store=store, source=FakeSource([person(1)]), health=lambda: None,
                               opener=FakeOpener(), notifier=Broken(), clock=lambda: NOW)
    assert asyncio.run(sweep.sweep_once()).braked == 1
    assert all(row["state"] == "frenada" for row in store.occurrences.values())


def test_without_an_engine_nothing_is_released() -> None:
    sweep, store = sweeper(GREEN, opener=None)
    report = asyncio.run(sweep.sweep_once())
    assert (report.materialized, report.released, report.waiting_for_engine) == (1, 0, 1)
    assert sweep.readiness()["flow_audience"] == fs.STATE_NO_ENGINE
    assert all(row["state"] == "confirmada" for row in store.occurrences.values())


def test_a_list_of_another_version_is_not_released() -> None:
    opener = FakeOpener()
    sweep, store = sweeper(GREEN, opener=opener)
    asyncio.run(sweep.sweep_once())
    store.occurrences["viejo"] = {"state": "confirmada", "form": "fecha", "version": 7,
                                  "pending": [person(4)], "unsettled": []}
    report = asyncio.run(sweep.sweep_once())
    assert report.refused == {"viejo": "flujo_no_corre"}
    assert person(4).contact_id not in opener.opened


def test_a_definition_the_sweeper_cannot_read_is_refused_by_name() -> None:
    store = FakeStore(difusion(entra={"permiso": "conversacion"}))
    sweep = fs.AudienceSweeper(store=store, source=FakeSource([]), health=lambda: GREEN, opener=FakeOpener(),
                               clock=lambda: NOW)
    report = asyncio.run(sweep.sweep_once())
    assert report.refused == {"difusion": "permiso"} and store.calls == []


def test_a_source_that_fails_refuses_that_flow_and_materializes_nothing() -> None:
    class Failing:
        async def candidates(self, flow: fs.AudienceFlow, spec: fa.AudienceSpec) -> list[fa.Candidate]:
            raise RuntimeError("chatwoot")

    store = FakeStore(difusion())
    sweep = fs.AudienceSweeper(store=store, source=Failing(), health=lambda: GREEN, opener=FakeOpener(),
                               clock=lambda: NOW)
    report = asyncio.run(sweep.sweep_once())
    assert report.refused == {"difusion": "materializar:RuntimeError"} and store.occurrences == {}


def test_manual_is_built_by_a_person_and_waits_for_confirmation() -> None:
    opener = FakeOpener()
    sweep, store = sweeper(GREEN, opener=opener, definition=difusion(dispara={"manual": {}}))
    assert asyncio.run(sweep.sweep_once()).materialized == 0  # la pasada nunca arma un manual
    result = asyncio.run(sweep.materialize_manual("f-1", slack_user_id="U0DANSCHWAB1"))
    assert result.ok and result.member_count == 2
    assert store.calls[-1].startswith("materialize:manual:") and store.calls[-1].endswith(":slack:U0DANSCHWAB1")
    asyncio.run(sweep.sweep_once())
    assert opener.opened == []  # sin confirmar no sale nadie
    other, _ = sweeper(GREEN, definition=difusion())
    assert asyncio.run(other.materialize_manual("f-1", slack_user_id="U0DANSCHWAB1")).reason_code == "flow_not_manual"
    assert asyncio.run(other.materialize_manual("f-9", slack_user_id="U0DANSCHWAB1")).reason_code == \
        "flow_not_running"


def test_the_health_file_reader_is_fail_closed(tmp_path: Any) -> None:
    missing = fs.file_health_reader(tmp_path / "no.json")
    assert missing() is None
    broken = tmp_path / "rota.json"
    broken.write_text("{", encoding="utf-8")
    assert fs.file_health_reader(broken)() is None
    good = tmp_path / "salud.json"
    good.write_text('{"calidad": "GREEN", "leido_en": "2026-12-01T14:00:00+00:00"}', encoding="utf-8")
    assert fs.health_verdict(fs.file_health_reader(good)(), now=NOW).allows


def test_the_health_file_of_meta_salud_is_read_by_instance_with_its_own_reading_time(tmp_path: Any) -> None:
    # La forma de /var/lib/meta-salud/estado.json (meta_salud.py, correr()): una lectura por instancia. La hora
    # es la leido_en de la lectura de la instancia, nunca la de la escritura del archivo.
    state = tmp_path / "estado.json"
    fresh = (NOW - timedelta(hours=3)).isoformat()
    state.write_text(json.dumps({
        "att1": {"calidad": "GREEN", "limite": "TIER_10K", "estado_numero": "CONNECTED", "puede_enviar": "AVAILABLE",
                 "leido_en": fresh},
        "maribel": {"calidad": "YELLOW", "limite": "TIER_1K", "estado_numero": "CONNECTED", "leido_en": fresh},
        "entregas": {}}), encoding="utf-8")
    assert fs.health_verdict(fs.file_health_reader(state, instance_key="att1")(), now=NOW).allows
    assert fs.health_verdict(fs.file_health_reader(state, instance_key="maribel")(), now=NOW).brake_reason == \
        "meta_salud:yellow"
    assert fs.file_health_reader(state, instance_key="johanna")() is None


def test_a_later_write_of_the_file_does_not_refresh_an_old_reading(tmp_path: Any) -> None:
    # Correccion de la revision (2026-11-13): el timer de las entregas reescribe el estado.json cada hora sin volver
    # a leer la salud. Con la hora del archivo como leido_en, una lectura de hace 30 h parecia de recien y el freno
    # por lectura vieja no vencia nunca.
    # La lectura de la salud de hace 30 h, como la deja meta_salud.py hoy (sin leido_en por instancia).
    state = tmp_path / "estado.json"
    state.write_text(json.dumps({"att1": {"calidad": "GREEN", "puede_enviar": "AVAILABLE"}}), encoding="utf-8")
    old = (NOW - timedelta(hours=30)).timestamp()
    os.utime(state, (old, old))
    # La segunda escritura (otra clave, la misma lectura de la salud), recien.
    document = json.loads(state.read_text(encoding="utf-8"))
    document["entregas"] = {"att1": {"episodio": None}}
    state.write_text(json.dumps(document), encoding="utf-8")
    recent = (NOW - timedelta(minutes=5)).timestamp()
    os.utime(state, (recent, recent))
    assert not fs.health_verdict(fs.file_health_reader(state, instance_key="att1")(), now=NOW).allows
    # Con su leido_en de hace 30 h, la segunda escritura tampoco la refresca.
    document["att1"]["leido_en"] = (NOW - timedelta(hours=30)).isoformat()
    state.write_text(json.dumps(document), encoding="utf-8")
    os.utime(state, (recent, recent))
    assert fs.health_verdict(fs.file_health_reader(state, instance_key="att1")(), now=NOW).brake_reason == \
        fs.BRAKE_STALE


def test_a_reading_without_its_time_is_an_unknown_reading(tmp_path: Any) -> None:
    # Sin leido_en de la instancia (el estado.json de hoy de meta_salud.py) la lectura no tiene hora: frena.
    state = tmp_path / "estado.json"
    state.write_text(json.dumps({"att1": {"calidad": "GREEN", "puede_enviar": "AVAILABLE"}}), encoding="utf-8")
    recent = (NOW - timedelta(minutes=5)).timestamp()
    os.utime(state, (recent, recent))
    assert fs.health_verdict(fs.file_health_reader(state, instance_key="att1")(), now=NOW).brake_reason == \
        fs.BRAKE_NO_READING
    loose = tmp_path / "suelta.json"
    loose.write_text(json.dumps({"calidad": "GREEN"}), encoding="utf-8")
    assert fs.health_verdict(fs.file_health_reader(loose)(), now=NOW).brake_reason == fs.BRAKE_NO_READING
