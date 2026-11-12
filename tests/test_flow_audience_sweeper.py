"""El barredor del disparo por audiencia (E-484, P19): la salud frena, el ritmo lo lleva la base.

Contrato: docs/contracts/flow-audience-trigger-v1.md. Los fakes imitan la forma de lo que
contestan las funciones de 20261112100100 (su validador PGlite las prueba contra la base).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from bridge import flow_audience as fa
from bridge import flow_audience_sweeper as fs

from test_flow_audience import difusion, person

NOW = datetime(2026, 12, 1, 16, 5, tzinfo=timezone.utc)
GREEN = {"calidad": "GREEN", "leido_en": (NOW - timedelta(hours=2)).isoformat()}


class FakeStore:
    def __init__(self, definition: Mapping[str, Any]) -> None:
        self.flow = fs.AudienceFlow("f-1", 1, "difusion", definition)
        self.occurrences: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    async def read_audience_flows(self) -> Sequence[fs.AudienceFlow]:
        return (self.flow,)

    async def read_occurrences(self) -> Sequence[fs.OccurrenceRow]:
        return tuple(fs.OccurrenceRow(key, "f-1", row["state"], row["form"]) for key, row in self.occurrences.items())

    async def materialize(self, flow: fs.AudienceFlow, occurrence: fa.Occurrence, members: Sequence[fa.Candidate],
                          fingerprint: str) -> Mapping[str, Any]:
        self.calls.append(f"materialize:{occurrence.key}")
        if occurrence.key in self.occurrences:
            return {"ok": True, "created": False}
        self.occurrences[occurrence.key] = {"state": "confirmada", "form": "fecha",
                                            "pending": [m.contact_id for m in members]}
        return {"ok": True, "created": True, "member_count": len(members), "omitted_count": 0}

    async def brake(self, occurrence_id: str, reason: str) -> Mapping[str, Any]:
        self.calls.append(f"brake:{reason}")
        self.occurrences[occurrence_id]["state"] = "frenada"
        return {"ok": True}

    async def release(self, occurrence_id: str, *, now: datetime, limit: int) -> Mapping[str, Any]:
        row = self.occurrences[occurrence_id]
        chosen, row["pending"] = row["pending"][:limit], row["pending"][limit:]
        row["state"] = "enviando" if row["pending"] else "terminada"
        return {"ok": True, "released": [{"contact_id": c} for c in chosen], "pending": len(row["pending"])}


class FakeSource:
    def __init__(self, candidates: list[fa.Candidate]) -> None:
        self.list = candidates

    async def candidates(self, spec: fa.AudienceSpec) -> list[fa.Candidate]:
        return self.list


class FakeOpener:
    def __init__(self) -> None:
        self.opened: list[str] = []

    async def open(self, flow_id: str, contact_id: str, occurrence_id: str) -> None:
        self.opened.append(contact_id)


class FakeNotifier:
    def __init__(self) -> None:
        self.notices: list[str] = []

    async def braked(self, flow_key: str, occurrence_id: str, reason: str) -> None:
        self.notices.append(reason)


def sweeper(health: Mapping[str, Any] | None, *, opener: FakeOpener | None = None,
            notifier: FakeNotifier | None = None) -> tuple[fs.AudienceSweeper, FakeStore]:
    store = FakeStore(difusion())
    source = FakeSource([person(1), person(2), person(3, opted_out=True)])
    return fs.AudienceSweeper(store=store, source=source, health=lambda: health, opener=opener,
                              notifier=notifier, clock=lambda: NOW), store


def test_the_flag_is_off_unless_it_says_true() -> None:
    assert not fs.enabled({})
    assert not fs.enabled({fs.FLAG: "1"})
    assert fs.enabled({fs.FLAG: "true"})


def test_a_due_date_materializes_once_and_releases_to_the_engine() -> None:
    opener = FakeOpener()
    sweep, store = sweeper(GREEN, opener=opener)
    first = asyncio.run(sweep.sweep_once())
    second = asyncio.run(sweep.sweep_once())
    assert (first.materialized, first.released) == (1, 2)
    assert (second.materialized, second.released) == (0, 0)
    assert len(opener.opened) == 2
    assert store.calls.count("materialize:fecha:2026-12-01T16:00Z") == 2  # la base contesta created: false


def test_red_or_yellow_or_no_reading_brakes_and_notifies() -> None:
    for health, reason in [
        ({"calidad": "RED", "leido_en": GREEN["leido_en"]}, "meta_salud:red"),
        ({"calidad": "YELLOW", "leido_en": GREEN["leido_en"]}, "meta_salud:yellow"),
        (None, fs.BRAKE_NO_READING),
        ({"calidad": "GREEN", "leido_en": (NOW - timedelta(hours=27)).isoformat()}, fs.BRAKE_STALE),
        ({"calidad": "GREEN", "leido_en": "2026-12-01T10:00"}, fs.BRAKE_NO_READING),
    ]:
        opener, notifier = FakeOpener(), FakeNotifier()
        sweep, store = sweeper(health, opener=opener, notifier=notifier)
        report = asyncio.run(sweep.sweep_once())
        assert (report.braked, report.released, opener.opened) == (1, 0, []), reason
        assert notifier.notices == [reason]
        assert asyncio.run(sweep.sweep_once()).released == 0


def test_without_an_engine_nothing_is_released() -> None:
    sweep, store = sweeper(GREEN, opener=None)
    report = asyncio.run(sweep.sweep_once())
    assert (report.materialized, report.released, report.waiting_for_engine) == (1, 0, 1)
    assert sweep.readiness()["state"] == fs.STATE_NO_ENGINE
    assert all(row["state"] == "confirmada" for row in store.occurrences.values())


def test_a_definition_the_sweeper_cannot_read_is_refused_by_name() -> None:
    store = FakeStore(difusion(entra={"permiso": "conversacion"}))
    sweep = fs.AudienceSweeper(store=store, source=FakeSource([]), health=lambda: GREEN, opener=FakeOpener(),
                               clock=lambda: NOW)
    report = asyncio.run(sweep.sweep_once())
    assert report.refused == {"difusion": "permiso"} and store.calls == []


def test_the_health_file_reader_is_fail_closed(tmp_path: Any) -> None:
    missing = fs.file_health_reader(tmp_path / "no.json")
    assert missing() is None
    broken = tmp_path / "rota.json"
    broken.write_text("{", encoding="utf-8")
    assert fs.file_health_reader(broken)() is None
    good = tmp_path / "salud.json"
    good.write_text('{"calidad": "GREEN", "leido_en": "2026-12-01T14:00:00+00:00"}', encoding="utf-8")
    assert fs.health_verdict(fs.file_health_reader(good)(), now=NOW).allows
