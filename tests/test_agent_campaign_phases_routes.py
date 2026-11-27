"""Las fases del lanzamiento en las rutas del AE, la pagina y /ready (agent-campaign-phases-v1, 4.8).

Los dobles de ``test_agent_campaign_routes`` (la app con el cliente real de la base,
PostgREST y Slack emulados). Lo que la base hace de verdad lo prueba PGlite
(``validate_agent_campaign_phases.mjs``). La campana es ficticia.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_agent_campaign_phases import _phased
from test_agent_campaign_routes import (
    ASKER,
    ENABLED,
    KNOWLEDGE,
    Base,
    Db,
    _ae,
    _app,
    _ready,
    _settings,
)

from bridge import agent_campaigns
from bridge.app import create_app

REST_ID = "99999999-2222-4333-8444-555555555555"


def _rest_row() -> dict[str, Any]:
    draft = agent_campaigns.parse_campaign(_phased(desde="2026-01-01T00:00:00Z", hasta="2099-12-01T00:00:00Z",
                                                   fases=[{"clave": "carrito", "nombre": "Carrito",
                                                           "desde": "2026-01-01T00:00:00Z",
                                                           "hasta": "2099-12-01T00:00:00Z"}]),
                                           phases=True)
    context = agent_campaigns.CampaignContext(manifest=_settings().instance_manifest, base=KNOWLEDGE,
                                              manifest_catalog=ENABLED["agent_segment_catalog"], phases_enabled=True)
    return {"campaign_id": REST_ID, "status": "pausada", "campaign_key": draft["clave"], "segment_version": 1,
            "label": draft["etiqueta"], "label_macro_id": draft["macro_etiqueta"], "draft": draft,
            "fingerprint": "f" * 64, "check_sha": agent_campaigns.expected_check_sha(draft, version=1, context=context),
            "approved_at": "2026-11-19T16:21:15Z"}


@pytest.fixture(autouse=True)
def _image_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETTER_VERSION", "v1.42.0")
    monkeypatch.setenv("GIT_SHA", "abcdef0")


class _Rests:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.calls: list[dict[str, str]] = []


@pytest.fixture
def rests(monkeypatch: pytest.MonkeyPatch) -> _Rests:
    state = _Rests()

    async def get_agent_campaign_rests(self: Any, instance: dict[str, str]) -> dict[str, Any]:
        state.calls.append(instance)
        return {"campaigns": state.rows}

    monkeypatch.setattr(Base, "get_agent_campaign_rests", get_agent_campaign_rests, raising=False)
    return state


def test_without_the_flag_a_campaign_with_phases_cannot_be_saved(rests: _Rests) -> None:
    client, db, *_ = _app()
    response = _ae(client, "POST", "", {"campaign": _phased(), "purpose": "Black por fases", "requested_by": ASKER})
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "campaign_invalid"
    assert db.campaign_calls == []
    preview = _ae(client, "POST", "/preview", {"campaign": _phased()})
    assert preview.status_code == 422


def test_without_the_flag_ready_says_disabled_and_the_rests_are_never_read(rests: _Rests) -> None:
    client, *_ = _app()
    ready = _ready(client)
    assert ready["agent_campaign_phases"] == "disabled"
    assert ready["agent_campaigns"] == "served=0,not_served=0,refresh=ok"
    assert rests.calls == []


def test_the_phases_flag_needs_the_campaigns() -> None:
    with pytest.raises(ValueError, match="AGENT_CAMPAIGN_PHASES_ENABLED requires AGENT_CAMPAIGNS_ENABLED"):
        create_app(_settings(agent_campaign_phases_enabled=True))


def test_with_the_flag_the_preview_shows_every_piece(rests: _Rests) -> None:
    client, db, *_ = _app(agent_campaign_phases_enabled=True)
    response = _ae(client, "POST", "/preview", {"campaign": _phased(desde="2026-11-14T15:22:00Z",
                                                                    hasta="2099-12-01T06:00:00Z")})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["ok"] is True, data["blocks"]
    assert [piece["clave"] for piece in data["preview"]["piezas"]] == ["preventa", "carrito", "ultimo-dia", "minimo"]
    assert db.campaign_calls == []


def test_with_the_flag_it_is_saved_validated_and_the_page_shows_the_pieces(rests: _Rests) -> None:
    client, db, *_ = _app(agent_campaign_phases_enabled=True)
    campaign = _phased(desde="2026-11-14T15:22:00Z", hasta="2099-12-01T06:00:00Z")
    saved = _ae(client, "POST", "", {"campaign": campaign, "purpose": "Black por fases", "requested_by": ASKER})
    assert saved.status_code == 201, saved.text
    assert db.row is not None and [phase["clave"] for phase in db.row["draft"]["fases"]] == [
        "preventa", "carrito", "ultimo-dia"]
    validated = _ae(client, "POST", "/11111111-2222-4333-8444-555555555555/validate",
                    {"revision": db.row["revision"], "requested_by": ASKER})
    assert validated.status_code == 200, validated.text
    assert validated.json()["data"]["status"] == "validada"
    assert len(db.row["validation"]["preview"]["piezas"]) == 4


def test_with_the_flag_a_paused_campaign_with_its_minimal_block_is_served(rests: _Rests) -> None:
    rests.rows.append(_rest_row())
    client, *_ = _app(Db(), agent_campaign_phases_enabled=True)
    ready = _ready(client)
    assert ready["agent_campaign_phases"] == "pieced=0,resting=1,rest_not_served=0,rests_refresh=ok"
    assert "black-november-2026--minimo:v1:" in ready["agent_segments"]
    assert rests.calls == [{"tenant_ref": "lancemos", "ally_ref": "att1"}]
