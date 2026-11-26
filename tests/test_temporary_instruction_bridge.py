"""La instruccion temporal en el turno real, desde el webhook (agent-temporary-instruction-v1, 5.3 de Z1).

Mismo camino y mismos datos que ``test_segment_bridge`` (la captura de la
conversacion 200 del inbox 9, el segmento FICTICIO vigente 2000-2999). El pedido
HTTP lo arma el procesador real de Hermes contra un transporte que lo guarda:

* con el flag apagado, y con el flag prendido sin instruccion, el pedido es byte
  a byte el mismo (D3);
* con una instruccion vigente: base, instruccion, segmento, contexto (D11);
* vencida, no va.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_agent_campaign_routes import AE_KEY
from test_segment_bridge import LABEL, _SegmentShadow, _settings, _turn
from test_flow_approval_page import BRAYAN, CLIENT_ID, CLIENT_SECRET, DAN, DB_KEY, ORIGIN, SESSION_SECRET, TEAM

from bridge import temporary_instruction
from bridge.app import Settings
from bridge.hermes import HermesShadowProcessor

ID = "6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b"
TEXT = "Hoy no ofrezcas cupón."


def _on(settings: Settings) -> Settings:
    return replace(
        settings,
        agent_temporary_instruction_enabled=True,
        automation_expert_change_enabled=True, automation_expert_change_token="c" * 48,
        automation_expert_change_db_key=AE_KEY,
        automation_expert_read_enabled=True, automation_expert_read_token="r" * 48,
        supabase_base_url="https://att1-rest.internal",
        chatwoot_control_api_access_token="control",
        flow_approval_enabled=True, flow_approval_base_url=ORIGIN, flow_approval_slack_team_id=TEAM,
        flow_approval_slack_approvers=f"{DAN},{BRAYAN}", flow_approval_slack_client_id=CLIENT_ID,
        flow_approval_slack_client_secret=CLIENT_SECRET, flow_approval_session_secret=SESSION_SECRET,
        flow_change_approver_db_key=DB_KEY,
    )


ROWS: dict[str, dict[str, Any] | None] = {}


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> list[temporary_instruction.LiveTemporaryInstruction]:
    """El turno corre sin el arranque de la app: la fila se carga como la cargaria la primera lectura."""
    made: list[temporary_instruction.LiveTemporaryInstruction] = []
    ROWS.clear()

    class Recorded(temporary_instruction.LiveTemporaryInstruction):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            made.append(self)
            row = ROWS.get("row")

            async def fetch() -> dict[str, Any]:
                return {"instruction": row}

            self._fetch = fetch
            asyncio.run(self.refresh())

    monkeypatch.setattr(temporary_instruction, "LiveTemporaryInstruction", Recorded)
    return made


def _row(*, until: datetime) -> dict[str, Any]:
    return {"instruction_id": ID, "version": 3, "texto": TEXT, "valid_until": until.isoformat(),
            "fingerprint": "f" * 64, "approved_at": "2026-11-26T13:00:00+00:00"}


def _requests(tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch,
              row: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], _SegmentShadow]:
    """El pedido a Hermes de un turno con la etiqueta del segmento."""
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(503, json={"detail": "unavailable"})

    assert settings.commercial_knowledge is not None
    real = HermesShadowProcessor(
        base_url="https://hermes.example.test/v1", api_key="test-hermes-key", model_name=settings.hermes_model_name,
        shadow_dir=tmp_path / "shadow", transport=httpx.MockTransport(handler),
        system_prompt=settings.commercial_knowledge.render(),
    )

    class _Through(_SegmentShadow):
        async def run(self, *, delivery_id: str, context: dict[str, object], **kwargs: Any) -> None:
            await real.run(delivery_id=delivery_id, context=context, **kwargs)
            await super().run(delivery_id=delivery_id, context=context, **kwargs)

    ROWS["row"] = row
    shadow, _ = _turn(tmp_path, settings=settings, labels=[LABEL],
                      shadow=_Through({"decision": "reply", "reply": "Te leemos."}))
    return requests, shadow


def test_with_the_flag_off_or_without_an_instruction_the_request_is_byte_for_byte_the_same(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: list[Any]
) -> None:
    off, _ = _requests(tmp_path / "off", _settings(tmp_path / "off"), monkeypatch)
    assert live == []  # apagado ni se crea
    on, _ = _requests(tmp_path / "on", _on(_settings(tmp_path / "on")), monkeypatch, row=None)
    assert len(live) == 1
    assert json.dumps(off, sort_keys=True).encode() == json.dumps(on, sort_keys=True).encode()
    assert [message["role"] for message in off[0]["messages"]] == ["system", "system", "user"]


def test_a_live_instruction_goes_after_the_base_and_before_the_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: list[Any]
) -> None:
    settings = _on(_settings(tmp_path))
    until = datetime.now(UTC) + timedelta(days=2)
    [body], shadow = _requests(tmp_path, settings, monkeypatch, row=_row(until=until))

    assert settings.commercial_knowledge is not None and settings.agent_segment_catalog is not None
    messages = body["messages"]
    assert [message["role"] for message in messages] == ["system", "system", "system", "user"]
    assert messages[0]["content"] == settings.commercial_knowledge.render()
    assert messages[1]["content"] == temporary_instruction.render(
        TEXT, until.replace(microsecond=0), time_zone=settings.instance_manifest.time_zone)  # type: ignore[union-attr]
    assert messages[2]["content"] == settings.agent_segment_catalog.segments[0].rendered
    [blocks] = shadow.extra
    assert [block.key for block in blocks] == [ID, LABEL]
    # El contexto que recibe el agente no cambia: la instruccion va como system, no adentro.
    assert TEXT not in messages[3]["content"]


def test_an_instruction_whose_until_passed_does_not_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: list[Any]
) -> None:
    settings = _on(_settings(tmp_path))
    [body], shadow = _requests(tmp_path, settings, monkeypatch,
                               row=_row(until=datetime.now(UTC) - timedelta(minutes=1)))
    assert [message["role"] for message in body["messages"]] == ["system", "system", "user"]
    [blocks] = shadow.extra
    assert [block.key for block in blocks] == [LABEL]
