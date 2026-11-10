"""La campana por la landing que vio la persona (E-321).

Contrato: ``docs/contracts/campaign-entry-v1.md``. Diseno: E-319, adenda del 03/11.

Datos:

* Lo que contesta la base sale de ``tests/fixtures/flow_engine/native_campaign_origin_rows.json``,
  que escribe ``validate_flow_engine_native_campaign_origin.mjs --emit`` con la cadena
  entera de migraciones: el formulario y el carrito capturado
  (``hotmart_cart_abandonment_rejected_v1.json``) por las admisiones portables, y el
  recorrido nativo del carrito. La landing es la de la intencion correlacionada
  (``alimenta-tu-tiroides-d`` en el mundo de ATT1) y la oferta, la del carrito
  (``2uafw5bg``, la de esa landing en el manifiesto de ATT1).
* El manifiesto es el de la instancia de prueba de ATT1 (``tests/fixtures/instances/att1``).
* El despacho usa los dobles de ``test_flow_engine_native_dispatch.py`` y de
  ``test_segment_labels.py`` (E-316): el inbox 11 de ATT1, la conversacion 200 y el
  macro del segmento con la forma del GET real de un macro de ATT1.
* **Deuda:** la landing de la campana del manifiesto es una de las de hoy
  (``alimenta-tu-tiroides-d``): las de Black November todavia no existen, y la etiqueta
  ``black-november`` es un ejemplo.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import tomllib
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from test_flow_engine_native_dispatch import _run, _templates
from test_segment_labels import (
    END,
    INSIDE,
    LABEL,
    SEGMENT_KEY,
    SEGMENT_MACRO_ID,
    START,
    _catalog,
    _declared,
    _segment,
    _segment_dispatcher,
    _SegmentAuthority,
    _SegmentChatwoot,
)

from bridge.campaign_entry import (
    CampaignEntry,
    CampaignEntryLabels,
    agent_campaign_entries,
    campaign_entry_labels,
    declared_campaigns,
    match_campaign,
)
from bridge.instance_manifest import Campaign, InstanceManifest, ManifestError
from bridge.product_label import ProductLabeler
from bridge.segment_labels import NativeSegmentLabels
from bridge.supabase import FlowEngineNativeCampaignOrigin, SupabaseClient, SupabaseError

FIXTURES = Path(__file__).parent / "fixtures"
ROWS = json.loads((FIXTURES / "flow_engine" / "native_campaign_origin_rows.json").read_text(encoding="utf-8"))
ATT1_MANIFEST = FIXTURES / "instances" / "att1" / "instancia.toml"
CART_FIXTURE = json.loads((FIXTURES / "hotmart_cart_abandonment_rejected_v1.json").read_text(encoding="utf-8"))
# La landing y la oferta del mundo de ATT1 de la cadena (las del fixture de la base).
LANDING = ROWS["cart"]["landing"]
OFFER = "2uafw5bg"
OTHER_OFFER = "gopi6lh7"


def _origin(row: dict[str, Any], **changes: Any) -> FlowEngineNativeCampaignOrigin:
    base = {**row, "oferta": OFFER if row.get("oferta") == "<OFERTA>" else row.get("oferta")}
    base.update(changes)
    return FlowEngineNativeCampaignOrigin(
        disparador=base["disparador"], oferta=base["oferta"], landing=base["landing"]
    )


# ---------------------------------------------------------------- el manifiesto


def _manifest_payload(campaigns: list[dict[str, Any]] | None, *, macro: bool = True) -> dict[str, Any]:
    payload = tomllib.loads(ATT1_MANIFEST.read_text(encoding="utf-8"))
    segment = {"clave": SEGMENT_KEY, "etiqueta": LABEL, "conocimiento": "conocimiento/segmentos/bn-v1.toml",
               "prioridad": 10}
    if macro:
        segment["macro_etiqueta"] = SEGMENT_MACRO_ID
    payload["agente"]["segmentos"] = [segment]
    if campaigns is not None:
        payload["campanas"] = campaigns
    return payload


def test_the_cart_fixture_carries_an_offer_and_no_coupon() -> None:
    # Lo que justifica la landing: el carrito de Hotmart trae offer.code y ningun cupon.
    text = json.dumps(CART_FIXTURE).lower()
    assert '"offer"' in text and '"code"' in text
    assert "coupon" not in text and "cupon" not in text


def test_a_campaign_declares_its_landings_and_its_own_offers() -> None:
    manifest = InstanceManifest.from_mapping(
        _manifest_payload([{"segmento": SEGMENT_KEY, "landings": [LANDING], "ofertas": [OFFER]}])
    )

    assert manifest.campaigns == (Campaign(segment=SEGMENT_KEY, landings=(LANDING,), offers=(OFFER,)),)
    # Sin [[campanas]], ninguna: el manifiesto de hoy carga igual.
    assert InstanceManifest.from_mapping(_manifest_payload(None)).campaigns == ()
    assert InstanceManifest.from_toml_file(ATT1_MANIFEST).campaigns == ()


@pytest.mark.parametrize(
    ("campaigns", "macro", "message"),
    [
        ([{"segmento": "otra", "landings": [LANDING]}], True, "no es la clave de un"),
        ([{"segmento": SEGMENT_KEY, "landings": [LANDING]}], False, "no tiene macro_etiqueta"),
        ([{"segmento": SEGMENT_KEY, "landings": ["bn-no-declarada"]}], True, "no es el landing_id de ninguna"),
        ([{"segmento": SEGMENT_KEY, "ofertas": ["noexiste1"]}], True, "no es una oferta declarada"),
        ([{"segmento": SEGMENT_KEY}], True, "al menos una landing o una oferta"),
        ([{"segmento": SEGMENT_KEY, "landings": [LANDING, LANDING]}], True, "repetidos"),
        ([{"segmento": SEGMENT_KEY, "landings": [LANDING]}, {"segmento": SEGMENT_KEY, "ofertas": [OFFER]}], True,
         "el mismo segmento|la misma segmento"),
        ([{"segmento": SEGMENT_KEY, "landings": [LANDING], "url": "https://x"}], True, "url"),
        ({"segmento": SEGMENT_KEY}, True, "lista de tablas"),
    ],
)
def test_a_wrong_campaign_does_not_load(campaigns: Any, macro: bool, message: str) -> None:
    with pytest.raises(ManifestError, match=message):
        InstanceManifest.from_mapping(_manifest_payload(campaigns, macro=macro))


# ---------------------------------------------------------------- la campana de una landing


def _entry(**changes: Any) -> CampaignEntry:
    values: dict[str, Any] = {"segment": _segment(), "landings": frozenset({LANDING}), "offers": frozenset()}
    values.update(changes)
    return CampaignEntry(**values)


def test_the_campaigns_come_with_the_window_of_their_segment() -> None:
    manifest = SimpleNamespace(
        agent_segments=(_declared(),),
        campaigns=(Campaign(segment=SEGMENT_KEY, landings=(LANDING,), offers=(OFFER,)),),
    )

    [campaign] = declared_campaigns(manifest, _catalog())

    assert campaign.segment == _segment()
    assert campaign.landings == frozenset({LANDING}) and campaign.offers == frozenset({OFFER})


def test_the_landing_names_the_campaign_only_while_it_runs() -> None:
    campaign = _entry()
    assert match_campaign([campaign], landing=LANDING, offer=None, now=INSIDE) == (campaign, "landing")
    assert match_campaign([campaign], landing=LANDING, offer=None, now=START) == (campaign, "landing")
    # Por empezar, vencida o sin vigencia conocida: nada (sin corte por horario adentro).
    assert match_campaign([campaign], landing=LANDING, offer=None, now=START - timedelta(seconds=1)) is None
    assert match_campaign([campaign], landing=LANDING, offer=None, now=END) is None
    unknown = _entry(segment=_segment(valid_from=None, valid_until=None))
    assert match_campaign([unknown], landing=LANDING, offer=None, now=INSIDE) is None
    # Otra landing (las de siempre) no es de la campana.
    assert match_campaign([campaign], landing="ads-a", offer=OFFER, now=INSIDE) is None


def test_the_own_offer_is_the_fallback_and_the_landing_wins() -> None:
    by_landing = _entry()
    by_offer = _entry(segment=_segment(key="otra", label="otra"), landings=frozenset(), offers=frozenset({OFFER}))
    # Sin formulario (sin landing), la oferta propia identifica.
    assert match_campaign([by_landing, by_offer], landing=None, offer=OFFER, now=INSIDE) == (by_offer, "oferta")
    # Con las dos, manda la landing.
    assert match_campaign([by_offer, by_landing], landing=LANDING, offer=OFFER, now=INSIDE) == (by_landing, "landing")


# ---------------------------------------------------------------- el cliente de la base


def test_the_client_reads_what_the_base_answers() -> None:
    bodies: list[tuple[str, Any]] = []

    def answer(body: Any) -> SupabaseClient:
        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, content=json.dumps(body).encode(),
                                  headers={"content-type": "application/json"})

        return SupabaseClient(base_url="https://supabase.example.test", service_role_key="k",
                              transport=httpx.MockTransport(handler))

    cart = asyncio.run(answer(ROWS["cart"]).get_flow_engine_native_campaign_origin("action-1"))
    fabricated = asyncio.run(answer(ROWS["cart_fabricated_intent"]).get_flow_engine_native_campaign_origin("a"))
    other = asyncio.run(answer(ROWS["not_native"]).get_flow_engine_native_campaign_origin("a"))

    assert cart == FlowEngineNativeCampaignOrigin(disparador="carrito", oferta="<OFERTA>", landing=LANDING)
    assert fabricated is not None and fabricated.landing is None
    assert other is None
    assert bodies[0] == ("/rest/v1/rpc/flow_engine_native_campaign_origin_v1", {"p_action_id": "action-1"})
    with pytest.raises(SupabaseError, match="invalid_row"):
        asyncio.run(answer({**ROWS["cart"], "landing": 3}).get_flow_engine_native_campaign_origin("a"))
    with pytest.raises(SupabaseError, match="invalid_shape"):
        asyncio.run(answer([ROWS["cart"]]).get_flow_engine_native_campaign_origin("a"))


# ---------------------------------------------------------------- el despacho nativo


class _CampaignAuthority(_SegmentAuthority):
    """La accion de un carrito nativo sin etiquetar en su flujo, y de donde viene su recorrido."""

    def __init__(self, *, origin: FlowEngineNativeCampaignOrigin | None = None, origin_fails: bool = False,
                 label: str | None = None, **kwargs: Any) -> None:
        super().__init__(label=label, **kwargs)
        self.origin = origin if origin is not None else _origin(ROWS["cart"])
        self.origin_fails = origin_fails

    async def get_flow_engine_native_campaign_origin(self, action_id: str) -> FlowEngineNativeCampaignOrigin | None:
        self.engine_calls.append("origin")
        if self.origin_fails:
            raise SupabaseError("flow_engine_native_campaign_origin_v1_failed: HTTP 503")
        return self.origin


def _campaign_dispatcher(authority: _CampaignAuthority, chatwoot: _SegmentChatwoot, tmp_path: Path, *,
                         now: Any = INSIDE, campaign: CampaignEntry | None = None, flow_labels: bool = False,
                         flag: bool = True) -> Any:
    # Con flow_labels, el plan de E-316 va adentro (FLOW_ENGINE_SEGMENT_LABELS_ENABLED prendido).
    dispatcher = _segment_dispatcher(authority, chatwoot, tmp_path, now=now, flag=flow_labels)
    if flag:
        client = chatwoot.client()
        dispatcher._native_segment_labels = CampaignEntryLabels(
            campaigns=[campaign or _entry()],
            labelers={LABEL: ProductLabeler(client=client, label=LABEL, macro_id=SEGMENT_MACRO_ID)},
            flow_labels=dispatcher._native_segment_labels,
            clock=lambda: now,
        )
    return dispatcher


def test_a_cart_from_a_campaign_landing_gets_the_label_before_its_first_template(tmp_path: Path) -> None:
    authority = _CampaignAuthority()
    chatwoot = _SegmentChatwoot()

    _run(_campaign_dispatcher(authority, chatwoot, tmp_path))

    assert chatwoot.order() == ["segmento", "plantilla"]
    [execute] = chatwoot.posts(f"/macros/{SEGMENT_MACRO_ID}/execute")
    assert execute == {"conversation_ids": [200]}
    assert chatwoot.labels == [LABEL]
    assert chatwoot.notes == []
    assert authority.engine_calls == ["context", "request_started", "origin", "tail"]
    assert authority.acceptances


def test_a_failed_campaign_label_does_not_stop_the_template_and_is_tried_again(tmp_path: Path) -> None:
    authority = _CampaignAuthority()
    chatwoot = _SegmentChatwoot(failures=1)

    _run(_campaign_dispatcher(authority, chatwoot, tmp_path))

    assert chatwoot.order() == ["segmento", "plantilla", "segmento"]
    assert chatwoot.labels == [LABEL]
    assert len(_templates(chatwoot)) == 1


@pytest.mark.parametrize(
    "origin",
    [
        _origin(ROWS["cart"], landing="ads-a", oferta=OTHER_OFFER),  # una landing de siempre
        _origin(ROWS["cart_fabricated_intent"], oferta=OTHER_OFFER),  # sin landing real ni oferta propia
        _origin(ROWS["cart"], disparador="compra"),  # la compra no entra
    ],
    ids=["otra_landing", "intencion_fabricada", "compra"],
)
def test_without_a_campaign_landing_the_cart_goes_as_today(tmp_path: Path, origin: Any) -> None:
    authority = _CampaignAuthority(origin=origin)
    chatwoot = _SegmentChatwoot()

    _run(_campaign_dispatcher(authority, chatwoot, tmp_path))

    assert chatwoot.order() == ["plantilla"]
    assert chatwoot.labels == []
    assert len(_templates(chatwoot)) == 1


def test_the_own_offer_labels_a_cart_without_a_form(tmp_path: Path) -> None:
    authority = _CampaignAuthority(origin=_origin(ROWS["cart_fabricated_intent"]))
    chatwoot = _SegmentChatwoot()
    campaign = _entry(landings=frozenset(), offers=frozenset({OFFER}))

    _run(_campaign_dispatcher(authority, chatwoot, tmp_path, campaign=campaign))

    assert chatwoot.order() == ["segmento", "plantilla"]


def test_outside_the_window_of_the_campaign_nothing_is_put(tmp_path: Path) -> None:
    for now in (START - timedelta(minutes=1), END):
        authority = _CampaignAuthority()
        chatwoot = _SegmentChatwoot()

        _run(_campaign_dispatcher(authority, chatwoot, tmp_path / now.strftime("%H%M%S"), now=now))

        assert chatwoot.order() == ["plantilla"]
        assert chatwoot.labels == []


def test_without_an_answer_from_the_base_the_template_goes(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    authority = _CampaignAuthority(origin_fails=True)
    chatwoot = _SegmentChatwoot()

    with caplog.at_level(logging.WARNING, logger="bridge.campaign_entry"):
        _run(_campaign_dispatcher(authority, chatwoot, tmp_path))

    assert chatwoot.order() == ["plantilla"]
    assert "campaign_entry_origin_unavailable" in caplog.text and "SupabaseError" in caplog.text


def test_the_segment_of_the_flow_wins_over_the_landing(tmp_path: Path) -> None:
    # El flujo ya etiqueta un segmento (E-316): manda el del flujo, y no se lee el origen.
    authority = _CampaignAuthority(label=LABEL)
    chatwoot = _SegmentChatwoot()

    _run(_campaign_dispatcher(authority, chatwoot, tmp_path, flow_labels=True))

    assert chatwoot.order() == ["segmento", "plantilla"]
    assert "origin" not in authority.engine_calls


def test_without_the_flag_the_dispatch_reads_nothing_new(tmp_path: Path) -> None:
    authority = _CampaignAuthority()
    chatwoot = _SegmentChatwoot()

    _run(_campaign_dispatcher(authority, chatwoot, tmp_path, flag=False))

    assert "origin" not in authority.engine_calls
    assert chatwoot.order() == ["plantilla"]
    assert chatwoot.labels == []


# ---------------------------------------------------------------- el armado


def _settings(**changes: Any) -> SimpleNamespace:
    manifest = SimpleNamespace(
        agent_segments=(_declared(),),
        campaigns=(Campaign(segment=SEGMENT_KEY, landings=(LANDING,)),),
    )
    values: dict[str, Any] = {
        "flow_engine_campaign_entry_enabled": True,
        "flow_engine_enabled": True,
        "agent_segments_enabled": True,
        "agent_segment_catalog": _catalog(),
        "instance_manifest": manifest,
        "chatwoot_pause_macro_id": 5,
        "chatwoot_resume_macro_id": 6,
        "chatwoot_opt_out_macro_id": 7,
        "chatwoot_client_label_macro_id": 31,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_without_the_flag_the_planner_is_the_one_of_e316() -> None:
    inner = object()
    assert campaign_entry_labels(_settings(flow_engine_campaign_entry_enabled=False), object(), inner) is inner  # type: ignore[arg-type]
    assert campaign_entry_labels(_settings(flow_engine_campaign_entry_enabled=False), object(), None) is None


def test_with_the_flag_it_wraps_the_planner_of_e316() -> None:
    inner = NativeSegmentLabels(
        segments=[_segment()],
        labelers={LABEL: ProductLabeler(client=SimpleNamespace(), label=LABEL, macro_id=SEGMENT_MACRO_ID)},
    )
    planner = campaign_entry_labels(_settings(), SimpleNamespace(), inner)
    assert isinstance(planner, CampaignEntryLabels)
    assert planner._flow_labels is inner


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"flow_engine_enabled": False}, "requires FLOW_ENGINE_ENABLED"),
        ({"agent_segments_enabled": False}, "requires AGENT_SEGMENTS_ENABLED"),
        ({"agent_segment_catalog": None}, "requires AGENT_SEGMENTS_ENABLED"),
        ({"instance_manifest": None}, "requires an instance manifest"),
        ({"instance_manifest": SimpleNamespace(agent_segments=(_declared(),), campaigns=())}, "requires \\[\\[campanas\\]\\]"),
        ({"chatwoot_client_label_macro_id": SEGMENT_MACRO_ID}, "cannot be a pause, resume, opt-out or client macro"),
    ],
)
def test_the_setup_fails_closed(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        campaign_entry_labels(_settings(**changes), SimpleNamespace(), None)


def test_the_setup_needs_the_control_client() -> None:
    with pytest.raises(ValueError, match="control client"):
        campaign_entry_labels(_settings(), None, None)


def test_the_capability_and_the_flag_are_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from bridge import app as app_module

    assert "flow_engine_campaign_entry_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    assert app_module.Settings.__dataclass_fields__["flow_engine_campaign_entry_enabled"].default is False


def test_a_fixture_copy_is_not_mutated() -> None:
    # Las filas de la base se leen tal cual: el doble no las toca.
    before = copy.deepcopy(ROWS)
    _origin(ROWS["cart"])
    assert ROWS == before
    assert replace(_entry(), offers=frozenset({OFFER})).offers == frozenset({OFFER})


# ---------------------------------------------------------------- el cableado en el bridge


def test_the_flag_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from bridge.app import Settings

    for name, value in {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "CHATWOOT_INBOX_ID": "9",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("FLOW_ENGINE_CAMPAIGN_ENTRY_ENABLED", raising=False)
    assert Settings.from_env().flow_engine_campaign_entry_enabled is False
    monkeypatch.setenv("FLOW_ENGINE_CAMPAIGN_ENTRY_ENABLED", "yes")
    assert Settings.from_env().flow_engine_campaign_entry_enabled is False
    monkeypatch.setenv("FLOW_ENGINE_CAMPAIGN_ENTRY_ENABLED", " TRUE ")
    assert Settings.from_env().flow_engine_campaign_entry_enabled is True


def test_create_app_does_not_start_with_the_flag_and_without_what_it_needs(tmp_path: Path) -> None:
    from test_customer_status import _settings as _manifest_app_settings
    from test_whatsapp_inbound_equivalence import StubChatwootClient, _johanna_settings

    from bridge.app import create_app

    with pytest.raises(ValueError, match="FLOW_ENGINE_CAMPAIGN_ENTRY_ENABLED requires an instance manifest"):
        create_app(replace(_johanna_settings(tmp_path / "j"), flow_engine_campaign_entry_enabled=True))
    settings = _manifest_app_settings(tmp_path / "m")
    assert settings.instance_manifest is not None and settings.flow_engine_enabled is False
    with pytest.raises(ValueError, match="FLOW_ENGINE_CAMPAIGN_ENTRY_ENABLED requires FLOW_ENGINE_ENABLED"):
        create_app(replace(settings, flow_engine_campaign_entry_enabled=True),
                   chatwoot_client=StubChatwootClient(messages=[]))


# ------------------------------------- las campanas del AE por su oferta (E-411 C10)
#
# El origen es el de las filas capturadas de flow_engine_native_campaign_origin_v1
# (native_campaign_origin_rows.json), con la oferta de Black de ATT1 (wmkq4ba9,
# la del checkout capturado en hotmart_checkout_offer_black_att1_20261110.json).

BLACK = "wmkq4ba9"
BLACK_OFFER = SimpleNamespace(code=BLACK, landing_id=LANDING)


class _LiveEntries:
    live = True

    def __init__(self, campaigns: tuple[CampaignEntry, ...] = ()) -> None:
        self.items = campaigns
        self.labeler_of = {LABEL: ProductLabeler(client=SimpleNamespace(), label=LABEL, macro_id=SEGMENT_MACRO_ID)}

    def campaigns(self) -> tuple[CampaignEntry, ...]:
        return self.items

    def labeler(self, label: str) -> Any:
        return self.labeler_of.get(label)


def _ae_segment(**changes: Any) -> Any:
    from bridge.segment_labels import ORIGIN_AGENT_CAMPAIGN

    return replace(_segment(origin=ORIGIN_AGENT_CAMPAIGN), **changes)


def test_an_ae_campaign_enters_by_its_offer_and_by_the_landing_of_that_offer() -> None:
    [entry] = agent_campaign_entries([(_ae_segment(), BLACK)], [BLACK_OFFER, SimpleNamespace(code=OFFER, landing_id="x")])
    assert entry.offers == frozenset({BLACK}) and entry.landings == frozenset({LANDING})
    # Sin codigo_oferta, con una oferta no declarada o sin macro, no entra.
    assert agent_campaign_entries([(_ae_segment(), None)], [BLACK_OFFER]) == ()
    assert agent_campaign_entries([(_ae_segment(), "zzzz9999")], [BLACK_OFFER]) == ()
    assert agent_campaign_entries([(_ae_segment(macro_id=None), BLACK)], [BLACK_OFFER]) == ()


def _plan(source: _LiveEntries, origin: FlowEngineNativeCampaignOrigin, *, now: Any = INSIDE) -> Any:
    labels = CampaignEntryLabels(source=source, clock=lambda: now)
    authority = _CampaignAuthority(origin=origin)
    return asyncio.run(labels.plan(authority, action_id="a")), authority


def test_a_cart_of_the_offer_of_the_campaign_gets_its_label(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="bridge")
    source = _LiveEntries(agent_campaign_entries([(_ae_segment(), BLACK)], [BLACK_OFFER]))

    by_offer, _ = _plan(source, _origin(ROWS["cart"], oferta=BLACK, landing=None))
    by_landing, _ = _plan(source, _origin(ROWS["cart"], oferta=BLACK))

    assert by_offer is not None and by_offer.segment.label == LABEL
    assert by_landing is not None
    assert "by=oferta" in caplog.text and "by=landing" in caplog.text


@pytest.mark.parametrize(
    ("window", "origin_changes"),
    [
        ({"valid_from": END, "valid_until": END + timedelta(days=1)}, {}),
        ({"valid_from": START - timedelta(days=9), "valid_until": START}, {}),
        ({}, {"oferta": OTHER_OFFER, "landing": None}),
        ({}, {"disparador": "compra"}),
    ],
    ids=["por-empezar", "vencida", "otra-oferta", "compra"],
)
def test_nothing_outside_the_campaign(window: dict[str, Any], origin_changes: dict[str, Any]) -> None:
    source = _LiveEntries(agent_campaign_entries([(_ae_segment(**window), BLACK)], [BLACK_OFFER]))
    plan, _ = _plan(source, _origin(ROWS["cart"], **{"oferta": BLACK, **origin_changes}))
    assert plan is None


def test_without_campaigns_the_origin_is_not_even_read() -> None:
    plan, authority = _plan(_LiveEntries(), _origin(ROWS["cart"], oferta=BLACK))
    assert plan is None
    assert "origin" not in authority.engine_calls


def test_the_manifest_wins_over_the_ae_with_the_same_offer() -> None:
    manifest_segment = replace(_segment(), key="del-manifiesto", label="del-manifiesto")
    manifest_entry = CampaignEntry(segment=manifest_segment, landings=frozenset(), offers=frozenset({BLACK}))
    source = _LiveEntries((manifest_entry, *agent_campaign_entries([(_ae_segment(), BLACK)], [BLACK_OFFER])))
    source.labeler_of["del-manifiesto"] = ProductLabeler(client=SimpleNamespace(), label="del-manifiesto", macro_id=42)

    plan, _ = _plan(source, _origin(ROWS["cart"], oferta=BLACK, landing=None))
    assert plan is not None and plan.segment.label == "del-manifiesto"
