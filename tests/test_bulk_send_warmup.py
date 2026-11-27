"""El calentamiento y el espaciado del masivo (F2, ``src/bridge/bulk_send_warmup.py``, D7-D10 y D-F2.1-8).

Mapa: ``OS/productos/soporte-infoproductores/documentacion/2026-11-27-mapa-frenos-meta-y-calentamiento.md``.

* La funcion pura (``warmup_today``, ``check_size``) con los bordes: sin historial, zona horaria y cambio de
  dia, las 24 h, el freno de Meta, la respuesta baja o sin leer, el techo, el espaciado y la configuracion
  invalida.
* ``BulkSendWarmup``: el historial de ``read_bulk_sends_v1`` y las respuestas del reporte, leidas una vez por
  tanda y fuera del candado de la pagina. La lectura real de Chatwoot es la del reporte del masivo sobre la
  conversacion 50 de ATT1 capturada (``chatwoot_conversation_inbox_11_conv_50_opt_out_paused_20261017.json``,
  la misma de ``test_bulk_send_wiring``): no hace falta un borde nuevo.
* El cableado: el motor frena al arrancar lo que no entra, la pagina no confirma, el AE recibe el mismo
  ``detalle``, ``/ready`` y el flag. Apagado, nada cambia.

Ningun dato personal: los telefonos son los sinteticos de los otros tests del masivo.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from bridge import bulk_send_warmup as bw
from bridge import flow_change_api
from bridge.bulk_send_brakes import Verdict
from bridge.bulk_send_warmup import (
    BulkSendWarmup,
    LargeSend,
    WarmupConfigError,
    WarmupPolicy,
    check_size,
    large_send_of,
    parse_steps,
    policy_from_env,
    warmup_today,
)

CDMX = ZoneInfo("America/Mexico_City")
POLICY = WarmupPolicy()
# Jueves 27/11 14:00 de CDMX (UTC-6).
NOW = datetime(2026, 11, 27, 20, 0, tzinfo=UTC)


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def big(send_id: str = "aaaaaaaa-0000-4000-8000-000000000001", *, size: int = 500, state: str = "terminado",
        started: datetime | None = None, ended: datetime | None = None, reason: str | None = None,
        delivered: int | None = 480, replies: int | None = 48) -> LargeSend:
    started = started if started is not None else NOW - timedelta(hours=50)
    if ended is None and state in ("terminado", "frenado"):
        ended = started + timedelta(minutes=40)
    return LargeSend(send_id=send_id, size=size, state=state, started_at=started, ended_at=ended,
                     brake_reason=reason, delivered=delivered, replies=replies)


def today(history: list[LargeSend], at: datetime = NOW, **kwargs: Any) -> bw.WarmupToday:
    return warmup_today(history, at=at, zone=CDMX, policy=kwargs.pop("policy", POLICY), **kwargs)


# ------------------------------------------------------------------ la configuracion


def test_the_defaults_are_the_ones_of_the_map() -> None:
    assert POLICY.steps == (500, 1000, 5000) and POLICY.min_reply_rate == 0.05
    assert POLICY.min_gap_days == 1 and POLICY.pilot_size == 20 and POLICY.settle == timedelta(hours=24)
    assert [POLICY.step_of(n) for n in (21, 500, 501, 1000, 4999, 5000, 9000)] == [0, 0, 1, 1, 2, 2, 2]


def test_off_unless_the_flag_says_true() -> None:
    for environ in ({}, {bw.FLAG: "false"}, {bw.FLAG: "1"}, {bw.FLAG: "yes"}):
        assert policy_from_env(environ) is None
    # Apagado no valida lo demas: nada de esto existe.
    assert policy_from_env({bw.FLAG: "false", bw.STEPS_VARIABLE: "basura"}) is None
    assert policy_from_env({bw.FLAG: " TRUE "}) == POLICY


def test_the_configuration_is_read_and_validated() -> None:
    policy = policy_from_env({bw.FLAG: "true", bw.STEPS_VARIABLE: "250, 1.000,2500",
                              bw.MIN_REPLY_RATE_VARIABLE: "0.02", bw.MIN_GAP_DAYS_VARIABLE: "2"})
    assert policy == WarmupPolicy(steps=(250, 1000, 2500), min_reply_rate=0.02, min_gap_days=2)
    assert parse_steps("500") == (500,)


@pytest.mark.parametrize("environ", [
    {bw.STEPS_VARIABLE: "500,abc"},
    {bw.STEPS_VARIABLE: ""},  # vacio es el default; ver abajo
    {bw.STEPS_VARIABLE: "1000,500"},
    {bw.STEPS_VARIABLE: "500,500"},
    {bw.STEPS_VARIABLE: "20,500"},  # no supera la tanda piloto
    {bw.STEPS_VARIABLE: "500,30000"},  # pasa el tope de una lista
    {bw.STEPS_VARIABLE: "-500"},
    {bw.STEPS_VARIABLE: "1,2,3,4,5,6,7,8,9,10,11".replace("1,", "100,", 1)},
    {bw.MIN_REPLY_RATE_VARIABLE: "1.5"},
    {bw.MIN_REPLY_RATE_VARIABLE: "-0.1"},
    {bw.MIN_REPLY_RATE_VARIABLE: "nan"},
    {bw.MIN_REPLY_RATE_VARIABLE: "cinco"},
    {bw.MIN_GAP_DAYS_VARIABLE: "0"},
    {bw.MIN_GAP_DAYS_VARIABLE: "31"},
    {bw.MIN_GAP_DAYS_VARIABLE: "1.5"},
])
def test_an_invalid_configuration_does_not_build(environ: dict[str, str]) -> None:
    if environ == {bw.STEPS_VARIABLE: ""}:
        assert policy_from_env({bw.FLAG: "true", **environ}) == POLICY
        return
    with pytest.raises(WarmupConfigError):
        policy_from_env({bw.FLAG: "true", **environ})


def test_the_policy_itself_refuses_what_does_not_close() -> None:
    for kwargs in ({"steps": ()}, {"steps": (500, True)}, {"min_reply_rate": math.inf},
                   {"min_gap_days": True}, {"settle": timedelta(hours=-1)}, {"steps": [500, 1000]}):
        with pytest.raises(WarmupConfigError):
            WarmupPolicy(**kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------------ el escalon


def test_without_history_the_first_step() -> None:
    result = today([])
    assert (result.cap, result.step, result.large_allowed, result.reason) == (500, 0, True, "sin_historial")
    assert "primer escalón es de 500" in result.detail and result.next_at is None


def test_sends_of_the_pilot_size_or_drafts_are_not_large_sends() -> None:
    result = today([big(size=20), big(size=5, state="enviando", ended=None)])
    assert result.reason == "sin_historial" and result.large_allowed


def test_a_good_send_24_hours_ago_goes_up_one_step() -> None:
    result = today([big(size=500)])
    assert (result.cap, result.step, result.reason, result.large_allowed) == (1000, 1, "subio", True)
    assert "10 %" in result.detail and "sube a 1.000" in result.detail
    # Con 600 personas uso el de 1.000 (D-F2.1): sube a 5.000.
    assert today([big(size=600, delivered=600, replies=30)]).cap == 5000


def test_the_reply_rate_is_measured_over_the_delivered() -> None:
    # Justo en el 5 %: alcanza. Un poco menos: repite.
    assert today([big(delivered=400, replies=20)]).reason == "subio"
    low = today([big(delivered=400, replies=19)])
    assert (low.cap, low.reason) == (500, "respuesta_baja") and "4,8 %" in low.detail
    # Nadie entregado: tasa cero, repite.
    assert today([big(delivered=0, replies=0)]).reason == "respuesta_baja"


def test_without_the_reply_reading_it_repeats_and_never_goes_up() -> None:
    result = today([big(delivered=None, replies=None)])
    assert (result.cap, result.reason) == (500, "respuestas_sin_leer")


def test_less_than_24_hours_repeats_and_says_from_when_it_could_go_up() -> None:
    # Termino ayer (26/11 14:00 de CDMX): hoy se puede otra, pero en el mismo escalon hasta las 24 h.
    ended = NOW - timedelta(hours=24) + timedelta(minutes=1)
    history = [big(started=ended - timedelta(minutes=40), ended=ended)]
    result = today(history)
    assert (result.cap, result.reason, result.large_allowed) == (500, "menos_de_24h", True)
    assert result.next_at == ended + timedelta(hours=24) and result.next_cap == 1000
    assert "podría subir a 1.000" in result.detail and "27/11 14:01" in result.detail
    # Cumplidas las 24 h, sube.
    assert today(history, at=ended + timedelta(hours=24)).reason == "subio"
    # Si ademas empezo hoy, el espaciado manda y dice la fecha mas tardia.
    ended = NOW - timedelta(hours=1)
    blocked = today([big(started=ended - timedelta(minutes=40), ended=ended)])
    assert (blocked.reason, blocked.step_reason) == ("espaciado", "menos_de_24h")
    assert blocked.next_at == ended + timedelta(hours=24) and blocked.next_cap == 1000


def test_a_meta_brake_goes_down_one_step() -> None:
    history = [big(size=900, state="frenado", reason="meta_codigo:131048_x1", delivered=None, replies=None)]
    result = today(history)
    assert (result.cap, result.reason) == (500, "freno_meta") and "131048" in result.detail
    # Ya en el primer escalon, se queda ahi. El freno de Meta dentro del piloto tambien cuenta.
    assert today([big(size=400, state="frenado", reason="piloto:entrega:fallidos_5_de_20")]).cap == 500
    assert today([big(size=4000, state="frenado", reason="salud:maribel:calidad_GREEN_a_YELLOW")]).cap == 1000


def test_a_send_that_did_not_finish_for_another_reason_repeats() -> None:
    for reason in ("chatwoot_api:12_de_50", "corte_por_hora", "a_mano"):
        result = today([big(size=900, state="frenado", reason=reason)])
        assert (result.cap, result.reason) == (1000, "no_termino"), reason


def test_the_ceiling_stays() -> None:
    result = today([big(size=5000, delivered=4800, replies=600)])
    assert (result.cap, result.reason) == (5000, "techo")
    assert today([big(size=8000, delivered=4800, replies=600)]).cap == 5000


def test_only_the_last_finished_large_send_counts() -> None:
    older = big("aaaaaaaa-0000-4000-8000-000000000002", size=5000, started=NOW - timedelta(days=9))
    last = big(size=500, state="frenado", reason="meta_codigo:368_x1", started=NOW - timedelta(days=3))
    assert today([older, last]).cap == 500
    assert today([last, older]).cap == 500


# ------------------------------------------------------------------ el espaciado y el dia de la instancia


def test_one_large_send_per_day_of_the_zone_of_the_instance() -> None:
    # Arranco el 27/11 a las 23:30 de CDMX (28/11 05:30 UTC). A las 00:30 de CDMX del 28/11 ya es otro dia en
    # la zona de la instancia, aunque en UTC sea el mismo: el espaciado deja, y el escalon repite (menos de 24 h).
    started = datetime(2026, 11, 28, 5, 30, tzinfo=UTC)
    history = [big(started=started)]
    same_utc_day = datetime(2026, 11, 28, 6, 30, tzinfo=UTC)
    result = today(history, at=same_utc_day)
    assert result.large_allowed and result.reason == "menos_de_24h"
    # Un minuto antes de la medianoche de CDMX, mismo dia local: no.
    result = today(history, at=datetime(2026, 11, 28, 5, 59, tzinfo=UTC))
    assert not result.large_allowed and result.reason == "espaciado"
    # La proxima: la mas tardia entre el dia siguiente (28/11 00:00) y las 24 h del fin para poder subir.
    assert "28/11 00:00" in result.detail and result.next_at == history[0].ended_at + timedelta(hours=24)
    # En la zona de Bogota (UTC-5) el mismo arranque cae el 28/11 a las 00:30: a las 05:59 UTC es el mismo dia.
    bogota = warmup_today(history, at=datetime(2026, 11, 28, 5, 59, tzinfo=UTC), zone=ZoneInfo("America/Bogota"),
                          policy=POLICY)
    assert not bogota.large_allowed and "29/11 00:00" in bogota.detail


def test_the_minimum_gap_in_days() -> None:
    policy = WarmupPolicy(min_gap_days=2)
    started = datetime(2026, 11, 27, 16, 0, tzinfo=UTC)  # 27/11 10:00 de CDMX
    history = [big(started=started)]
    blocked = today(history, at=datetime(2026, 11, 28, 22, 0, tzinfo=UTC), policy=policy)
    assert not blocked.large_allowed and blocked.next_at == datetime(2026, 11, 29, 0, 0, tzinfo=CDMX)
    assert today(history, at=datetime(2026, 11, 29, 6, 1, tzinfo=UTC), policy=policy).large_allowed


def test_a_live_large_send_blocks_and_a_pending_one_blocks_only_the_approval() -> None:
    live = big("aaaaaaaa-0000-4000-8000-0000000000aa", state="enviando", started=NOW - timedelta(minutes=5), ended=None)
    assert today([live]).reason == "tanda_grande_en_curso" and not today([live]).large_allowed
    pending = LargeSend(send_id="aaaaaaaa-0000-4000-8000-0000000000bb", size=800, state="confirmado")
    assert today([pending]).large_allowed  # al arrancar, la otra confirmada no frena (D-F2.6)
    blocked = today([pending], count_pending=True)
    assert not blocked.large_allowed and blocked.reason == "tanda_grande_confirmada"
    # El masivo que se evalua no se cuenta a si mismo.
    assert today([pending], count_pending=True, exclude_send_id=pending.send_id).large_allowed


def test_times_need_a_zone() -> None:
    with pytest.raises(ValueError, match="con zona"):
        today([], at=datetime(2026, 11, 27, 14, 0))
    with pytest.raises(ValueError, match="con zona"):
        today([big(started=datetime(2026, 11, 25, 14, 0), ended=datetime(2026, 11, 25, 15, 0))])


# ------------------------------------------------------------------ si un masivo entra


def test_check_size() -> None:
    day = today([])
    assert check_size(day, 20, policy=POLICY, zone=CDMX).allows  # la tanda piloto no es grande
    assert check_size(day, 500, policy=POLICY, zone=CDMX) == Verdict(
        True, "calentamiento:entra", check_size(day, 500, policy=POLICY, zone=CDMX).detail)
    over = check_size(day, 501, policy=POLICY, zone=CDMX)
    assert not over.allows and over.reason == "calentamiento:supera_tope_500"
    assert over.detail.startswith("Son 501 personas y el tope del día de este número es 500. Partilo en masivos "
                                  "de hasta 500, uno por día.")
    blocked = today([big(started=NOW - timedelta(hours=2))])
    verdict = check_size(blocked, 100, policy=POLICY, zone=CDMX)
    assert not verdict.allows and verdict.reason == "calentamiento:espaciado" and "28/11 00:00" in verdict.detail
    assert check_size(blocked, 20, policy=POLICY, zone=CDMX).allows


def test_check_size_says_when_waiting_is_enough() -> None:
    ended = NOW - timedelta(hours=20)  # 26/11 18:00 de CDMX: hoy se puede, pero sin subir hasta las 24 h
    day = today([big(started=ended - timedelta(minutes=40), ended=ended)])
    verdict = check_size(day, 900, policy=POLICY, zone=CDMX)
    assert not verdict.allows and "O esperá: desde el" in verdict.detail and "podría ser 1.000" in verdict.detail
    assert "O esperá" not in check_size(day, 2000, policy=POLICY, zone=CDMX).detail


# ------------------------------------------------------------------ de la base al historial


def bulk_view(send_id: str, **changes: Any) -> dict[str, Any]:
    """La forma de ``_bulk_send_view`` (20261125000200) en lo que usa el calentamiento."""
    out: dict[str, Any] = {"send_id": send_id, "state": "terminado", "confirmed_count": 500,
                           "scheduled_at": "2026-11-25T20:00:00+00:00", "started_at": "2026-11-25T19:40:00+00:00",
                           "finished_at": "2026-11-25T20:20:00+00:00", "brake_reason": None, "braked_at": None,
                           "list": {"state": "revisada", "eligible_count": 500}}
    out.update(changes)
    return out


def test_large_send_of_reads_the_view() -> None:
    item = large_send_of(bulk_view("s1"))
    assert item is not None and item.size == 500 and item.ended_at == datetime(2026, 11, 25, 20, 20, tzinfo=UTC)
    braked = large_send_of(bulk_view("s2", state="frenado", finished_at=None, brake_reason="meta_codigo:368_x1",
                                      braked_at="2026-11-25T19:50:00+00:00"))
    assert braked is not None and braked.meta_brake and braked.ended_at == datetime(2026, 11, 25, 19, 50, tzinfo=UTC)
    # Frenado antes de arrancar no es una tanda; un borrador tampoco.
    assert large_send_of(bulk_view("s3", state="frenado", started_at=None, finished_at=None,
                                   brake_reason="regla_24h:freno_meta_x", braked_at="2026-11-25T19:00:00Z")) is None
    assert large_send_of(bulk_view("s4", state="borrador", confirmed_count=None, started_at=None,
                                   finished_at=None)) is None
    # Antes de confirmar, la lista revisada da el tamano; una sin revisar, ninguno.
    assert large_send_of(bulk_view("s5", state="confirmado", confirmed_count=None, started_at=None,
                                   finished_at=None)).size == 500  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        large_send_of({"state": "terminado"})


class Reports:
    def __init__(self, *, delivered: int = 480, replies: int = 48, failures: int = 0) -> None:
        self.delivered, self.replies, self.failures = delivered, replies, failures
        self.calls: list[str] = []

    async def __call__(self, send_id: str) -> Any:
        self.calls.append(send_id)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("chatwoot_unavailable")
        return type("R", (), {"delivered": self.delivered, "replies": self.replies})()


def service(views: list[dict[str, Any]], reports: Reports, now: list[datetime]) -> BulkSendWarmup:
    async def read_sends() -> list[dict[str, Any]]:
        reads.append(1)
        return views

    reads: list[int] = []
    warm = BulkSendWarmup(policy=POLICY, zone=CDMX, instance_key="att1", read_sends=read_sends, report=reports,
                          clock=lambda: now[0])
    warm.reads = reads  # type: ignore[attr-defined]
    return warm


def test_the_replies_are_read_once_after_24_hours_and_only_by_the_engine_or_the_loop() -> None:
    views = [bulk_view("s1")]  # termino el 25/11 20:20 UTC
    reports, now = Reports(), [datetime(2026, 11, 26, 12, 0, tzinfo=UTC)]
    warm = service(views, reports, now)
    # Antes de las 24 h no se lee: repite igual.
    assert run(warm.refresh()) == "att1=500" and reports.calls == []
    now[0] = datetime(2026, 11, 26, 21, 0, tzinfo=UTC)
    # La pagina no lee Chatwoot: sin lectura, repite.
    assert run(warm.today()).reason == "respuestas_sin_leer" and reports.calls == []
    assert run(warm.refresh()) == "att1=1000" and reports.calls == ["s1"]
    now[0] += timedelta(hours=5)
    assert run(warm.refresh()) == "att1=1000" and reports.calls == ["s1"]  # la lectura queda
    assert run(warm.today()).reason == "subio"


def test_a_failed_reply_reading_is_retried_after_a_while() -> None:
    views = [bulk_view("s1")]
    reports, now = Reports(failures=1), [datetime(2026, 11, 27, 12, 0, tzinfo=UTC)]
    warm = service(views, reports, now)
    assert run(warm.refresh()) == "att1=500" and reports.calls == ["s1"]
    now[0] += timedelta(minutes=10)
    run(warm.refresh())
    assert reports.calls == ["s1"]
    now[0] += timedelta(minutes=30)
    assert run(warm.refresh()) == "att1=1000" and reports.calls == ["s1", "s1"]


def test_the_history_is_cached_and_the_engine_reads_it_fresh() -> None:
    reports, now = Reports(), [NOW]
    warm = service([bulk_view("s1")], reports, now)
    run(warm.today())
    run(warm.today())
    assert len(warm.reads) == 1  # type: ignore[attr-defined]
    verdict = run(warm.check(send_id="s9", size=900))
    assert len(warm.reads) == 2 and verdict.allows and warm.readiness() == "att1=1000"  # type: ignore[attr-defined]


def test_view_for_gives_the_page_and_the_ae_the_same_text() -> None:
    reports, now = Reports(), [NOW]
    candidate = bulk_view("s9", state="borrador", confirmed_count=None, started_at=None, finished_at=None,
                          scheduled_at="2026-11-29T23:00:00+00:00", list={"state": "revisada", "eligible_count": 1200})
    warm = service([candidate, bulk_view("s1")], reports, now)
    run(warm.refresh())
    out = run(warm.view_for(candidate))
    assert out["dia"] == "2026-11-29" and out["zona"] == "America/Mexico_City"
    assert (out["tope_del_dia"], out["escalon"], out["escalones"]) == (1000, 2, [500, 1000, 5000])
    assert out["tanda_grande_ese_dia"] is True and out["motivo"] == "subio"
    mine = out["este_masivo"]
    assert mine["personas"] == 1200 and mine["entra"] is False and mine["motivo"] == "calentamiento:supera_tope_1000"
    # Un masivo terminado no trae «este_masivo».
    assert run(warm.view_for(bulk_view("s1")))["este_masivo"] is None


def test_refresh_never_raises_and_says_it_in_ready() -> None:
    async def broken() -> list[dict[str, Any]]:
        raise RuntimeError("supabase_down")

    warm = BulkSendWarmup(policy=POLICY, zone=CDMX, instance_key="maribel", read_sends=broken, report=None,
                          clock=lambda: NOW)
    assert warm.readiness() == "sin_calcular"
    assert run(warm.refresh()) == "maribel=sin_lectura"


def test_the_real_report_over_the_captured_conversation_feeds_the_reply_rate() -> None:
    from test_bulk_send_wiring import REGISTER, SEND_ID, Rest, _Conversation50, _paged, _report_source

    from bridge import bulk_send_wiring as wiring

    rest = Rest({"read_bulk_send_recipients_v1": _paged(REGISTER, wiring.RECIPIENTS_PAGE),
                 "read_bulk_sends_v1": [bulk_view(SEND_ID, confirmed_count=30)]})
    report = _report_source(rest, _Conversation50())
    warm = BulkSendWarmup(policy=POLICY, zone=CDMX, instance_key="att1", read_sends=rest.store().read_sends,
                          report=report.report, clock=lambda: NOW)
    # La conversacion 50 capturada: leida y contestada. 1 respuesta de 1 entregada: sube.
    assert run(warm.refresh()) == "att1=1000"
    assert [c[0] for c in rest.calls] == ["read_bulk_sends_v1", "read_bulk_send_recipients_v1"]


# ------------------------------------------------------------------ el motor


def test_the_engine_brakes_at_start_what_does_not_fit_and_sends_nothing(tmp_path: Path) -> None:
    from test_bulk_send_worker import CENTER, Clock, FakeProducer, FakeSender, FakeStore, make

    clock = Clock(CENTER)
    store = FakeStore(clock, 30)
    sender, producer = FakeSender(clock), FakeProducer()
    worker = make(tmp_path, clock, store, sender=sender, producer=producer)
    previous = {"send_id": "bbbbbbbb-0000-4000-8000-000000000001", "state": "terminado", "confirmed_count": 25,
                "started_at": (CENTER - timedelta(hours=3)).isoformat(),
                "finished_at": (CENTER - timedelta(hours=2)).isoformat(), "list": None}

    async def read_sends() -> list[dict[str, Any]]:
        return [store.view(), previous]

    warm = BulkSendWarmup(policy=WarmupPolicy(steps=(25, 100), pilot_size=20), zone=CDMX, instance_key="maribel",
                          read_sends=read_sends, report=None, clock=clock)
    seen: list[tuple[str, int]] = []

    async def check(send: Any, at: datetime) -> Verdict:
        seen.append((send.send_id, send.eligible_count))
        return await warm.check(send_id=send.send_id, size=send.eligible_count, at=at)

    worker._warmup = check  # type: ignore[attr-defined]  # lo que arma build_bulk_send con el flag
    report = run(worker.tick())
    assert report.outcome == "frenado:calentamiento:espaciado" and not sender.calls
    assert store.transitions == [("frenado", "calentamiento:espaciado")]
    assert len(producer.admitted) == 1 and seen == [(store.view()["send_id"], 30)]
    # No es un freno de Meta: no queda para la regla de las 24 h.
    assert not (tmp_path / "candado" / "frenos-meta.json").exists()


def test_the_engine_waits_if_the_warmup_cannot_be_read_and_sends_if_it_fits(tmp_path: Path) -> None:
    from test_bulk_send_worker import CENTER, Clock, FakeSender, FakeStore, make

    clock = Clock(CENTER)
    store = FakeStore(clock, 10)
    sender = FakeSender(clock)
    worker = make(tmp_path, clock, store, sender=sender)

    async def broken(send: Any, at: datetime) -> Verdict:
        raise RuntimeError("supabase_down")

    worker._warmup = broken  # type: ignore[attr-defined]
    assert run(worker.tick()).outcome == "calentamiento_ilegible"
    assert store.send["state"] == "confirmado" and not sender.calls

    async def fits(send: Any, at: datetime) -> Verdict:
        return Verdict(True, "calentamiento:entra")

    worker._warmup = fits  # type: ignore[attr-defined]
    assert run(worker.tick()).outcome == "terminado" and len(sender.calls) == 10


# ------------------------------------------------------------------ la pagina


def _page_harness(warmup: Any) -> Any:
    from test_bulk_send_page import _vetted

    return _vetted(warmup=warmup)


def _warm_for_page(history: list[dict[str, Any]]) -> BulkSendWarmup:
    async def read_sends() -> list[dict[str, Any]]:
        return history

    # La lista de la pagina tiene 5 personas: tanda piloto de 2 y escalones de 3 y 10 para que cuente.
    return BulkSendWarmup(policy=WarmupPolicy(steps=(3, 10), pilot_size=2), zone=CDMX, instance_key="maribel",
                          read_sends=read_sends, report=None, clock=lambda: datetime(2026, 11, 25, 16, 0, tzinfo=UTC))


def test_the_page_shows_the_warmup_and_does_not_confirm_what_does_not_fit() -> None:
    from test_bulk_send_page import _done

    harness = _page_harness(_warm_for_page([]).view_for)
    page = harness.page()
    assert "El calentamiento del número" in page and "Son 5 personas y el tope del día de este número es 3" in page
    assert _done(harness.post("confirmar", {"csrf_token": harness.csrf(), "conteo": "5", "confirmo": "si"})) \
        == "warmup_blocks"
    assert harness.db.send["state"] == "borrador"
    assert "approver_confirm_bulk_send_v1" not in harness.db.names()


def test_the_page_confirms_what_fits() -> None:
    from test_bulk_send_page import _done

    good = {"send_id": "cccccccc-0000-4000-8000-000000000001", "state": "terminado", "confirmed_count": 3,
            "started_at": "2026-11-23T16:00:00+00:00", "finished_at": "2026-11-23T16:10:00+00:00", "list": None}
    warm = _warm_for_page([good])
    warm._replies[good["send_id"]] = bw.ReplyReading(delivered=3, replies=1, read_at=datetime(2026, 11, 25, tzinfo=UTC))
    harness = _page_harness(warm.view_for)
    assert "entra en el tope del día (10)" in harness.page()
    assert _done(harness.post("confirmar", {"csrf_token": harness.csrf(), "conteo": "5", "confirmo": "si"})) \
        == "confirmado"


def test_the_page_does_not_confirm_if_the_warmup_cannot_be_read() -> None:
    from test_bulk_send_page import _done

    async def broken(view: Any) -> Any:
        raise RuntimeError("supabase_down")

    harness = _page_harness(broken)
    assert "No se pudo leer el calentamiento del número" in harness.page()
    assert _done(harness.post("confirmar", {"csrf_token": harness.csrf(), "conteo": "5", "confirmo": "si"})) \
        == "warmup_unreadable"
    assert harness.db.send["state"] == "borrador"


def test_without_the_warmup_the_page_is_the_one_of_always() -> None:
    from test_bulk_send_page import _done

    harness = _page_harness(None)
    assert "calentamiento" not in harness.page()
    assert _done(harness.post("confirmar", {"csrf_token": harness.csrf(), "conteo": "5", "confirmo": "si"})) \
        == "confirmado"


def test_a_send_braked_by_the_warmup_says_why() -> None:
    from test_bulk_send_page import Harness

    harness = Harness().sign_in()
    harness.db.send.update(state="frenado", brake_reason="calentamiento:supera_tope_500", braked_by="motor",
                           braked_at="2026-11-29T22:40:00Z")
    assert "no entraba en el calentamiento del número al arrancar" in harness.page()


# ------------------------------------------------------------------ el AE


def _ae_app(warmup: Any) -> tuple[TestClient, Any]:
    from test_bulk_send_ae_routes import ORIGIN, SERVICE_KEY, Chatwoot, Db

    db = Db()
    store = flow_change_api.BulkSendStore(base_url="https://att1-rest.internal", db_key=SERVICE_KEY,
                                          transport=httpx.MockTransport(db.handler))
    app = FastAPI()

    @app.exception_handler(flow_change_api.EntryRefused)
    async def refused(_: Any, error: flow_change_api.EntryRefused) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": error.code, "reason": error.reason})

    flow_change_api.register_bulk_send_routes(
        app, token="p" * 48, store=store, chatwoot=Chatwoot(), tenant_ref="lancemos", ally_ref="att1", inbox_id=11,
        product_refs=frozenset({"att1"}), approval_origin=ORIGIN, git_sha="test",
        clock=lambda: datetime(2026, 11, 25, 15, 0, tzinfo=UTC), warmup=warmup)
    return TestClient(app, base_url=ORIGIN), db


def test_the_ae_gets_the_same_reason_as_the_page() -> None:
    from test_bulk_send_ae_routes import PREFIX, _draft, _no_phone

    async def read_sends() -> list[dict[str, Any]]:
        return []

    warm = BulkSendWarmup(policy=POLICY, zone=CDMX, instance_key="att1", read_sends=read_sends, report=None,
                          clock=lambda: datetime(2026, 11, 25, 15, 0, tzinfo=UTC))
    client, _ = _ae_app(warm.view_for)
    headers = {"Authorization": "Bearer " + "p" * 48}
    created = client.post(PREFIX, json=_draft(), headers=headers)
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    # La lista emulada tiene 110 personas: entra en el primer escalon.
    assert data["warmup"]["tope_del_dia"] == 500 and data["warmup"]["este_masivo"]["entra"] is True
    page_text = run(warm.view_for(client.get(f"{PREFIX}/{data['send_id']}", headers=headers).json()["data"]))
    one = client.get(f"{PREFIX}/{data['send_id']}", headers=headers).json()["data"]["warmup"]
    assert one["detalle"] == page_text["detalle"] and one["este_masivo"] == page_text["este_masivo"]
    listed = client.get(PREFIX, headers=headers).json()["data"]["bulk_sends"]
    assert listed[0]["warmup"]["motivo"] == "sin_historial"
    _no_phone(created)


def test_the_ae_says_when_the_warmup_cannot_be_read_and_without_it_nothing_changes() -> None:
    from test_bulk_send_ae_routes import PREFIX, _draft

    async def broken(view: Any) -> Any:
        raise RuntimeError("supabase_down")

    headers = {"Authorization": "Bearer " + "p" * 48}
    client, _ = _ae_app(broken)
    assert client.post(PREFIX, json=_draft(), headers=headers).json()["data"]["warmup"] == {
        "error": "warmup_unavailable"}
    client, _ = _ae_app(None)
    assert "warmup" not in client.post(PREFIX, json=_draft(), headers=headers).json()["data"]


# ------------------------------------------------------------------ el armado y /ready


def test_the_wiring_builds_the_warmup_only_with_its_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_bulk_send_wiring import _build

    from bridge import bulk_send_wiring as wiring
    from bridge.bulk_send_worker import BulkSendConfigError

    off = _build(tmp_path)
    assert off is not None and off.warmup is None and off.worker._warmup is None  # type: ignore[attr-defined]
    assert off.readiness()["bulk_send_warmup"] == "disabled"

    seen: dict[str, Any] = {}
    real = wiring.page_routes

    def capture(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(wiring, "page_routes", capture)
    on = _build(tmp_path, env={bw.FLAG: "true", bw.STEPS_VARIABLE: "300,900"})
    assert on is not None and on.warmup is not None and on.warmup.policy.steps == (300, 900)
    assert on.warmup.zone == CDMX and on.worker._warmup is not None  # type: ignore[attr-defined]
    assert seen["warmup"] == on.warmup.view_for
    assert on.readiness()["bulk_send_warmup"] == "sin_calcular"
    with pytest.raises(BulkSendConfigError, match=bw.STEPS_VARIABLE):
        _build(tmp_path, env={bw.FLAG: "true", bw.STEPS_VARIABLE: "900,300"})


def test_create_app_hands_the_warmup_to_the_ae_routes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from test_bulk_send_ae_routes import _create_app

    seen: dict[str, Any] = {}
    real = flow_change_api.register_bulk_send_routes

    def capture(app: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(app, **kwargs)

    monkeypatch.setattr(flow_change_api, "register_bulk_send_routes", capture)
    client, _ = _create_app(enabled=True, monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert seen["warmup"] is None and client.app.state.bulk_send.warmup is None
    monkeypatch.setenv(bw.FLAG, "true")
    client, _ = _create_app(enabled=True, monkeypatch=monkeypatch, tmp_path=tmp_path)
    bulk = client.app.state.bulk_send
    assert bulk.warmup is not None and seen["warmup"] == bulk.warmup.view_for


# ------------------------------------------------------------------ convivencia con F1 (frenos ante Meta)


def test_the_f1_meta_brakes_also_step_down_and_their_names_are_the_ones_of_f1() -> None:
    from bridge import meta_enforcement

    assert bw.META_ENFORCEMENT_REASONS == {
        meta_enforcement.REASON_RECATEGORIZED, meta_enforcement.REASON_ACCOUNT_RESTRICTED,
        meta_enforcement.REASON_STREAK}
    for reason in sorted(bw.META_ENFORCEMENT_REASONS):
        result = today([big(size=900, state="frenado", reason=reason, delivered=None, replies=None)])
        assert (result.cap, result.reason) == (500, "freno_meta"), reason


def test_the_wiring_hands_the_worker_both_the_meta_brake_and_the_warmup(tmp_path: Path) -> None:
    from test_bulk_send_wiring import _build

    def meta_brake(name: str, language: str) -> str | None:
        return None

    both = _build(tmp_path, env={bw.FLAG: "true"}, meta_brake=meta_brake)
    assert both is not None and both.warmup is not None
    assert both.worker._meta_brake is meta_brake and both.worker._warmup is not None  # type: ignore[attr-defined]
    only_f1 = _build(tmp_path, meta_brake=meta_brake)
    assert only_f1 is not None and only_f1.warmup is None
    assert only_f1.worker._meta_brake is meta_brake and only_f1.worker._warmup is None  # type: ignore[attr-defined]


def test_the_meta_brake_goes_first_and_the_warmup_after(tmp_path: Path) -> None:
    from test_bulk_send_worker import CENTER, Clock, FakeSender, FakeStore, make

    seen: list[str] = []

    async def warm(send: Any, at: datetime) -> Verdict:
        seen.append(send.send_id)
        return Verdict(False, "calentamiento:espaciado")

    # Con la plantilla retirada por F1, frena por F1 sin preguntarle al calentamiento.
    clock = Clock(CENTER)
    store = FakeStore(clock, 30)
    sender = FakeSender(clock)
    worker = make(tmp_path, clock, store, sender=sender)
    worker._meta_brake = lambda name, language: "template_recategorized"  # type: ignore[attr-defined]
    worker._warmup = warm  # type: ignore[attr-defined]
    assert run(worker.tick()).outcome == "frenado:template_recategorized"
    assert not seen and not sender.calls

    # Con F1 sin freno, decide el calentamiento.
    clock = Clock(CENTER)
    store = FakeStore(clock, 30)
    sender = FakeSender(clock)
    worker = make(tmp_path / "dos", clock, store, sender=sender)
    worker._meta_brake = lambda name, language: None  # type: ignore[attr-defined]
    worker._warmup = warm  # type: ignore[attr-defined]
    assert run(worker.tick()).outcome == "frenado:calentamiento:espaciado"
    assert seen == [store.view()["send_id"]] and not sender.calls
