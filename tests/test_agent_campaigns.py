"""La campana como dato y el catalogo vivo (agent-campaigns-v1, E-355).

Instancia y conocimiento: los de ATT1 de los tests de segmentos. La campana es
FICTICIA (la forma es la del esquema de ``segment-knowledge-v1``). El juez va
grabado: ``segment_check`` es el mismo del alta de un segmento por archivo.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import tomllib
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_campaign_inbound_entry_bridge import _settings as _segment_settings
from test_flow_approval_page import ORIGIN, _NoSupabase
from test_flow_approval_page import _settings as _approval_settings
from test_segment_bridge import ALWAYS
from test_whatsapp_inbound_equivalence import _manifest_settings

from bridge.agent_campaigns import (
    NOT_SERVED_CHECK,
    NOT_SERVED_CONFLICT,
    NOT_SERVED_DRAFT,
    CampaignContext,
    CampaignInvalid,
    LiveSegmentCatalog,
    expected_check_sha,
    instance_blocks,
    parse_campaign,
    render_toml,
    segment_mapping,
    wa_me_link,
)
from bridge.app import create_app
from bridge.campaign_inbound_entry import CampaignEntry
from bridge.instance_manifest import InstanceManifest
from bridge.segment_check import calcular_chequeo_sha
from bridge.segment_knowledge import SegmentCatalog, SegmentKnowledge, load_segment_catalog

NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)
CAMPAIGN = "11111111-2222-4333-8444-555555555555"
FINGERPRINT = "f" * 64
LABEL, MACRO = "black-november", 77


def _draft(**overrides: Any) -> dict[str, Any]:
    draft = {
        "clave": "black-november-2026", "nombre": "Black November", "etiqueta": LABEL, "macro_etiqueta": MACRO,
        "desde": "2026-11-20T00:00:00-06:00", "hasta": "2026-12-01T00:00:00-06:00",
        "palabras_clave": ["Black November"], "pregunta_entrada": "¿Nos escribes por la promoción de Black November?",
        "oferta": {"precio": "37", "moneda": "USD", "cupon": "BLACK37", "reemplaza": ["precio", "moneda"]},
        "asesoramiento": ["Si pregunta por México: USD 37 con el link de la campaña."],
    }
    draft.update(overrides)
    return draft


def _context(tmp_path: Path, *, with_manifest_segment: bool = False, offer_link: bool = True) -> CampaignContext:
    if with_manifest_segment:
        settings = _segment_settings(tmp_path, window=ALWAYS)
        manifest = settings.instance_manifest
        return CampaignContext(manifest=manifest, base=settings.commercial_knowledge,
                               manifest_catalog=settings.agent_segment_catalog, reserved_macros=frozenset({5}),
                               offer_link_enabled=offer_link)
    settings = _manifest_settings(tmp_path)
    assert settings.commercial_knowledge is not None
    return CampaignContext(manifest=settings.instance_manifest, base=settings.commercial_knowledge,
                           manifest_catalog=SegmentCatalog((), len(settings.commercial_knowledge.render().encode("utf-8"))),
                           reserved_macros=frozenset({5}), offer_link_enabled=offer_link)


def _row(context: CampaignContext, *, draft: dict[str, Any] | None = None, version: int = 1, check_sha: str | None = None,
         campaign_id: str = CAMPAIGN) -> dict[str, Any]:
    body = parse_campaign(draft or _draft())
    return {"campaign_id": campaign_id, "status": "publicada", "campaign_key": body["clave"], "segment_version": version,
            "label": body["etiqueta"], "label_macro_id": body["macro_etiqueta"], "draft": body, "fingerprint": FINGERPRINT,
            "check_sha": check_sha or expected_check_sha(body, version=version, context=context),
            "approved_at": "2026-11-08T12:00:00+00:00"}


class _Db:
    """La lectura del catalogo vivo: contesta lo que le pongan, o se cae."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.down = False
        self.calls = 0

    async def fetch(self) -> dict[str, Any]:
        self.calls += 1
        if self.down:
            raise OSError("la base no contesta")
        return {"campaigns": copy.deepcopy(self.rows)}


# -------------------------------------------------------------- la forma y el armado


def test_a_campaign_is_normalized_and_keeps_only_what_it_may_say() -> None:
    parsed = parse_campaign(_draft())
    assert parsed["desde"] == "2026-11-20T00:00:00-06:00"
    assert parsed["palabras_clave"] == ["Black November"]


@pytest.mark.parametrize(("change", "field"), [
    ({"clave": "Con Mayusculas"}, "clave"),
    ({"etiqueta": "Otra Etiqueta"}, "etiqueta"),
    ({"macro_etiqueta": "77"}, "macro_etiqueta"),
    ({"desde": "2026-11-20T00:00:00"}, "desde"),
    ({"desde": "2026-12-20T00:00:00-06:00"}, "desde"),
    ({"palabras_clave": ["   ", "!!"]}, "palabras_clave"),
    ({"palabras_clave": ["Black November", "BLACK NOVEMBER"]}, "palabras_clave"),
    ({"oferta": {"cualquiera": "cosa"}}, "oferta"),
    ({"asesoramiento": []}, "asesoramiento"),
])
def test_what_does_not_close_is_refused_with_the_field_and_no_value(change: dict, field: str) -> None:
    with pytest.raises(CampaignInvalid) as caught:
        parse_campaign(_draft(**change))
    assert caught.value.reason["field"] == field
    assert "cosa" not in json.dumps(caught.value.reason)


def test_without_keyword_or_question_nobody_could_enter() -> None:
    draft = _draft()
    del draft["palabras_clave"], draft["pregunta_entrada"]
    with pytest.raises(CampaignInvalid):
        parse_campaign(draft)


def test_extra_keys_are_refused() -> None:
    with pytest.raises(CampaignInvalid) as caught:
        parse_campaign(_draft(hackeo="x"))
    assert caught.value.reason["extra"] == ["hackeo"]


def test_a_phone_in_the_visible_question_is_refused() -> None:
    with pytest.raises(CampaignInvalid) as caught:
        parse_campaign(_draft(pregunta_entrada="Llamame al 5512345678"))
    assert caught.value.code == "identifier_not_allowed"


def test_the_toml_comes_back_exactly_the_same(tmp_path: Path) -> None:
    mapping = segment_mapping(parse_campaign(_draft(faq=[{"pregunta": "¿Sirve?", "respuesta": "Sí.\n\"citado\"", "fuente": "Dan"}])),
                              ally_ref="att1", version=2, status="aprobado", check_sha="a" * 64,
                              approved_on=date(2026, 11, 8))
    assert tomllib.loads(render_toml(mapping)) == mapping


def test_the_mapping_is_a_valid_segment_with_the_same_render_as_a_file(tmp_path: Path) -> None:
    context = _context(tmp_path)
    mapping = segment_mapping(parse_campaign(_draft()), ally_ref=context.manifest.ally_ref, version=1)
    knowledge = SegmentKnowledge.from_mapping(mapping, time_zone=context.manifest.time_zone, require_approved=False)
    assert "Precio en esta campaña: USD 37." in knowledge.render()
    assert "Cupón de la campaña: BLACK37." in knowledge.render()


def test_the_check_sha_of_the_row_is_the_one_the_judge_gives_for_its_toml(tmp_path: Path) -> None:
    context = _context(tmp_path)
    draft = parse_campaign(_draft())
    raw = tomllib.loads(render_toml(segment_mapping(draft, ally_ref=context.manifest.ally_ref, version=1)))
    assert calcular_chequeo_sha(raw, base_sha256=context.base.rendered_sha256) == expected_check_sha(
        draft, version=1, context=context)


def test_wa_me_link() -> None:
    assert wa_me_link("+52 1 55 1234 5678", "Black November") == "https://wa.me/5215512345678?text=Black%20November"
    assert wa_me_link(None, "x") is None


# ------------------------------------------------------- las reglas contra la instancia


def _codes(blocks: list[dict[str, Any]]) -> list[str]:
    return [block["code"] for block in blocks]


def test_a_clean_campaign_has_no_blocks_and_says_the_coupon_is_not_verified(tmp_path: Path) -> None:
    blocks, warnings, segment = instance_blocks(parse_campaign(_draft()), context=_context(tmp_path), now=NOW)
    assert blocks == [] and segment is not None
    assert _codes(warnings) == ["campaign_coupon_not_verified"]


@pytest.mark.parametrize(("change", "code"), [
    ({"etiqueta": "cliente"}, "campaign_label_reserved"),
    ({"etiqueta": "automation_x"}, "campaign_label_reserved"),
    ({"macro_etiqueta": 5}, "campaign_macro_reserved"),
    ({"hasta": "2026-11-21T00:00:00-06:00", "desde": "2026-11-20T00:00:00-06:00"}, "campaign_window_over"),
    ({"oferta": {"reemplaza": ["precio"]}}, "campaign_segment_invalid"),
])
def test_rules_against_the_instance(tmp_path: Path, change: dict, code: str) -> None:
    blocks, _, _ = instance_blocks(parse_campaign(_draft(**change)), context=_context(tmp_path), now=NOW)
    assert code in _codes(blocks)


def test_a_campaign_cannot_reuse_a_keyword_or_a_label_of_the_manifest(tmp_path: Path) -> None:
    context = _context(tmp_path, with_manifest_segment=True)
    segment = context.manifest.agent_segments[0]
    blocks, _, _ = instance_blocks(parse_campaign(_draft(palabras_clave=["Campaña de prueba"], etiqueta=segment.label)),
                                   context=context, now=NOW)
    assert {"campaign_keyword_taken", "campaign_label_taken"} <= set(_codes(blocks))


# ------------------------------------------------------------------- el catalogo vivo


def _live(context: CampaignContext, db: _Db, **kwargs: Any) -> LiveSegmentCatalog:
    return LiveSegmentCatalog(context=context, fetch=db.fetch, **kwargs)


def test_a_published_campaign_enters_the_catalog_and_the_turn_selects_it(tmp_path: Path) -> None:
    context = _context(tmp_path)
    db = _Db([_row(context)])
    live = _live(context, db)
    assert asyncio.run(live.refresh()) is True
    assert [segment.key for segment in live.segments] == ["black-november-2026"]
    selection = live.select(labels={LABEL}, now=NOW)
    assert [segment.key for segment in selection.active] == ["black-november-2026"]
    assert live.blocks_for(selection.context_entries())[0].knowledge.offer.coupon == "BLACK37"
    assert live.serving_state(CAMPAIGN) == "served"
    assert live.status_line() == "served=1,not_served=0,refresh=ok"


def test_a_check_sha_that_does_not_match_is_not_served_it_warns_and_the_bridge_goes_on(tmp_path: Path,
                                                                                       caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    context = _context(tmp_path)
    notices: list[tuple[str, str]] = []

    async def notify(key: str, reason: str) -> None:
        notices.append((key, reason))

    db = _Db([_row(context, check_sha="b" * 64)])
    live = _live(context, db, notify=notify)
    assert asyncio.run(live.refresh()) is True
    assert live.segments == ()
    assert live.select(labels={LABEL}, now=NOW).active == ()
    assert live.serving_state(CAMPAIGN) == NOT_SERVED_CHECK
    assert "agent_campaign_not_served key=black-november-2026" in caplog.text
    assert notices == [("black-november-2026", NOT_SERVED_CHECK)]
    # Una recarga de lo mismo no vuelve a avisar.
    asyncio.run(live.refresh())
    assert len(notices) == 1


def test_a_recertified_campaign_is_picked_up_with_the_same_fingerprint(tmp_path: Path) -> None:
    # Volver a correr el juez cambia solo check_sha: el contenido y la huella son los mismos.
    context = _context(tmp_path)
    db = _Db([_row(context, check_sha="b" * 64)])
    live = _live(context, db)
    asyncio.run(live.refresh())
    assert live.serving_state(CAMPAIGN) == NOT_SERVED_CHECK

    db.rows = [_row(context)]
    assert db.rows[0]["fingerprint"] == FINGERPRINT
    asyncio.run(live.refresh())
    assert live.serving_state(CAMPAIGN) == "served"


def test_the_same_reason_is_announced_once_even_when_the_row_changes(tmp_path: Path) -> None:
    context = _context(tmp_path)
    notices: list[tuple[str, str]] = []

    async def notify(key: str, reason: str) -> None:
        notices.append((key, reason))

    db = _Db([_row(context, check_sha="b" * 64)])
    live = _live(context, db, notify=notify)
    asyncio.run(live.refresh())
    db.rows = [_row(context, check_sha="c" * 64)]
    asyncio.run(live.refresh())
    assert live.serving_state(CAMPAIGN) == NOT_SERVED_CHECK
    assert notices == [("black-november-2026", NOT_SERVED_CHECK)]


def test_when_the_base_knowledge_changes_the_campaign_stops_being_served_without_taking_the_bridge(tmp_path: Path) -> None:
    context = _context(tmp_path)
    db = _Db([_row(context)])
    live = _live(context, db)
    asyncio.run(live.refresh())
    # El mismo draft contra otro conocimiento: el chequeo_sha de la fila ya no calza.
    stale = LiveSegmentCatalog(context=replace(context, base=_tweaked(context.base)), fetch=db.fetch)
    assert asyncio.run(stale.refresh()) is True
    assert stale.serving_state(CAMPAIGN) == NOT_SERVED_CHECK
    assert stale.segments == ()


def _tweaked(base: Any) -> Any:
    class Other:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        rendered_sha256 = "0" * 64

        def render(self) -> str:
            return self._inner.render()

    return Other(base)


def test_with_the_base_down_it_keeps_serving_what_was_last_good(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    context = _context(tmp_path)
    db = _Db([_row(context)])
    live = _live(context, db)
    asyncio.run(live.refresh())
    db.down = True
    assert asyncio.run(live.refresh()) is False
    assert [segment.key for segment in live.segments] == ["black-november-2026"]
    assert live.status_line() == "served=1,not_served=0,refresh=failing:1"
    assert "agent_campaigns_refresh_failed failures=1" in caplog.text
    db.down = False
    assert asyncio.run(live.refresh()) is True
    assert live.status_line() == "served=1,not_served=0,refresh=ok"


def test_a_malformed_answer_of_the_base_also_keeps_the_last_good(tmp_path: Path) -> None:
    context = _context(tmp_path)
    db = _Db([_row(context)])
    live = _live(context, db)
    asyncio.run(live.refresh())

    async def broken() -> dict[str, Any]:
        return {"campaigns": "algo raro"}

    live._fetch = broken  # noqa: SLF001
    assert asyncio.run(live.refresh()) is False
    assert [segment.key for segment in live.segments] == ["black-november-2026"]


def test_starting_with_the_base_down_serves_the_manifest_and_recovers(tmp_path: Path) -> None:
    context = _context(tmp_path, with_manifest_segment=True)
    manifest_keys = [segment.key for segment in context.manifest_catalog.segments]
    db = _Db([_row(context)])
    db.down = True
    live = _live(context, db)
    assert asyncio.run(live.refresh()) is False
    assert [segment.key for segment in live.segments] == manifest_keys
    db.down = False
    asyncio.run(live.refresh())
    assert sorted(segment.key for segment in live.segments) == sorted([*manifest_keys, "black-november-2026"])


def test_an_instance_that_reads_segments_from_the_manifest_stays_the_same(tmp_path: Path) -> None:
    context = _context(tmp_path, with_manifest_segment=True)
    live = _live(context, _Db([]))
    asyncio.run(live.refresh())
    manifest = context.manifest_catalog
    assert live.segments == manifest.segments
    labels = {segment.label for segment in manifest.segments}
    assert live.select(labels=labels, now=NOW) == manifest.select(labels=labels, now=NOW)
    assert live.readiness(NOW) == manifest.readiness(NOW)
    assert live.base_bytes == manifest.base_bytes


def test_a_campaign_with_invalid_content_is_not_served(tmp_path: Path) -> None:
    context = _context(tmp_path)
    row = _row(context)
    row["draft"] = {"clave": "roto"}
    live = _live(context, _Db([row]))
    asyncio.run(live.refresh())
    assert live.serving_state(CAMPAIGN) == NOT_SERVED_DRAFT


def test_a_campaign_with_a_reserved_macro_or_that_does_not_fit_is_not_served(tmp_path: Path) -> None:
    context = _context(tmp_path)
    row = _row(context, draft=_draft(macro_etiqueta=5))
    live = _live(context, _Db([row]))
    asyncio.run(live.refresh())
    assert live.serving_state(CAMPAIGN) == NOT_SERVED_CONFLICT


def test_a_pause_or_a_new_row_is_picked_up_on_the_next_read(tmp_path: Path) -> None:
    context = _context(tmp_path)
    db = _Db([_row(context)])
    live = _live(context, db)
    asyncio.run(live.refresh())
    db.rows = []
    asyncio.run(live.refresh())
    assert live.segments == ()
    assert live.serving_state(CAMPAIGN) is None


# ---------------------------------------------------- la entrada: palabra clave y pregunta


class _Labeler:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def ensure(self, conversation_id: int, *, source: str) -> str:
        self.calls.append(conversation_id)
        return "added"


class _Answers:
    async def get_segment_entry_answers(self, **_: Any) -> dict[str, str]:
        return {}

    async def record_segment_entry_answer(self, **_: Any) -> str:
        return "recorded"


def _entry() -> CampaignEntry:
    return CampaignEntry(segments=(), labelers={}, answers=_Answers(), account_id=2, inbox_id=11, reset_command="/nuevo",
                         reset_confirmation="ok", clock=lambda: NOW, allow_empty=True)


def _bound(context: CampaignContext, db: _Db) -> tuple[LiveSegmentCatalog, CampaignEntry, _Labeler]:
    live = _live(context, db)
    labeler = _Labeler()
    entry = _entry()
    live.bind_entry(entry, make_labeler=lambda label, macro: labeler)
    return live, entry, labeler


def test_without_segments_the_entry_starts_empty_and_does_nothing() -> None:
    entry = _entry()
    turn = asyncio.run(entry.turn(conversation_id=1, history=[], history_complete=True,
                                  messages=[{"actor": "prospect", "text": "Black November", "_message_ref": "a"}],
                                  batch_refs={"a"}, labels=[], agent_replies_before=0))
    assert turn.labels == ()


def test_the_keyword_and_the_question_come_from_the_row_and_work_on_the_next_turn(tmp_path: Path) -> None:
    context = _context(tmp_path)
    db = _Db([_row(context)])
    live, entry, labeler = _bound(context, db)
    asyncio.run(live.refresh())
    assert entry.segments[0].keywords == ("black november",)
    assert entry.segments[0].question == "¿Nos escribes por la promoción de Black November?"
    turn = asyncio.run(entry.turn(conversation_id=9, history=[], history_complete=True,
                                  messages=[{"actor": "prospect", "text": "Hola, BLACK-NOVEMBER por favor", "_message_ref": "a"}],
                                  batch_refs={"a"}, labels=[], agent_replies_before=0))
    assert turn.labels == (LABEL,)
    assert labeler.calls == [9]


def test_when_the_campaign_pauses_the_keyword_stops_working(tmp_path: Path) -> None:
    context = _context(tmp_path)
    db = _Db([_row(context)])
    live, entry, _ = _bound(context, db)
    asyncio.run(live.refresh())
    db.rows = []
    asyncio.run(live.refresh())
    assert entry.segments == ()
    assert entry.has_question is False


def test_two_questions_that_overlap_are_a_conflict_not_an_outage(tmp_path: Path) -> None:
    context = _context(tmp_path, with_manifest_segment=True)
    other = _Db([_row(context)])
    live = _live(context, other)
    # El manifiesto ya tiene una pregunta vigente (la del segmento de prueba): la campana no entra.
    existing = context.manifest.agent_segments[0]
    assert existing.entry_question is not None
    labeler = _Labeler()
    entry = CampaignEntry(segments=[
        _entry_segment_from_manifest(context)], labelers={existing.label: labeler}, answers=_Answers(), account_id=2,
        inbox_id=11, reset_command="/nuevo", reset_confirmation="ok", clock=lambda: NOW)
    live.bind_entry(entry, make_labeler=lambda label, macro: labeler)
    asyncio.run(live.refresh())
    assert live.serving_state(CAMPAIGN) == NOT_SERVED_CONFLICT
    assert [segment.key for segment in entry.segments] == [existing.key]


def _entry_segment_from_manifest(context: CampaignContext) -> Any:
    from bridge.campaign_inbound_entry import EntrySegment

    declared = context.manifest.agent_segments[0]
    knowledge = context.manifest_catalog.segments[0].knowledge
    return EntrySegment(key=declared.key, label=declared.label, name=knowledge.name, version=knowledge.version,
                        valid_from=knowledge.valid_from, valid_until=knowledge.valid_until,
                        keywords=tuple(declared.entry_keywords), question=declared.entry_question)


# ------------------------------------------------------------------ el flag en el bridge


def _app_settings(tmp_path: Path, **overrides: Any) -> Any:
    base = _segment_settings(tmp_path, window=ALWAYS)
    defaults = dict(agent_campaigns_enabled=True, automation_expert_change_enabled=True,
                    flow_approval_enabled=True)
    defaults.update(overrides)
    return replace(base, **defaults)


class _NoCampaignRead(_NoSupabase):
    """Una base que anota si le leen las campanas."""

    reads = 0

    async def get_live_agent_campaigns(self, instance: dict[str, str]) -> dict[str, Any]:
        type(self).reads += 1
        return {"campaigns": []}


def test_with_the_flag_off_there_are_no_routes_and_the_base_is_not_read() -> None:
    _NoCampaignRead.reads = 0
    app = create_app(_approval_settings(flow_approval_enabled=False), supabase_client=_NoCampaignRead())  # type: ignore[arg-type]
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.post("/internal/automation-expert/v1/campaigns/preview", json={}).status_code == 404
        assert client.get("/internal/automation-expert/v1/campaigns").status_code == 404
        assert client.get(f"/flow-approval/c/{CAMPAIGN}", follow_redirects=False).status_code == 404
    assert _NoCampaignRead.reads == 0


@pytest.mark.parametrize(("override", "message"), [
    ({"agent_segments_enabled": False, "agent_segment_catalog": None}, "AGENT_SEGMENTS_ENABLED"),
    ({"campaign_inbound_entry_enabled": False}, "CAMPAIGN_INBOUND_ENTRY_ENABLED"),
    ({"automation_expert_change_enabled": False}, "AUTOMATION_EXPERT_CHANGE_ENABLED"),
    ({"flow_approval_enabled": False}, "FLOW_APPROVAL_ENABLED"),
])
def test_the_flag_does_not_start_without_what_it_needs(tmp_path: Path, override: dict, message: str) -> None:
    from bridge.agent_campaign_api import check_settings

    settings = _app_settings(tmp_path, **override)
    with pytest.raises(ValueError, match=message):
        check_settings(settings)


def test_the_manifest_catalog_rule_still_stops_the_start_with_a_bad_file(tmp_path: Path) -> None:
    """La decision del 06/11 queda para los archivos: un sha que no calza en el manifiesto no arranca."""
    from bridge.segment_knowledge import SegmentError

    settings = _segment_settings(tmp_path, window=ALWAYS)
    folder = tmp_path / "instancia" / "conocimiento" / "segmentos"
    target = next(folder.glob("*.toml"))
    text = target.read_text(encoding="utf-8")
    target.write_text(re.sub(r'chequeo_sha = "[0-9a-f]{64}"', 'chequeo_sha = "' + "0" * 64 + '"', text), encoding="utf-8")
    assert settings.instance_manifest is not None and settings.commercial_knowledge is not None
    with pytest.raises(SegmentError):
        load_segment_catalog(settings.instance_manifest, tmp_path / "instancia", settings.commercial_knowledge)


# ------------------------------------- la oferta de la campana en el link (E-410)
#
# oferta.codigo_oferta: la oferta de Hotmart con la que sale el link del agente.
# Las ofertas son las del manifiesto de ATT1 (tests/fixtures/instances/att1/
# instancia.toml); wmkq4ba9 es la de Black, que existe en el checkout publico
# (tests/fixtures/hotmart_checkout_offer_black_att1_20261110.json) y todavia
# no esta declarada.


def _with_offer_code(code: str, **offer: Any) -> dict[str, Any]:
    return _draft(oferta={"precio": "37", "moneda": "USD", "reemplaza": ["precio", "moneda"],
                          "codigo_oferta": code, **offer})


def test_the_offer_code_is_part_of_the_draft_and_of_the_segment(tmp_path: Path) -> None:
    body = parse_campaign(_with_offer_code("bmaztyhg"))
    assert body["oferta"]["codigo_oferta"] == "bmaztyhg"
    context = _context(tmp_path)
    blocks, warnings, segment = instance_blocks(body, context=context, now=NOW)
    assert blocks == []
    assert _codes(warnings) == ["campaign_offer_not_verified"]
    assert segment is not None and segment.knowledge.offer is not None
    assert segment.knowledge.offer.offer_code == "bmaztyhg"
    assert "El link de pago lo arma el sistema con la oferta de esta campaña." in segment.rendered


def test_an_offer_the_instance_does_not_declare_is_blocked(tmp_path: Path) -> None:
    blocks, warnings, _ = instance_blocks(parse_campaign(_with_offer_code("wmkq4ba9")),
                                          context=_context(tmp_path), now=NOW)
    assert {"code": "campaign_offer_unknown", "field": "oferta.codigo_oferta"} in blocks
    assert "campaign_offer_not_verified" in _codes(warnings)


def test_an_offer_with_the_wrong_shape_is_a_segment_error(tmp_path: Path) -> None:
    blocks, _, _ = instance_blocks(parse_campaign(_with_offer_code("wm-kq")), context=_context(tmp_path), now=NOW)
    assert "campaign_segment_invalid" in _codes(blocks)


def test_the_offer_code_and_the_coupon_together_warn_both(tmp_path: Path) -> None:
    blocks, warnings, _ = instance_blocks(parse_campaign(_with_offer_code("2uafw5bg", cupon="BLACK37")),
                                          context=_context(tmp_path), now=NOW)
    assert blocks == []
    assert _codes(warnings) == ["campaign_coupon_not_verified", "campaign_offer_not_verified"]


def test_with_several_products_the_offer_has_to_be_of_the_product_of_the_campaign(tmp_path: Path) -> None:
    from test_multi_product_manifest import SECOND

    context = _context(tmp_path)
    payload = tomllib.loads((Path(__file__).parent / "fixtures" / "instances" / "att1" / "instancia.toml")
                            .read_text(encoding="utf-8"))
    payload["productos"] = [{"clave": "att1"}, copy.deepcopy(SECOND)]
    for product in payload["productos"]:
        product.pop("etiqueta", None)
        product.pop("macro_etiqueta", None)
    two = InstanceManifest.from_mapping(payload)
    context = replace(context, manifest=replace(context.manifest, products=two.products))
    # Sin oferta.producto la campana es del principal: la del segundo producto no sirve.
    blocks, _, _ = instance_blocks(parse_campaign(_with_offer_code("prueba2b")), context=context, now=NOW)
    assert "campaign_offer_unknown" in _codes(blocks)
    blocks, _, _ = instance_blocks(parse_campaign(_with_offer_code("bmaztyhg")), context=context, now=NOW)
    assert "campaign_offer_unknown" not in _codes(blocks)



# ------------------------------------- las dos listas del catalogo vivo (E-411 C11)


def _offer_row(context: CampaignContext, code: str = "bmaztyhg", **kwargs: Any) -> dict[str, Any]:
    draft = _draft(oferta={"precio": "37", "moneda": "USD", "reemplaza": ["precio", "moneda"], "codigo_oferta": code})
    return _row(context, draft=draft, **kwargs)


def _labels_bound(context: CampaignContext, db: _Db) -> tuple[LiveSegmentCatalog, CampaignEntry, _Labeler]:
    live, entry, labeler = _bound(context, db)
    live.bind_labels(segments=(), labelers={}, campaigns=(), make_labeler=lambda label, macro: labeler)
    return live, entry, labeler


def test_a_row_appears_in_both_lists_and_leaves_them_when_it_goes(tmp_path: Path) -> None:
    context = _context(tmp_path)
    db = _Db([_offer_row(context)])
    live, _, labeler = _labels_bound(context, db)
    labels, entries = live.label_source(), live.entry_source()
    assert labels.segments() == () and entries.campaigns() == ()

    asyncio.run(live.refresh())
    [segment] = labels.segments()
    assert (segment.key, segment.label, segment.macro_id, segment.origin) == (
        "black-november-2026", LABEL, MACRO, "campana_ae")
    [campaign] = entries.campaigns()
    assert campaign.offers == frozenset({"bmaztyhg"})
    assert campaign.landings == frozenset({"org-a"})  # la landing de bmaztyhg en el manifiesto de ATT1
    # El mismo etiquetador que la palabra clave, para los cuatro caminos.
    assert labels.labeler(LABEL) is labeler and entries.labeler(LABEL) is labeler

    db.rows = []
    asyncio.run(live.refresh())
    assert labels.segments() == () and entries.campaigns() == ()


def test_a_campaign_without_its_own_offer_labels_but_does_not_enter_by_offer(tmp_path: Path) -> None:
    context = _context(tmp_path)
    live, _, _ = _labels_bound(context, _Db([_row(context)]))
    asyncio.run(live.refresh())
    assert [segment.label for segment in live.label_source().segments()] == [LABEL]
    assert live.entry_source().campaigns() == ()


def test_a_failed_read_keeps_both_lists(tmp_path: Path) -> None:
    context = _context(tmp_path)
    db = _Db([_offer_row(context)])
    live, _, _ = _labels_bound(context, db)
    asyncio.run(live.refresh())
    db.down = True
    db.rows = []
    assert asyncio.run(live.refresh()) is False
    assert len(live.label_source().segments()) == 1 and len(live.entry_source().campaigns()) == 1


def _with_manifest_campaign(context: CampaignContext, **campaign: Any) -> CampaignContext:
    from bridge.instance_manifest import Campaign

    values = {"segment": "otra", "landings": (), "offers": ()}
    values.update(campaign)
    return replace(context, manifest=replace(context.manifest, campaigns=(Campaign(**values),)))


@pytest.mark.parametrize("taken", [{"offers": ("bmaztyhg",)}, {"landings": ("org-a",)}], ids=["oferta", "landing"])
def test_an_offer_or_its_landing_of_a_manifest_campaign_is_blocked(tmp_path: Path, taken: dict[str, Any]) -> None:
    context = _with_manifest_campaign(_context(tmp_path), **taken)
    blocks, _, _ = instance_blocks(parse_campaign(_with_offer_code("bmaztyhg")), context=context, now=NOW)
    assert {"code": "campaign_offer_entry_taken", "field": "oferta.codigo_oferta"} in blocks


def test_a_published_campaign_that_clashes_keeps_being_served_without_the_offer_entry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="bridge")
    plain = _context(tmp_path)
    row = _offer_row(plain)
    context = _with_manifest_campaign(plain, offers=("bmaztyhg",))
    live, _, _ = _labels_bound(context, _Db([row]))
    asyncio.run(live.refresh())
    assert live.serving_state(CAMPAIGN) == "served"
    assert [segment.label for segment in live.label_source().segments()] == [LABEL]
    assert live.entry_source().campaigns() == ()
    assert "agent_campaign_entry_skipped key=black-november-2026 reason=offer_taken" in caplog.text


# ------------------------------------- revision (E-410/E-411): el flag y la oferta por defecto


def test_without_the_offer_link_flag_a_campaign_with_its_offer_is_blocked_and_not_served(tmp_path: Path) -> None:
    # Hallazgo 1: con el flag apagado el bloque prometeria un link con la oferta que no sale.
    off = _context(tmp_path, offer_link=False)
    blocks, _, _ = instance_blocks(parse_campaign(_with_offer_code("bmaztyhg")), context=off, now=NOW)
    assert {"code": "campaign_offer_link_disabled", "field": "oferta.codigo_oferta"} in blocks
    row = _offer_row(_context(tmp_path / "otra"))
    live = _live(off, _Db([row]))
    asyncio.run(live.refresh())
    assert live.serving_state(CAMPAIGN) == "contenido_invalido"
    # Una campana sin la oferta se sirve igual con el flag apagado.
    plain = _live(off, _Db([_row(off)]))
    asyncio.run(plain.refresh())
    assert plain.serving_state(CAMPAIGN) == "served"


def test_the_default_offer_is_not_an_offer_of_a_campaign(tmp_path: Path) -> None:
    # Hallazgo 3: gopi6lh7 es la por defecto de ATT1: el link ya sale con ella, y como
    # entrada por oferta etiquetaria todo carrito de su landing.
    blocks, _, _ = instance_blocks(parse_campaign(_with_offer_code("gopi6lh7")), context=_context(tmp_path), now=NOW)
    assert {"code": "campaign_offer_is_default", "field": "oferta.codigo_oferta"} in blocks
