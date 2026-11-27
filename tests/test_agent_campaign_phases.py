"""El lanzamiento por fechas (agent-campaign-phases-v1, 4.8).

Una campana del AE con fases fechadas y un bloque minimo: se validan, se juzgan y
se aprueban juntas; el catalogo vivo sirve la fase vigente y, fuera de toda fase
(entre fases, despues del cierre o pausada), el bloque minimo. Sin
``AGENT_CAMPAIGN_PHASES_ENABLED`` una campana con fases no se guarda ni se sirve y
una sin fases es la de siempre.

Las fechas siguen el lanzamiento real de Maribel (``forever-young-2026``, v1 a v5,
capturado en ``fixtures/agent_campaign_phases_maribel_att1_20261126.json``): la
preventa hasta el 19/11 16:00Z, el carrito despues, y las dos horas en que se cambio
de fase a mano. La campana es FICTICIA; la instancia y el conocimiento son los de
ATT1 de los tests de campanas.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_agent_campaigns import CAMPAIGN, FINGERPRINT, LABEL, MACRO, _context, _Db, _draft
from test_campaign_check import _clean_judge, _instance, _soul
from test_campaign_check import _Store as _CheckStore

from bridge.agent_campaigns import (
    MINIMAL_DEFAULT_INSTRUCTION,
    NOT_SERVED_CHECK,
    NOT_SERVED_DRAFT,
    CampaignInvalid,
    LiveSegmentCatalog,
    campaign_pieces,
    expected_check_sha,
    instance_blocks,
    parse_campaign,
    pieces_preview,
)
from bridge.campaign_approval import render_campaign
from bridge.campaign_check import run_check

FIXTURE = Path(__file__).parent / "fixtures" / "agent_campaign_phases_maribel_att1_20261126.json"
KEY = "black-november-2026"
REST_ID = "99999999-2222-4333-8444-555555555555"

# Las horas del lanzamiento real (UTC), del fixture.
PREVENTA = datetime(2026, 11, 16, 12, tzinfo=UTC)
CARRITO = datetime(2026, 11, 20, 2, tzinfo=UTC)  # la ola del 20/11
GAP = datetime(2026, 11, 30, 9, tzinfo=UTC)  # entre el carrito y el ultimo dia
LAST_DAY = datetime(2026, 11, 30, 20, tzinfo=UTC)
AFTER_CLOSE = datetime(2026, 12, 3, tzinfo=UTC)
AFTER_GRACE = datetime(2026, 12, 16, tzinfo=UTC)


def _phased(**overrides: Any) -> dict[str, Any]:
    draft = _draft(desde="2026-11-14T15:22:00Z", hasta="2026-12-01T06:00:00Z")
    offer = draft.pop("oferta")
    draft["fases"] = [
        {"clave": "preventa", "nombre": "Preventa", "desde": "2026-11-14T15:22:00Z", "hasta": "2026-11-19T16:00:00Z",
         "asesoramiento": ["En la preventa se anota la lista de espera: todavía no hay link de pago."]},
        {"clave": "carrito", "nombre": "Carrito abierto", "desde": "2026-11-19T16:00:00Z",
         "hasta": "2026-11-30T06:00:00Z", "oferta": offer,
         "asesoramiento": ["Si pregunta por México: USD 37 con el link de la campaña."]},
        {"clave": "ultimo-dia", "nombre": "Último día", "desde": "2026-11-30T12:00:00Z",
         "hasta": "2026-12-01T06:00:00Z", "oferta": offer,
         "asesoramiento": ["Hoy es el último día del precio de lanzamiento: USD 37."]},
    ]
    draft["bloque_minimo"] = {"asesoramiento": ["Es un programa digital, de por vida; se paga por Hotmart."]}
    draft.update(overrides)
    return draft


def _phases_context(tmp_path: Path, **kwargs: Any) -> Any:
    return replace(_context(tmp_path, **kwargs), phases_enabled=True)


def _row(context: Any, draft: dict[str, Any], *, campaign_id: str = CAMPAIGN, status: str = "publicada",
         version: int = 1, check_sha: str | None = None, approved_at: str = "2026-11-14T16:00:00+00:00") -> dict:
    body = parse_campaign(draft, phases=True)
    return {"campaign_id": campaign_id, "status": status, "campaign_key": body["clave"], "segment_version": version,
            "label": body["etiqueta"], "label_macro_id": body["macro_etiqueta"], "draft": body,
            "fingerprint": FINGERPRINT,
            "check_sha": check_sha or expected_check_sha(body, version=version, context=context),
            "approved_at": approved_at}


class _Rests(_Db):
    async def fetch(self) -> dict[str, Any]:  # la lectura del reposo
        return await super().fetch()


def _live(context: Any, live_rows: list[dict], rest_rows: list[dict] | None = None) -> tuple[LiveSegmentCatalog,
                                                                                               _Db, _Rests]:
    db, rests = _Db(live_rows), _Rests(rest_rows or [])
    catalog = LiveSegmentCatalog(context=context, fetch=db.fetch, fetch_rests=rests.fetch)
    assert asyncio.run(catalog.refresh()) is True
    return catalog, db, rests


def _active(catalog: LiveSegmentCatalog, now: datetime) -> list[str]:
    return [segment.key for segment in catalog.select(labels={LABEL}, now=now).active]


# ------------------------------------------------------------------- el dato real


def test_the_real_launch_changed_phase_by_hand_and_left_hours_without_a_block() -> None:
    capture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert capture["_capture"]["fecha"] == "2026-11-26"
    versions = capture["maribel"]["versions"]
    assert [version["v"] for version in versions] == [1, 2, 3, 4, 5]
    published = [version for version in versions if version["approved_at"]]
    # Cada fase fue una version nueva, publicada a mano: la v3 21 minutos tarde.
    assert published[0]["valid_until"] == "2026-11-19T16:00:00Z" and published[1]["approved_at"] == "2026-11-19T16:21:15Z"
    gaps = capture["maribel"]["sin_bloque"]
    hours = [(datetime.fromisoformat(gap["hasta"]) - datetime.fromisoformat(gap["desde"])).total_seconds() / 3600
             for gap in gaps]
    assert hours[0] < 0.5 and hours[1] > 43
    # La ola cae adentro del segundo hueco.
    assert datetime.fromisoformat(gaps[1]["desde"]) < CARRITO < datetime.fromisoformat(gaps[1]["hasta"])


# ------------------------------------------------------------ con el flag apagado


def test_without_the_flag_a_campaign_with_phases_is_refused_with_the_extra_keys() -> None:
    with pytest.raises(CampaignInvalid) as caught:
        parse_campaign(_phased())
    assert caught.value.reason["extra"] == ["bloque_minimo", "fases"]
    with pytest.raises(CampaignInvalid):
        parse_campaign(_draft(bloque_minimo={"asesoramiento": ["x"]}))


def test_a_campaign_without_phases_is_the_same_with_or_without_the_flag(tmp_path: Path) -> None:
    off = _context(tmp_path)
    on = replace(off, phases_enabled=True)
    assert parse_campaign(_draft(), phases=True) == parse_campaign(_draft())
    draft = parse_campaign(_draft())
    assert expected_check_sha(draft, version=1, context=on) == expected_check_sha(draft, version=1, context=off)
    assert instance_blocks(draft, context=on, now=CARRITO)[:2] == instance_blocks(draft, context=off, now=CARRITO)[:2]
    assert pieces_preview(draft, context=on) is None
    row = {**_row(off, _draft()), "draft": draft}
    catalogs = []
    for context in (off, on):
        catalog, *_ = _live(context, [copy.deepcopy(row)])
        catalogs.append(catalog)
    assert [s.rendered for s in catalogs[0].segments] == [s.rendered for s in catalogs[1].segments]
    inside = datetime(2026, 11, 25, 12, tzinfo=UTC)
    assert _active(catalogs[0], inside) == _active(catalogs[1], inside) == [KEY]
    assert catalogs[0].phases_status_line() == "disabled"


def test_without_the_flag_the_rests_are_never_read_and_a_stored_phased_row_is_not_served(tmp_path: Path) -> None:
    off = _context(tmp_path)
    context = replace(off, phases_enabled=True)
    row = _row(context, _phased())
    db, rests = _Db([row]), _Rests([_row(context, _phased(), campaign_id=REST_ID, status="pausada")])
    catalog = LiveSegmentCatalog(context=off, fetch=db.fetch, fetch_rests=rests.fetch)
    asyncio.run(catalog.refresh())
    assert rests.calls == 0
    assert catalog.serving_state(CAMPAIGN) == NOT_SERVED_DRAFT
    assert catalog.segments == ()


# --------------------------------------------------------------------- la forma


def test_the_phases_are_normalized_and_the_minimal_block_gets_the_fixed_instruction() -> None:
    draft = parse_campaign(_phased(), phases=True)
    assert [phase["clave"] for phase in draft["fases"]] == ["preventa", "carrito", "ultimo-dia"]
    assert draft["bloque_minimo"]["instruccion"] == MINIMAL_DEFAULT_INSTRUCTION
    written = parse_campaign(_phased(bloque_minimo={"instruccion": "Otra.", "asesoramiento": ["x"]}), phases=True)
    assert written["bloque_minimo"]["instruccion"] == "Otra."


def _with_phases(change: Any) -> dict[str, Any]:
    draft = _phased()
    change(draft)
    return draft


@pytest.mark.parametrize(("change", "field", "problem"), [
    (lambda d: d.pop("bloque_minimo"), "bloque_minimo", "required_with_fases"),
    (lambda d: d["fases"].reverse(), "fases", "not_in_order"),
    (lambda d: d["fases"][1].update(desde="2026-11-19T15:00:00Z"), "fases", "windows_overlap"),
    (lambda d: d["fases"][0].update(desde="2026-11-01T00:00:00Z"), "fases[0]", "outside_the_campaign_window"),
    (lambda d: d["fases"][2].update(clave="minimo"), "fases[2].clave", "short_lowercase_slug"),
    (lambda d: d["fases"][2].update(clave="a--b"), "fases[2].clave", "short_lowercase_slug"),
    (lambda d: d["fases"][2].update(clave="carrito"), "fases", "repeated_clave"),
    (lambda d: d["fases"][0].update(cupon="X"), "fases[0]", "clave_nombre_desde_hasta_and_sections"),
    (lambda d: d.update(fases=[]), "fases", "one_to_6"),
    (lambda d: d["bloque_minimo"].update(oferta={"precio": "37", "moneda": "USD", "cupon": "BLACK37"}),
     "bloque_minimo.oferta", "no_coupon_no_offer_code_no_old_price"),
    (lambda d: d["bloque_minimo"].pop("asesoramiento"), "bloque_minimo", "asesoramiento_and_sections"),
])
def test_what_does_not_close_in_the_phases_is_refused_with_the_field(change: Any, field: str, problem: str) -> None:
    with pytest.raises(CampaignInvalid) as caught:
        parse_campaign(_with_phases(change), phases=True)
    assert caught.value.reason == {"field": field, "problem": problem}


def test_each_phase_inherits_what_it_does_not_bring_and_the_minimal_block_lasts_14_days_more() -> None:
    draft = parse_campaign(_phased(instruccion="Usá el bloque de la fase."), phases=True)
    pieces = campaign_pieces(draft)
    assert [piece.key for piece in pieces] == [f"{KEY}--preventa", f"{KEY}--carrito", f"{KEY}--ultimo-dia",
                                               f"{KEY}--minimo"]
    preventa, carrito, _, minimal = pieces
    assert preventa.draft["instruccion"] == "Usá el bloque de la fase." and "oferta" not in preventa.draft
    assert carrito.draft["oferta"]["cupon"] == "BLACK37"
    assert "oferta" not in minimal.draft and minimal.draft["instruccion"] == MINIMAL_DEFAULT_INSTRUCTION
    assert minimal.draft["desde"] == draft["desde"] and minimal.draft["hasta"] == "2026-12-15T06:00:00Z"


# ---------------------------------------------------------------- contra la instancia


def test_a_clean_phased_campaign_has_no_blocks_and_each_piece_is_previewed(tmp_path: Path) -> None:
    context = _phases_context(tmp_path)
    draft = parse_campaign(_phased(), phases=True)
    blocks, warnings, segment = instance_blocks(draft, context=context, now=PREVENTA)
    assert blocks == [] and segment is not None and segment.key == f"{KEY}--preventa"
    assert {warning["field"] for warning in warnings} == {"fases[1].oferta.cupon", "fases[2].oferta.cupon"}
    preview = pieces_preview(draft, context=context)
    assert [piece["tipo"] for piece in preview] == ["fase", "fase", "fase", "minimo"]
    assert "cupón de lanzamiento" not in preview[3]["render"] and "BLACK37" not in preview[3]["render"]
    assert "No ofrezcas precio de lanzamiento" in preview[3]["render"]


def test_a_phase_that_breaks_the_segment_rules_blocks_with_its_field(tmp_path: Path) -> None:
    context = _phases_context(tmp_path)
    draft = parse_campaign(_with_phases(lambda d: d["fases"][1].update(oferta={"reemplaza": ["precio"]})),
                           phases=True)
    blocks, _, _ = instance_blocks(draft, context=context, now=PREVENTA)
    assert {"code": "campaign_segment_invalid", "field": "fases[1]"} == {key: blocks[0][key] for key in ("code", "field")}


# ------------------------------------------------------------------- el catalogo vivo


def test_the_catalog_serves_the_phase_of_now_and_the_minimal_block_outside_every_phase(tmp_path: Path) -> None:
    context = _phases_context(tmp_path)
    catalog, *_ = _live(context, [_row(context, _phased())])
    assert catalog.serving_state(CAMPAIGN) == "served"
    assert _active(catalog, PREVENTA) == [f"{KEY}--preventa"]
    assert _active(catalog, CARRITO) == [f"{KEY}--carrito"]
    assert _active(catalog, GAP) == [f"{KEY}--minimo"]
    assert _active(catalog, LAST_DAY) == [f"{KEY}--ultimo-dia"]
    assert _active(catalog, AFTER_CLOSE) == [f"{KEY}--minimo"]
    assert _active(catalog, AFTER_GRACE) == []
    # Nunca la fase y el bloque minimo juntos: a cada hora, a lo sumo uno.
    for hours in range(0, 24 * 33, 3):
        assert len(_active(catalog, datetime(2026, 11, 14, tzinfo=UTC) + timedelta(hours=hours))) <= 1
    # El turno siguiente encuentra la pieza por su clave y su version (la procedencia).
    selection = catalog.select(labels={LABEL}, now=CARRITO)
    assert catalog.blocks_for(selection.context_entries())[0].knowledge.offer.coupon == "BLACK37"
    assert catalog.phases_status_line() == "pieced=1,resting=0,rest_not_served=0,rests_refresh=ok"


def test_a_campaign_without_phases_but_with_a_minimal_block_serves_itself_and_the_block_after(tmp_path: Path) -> None:
    context = _phases_context(tmp_path)
    draft = _draft(desde="2026-11-19T16:00:00Z", hasta="2026-12-01T06:00:00Z",
                   bloque_minimo={"asesoramiento": ["Es un programa digital."]})
    catalog, *_ = _live(context, [_row(context, draft)])
    assert _active(catalog, CARRITO) == [KEY]
    assert _active(catalog, AFTER_CLOSE) == [f"{KEY}--minimo"]


def test_a_phased_check_sha_that_does_not_match_is_not_served(tmp_path: Path) -> None:
    context = _phases_context(tmp_path)
    catalog, *_ = _live(context, [_row(context, _phased(), check_sha="b" * 64)])
    assert catalog.serving_state(CAMPAIGN) == NOT_SERVED_CHECK and catalog.segments == ()


def test_the_check_sha_of_one_piece_covers_the_whole_campaign(tmp_path: Path) -> None:
    context = _phases_context(tmp_path)
    draft = parse_campaign(_phased(), phases=True)
    changed = copy.deepcopy(draft)
    changed["bloque_minimo"]["asesoramiento"] = ["Otro texto."]
    assert expected_check_sha(changed, version=1, context=context) != expected_check_sha(draft, version=1,
                                                                                         context=context)


def test_a_paused_campaign_keeps_serving_its_minimal_block(tmp_path: Path) -> None:
    # La pausa de la v3 del 19/11 22:38Z: con el bloque minimo, la ola habria tenido con que contestar.
    context = _phases_context(tmp_path)
    paused = _row(context, _phased(), campaign_id=REST_ID, status="pausada")
    catalog, *_ = _live(context, [], [paused])
    assert _active(catalog, CARRITO) == [f"{KEY}--minimo"]
    assert catalog.serving_state(REST_ID) is None  # no esta publicada
    assert catalog.phases_status_line() == "pieced=0,resting=1,rest_not_served=0,rests_refresh=ok"


def test_a_published_version_with_the_same_label_wins_over_the_paused_one(tmp_path: Path) -> None:
    # La v5 publicada el 21/11 con la v3 pausada: se sirve la v5, nunca las dos.
    context = _phases_context(tmp_path)
    paused = _row(context, _phased(), campaign_id=REST_ID, status="pausada")
    live = _row(context, _draft(desde="2026-11-21T16:00:00Z", hasta="2026-12-01T06:00:00Z"), version=5,
                approved_at="2026-11-21T18:20:00+00:00")
    catalog, *_ = _live(context, [live], [paused])
    assert _active(catalog, datetime(2026, 11, 22, tzinfo=UTC)) == [KEY]
    assert catalog.phases_status_line().startswith("pieced=0,resting=0,")


def test_a_rest_whose_check_does_not_match_is_not_served_and_the_rest_goes_on(tmp_path: Path) -> None:
    context = _phases_context(tmp_path)
    paused = _row(context, _phased(), campaign_id=REST_ID, status="pausada", check_sha="b" * 64)
    catalog, *_ = _live(context, [], [paused])
    assert catalog.segments == ()
    assert catalog.phases_status_line() == "pieced=0,resting=0,rest_not_served=1,rests_refresh=ok"


def test_with_the_rests_down_the_live_campaigns_are_still_served(tmp_path: Path) -> None:
    context = _phases_context(tmp_path)

    async def down() -> dict[str, Any]:
        raise OSError("sin la funcion")

    db = _Db([_row(context, _phased())])
    catalog = LiveSegmentCatalog(context=context, fetch=db.fetch, fetch_rests=down)
    assert asyncio.run(catalog.refresh()) is True
    assert _active(catalog, CARRITO) == [f"{KEY}--carrito"]
    assert catalog.status_line() == "served=1,not_served=0,refresh=ok"
    assert catalog.phases_status_line().endswith("rests_refresh=failing:1")


class _Labeler:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def ensure(self, conversation_id: int, *, source: str) -> str:
        self.calls.append((conversation_id, source))
        return "added"


def _entry(context: Any, live_rows: list[dict], rest_rows: list[dict], now: datetime) -> tuple[Any, _Labeler]:
    from bridge.campaign_inbound_entry import CampaignEntry

    labeler = _Labeler()
    db, rests = _Db(live_rows), _Rests(rest_rows)
    catalog = LiveSegmentCatalog(context=context, fetch=db.fetch, fetch_rests=rests.fetch)
    entry = CampaignEntry(segments=(), labelers={}, answers=_Answers(), account_id=2, inbox_id=11,
                          reset_command="/nuevo", reset_confirmation="ok", clock=lambda: now, allow_empty=True)
    catalog.bind_entry(entry, make_labeler=lambda label, macro: labeler)
    asyncio.run(catalog.refresh())
    return entry, labeler


class _Answers:
    async def get_segment_entry_answers(self, **_: Any) -> dict[str, str]:
        return {}

    async def record_segment_entry_answer(self, **_: Any) -> str:
        return "recorded"


def _first_message(entry: Any, text: str) -> list[str]:
    message = {"actor": "prospect", "text": text, "_message_ref": "m1"}
    turn = asyncio.run(entry.turn(conversation_id=7, history=[message], history_complete=True, messages=[message],
                                  batch_refs={"m1"}, labels=set(), agent_replies_before=0))
    return list(turn.labels)


def test_the_keyword_still_labels_after_the_close_and_while_paused_so_the_minimal_block_reaches_them(
        tmp_path: Path) -> None:
    context = _phases_context(tmp_path)
    for live, rests in (([_row(context, _phased())], []),
                        ([], [_row(context, _phased(), campaign_id=REST_ID, status="pausada")])):
        entry, labeler = _entry(context, live, rests, AFTER_CLOSE)
        _first_message(entry, "Black November")
        assert labeler.calls == [(7, "segment_entry_keyword")]
        # La pregunta de entrada no se hace despues del cierre ni en reposo.
        assert all(segment.question is None or segment.valid_until <= AFTER_CLOSE for segment in entry.segments)


def test_without_pieces_the_keyword_stops_labelling_at_the_close_as_today(tmp_path: Path) -> None:
    context = _phases_context(tmp_path)
    entry, labeler = _entry(context, [_row(context, _draft(desde="2026-11-19T16:00:00Z",
                                                          hasta="2026-12-01T06:00:00Z"))], [], AFTER_CLOSE)
    _first_message(entry, "Black November")
    assert labeler.calls == []


# ----------------------------------------------------------- el juez y la pagina


def test_the_judge_runs_on_every_piece_and_records_the_check_sha_the_bridge_expects(tmp_path: Path) -> None:
    context = _phases_context(tmp_path)
    draft = parse_campaign(_phased(), phases=True)
    store = _CheckStore({"status": "validada", "fingerprint": "f" * 64, "segment_version": 1, "draft": draft})
    calls: list[Any] = []

    def judge(messages: Any) -> Any:
        calls.append(messages)
        return _clean_judge(messages)

    report, _ = run_check(store, manifest=context.manifest, instance_dir=_instance(tmp_path), campaign_id=CAMPAIGN,
                          judge=judge, soul_path=_soul(tmp_path), runs=3)
    assert report.clean is True and len(calls) == 4 * 3
    payload = store.calls[-1][1]
    assert payload["p_report"]["veredicto"] == "limpio"
    assert payload["p_check_sha"] == expected_check_sha(draft, version=1, context=context)


def test_an_open_question_in_one_piece_leaves_the_whole_campaign_without_check(tmp_path: Path) -> None:
    context = _phases_context(tmp_path)
    draft = parse_campaign(_phased(), phases=True)
    store = _CheckStore({"status": "validada", "fingerprint": "f" * 64, "segment_version": 1, "draft": draft})
    item = {"tipo": "hueco", "clave": "x", "titulo": "Falta decir", "citas": [{"fuente": "segmento", "texto": "x"}],
            "pregunta": "¿Cuándo llegan los bonos?", "campo": "z"}
    seen = {"n": 0}

    def judge(messages: Any) -> Any:
        from bridge.segment_check import JudgeAnswer

        seen["n"] += 1
        # Solo la ultima pieza (el bloque minimo, corridas 10 a 12) deja una pregunta.
        return JudgeAnswer(content=json.dumps({"items": [item] if seen["n"] > 9 else []}), model="grabado")

    report, _ = run_check(store, manifest=context.manifest, instance_dir=_instance(tmp_path), campaign_id=CAMPAIGN,
                          judge=judge, soul_path=_soul(tmp_path), runs=3)
    payload = store.calls[-1][1]
    assert report.clean is False and payload["p_check_sha"] is None
    assert payload["p_report"]["items"][0]["numero"].startswith("minimo:")


def test_the_page_shows_every_piece_and_what_happens_when_paused(tmp_path: Path) -> None:
    context = _phases_context(tmp_path)
    draft = parse_campaign(_phased(), phases=True)
    found = {"status": "validada", "effective_status": "validada", "draft": draft, "valid_from": draft["desde"],
             "valid_until": draft["hasta"], "validation": {"preview": {"render": "x",
                                                                       "piezas": pieces_preview(draft, context=context)}}}
    page = render_campaign(found, CAMPAIGN, serving=None, can_publish=False, check_stale=False, time_zone="UTC")
    assert "Fase: Preventa" in page and "Fase: Último día" in page and "Bloque mínimo: Black November" in page
    assert "mientras esté pausada" in page


def test_the_page_of_a_campaign_without_pieces_is_the_same() -> None:
    draft = parse_campaign(_draft())
    found = {"status": "validada", "effective_status": "validada", "draft": draft, "valid_from": draft["desde"],
             "valid_until": draft["hasta"], "validation": {"preview": {"render": "# Segmento: Black November"}}}
    page = render_campaign(found, CAMPAIGN, serving=None, can_publish=False, check_stale=False, time_zone="UTC")
    assert '<pre class="bubble"># Segmento: Black November</pre>' in page and "Bloque mínimo" not in page


def test_macro_and_label_are_the_campaign_ones_in_every_piece(tmp_path: Path) -> None:
    context = _phases_context(tmp_path)
    catalog, *_ = _live(context, [_row(context, _phased())])
    assert {(segment.label, segment.label_macro_id) for segment in catalog.segments} == {(LABEL, MACRO)}


def test_the_offer_on_top_of_a_phased_campaign_goes_through_the_offer_rules(tmp_path: Path) -> None:
    # Es la de la entrada por oferta y la heredan las fases que no traen la suya.
    context = _phases_context(tmp_path)
    offer = {"precio": "37", "moneda": "USD", "codigo_oferta": "zzzz9999", "reemplaza": ["precio", "moneda"]}
    draft = parse_campaign(_phased(oferta=offer), phases=True)
    blocks, _, _ = instance_blocks(draft, context=context, now=PREVENTA)
    assert {"code": "campaign_offer_unknown", "field": "oferta.codigo_oferta"} in blocks
    assert {"code": "campaign_offer_unknown", "field": "fases[0].oferta.codigo_oferta"} in blocks


def test_each_phase_block_is_titled_with_the_campaign_and_the_phase(tmp_path: Path) -> None:
    context = _phases_context(tmp_path)
    catalog, *_ = _live(context, [_row(context, _phased())])
    [segment] = catalog.select(labels={LABEL}, now=CARRITO).active
    assert segment.rendered.startswith("# Segmento: Black November · Carrito abierto (vigente hasta el ")


# ------------------------------------------- con el juez del AE, las reglas y el conocimiento


def test_the_judge_asked_by_the_ae_runs_on_every_piece_and_tells_the_piece_of_each_notice(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 4.9: judge_campaign corre campaign_check.run_check; con piezas, juzga cada una (D-48.5).
    from bridge import campaign_check, campaign_judge_api, segment_check
    from bridge.segment_check import JudgeAnswer

    context = _phases_context(tmp_path)
    draft = parse_campaign(_phased(), phases=True)
    job = campaign_judge_api.JudgeJob(base_url="https://att1-rest.internal", service_role_key="s" * 40,
                                      manifest=context.manifest, instance_dir=_instance(tmp_path),
                                      soul_path=_soul(tmp_path), openrouter_api_key="k" * 40)
    clean = _CheckStore({"status": "validada", "fingerprint": "f" * 64, "segment_version": 1, "draft": draft})
    calls: list[Any] = []

    def judge(messages: Any) -> Any:
        calls.append(messages)
        return _clean_judge(messages)

    monkeypatch.setattr(campaign_check, "_Store", lambda base_url, key: clean)
    monkeypatch.setattr(segment_check, "openrouter_judge", lambda **_: judge)
    state, result, error = campaign_judge_api.run_judge(job, CAMPAIGN)
    assert (state, error) == ("limpio", None) and len(calls) == 4 * 3
    assert result is not None and result["veredicto"] == "limpio" and result["criterio"] == "frenos-v1"
    assert clean.calls[-1][1]["p_check_sha"] == expected_check_sha(draft, version=1, context=context)

    item = {"tipo": "hueco", "clave": "x", "titulo": "Falta decir", "citas": [{"fuente": "segmento", "texto": "x"}],
            "pregunta": "¿Cuándo llegan los bonos?", "campo": "z"}
    seen = {"n": 0}

    def one_question(_: Any) -> JudgeAnswer:
        seen["n"] += 1
        return JudgeAnswer(content=json.dumps({"items": [item] if seen["n"] > 9 else []}), model="grabado")

    open_store = _CheckStore({"status": "validada", "fingerprint": "f" * 64, "segment_version": 1, "draft": draft})
    monkeypatch.setattr(campaign_check, "_Store", lambda base_url, key: open_store)
    monkeypatch.setattr(segment_check, "openrouter_judge", lambda **_: one_question)
    state, result, error = campaign_judge_api.run_judge(job, CAMPAIGN)
    # Con el criterio de frenos, un hueco queda como aviso; el AE lo lee con la pieza adelante.
    assert (state, error) == ("limpio", None) and result is not None
    assert [notice["numero"].split(":", 1)[0] for notice in result["avisos"]] == ["minimo"]


class _KeywordBase:
    def __init__(self, rule: dict[str, Any]) -> None:
        self.rule = rule

    async def get_live_keyword_rules(self, instance: dict[str, str]) -> dict[str, Any]:
        return {"rules": [self.rule]}


def _keyword_state(entry: Any) -> tuple[str | None, list[str]]:
    from test_keyword_rules import RULE_ID, _settings
    from test_keyword_rules import _row as _rule_row

    from bridge.keyword_rules import keyword_rules_setup

    live = keyword_rules_setup(_settings(), object(), _KeywordBase(_rule_row(palabra_clave="Black November")),
                               campaign_entry=entry, reset_command="/nuevo", reset_confirmation="ok")
    assert live is not None and asyncio.run(live.refresh()) is True
    return live.serving_state(RULE_ID), list(live.context.campaign_keywords())


def test_a_resting_campaign_with_its_minimal_block_keeps_its_keyword_against_the_keyword_rules(
        tmp_path: Path) -> None:
    # D-48.8: en reposo, la palabra de la campana sigue etiquetando, asi que una regla con esa palabra
    # choca igual que con una publicada (keyword-rules-v1: la campana gana).
    context = _phases_context(tmp_path)
    published, _ = _entry(context, [_row(context, _phased())], [], CARRITO)
    paused, _ = _entry(context, [], [_row(context, _phased(), campaign_id=REST_ID, status="pausada")], AFTER_CLOSE)
    nothing, _ = _entry(context, [], [], CARRITO)
    assert _keyword_state(published) == ("choca_con_una_campana", ["black november"])
    assert _keyword_state(paused) == ("choca_con_una_campana", ["black november"])
    assert _keyword_state(nothing) == ("served", [])


def test_a_resting_campaign_serving_its_minimal_block_is_a_segment_in_force_for_the_live_knowledge(
        tmp_path: Path) -> None:
    # D-48.9 (D-55.4): mientras se sirve el bloque minimo de una campana en reposo, el agente usa el
    # conocimiento del archivo, contra el que se juzgo. Fechas relativas a hoy: el cableado mira el reloj.
    from test_live_knowledge_routes import ENABLED, Base, Db, _Processor, _settings

    from bridge import app as app_module

    context = _phases_context(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)

    def stamp(moment: datetime) -> str:
        return moment.isoformat().replace("+00:00", "Z")

    ended = _draft(desde=stamp(now - timedelta(days=10)), hasta=stamp(now - timedelta(days=3)))
    ended["bloque_minimo"] = {"asesoramiento": ["Es un programa digital, de por vida; se paga por Hotmart."]}
    for status in ("pausada", "terminada"):
        catalog, *_ = _live(context, [], [_row(context, ended, campaign_id=REST_ID, status=status)])
        assert [segment.key for segment in catalog.select(labels={LABEL}, now=now).active] == [f"{KEY}--minimo"]
        source = app_module._live_knowledge_setup(_settings(**ENABLED), Base(Db()), _Processor(), catalog)
        assert source is not None and source.segments_in_force() == (f"{KEY}--minimo",)
    # Sin bloque minimo, una terminada no se sirve ni cuenta: como hoy.
    bare = _draft(desde=stamp(now - timedelta(days=10)), hasta=stamp(now - timedelta(days=3)))
    catalog, *_ = _live(context, [], [_row(context, bare, campaign_id=REST_ID, status="terminada")])
    source = app_module._live_knowledge_setup(_settings(**ENABLED), Base(Db()), _Processor(), catalog)
    assert source is not None and source.segments_in_force() == ()
