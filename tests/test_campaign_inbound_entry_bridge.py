"""La entrada a una campana en el bridge (E-317, ``campaign-inbound-entry-v1``): Hermes, el turno y el arranque.

Contrato: ``docs/contracts/campaign-inbound-entry-v1.md``.

Datos: el webhook es la captura de la conversacion 200 del inbox 9 con ``content``
reemplazado (``_text_webhook`` de ``test_whatsapp_inbound_equivalence``): la
historia que devuelve Chatwoot es ese mismo mensaje, asi que la conversacion la
abrio la persona. La abierta por una plantilla nuestra suma antes una respuesta
del AgentBot con la forma de esa captura (``_agent_reply_before`` de
``test_segment_bridge``). El macro del segmento tiene la forma del GET real de un
macro de ATT1 (``chatwoot_macros_account_2_20261016.json``) con la accion
``add_label`` de la etiqueta del segmento. El segmento es el FICTICIO de
``tests/fixtures/segments`` con la vigencia reescrita para no depender del reloj.

**Deuda:** no hay captura de un primer mensaje real con el texto prellenado de un
boton de campana, ni de una salida de Hermes con ``campana_respuesta``: el texto y
la clave son ejemplos.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_hermes import _valid_proposal
from test_segment_bridge import (
    ALWAYS,
    PAST,
    SEGMENT,
    _agent_reply_before,
    _ReadySupabase,
    _SegmentShadow,
)
from test_webhook import StubChatwootClient, _post, _signed_headers
from test_whatsapp_inbound_equivalence import FORM_ID, SECRET, _manifest_settings, _text_webhook

from bridge import app as app_module
from bridge.app import Settings, create_app
from bridge.campaign_inbound_entry import CAMPAIGN_ANSWER_KEY, NOTICE_KIND, campaign_inbound_entry_setup
from bridge.hermes import HermesShadowProcessor
from bridge.instance_manifest import InstanceManifest
from bridge.segment_knowledge import SEGMENT_CONTEXT_KEY, load_segment_catalog
from test_segment_knowledge import sellar_chequeo
from bridge.supabase import SupabaseClient, SupabaseError

FIXTURES = Path(__file__).parent / "fixtures"
MACRO = json.loads((FIXTURES / "chatwoot_macros_account_2_20261016.json").read_text(encoding="utf-8"))
LABEL = "campana-de-prueba"
MACRO_ID = 41
QUESTION = "¿Nos escribes por la campaña de prueba?"


def _declaration(*, keywords: bool = True, question: bool = True) -> str:
    lines = [
        "[[agente.segmentos]]",
        f'clave = "{LABEL}"',
        f'etiqueta = "{LABEL}"',
        f'conocimiento = "conocimiento/segmentos/{SEGMENT.name}"',
        "prioridad = 10",
        f"macro_etiqueta = {MACRO_ID}",
    ]
    if keywords:
        lines.append('palabras_clave = ["Campaña de prueba"]')
    if question:
        lines.append(f'pregunta_entrada = "{QUESTION}"')
    return "\n" + "\n".join(lines) + "\n"


def _settings(
    tmp_path: Path,
    *,
    window: tuple[str, str] = ALWAYS,
    entry: bool = True,
    keywords: bool = True,
    question: bool = True,
) -> Settings:
    base = _manifest_settings(tmp_path)
    instance = tmp_path / "instancia"
    folder = instance / "conocimiento" / "segmentos"
    folder.mkdir()
    text = SEGMENT.read_text(encoding="utf-8")
    text = text.replace("desde = 2026-11-20T00:00:00-06:00", f"desde = {window[0]}")
    text = text.replace("hasta = 2026-12-01T00:00:00-06:00", f"hasta = {window[1]}")
    assert base.commercial_knowledge is not None
    (folder / SEGMENT.name).write_text(sellar_chequeo(text, base.commercial_knowledge), encoding="utf-8")
    manifest_path = instance / "instancia.toml"
    anchor = 'conocimiento = "conocimiento/knowledge-v1.toml"\n'
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            anchor, anchor + _declaration(keywords=keywords, question=question)
        ),
        encoding="utf-8",
    )
    assert base.instance_manifest is not None and base.commercial_knowledge is not None
    manifest = replace(
        base.instance_manifest,
        agent_segments=InstanceManifest.from_toml_file(manifest_path).agent_segments,
    )
    return replace(
        base,
        instance_manifest=manifest,
        agent_segments_enabled=True,
        agent_segment_catalog=load_segment_catalog(manifest, instance, base.commercial_knowledge),
        campaign_inbound_entry_enabled=entry,
    )


class _Chatwoot(StubChatwootClient):
    """El doble de siempre, con el macro del segmento (forma del GET real de ATT1)."""

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


class _Answers(_ReadySupabase):
    """La base falsa de la equivalencia, con las respuestas a la pregunta (D4)."""

    def __init__(self, stored: dict[str, str] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.stored = dict(stored or {})
        self.records: list[tuple[int, int, int, str, str]] = []

    async def get_segment_entry_answers(self, *, chatwoot_account_id: int, chatwoot_inbox_id: int,
                                        chatwoot_conversation_id: int) -> dict[str, str]:
        return dict(self.stored)

    async def record_segment_entry_answer(self, *, chatwoot_account_id: int, chatwoot_inbox_id: int,
                                          chatwoot_conversation_id: int, segment_key: str, answer: str) -> str:
        self.records.append((chatwoot_account_id, chatwoot_inbox_id, chatwoot_conversation_id, segment_key, answer))
        self.stored.setdefault(segment_key, answer)
        return "recorded"


class _EntryShadow(_SegmentShadow):
    """El doble que guarda los `system` extra, y que lee campana_respuesta."""

    accepts_campaign_answers = True

    def __init__(self, proposal: dict[str, object] | None = None, *, answer: str | None = None,
                 chatwoot: _Chatwoot | None = None) -> None:
        super().__init__(proposal)
        self.answer = answer
        self.chatwoot = chatwoot

    async def run(self, *, delivery_id: str, context: dict[str, object], **kwargs: Any) -> None:
        if self.chatwoot is not None:
            self.chatwoot.events.append("agent")
        await super().run(delivery_id=delivery_id, context=context, **kwargs)

    def get_campaign_answer(self, *, delivery_id: str) -> str | None:
        return self.answer


def _turn(
    settings: Settings,
    *,
    text: str,
    shadow: Any = None,
    supabase: _Answers | None = None,
    opened_by_us: bool = False,
    labels: list[str] | None = None,
) -> tuple[Any, _Chatwoot, _Answers]:
    webhook, history = _text_webhook(text)
    if opened_by_us:
        history = [_agent_reply_before(history[0]), *history]
    if labels is not None:
        webhook["conversation"]["labels"] = labels
    chatwoot = _Chatwoot(messages=history)
    if shadow is None:
        shadow = _EntryShadow({"decision": "reply", "reply": "Te leemos."})
    if isinstance(shadow, _EntryShadow):
        shadow.chatwoot = chatwoot
    supabase = supabase or _Answers(identities=(FORM_ID,))
    app = create_app(settings, chatwoot_client=chatwoot, shadow_processor=shadow,
                     supabase_client=supabase)  # type: ignore[arg-type]
    raw_body = json.dumps(webhook, separators=(",", ":")).encode("utf-8")
    response = _post(app, raw_body, _signed_headers(raw_body, secret=SECRET, delivery="inbound-200"))
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())
    return shadow, chatwoot, supabase


# ------------------------------------------------------------------ el turno


def test_the_keyword_puts_the_label_before_the_agent_and_the_first_reply_has_the_segment(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="bridge")

    shadow, chatwoot, supabase = _turn(_settings(tmp_path), text="Hola! Vengo por la CAMPAÑA DE PRUEBA")

    assert chatwoot.macro_runs == [(MACRO_ID, 200)]
    assert chatwoot.events.index("macro") < chatwoot.events.index("agent")
    [(_, context)] = shadow.calls
    assert context[SEGMENT_CONTEXT_KEY] == [{"key": LABEL, "version": 1}]
    [blocks] = shadow.extra
    assert [block.key for block in blocks] == [LABEL]
    assert blocks[0].agent_replies_before == 0  # llego a tiempo: el primer turno
    assert getattr(blocks[0], "kind", None) is None  # el bloque, no el aviso
    assert supabase.records == []
    assert f"segment_label_by_keyword conversation=200 segment={LABEL} label={LABEL} outcome=added" in [
        record.getMessage() for record in caplog.records
    ]


def test_an_inbound_first_message_without_the_keyword_gets_the_notice_last(tmp_path: Path) -> None:
    shadow, chatwoot, supabase = _turn(_settings(tmp_path), text="¿Qué precio tiene ahora?")

    assert chatwoot.macro_runs == []
    [(_, context)] = shadow.calls
    assert SEGMENT_CONTEXT_KEY not in context  # el bloque entero todavia no
    [blocks] = shadow.extra
    [notice] = blocks
    assert notice.kind == NOTICE_KIND and notice.key == LABEL
    assert QUESTION in notice.rendered and CAMPAIGN_ANSWER_KEY in notice.rendered
    assert supabase.records == []  # sin respuesta, nada


def test_a_si_from_the_agent_puts_the_label_and_is_recorded(tmp_path: Path) -> None:
    shadow = _EntryShadow({"decision": "reply", "reply": "¡Genial! Te cuento."}, answer="si")

    _, chatwoot, supabase = _turn(_settings(tmp_path), text="Hola", shadow=shadow)

    assert supabase.records == [(1, 9, 200, LABEL, "si")]
    assert chatwoot.macro_runs == [(MACRO_ID, 200)]
    assert chatwoot.events.index("agent") < chatwoot.events.index("macro")


def test_a_no_from_the_agent_is_recorded_without_a_label(tmp_path: Path) -> None:
    shadow = _EntryShadow({"decision": "reply", "reply": "Perfecto."}, answer="no")

    _, chatwoot, supabase = _turn(_settings(tmp_path), text="Hola", shadow=shadow)

    assert supabase.records == [(1, 9, 200, LABEL, "no")] and chatwoot.macro_runs == []


def test_a_no_in_the_database_means_no_notice(tmp_path: Path) -> None:
    shadow, _, _ = _turn(_settings(tmp_path), text="Hola", supabase=_Answers({LABEL: "no"}, identities=(FORM_ID,)))

    assert shadow.extra == [()]


def test_a_conversation_opened_by_our_template_gets_no_notice(tmp_path: Path) -> None:
    shadow, chatwoot, _ = _turn(_settings(tmp_path), text="Hola", opened_by_us=True)

    assert shadow.extra == [()] and chatwoot.macro_runs == []


def test_with_the_label_already_on_the_conversation_the_segment_goes_as_today(tmp_path: Path) -> None:
    shadow, chatwoot, _ = _turn(_settings(tmp_path), text="Hola", labels=[LABEL])

    [blocks] = shadow.extra
    assert [block.key for block in blocks] == [LABEL] and chatwoot.macro_runs == []


# ------------------------------------------------------------------ byte a byte


def _hermes_body(tmp_path: Path, settings: Settings, *, text: str = "¿Qué precio tiene ahora?",
                 campaign_answers: bool = False) -> dict[str, Any]:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(503, json={"detail": "unavailable"})

    assert settings.commercial_knowledge is not None
    real = HermesShadowProcessor(
        base_url="https://hermes.example.test/v1",
        api_key="test-hermes-key",
        model_name=settings.hermes_model_name,
        shadow_dir=tmp_path / "shadow",
        transport=httpx.MockTransport(handler),
        system_prompt=settings.commercial_knowledge.render(),
        campaign_answers=campaign_answers,
    )

    class _Through(_EntryShadow):
        async def run(self, *, delivery_id: str, context: dict[str, object], **kwargs: Any) -> None:
            await real.run(delivery_id=delivery_id, context=context, **kwargs)
            await super().run(delivery_id=delivery_id, context=context, **kwargs)

    _turn(settings, text=text, shadow=_Through({"decision": "reply", "reply": "Te leemos."}))
    [body] = requests
    return body


def test_without_the_flag_or_a_campaign_in_force_the_request_is_byte_for_byte_the_same(tmp_path: Path) -> None:
    today = _hermes_body(tmp_path / "hoy", _settings(tmp_path / "hoy", entry=False))
    expired = _hermes_body(tmp_path / "vencida", _settings(tmp_path / "vencida", window=PAST),
                           campaign_answers=True)
    by_us = _hermes_body(tmp_path / "sin-pregunta", _settings(tmp_path / "sin-pregunta", question=False),
                         campaign_answers=True)

    assert [message["role"] for message in today["messages"]] == ["system", "user"]
    assert json.dumps(expired, ensure_ascii=False) == json.dumps(today, ensure_ascii=False)
    assert json.dumps(by_us, ensure_ascii=False) == json.dumps(today, ensure_ascii=False)


def test_with_the_notice_the_request_is_today_plus_one_system_before_the_context(tmp_path: Path) -> None:
    today = _hermes_body(tmp_path / "hoy", _settings(tmp_path / "hoy", entry=False))
    asked = _hermes_body(tmp_path / "pregunta", _settings(tmp_path / "pregunta"), campaign_answers=True)

    assert [message["role"] for message in asked["messages"]] == ["system", "system", "user"]
    assert asked["messages"][0] == today["messages"][0]
    assert asked["messages"][2] == today["messages"][1]
    assert QUESTION in asked["messages"][1]["content"]


# ------------------------------------------------------------------ Hermes y campana_respuesta


def _context() -> dict[str, object]:
    return {"conversation_ref": "200", "messages": [{"actor": "prospect", "text": "Hola"}]}


def _hermes(tmp_path: Path, content: str, *, campaign_answers: bool) -> HermesShadowProcessor:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": content}}]})

    return HermesShadowProcessor(
        base_url="https://hermes.example.test/v1",
        api_key="test-hermes-key",
        model_name="agente-comercial",
        shadow_dir=tmp_path / "shadow",
        transport=httpx.MockTransport(handler),
        campaign_answers=campaign_answers,
    )


@pytest.mark.parametrize(("raw", "stored"), [("si", "si"), ("no", "no"), (None, None), ("tal vez", None)])
def test_hermes_takes_the_answer_out_of_the_proposal(tmp_path: Path, raw: object, stored: str | None) -> None:
    proposal = {**_valid_proposal(), CAMPAIGN_ANSWER_KEY: raw}
    processor = _hermes(tmp_path, json.dumps(proposal, ensure_ascii=False), campaign_answers=True)

    asyncio.run(processor.run(delivery_id="d-1", context=_context()))

    assert processor.get_completed_proposal(delivery_id="d-1") == _valid_proposal()
    assert processor.get_campaign_answer(delivery_id="d-1") == stored
    digest = hashlib.sha256(b"d-1").hexdigest()
    saved = json.loads((tmp_path / "shadow" / f"{digest}.json").read_text(encoding="utf-8"))
    assert CAMPAIGN_ANSWER_KEY not in saved["proposal"]
    assert ("campaign_answer" in saved) is (stored is not None)


def test_without_the_flag_campana_respuesta_invalidates_the_proposal_as_today(tmp_path: Path) -> None:
    proposal = {**_valid_proposal(), CAMPAIGN_ANSWER_KEY: "si"}
    processor = _hermes(tmp_path, json.dumps(proposal, ensure_ascii=False), campaign_answers=False)

    asyncio.run(processor.run(delivery_id="d-1", context=_context()))

    assert processor.get_completed_proposal(delivery_id="d-1") is None
    assert processor.get_failure_reason(delivery_id="d-1") == "invalid_agent_output"
    assert processor.get_campaign_answer(delivery_id="d-1") is None
    assert processor.accepts_campaign_answers is False


def test_the_notice_goes_to_the_provenance_with_its_kind(tmp_path: Path) -> None:
    from bridge.campaign_inbound_entry import EntryNotice
    from test_campaign_inbound_entry import _segment

    turns: list[Any] = []

    class _Recorder:
        async def record_agent_turn(self, *, turn: Any) -> None:
            turns.append(turn)

    processor = _hermes(tmp_path, json.dumps(_valid_proposal(), ensure_ascii=False), campaign_answers=True)
    processor.bind_provenance_recorder(_Recorder())
    notice = EntryNotice(segment=_segment(), agent_replies_before=0)

    asyncio.run(processor.run(delivery_id="d-1", context=_context(), extra_system=(notice,)))

    [turn] = turns
    assert turn.context_added["segments"] == [
        {"key": "black-november", "version": 1, "sha256": notice.sha256, "agent_replies_before": 0,
         "kind": NOTICE_KIND}
    ]


# ------------------------------------------------------------------ la base


def _supabase(handler: Any) -> SupabaseClient:
    return SupabaseClient(base_url="https://supabase.example.test", service_role_key="test-service-role",
                          transport=httpx.MockTransport(handler))


def test_the_client_reads_and_records_the_answers() -> None:
    bodies: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("get_segment_entry_answers_v1"):
            return httpx.Response(200, json=[{"segment_key": LABEL, "answer": "no"}])
        return httpx.Response(200, json=[{"outcome": "replayed", "answer": "no"}])

    client = _supabase(handler)
    read = asyncio.run(client.get_segment_entry_answers(
        chatwoot_account_id=2, chatwoot_inbox_id=11, chatwoot_conversation_id=701))
    outcome = asyncio.run(client.record_segment_entry_answer(
        chatwoot_account_id=2, chatwoot_inbox_id=11, chatwoot_conversation_id=701, segment_key=LABEL, answer="si"))

    assert read == {LABEL: "no"} and outcome == "replayed"
    assert bodies == [
        ("/rest/v1/rpc/get_segment_entry_answers_v1",
         {"p_chatwoot_account_id": 2, "p_chatwoot_inbox_id": 11, "p_chatwoot_conversation_id": 701}),
        ("/rest/v1/rpc/record_segment_entry_answer_v1",
         {"p_chatwoot_account_id": 2, "p_chatwoot_inbox_id": 11, "p_chatwoot_conversation_id": 701,
          "p_segment_key": LABEL, "p_answer": "si"}),
    ]


@pytest.mark.parametrize("rows", [[{"segment_key": LABEL, "answer": "tal vez"}], [{"segment_key": LABEL}], {}])
def test_the_client_fails_closed_on_another_shape(rows: object) -> None:
    client = _supabase(lambda request: httpx.Response(200, json=rows))
    with pytest.raises(SupabaseError):
        asyncio.run(client.get_segment_entry_answers(
            chatwoot_account_id=2, chatwoot_inbox_id=11, chatwoot_conversation_id=701))


# ------------------------------------------------------------------ el arranque


def test_the_flag_is_off_by_default_and_portable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings.__dataclass_fields__["campaign_inbound_entry_enabled"].default is False
    assert "campaign_inbound_entry_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
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
    monkeypatch.delenv("CAMPAIGN_INBOUND_ENTRY_ENABLED", raising=False)
    assert Settings.from_env().campaign_inbound_entry_enabled is False
    monkeypatch.setenv("CAMPAIGN_INBOUND_ENTRY_ENABLED", " TRUE ")
    assert Settings.from_env().campaign_inbound_entry_enabled is True


def test_the_real_processor_reads_the_answer_only_with_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[HermesShadowProcessor] = []
    monkeypatch.setattr(app_module, "create_app", lambda settings, shadow_processor: built.append(shadow_processor))
    base = replace(
        _manifest_settings(tmp_path),
        hermes_shadow_enabled=True,
        hermes_api_base_url="https://hermes.example.test/v1",
        hermes_api_key="test-hermes-key",
    )
    for flag in (False, True):
        settings = replace(base, campaign_inbound_entry_enabled=flag)
        monkeypatch.setattr(app_module.Settings, "from_env", classmethod(lambda cls, s=settings: s))
        app_module.build_app()
    assert [processor.accepts_campaign_answers for processor in built] == [False, True]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"agent_segments_enabled": False, "agent_segment_catalog": None}, "requires AGENT_SEGMENTS_ENABLED"),
        ({"instance_manifest": None}, "requires an instance manifest"),
    ],
)
def test_it_fails_closed_without_what_it_needs(tmp_path: Path, changes: dict[str, Any], message: str) -> None:
    settings = replace(_settings(tmp_path), **changes)
    with pytest.raises(ValueError, match=message):
        campaign_inbound_entry_setup(settings, _Chatwoot(), _Answers(), reset_command="/nuevo", reset_confirmation="ok")


def test_it_does_not_start_without_an_entry_or_a_control_client(tmp_path: Path) -> None:
    plain = _settings(tmp_path / "a", keywords=False, question=False)
    with pytest.raises(ValueError, match="requires palabras_clave or pregunta_entrada"):
        campaign_inbound_entry_setup(plain, _Chatwoot(), _Answers(), reset_command="/nuevo", reset_confirmation="ok")
    with pytest.raises(ValueError, match="Chatwoot control client"):
        campaign_inbound_entry_setup(_settings(tmp_path / "b"), None, _Answers(), reset_command="/nuevo",
                             reset_confirmation="ok")
    with pytest.raises(ValueError, match="requires the Supabase client"):
        campaign_inbound_entry_setup(_settings(tmp_path / "c"), _Chatwoot(), None, reset_command="/nuevo",
                             reset_confirmation="ok")
    # Solo la palabra clave no necesita la base.
    only_keyword = _settings(tmp_path / "d", question=False)
    entry = campaign_inbound_entry_setup(only_keyword, _Chatwoot(), None, reset_command="/nuevo", reset_confirmation="ok")
    assert entry is not None and not entry.has_question
    assert campaign_inbound_entry_setup(replace(only_keyword, campaign_inbound_entry_enabled=False), None, None,
                                reset_command="/nuevo", reset_confirmation="ok") is None


def test_a_reserved_macro_cannot_label_a_campaign(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), chatwoot_pause_macro_id=MACRO_ID)
    with pytest.raises(ValueError, match="cannot be a pause"):
        campaign_inbound_entry_setup(settings, _Chatwoot(), _Answers(), reset_command="/nuevo", reset_confirmation="ok")


def test_the_question_needs_a_processor_that_reads_the_answer(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reads campana_respuesta"):
        create_app(_settings(tmp_path), chatwoot_client=_Chatwoot(), shadow_processor=_SegmentShadow(),
                   supabase_client=_Answers(identities=(FORM_ID,)))  # type: ignore[arg-type]
