"""El cableado de E-411: los tres caminos de la etiqueta leen las campanas del AE.

Con ``AGENT_CAMPAIGN_LABEL_PATHS_ENABLED`` (apagado por defecto, exige
``AGENT_CAMPAIGNS_ENABLED``), ``segment_labels_setup`` y ``campaign_entry_labels``
reciben el catalogo vivo: arrancan sin ``[[agente.segmentos]]`` ni ``[[campanas]]``
y la herencia, el plan nativo (el ``etiquetar`` del flujo), el validador, lo que
ve el AE en ``/flows`` y la entrada por oferta leen las campanas servidas. Sin el
flag, el bridge llama a los dos armados como hoy.

Datos: la fila de la campana es la de ``tests/test_agent_campaigns.py`` (el
borrador del AE contra el manifiesto de ATT1), con ``codigo_oferta`` una oferta
declarada de ATT1; el origen del carrito sale de las filas capturadas de
``native_campaign_origin_rows.json``; las lecturas de Chatwoot de la herencia
son las capturadas del contacto de Maribel (``_InheritInbox``).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_agent_campaigns import LABEL, NOW, _context, _Db, _draft, _live, _row
from test_campaign_entry import ROWS, _CampaignAuthority, _origin
from test_first_touch_active_conversation import INBOX
from test_segment_labels import _InheritInbox, _listing, _SegmentAuthority, _webhook
from test_segment_labels_wiring import _ask_for_the_link, _Customer
from test_whatsapp_inbound_equivalence import FORM_ID

from bridge import app as app_module
from bridge.app import Settings, create_app
from bridge.campaign_entry import campaign_entry_labels
from bridge.segment_labels import automation_expert_segments, segment_labels_setup

OFFER = "bmaztyhg"  # una oferta declarada de ATT1, la de la landing org-a


def _offer_row(context: Any) -> dict[str, Any]:
    draft = _draft(oferta={"precio": "37", "moneda": "USD", "reemplaza": ["precio", "moneda"],
                           "codigo_oferta": OFFER})
    return _row(context, draft=draft)


def _settings(context: Any, **changes: Any) -> SimpleNamespace:
    """Los flags de los tres caminos, con el manifiesto de ATT1 sin segmentos ni campanas."""

    values: dict[str, Any] = {
        "instance_manifest": context.manifest,
        "agent_segment_catalog": context.manifest_catalog,
        "agent_segments_enabled": True,
        "flow_engine_enabled": True,
        "flow_engine_segment_labels_enabled": True,
        "segment_label_inheritance_enabled": True,
        "flow_engine_campaign_entry_enabled": True,
        "chatwoot_inbox_id": 11,
        "chatwoot_pause_macro_id": 4, "chatwoot_resume_macro_id": 5, "chatwoot_opt_out_macro_id": 6,
        "chatwoot_client_label_macro_id": 31,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_without_the_live_catalog_the_paths_still_need_the_manifest(tmp_path: Path) -> None:
    context = _context(tmp_path)
    assert context.manifest.agent_segments == () and context.manifest.campaigns == ()
    with pytest.raises(ValueError, match=r"requires \[\[agente.segmentos\]\]"):
        segment_labels_setup(_settings(context), SimpleNamespace())
    with pytest.raises(ValueError, match=r"requires \[\[campanas\]\]"):
        campaign_entry_labels(_settings(context), SimpleNamespace(), None)


def test_with_the_live_catalog_the_three_paths_start_empty_and_see_the_campaign(tmp_path: Path) -> None:
    context = _context(tmp_path)
    db = _Db([])
    live = _live(context, db)
    settings = _settings(context)
    control = SimpleNamespace()

    setup = segment_labels_setup(settings, control, live=live)
    planner = campaign_entry_labels(settings, control, setup.native, live=live)
    # El reloj de la vigencia de la campana (20/11 al 1/12), como en test_agent_campaigns.
    setup.native._clock = planner._clock = lambda: NOW  # type: ignore[union-attr]
    assert setup.current_segments() == () and setup.validator_labels(NOW) == []

    db.rows = [_offer_row(context)]
    asyncio.run(live.refresh())

    # El validador y el AE ven la campana con su vigencia.
    assert setup.validator_labels(NOW) == [{"etiqueta": LABEL, "segmento": "black-november-2026", "estado": "vigente"}]
    seen = automation_expert_segments(setup.current_segments(), [], now=NOW)
    assert seen["segments"][0]["label"] == LABEL and seen["segments"][0]["labelable"] is True
    assert LABEL in seen["allowed_labels"]
    # El etiquetar del flujo con la etiqueta de la campana.
    flow_plan = asyncio.run(setup.native.plan(_SegmentAuthority(label=LABEL), action_id="a"))  # type: ignore[union-attr]
    assert flow_plan is not None and flow_plan.segment.origin == "campana_ae"
    # El carrito de la oferta de la campana, por la entrada por oferta.
    cart = _CampaignAuthority(origin=_origin(ROWS["cart"], oferta=OFFER, landing=None))
    entry_plan = asyncio.run(planner.plan(cart, action_id="b"))  # type: ignore[union-attr]
    assert entry_plan is not None and entry_plan.segment.label == LABEL
    # Los tres caminos con el mismo etiquetador.
    assert flow_plan.labeler is entry_plan.labeler
    assert setup.inheritance is not None


def test_with_the_live_catalog_and_no_campaign_nothing_new_is_read(tmp_path: Path) -> None:
    context = _context(tmp_path)
    live = _live(context, _Db([]))
    settings = _settings(context)
    setup = segment_labels_setup(settings, SimpleNamespace(), live=live)
    planner = campaign_entry_labels(settings, SimpleNamespace(), setup.native, live=live)

    cart = _CampaignAuthority(origin=_origin(ROWS["cart"], oferta=OFFER, landing=None))
    assert asyncio.run(planner.plan(cart, action_id="b")) is None  # type: ignore[union-attr]
    assert "origin" not in cart.engine_calls
    # La herencia no lee Chatwoot sin campanas.
    inbox = _InheritInbox(_listing([LABEL]))
    inheritance = replace_client(setup.inheritance, inbox)
    asyncio.run(inheritance.apply(_webhook(), conversation_id=9))
    assert inbox.listings() == 0


def replace_client(inheritance: Any, inbox: _InheritInbox) -> Any:
    inheritance._client = inbox.client()
    inheritance._inbox_id = INBOX
    return inheritance


# ------------------------------------------------------------- el bridge


def test_the_flag_is_off_by_default_and_portable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings.__dataclass_fields__["agent_campaign_label_paths_enabled"].default is False
    assert "agent_campaign_label_paths_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
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
    monkeypatch.delenv("AGENT_CAMPAIGN_LABEL_PATHS_ENABLED", raising=False)
    assert Settings.from_env().agent_campaign_label_paths_enabled is False
    monkeypatch.setenv("AGENT_CAMPAIGN_LABEL_PATHS_ENABLED", " TRUE ")
    assert Settings.from_env().agent_campaign_label_paths_enabled is True


def test_without_the_campaigns_the_flag_does_not_start(tmp_path: Path) -> None:
    from test_customer_status import _settings as _customer_settings

    settings = replace(_customer_settings(tmp_path), agent_campaign_label_paths_enabled=True)
    with pytest.raises(ValueError, match="AGENT_CAMPAIGN_LABEL_PATHS_ENABLED requires AGENT_CAMPAIGNS_ENABLED"):
        create_app(settings)


def test_with_the_flag_off_the_bridge_calls_the_setups_as_today(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Any, ...]] = []
    real_labels, real_entry = app_module._segment_labels_setup, app_module._campaign_entry_labels

    def labels(settings: Any, client: Any) -> Any:  # sin ``live``: la firma de hoy
        calls.append(("labels",))
        return real_labels(settings, client)

    def entry(settings: Any, client: Any, flow_labels: Any) -> Any:
        calls.append(("entry",))
        return real_entry(settings, client, flow_labels)

    monkeypatch.setattr(app_module, "_segment_labels_setup", labels)
    monkeypatch.setattr(app_module, "_campaign_entry_labels", entry)
    _ask_for_the_link(tmp_path, _Customer(identities=(FORM_ID,)), labels=[])
    assert calls == [("labels",), ("entry",)]

