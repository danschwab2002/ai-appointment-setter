"""Las reglas de palabra clave en el turno del bridge (Z1 3.9, ``keyword-rules-v1``).

Contrato: ``docs/contracts/keyword-rules-v1.md``.

Datos: el webhook es la captura de la conversacion 200 del inbox 9 con ``content``
reemplazado (``_text_webhook`` de ``test_whatsapp_inbound_equivalence``), como en
``test_campaign_inbound_entry_bridge``: la historia que devuelve Chatwoot es ese mismo
mensaje, asi que la conversacion la abrio la persona y es su primer mensaje. La forma
real de un primer mensaje con el texto de un link (dos entrantes antes de la primera
respuesta) la prueba ``test_keyword_rules`` sobre la captura de la 455 de Maribel. El
macro de la regla tiene la forma del GET real de un macro de ATT1
(``chatwoot_macros_account_2_20261016.json``).
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
from test_agent_campaign_routes import AE_KEY
from test_campaign_inbound_entry_bridge import _Answers, _EntryShadow
from test_flow_approval_page import (
    BRAYAN,
    CLIENT_ID,
    CLIENT_SECRET,
    DAN,
    DB_KEY,
    ORIGIN,
    SESSION_SECRET,
    TEAM,
)
from test_segment_bridge import _agent_reply_before
from test_webhook import StubChatwootClient, _post, _signed_headers
from test_whatsapp_inbound_equivalence import FORM_ID, SECRET, WA_ID, _manifest_settings, _text_webhook

from bridge import keyword_rules
from bridge.app import Settings, create_app
from bridge.hermes import HermesShadowProcessor

FIXTURES = Path(__file__).parent / "fixtures"
MACRO = json.loads((FIXTURES / "chatwoot_macros_account_2_20261016.json").read_text(encoding="utf-8"))
LABEL = "soporte"
MACRO_ID = 77
RULE = {"rule_id": "7a2b3c4d-2222-4b9f-8d2e-000000000001", "rule_key": "soporte", "revision": 1,
        "rule": keyword_rules.parse_rule({"clave": "soporte", "nombre": "Link de soporte",
                                          "palabra_clave": "Hola, necesito soporte", "etiqueta": LABEL,
                                          "macro_etiqueta": MACRO_ID}),
        "fingerprint": "f" * 64, "approved_at": "2026-11-26T13:00:00Z"}
LINK_TEXT = "Hola, necesito soporte"


def _settings(tmp_path: Path, *, enabled: bool = True) -> Settings:
    return replace(
        _manifest_settings(tmp_path),
        keyword_rules_enabled=enabled,
        automation_expert_change_enabled=enabled,
        automation_expert_change_token="c" * 48,
        automation_expert_change_db_key=AE_KEY,
        automation_expert_read_enabled=enabled,
        automation_expert_read_token="r" * 48,
        supabase_base_url="https://rest.internal",
        flow_approval_enabled=enabled,
        flow_approval_base_url=ORIGIN,
        flow_approval_slack_team_id=TEAM,
        flow_approval_slack_approvers=f"{DAN},{BRAYAN}",
        flow_approval_slack_client_id=CLIENT_ID,
        flow_approval_slack_client_secret=CLIENT_SECRET,
        flow_approval_session_secret=SESSION_SECRET,
        flow_change_approver_db_key=DB_KEY,
    )


class _Chatwoot(StubChatwootClient):
    """El doble de siempre, con el macro de la regla (forma del GET real de ATT1)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.macro_runs: list[tuple[int, int]] = []

    async def get_macro_actions(self, *, macro_id: int) -> list[dict[str, Any]]:
        assert macro_id == MACRO_ID
        actions = copy.deepcopy(MACRO["get_macro_4"]["body"]["payload"]["actions"])
        actions[0]["action_params"] = [LABEL]
        return actions

    async def get_conversation_labels(self, *, conversation_id: int) -> list[str]:
        return []

    async def execute_macro(self, *, macro_id: int, conversation_id: int) -> None:
        self.events.append("macro")
        self.macro_runs.append((macro_id, conversation_id))


class _Base(_Answers):
    def __init__(self, rules: list[dict[str, Any]], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.rules = rules

    async def get_live_keyword_rules(self, instance: dict[str, str]) -> dict[str, Any]:
        return {"rules": copy.deepcopy(self.rules)}


def _turn(settings: Settings, *, text: str, shadow: Any = None, rules: list[dict[str, Any]] | None = None,
          opened_by_us: bool = False, opted_out: bool = False) -> tuple[Any, _Chatwoot]:
    webhook, history = _text_webhook(text)
    if opened_by_us:
        history = [_agent_reply_before(history[0]), *history]
    chatwoot = _Chatwoot(messages=history)
    if shadow is None:
        shadow = _EntryShadow({"decision": "reply", "reply": "Te leemos."})
    shadow.chatwoot = chatwoot
    supabase = _Base([RULE] if rules is None else rules, identities=(FORM_ID,),
                     **({"stopped_user_ids": (WA_ID,)} if opted_out else {}))
    app = create_app(settings, chatwoot_client=chatwoot, shadow_processor=shadow,
                     supabase_client=supabase)  # type: ignore[arg-type]
    if app.state.keyword_rules is not None:
        # La primera lectura corre al arrancar (lifespan); aca se hace a mano, como el turno.
        assert asyncio.run(app.state.keyword_rules.refresh()) is True
    raw_body = json.dumps(webhook, separators=(",", ":")).encode("utf-8")
    response = _post(app, raw_body, _signed_headers(raw_body, secret=SECRET, delivery="inbound-200"))
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())
    return shadow, chatwoot


def test_the_link_text_puts_the_label_before_the_agent_and_the_note_goes_last(tmp_path: Path) -> None:
    shadow, chatwoot = _turn(_settings(tmp_path), text=LINK_TEXT)

    assert chatwoot.macro_runs == [(MACRO_ID, 200)]
    assert chatwoot.events.index("macro") < chatwoot.events.index("agent")
    [blocks] = shadow.extra
    note = blocks[-1]
    assert note.kind == keyword_rules.NOTE_KIND and note.rendered == keyword_rules.NOTE_TEXT
    assert note.agent_replies_before == 0


def test_without_the_link_text_nothing_changes(tmp_path: Path) -> None:
    shadow, chatwoot = _turn(_settings(tmp_path), text="¿Qué precio tiene ahora?")

    assert chatwoot.macro_runs == [] and shadow.extra == [()]


def test_a_reply_to_our_template_with_the_word_is_not_the_link(tmp_path: Path) -> None:
    shadow, chatwoot = _turn(_settings(tmp_path), text=LINK_TEXT, opened_by_us=True)

    assert chatwoot.macro_runs == [] and shadow.extra == [()]


def test_an_opted_out_person_gets_no_label_and_no_note(tmp_path: Path) -> None:
    shadow, chatwoot = _turn(_settings(tmp_path), text=LINK_TEXT, opted_out=True)

    assert chatwoot.macro_runs == [] and shadow.extra == [()]


def test_without_the_flag_the_link_text_does_nothing(tmp_path: Path) -> None:
    shadow, chatwoot = _turn(_settings(tmp_path, enabled=False), text=LINK_TEXT)

    assert chatwoot.macro_runs == [] and shadow.extra == [()]


def _hermes_body(tmp_path: Path, settings: Settings, *, text: str) -> dict[str, Any]:
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

    class _Through(_EntryShadow):
        async def run(self, *, delivery_id: str, context: dict[str, object], **kwargs: Any) -> None:
            await real.run(delivery_id=delivery_id, context=context, **kwargs)
            await super().run(delivery_id=delivery_id, context=context, **kwargs)

    _turn(settings, text=text, shadow=_Through({"decision": "reply", "reply": "Te leemos."}))
    [body] = requests
    return body


def test_the_request_is_byte_for_byte_today_without_the_flag_or_a_match(tmp_path: Path) -> None:
    today = _hermes_body(tmp_path / "hoy", _settings(tmp_path / "hoy", enabled=False), text=LINK_TEXT)
    no_match = _hermes_body(tmp_path / "sin", _settings(tmp_path / "sin"), text="¿Qué precio tiene ahora?")
    today_other = _hermes_body(tmp_path / "hoy2", _settings(tmp_path / "hoy2", enabled=False),
                               text="¿Qué precio tiene ahora?")
    with_rule = _hermes_body(tmp_path / "con", _settings(tmp_path / "con"), text=LINK_TEXT)

    assert [message["role"] for message in today["messages"]] == ["system", "user"]
    assert json.dumps(no_match, ensure_ascii=False) == json.dumps(today_other, ensure_ascii=False)
    # Con la regla: el de hoy mas un system (la nota) antes del contexto.
    assert [message["role"] for message in with_rule["messages"]] == ["system", "system", "user"]
    assert with_rule["messages"][0] == today["messages"][0] and with_rule["messages"][2] == today["messages"][1]
    assert with_rule["messages"][1]["content"] == keyword_rules.NOTE_TEXT
