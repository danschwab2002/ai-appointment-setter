"""La guarda de precio en el turno del agente (E-564, price-guard-v1 §4 y §5).

El procesador corre contra un api_server falso que contesta con propuestas de la
forma capturada en ``hermes_glm_prose_then_json_att1_20261029.json`` (las claves,
los ``captured_fields`` nulos y los ocho ``missing_fields``). Los ``reply`` son
respuestas reales del agente de ATT1 (F2, ``price_guard/agent_replies_att1_20261113.json``,
casos att1-005 y att1-004); donde el test cambia el precio para provocar el
bloqueo, lo dice. El conocimiento es el v14 de ATT1 (``instances/att1-b7580bc-20261103``).

Las dos derivaciones de punta a punta (el webhook de Chatwoot, la derivacion y la
nota) usan el mismo andamiaje que ``test_app_agent_turn_failed.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

import httpx
import pytest

from bridge import app as app_module
from bridge.agent_provenance import AgentTurn
from bridge.app import Settings, create_app
from bridge.commercial_knowledge import CommercialKnowledge
from bridge.hermes import HermesShadowProcessor, retry_correction
from bridge.instance_manifest import InstanceManifest
from bridge.price_guard import (
    PRICE_GUARD_LINK_NOTE_MARKER_PREFIX,
    PriceGuard,
    guard_from_manifest,
    link_note_body,
    link_note_marker,
)
from bridge.chatwoot import ChatwootClient
from test_price_guard import _black_campaign_block
from test_webhook import (
    StubChatwootClient,
    StubInboundCommercialSupabase,
    _post,
    _signed_headers,
)

FIXTURES = Path(__file__).parent / "fixtures"
ATT1 = FIXTURES / "instances" / "att1-b7580bc-20261103"
KNOWLEDGE = CommercialKnowledge.from_toml_file(ATT1 / "conocimiento" / "knowledge-v14.toml")
MANIFEST = InstanceManifest.from_toml_file(ATT1 / "instancia.toml")


def _f2(case: str) -> str:
    data = json.loads(
        (FIXTURES / "price_guard" / "agent_replies_att1_20261113.json").read_text(encoding="utf-8")
    )
    [found] = [item for item in data["casos"] if item["caso"] == case]
    return str(found["respuesta"])


# att1-005: «Alimenta Tu Tiroides cuesta USD 47. Si quieres, te envío el enlace...»
GOOD_PRICE_REPLY = _f2("att1-005")
# El mismo, con otro precio (la mutacion es del test).
BAD_PRICE_REPLY = GOOD_PRICE_REPLY.replace("USD 47", "USD 97")
# att1-004 sin el link que agrega el bridge (con send_payment_link el reply no
# lleva la URL), con otro precio (la mutacion es del test).
LINK_REPLY = _f2("att1-004").split("\n", 1)[0].replace("USD 47", "USD 37")
assert "USD 47" in GOOD_PRICE_REPLY and "USD 37" in LINK_REPLY and "http" not in LINK_REPLY


def _proposal(reply: str, decision: str = "ask_question", reason: str = "commercial_response") -> str:
    responses = json.loads(
        (FIXTURES / "hermes_glm_prose_then_json_att1_20261029.json").read_text(encoding="utf-8")
    )["responses"]
    content = responses[0]["content"]
    proposal = json.loads(content[content.index("{") :], strict=False)
    proposal.update(
        {
            "decision": decision,
            "qualification_status": "needs_human" if decision == "handoff" else "in_progress",
            "reason_code": reason,
            "reply": reply,
        }
    )
    return json.dumps(proposal, ensure_ascii=False)


class _Hermes:
    """El api_server falso: cada respuesta es el contenido del modelo, o None = 503."""

    def __init__(self, answers: list[str | None]) -> None:
        self.answers = list(answers)
        self.requests: list[dict[str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        if self.answers[0] is None:
            self.answers.pop(0)
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(
            200,
            json={
                "model": "z-ai/glm-5.3-flash",
                "choices": [{"message": {"content": self.answers.pop(0)}}],
            },
        )


class _Recorder:
    def __init__(self) -> None:
        self.turns: list[AgentTurn] = []

    async def record_agent_turn(self, *, turn: AgentTurn) -> None:
        self.turns.append(turn)


def _processor(
    tmp_path: Path, hermes: _Hermes, *, guard: bool, recorder: _Recorder | None = None
) -> HermesShadowProcessor:
    return HermesShadowProcessor(
        base_url="https://hermes.example.test/v1",
        api_key="test-hermes-key",
        model_name="agente-comercial",
        shadow_dir=tmp_path / "shadow",
        transport=httpx.MockTransport(hermes),
        system_prompt=KNOWLEDGE.render(),
        provenance_recorder=recorder,
        **({"price_guard": guard_from_manifest(MANIFEST)} if guard else {}),
    )


CONTEXT: dict[str, object] = {
    "conversation_ref": "71",
    # Un mensaje de prueba (no de una persona) con un monto: no se permite.
    "messages": [{"actor": "prospect", "text": "¿Me lo dejás en USD 97?"}],
    "known_fields": {},
}


def _run(processor: HermesShadowProcessor, delivery: str = "turno-71") -> None:
    asyncio.run(processor.run(delivery_id=delivery, context=dict(CONTEXT)))


def _result(tmp_path: Path, delivery: str = "turno-71") -> dict[str, object]:
    digest = hashlib.sha256(delivery.encode("utf-8")).hexdigest()
    return json.loads((tmp_path / "shadow" / f"{digest}.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------- con el flag en off


def test_with_the_flag_off_the_turn_is_byte_for_byte_the_same(tmp_path: Path) -> None:
    """Sin la guarda, el precio inventado sale como hoy, en un solo pedido."""
    runs = {}
    for name, kwargs in (("sin_kwarg", {}), ("off", app_module.price_guard_processor_kwargs(
        Settings(  # type: ignore[call-arg]
            webhook_secret="s", allowed_jid=None, capture_dir=tmp_path, max_age_seconds=300,
            instance_manifest=MANIFEST,
        )
    ))):
        assert kwargs == {}
        hermes = _Hermes([_proposal(BAD_PRICE_REPLY)])
        recorder = _Recorder()
        processor = HermesShadowProcessor(
            base_url="https://hermes.example.test/v1",
            api_key="test-hermes-key",
            model_name="agente-comercial",
            shadow_dir=tmp_path / name,
            transport=httpx.MockTransport(hermes),
            system_prompt=KNOWLEDGE.render(),
            provenance_recorder=recorder,
            **kwargs,
        )
        asyncio.run(processor.run(delivery_id="turno-71", context=dict(CONTEXT)))
        digest = hashlib.sha256(b"turno-71").hexdigest()
        runs[name] = (
            hermes.requests,
            (tmp_path / name / f"{digest}.json").read_bytes(),
            [turn.context_added for turn in recorder.turns],
        )
    assert runs["sin_kwarg"] == runs["off"]
    requests, stored, added = runs["off"]
    assert len(requests) == 1
    assert json.loads(stored)["proposal"]["reply"] == BAD_PRICE_REPLY
    assert "price_guard" not in added[0]


def test_the_default_mode_is_off_and_another_value_does_not_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings.__dataclass_fields__["agent_price_guard_mode"].default == "off"
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
    monkeypatch.delenv("AGENT_PRICE_GUARD_MODE", raising=False)
    assert Settings.from_env().agent_price_guard_mode == "off"
    monkeypatch.setenv("AGENT_PRICE_GUARD_MODE", "bloquea")
    assert Settings.from_env().agent_price_guard_mode == "bloquea"
    monkeypatch.setenv("AGENT_PRICE_GUARD_MODE", "registra")
    with pytest.raises(ValueError, match="price_guard_configuration_invalid"):
        Settings.from_env()


# ------------------------------------------------------------ con el flag en bloquea


def test_a_good_price_passes_at_the_first_attempt(tmp_path: Path) -> None:
    hermes = _Hermes([_proposal(GOOD_PRICE_REPLY)])
    recorder = _Recorder()

    _run(_processor(tmp_path, hermes, guard=True, recorder=recorder))

    assert len(hermes.requests) == 1
    assert _result(tmp_path)["proposal"]["reply"] == GOOD_PRICE_REPLY  # type: ignore[index]
    assert recorder.turns[0].context_added["price_guard"] == {
        "mode": "bloquea",
        "attempts": [{"attempt": 1, "blocked": []}],
        "outcome": "passed",
    }


def test_a_blocked_price_is_retried_with_the_correction(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    hermes = _Hermes([_proposal(BAD_PRICE_REPLY), _proposal(GOOD_PRICE_REPLY)])
    recorder = _Recorder()

    with caplog.at_level(logging.INFO):
        _run(_processor(tmp_path, hermes, guard=True, recorder=recorder))

    assert len(hermes.requests) == 2
    first, second = (request["messages"] for request in hermes.requests)
    assert second[:-1] == first  # el mismo pedido, mas la correccion
    assert second[-1] == {
        "role": "user",
        "content": (
            "Tu respuesta anterior no se pudo usar: el monto USD 97 no esta en lo que "
            "te pasamos; usa solo estos: USD 47. Devolve solo el objeto JSON."
        ),
    }
    result = _result(tmp_path)
    assert result["status"] == "completed" and result["attempts"] == 2
    assert result["proposal"]["reply"] == GOOD_PRICE_REPLY  # type: ignore[index]
    assert recorder.turns[0].context_added["price_guard"] == {
        "mode": "bloquea",
        "attempts": [
            {"attempt": 1, "blocked": [{"monto": "97", "moneda": "USD"}]},
            {"attempt": 2, "blocked": []},
        ],
        "outcome": "retried_ok",
    }
    # El log cuenta los montos: no los nombra ni trae texto.
    assert "agent_price_guard_blocked attempt=1 conversation=71 amounts=1" in caplog.text
    assert "USD 97" not in caplog.text


def test_two_blocked_attempts_fail_with_price_not_in_catalog_and_keep_the_link_request(
    tmp_path: Path,
) -> None:
    hermes = _Hermes(
        [
            _proposal(LINK_REPLY, "send_payment_link", "payment_link_requested"),
            _proposal(LINK_REPLY, "send_payment_link", "payment_link_requested"),
        ]
    )
    recorder = _Recorder()
    processor = _processor(tmp_path, hermes, guard=True, recorder=recorder)

    _run(processor)

    assert len(hermes.requests) == 2  # nunca un tercer llamado
    result = _result(tmp_path)
    assert result["status"] == "failed"
    assert result["reason"] == "price_not_in_catalog"
    assert result["payment_link_requested"] is True
    assert processor.get_completed_proposal(delivery_id="turno-71") is None
    assert processor.get_failure_reason(delivery_id="turno-71") == "price_not_in_catalog"
    # Sin [[productos]] en el procesador, el link no tiene clave de producto.
    assert processor.get_price_guard_link_request(delivery_id="turno-71") == {"product": None}
    assert recorder.turns[0].context_added["price_guard"]["outcome"] == "handoff"  # type: ignore[index]
    # Ni el reply bloqueado ni el texto de la persona quedan en la procedencia.
    assert "USD 37" not in json.dumps(recorder.turns[0].context_added, ensure_ascii=False)


def test_a_blocked_reply_without_the_link_has_no_link_request(tmp_path: Path) -> None:
    hermes = _Hermes([_proposal(BAD_PRICE_REPLY), _proposal(BAD_PRICE_REPLY)])
    processor = _processor(tmp_path, hermes, guard=True)

    _run(processor)

    assert processor.get_failure_reason(delivery_id="turno-71") == "price_not_in_catalog"
    assert processor.get_price_guard_link_request(delivery_id="turno-71") is None


def test_a_handoff_reply_is_not_guarded(tmp_path: Path) -> None:
    """D1: al derivar no sale el reply; el motivo del agente no se pisa."""
    hermes = _Hermes([_proposal(BAD_PRICE_REPLY, "handoff", "commercial_exception")])
    recorder = _Recorder()

    _run(_processor(tmp_path, hermes, guard=True, recorder=recorder))

    assert len(hermes.requests) == 1
    assert _result(tmp_path)["proposal"]["reason_code"] == "commercial_exception"  # type: ignore[index]
    assert recorder.turns[0].context_added["price_guard"] == {
        "mode": "bloquea",
        "attempts": [{"attempt": 1, "blocked": [], "skipped": True}],
        "outcome": "passed",
    }


def test_two_rules_in_one_attempt_go_together_in_the_correction(tmp_path: Path) -> None:
    bad = BAD_PRICE_REPLY + " ¿Te lo mando? ¿O prefieres más información?"
    hermes = _Hermes([_proposal(bad), _proposal(GOOD_PRICE_REPLY)])

    _run(_processor(tmp_path, hermes, guard=True))

    correction = hermes.requests[1]["messages"][-1]["content"]  # type: ignore[index]
    assert correction == (
        "Tu respuesta anterior no se pudo usar: `reply` tiene mas de una pregunta. "
        "Deja una sola. Ademas, el monto USD 97 no esta en lo que te pasamos; usa solo "
        "estos: USD 47. Devolve solo el objeto JSON."
    )


def test_a_format_failure_after_a_block_derives_as_a_failed_turn(tmp_path: Path) -> None:
    """Manda la regla del ultimo intento (§4.4): el formato, no la guarda."""
    hermes = _Hermes([_proposal(BAD_PRICE_REPLY), "no es un objeto"])
    processor = _processor(tmp_path, hermes, guard=True)

    _run(processor)

    assert processor.get_failure_reason(delivery_id="turno-71") == "invalid_agent_output"


def test_a_block_after_a_format_failure_derives_with_price_not_in_catalog(tmp_path: Path) -> None:
    """Al reves del anterior: el ultimo intento lo bloqueo la guarda (§4.4)."""
    hermes = _Hermes(["no es un objeto", _proposal(BAD_PRICE_REPLY)])
    recorder = _Recorder()
    processor = _processor(tmp_path, hermes, guard=True, recorder=recorder)

    _run(processor)

    assert processor.get_failure_reason(delivery_id="turno-71") == "price_not_in_catalog"
    assert recorder.turns[0].context_added["price_guard"] == {
        "mode": "bloquea",
        "attempts": [
            {"attempt": 1, "blocked": [], "skipped": True},
            {"attempt": 2, "blocked": [{"monto": "97", "moneda": "USD"}]},
        ],
        "outcome": "handoff",
    }


def test_hermes_down_after_a_block_keeps_its_reason(tmp_path: Path) -> None:
    hermes = _Hermes([_proposal(BAD_PRICE_REPLY), None])
    recorder = _Recorder()
    processor = _processor(tmp_path, hermes, guard=True, recorder=recorder)

    _run(processor)

    assert processor.get_failure_reason(delivery_id="turno-71") == "hermes_unavailable"
    assert recorder.turns[0].context_added["price_guard"]["outcome"] == "handoff"  # type: ignore[index]


def test_the_campaign_price_is_allowed_inside_the_turn(tmp_path: Path) -> None:
    """La oferta Black de ATT1 (fixture de la base) como `system` del turno."""
    block = _black_campaign_block()
    reply = "El Black de Alimenta Tu Tiroides cuesta USD 47."
    hermes = _Hermes([_proposal(reply)])
    processor = HermesShadowProcessor(
        base_url="https://hermes.example.test/v1",
        api_key="test-hermes-key",
        model_name="agente-comercial",
        shadow_dir=tmp_path / "shadow",
        transport=httpx.MockTransport(hermes),
        # Sin el conocimiento base: el USD 47 sale solo de la campana.
        price_guard=PriceGuard(),
    )

    asyncio.run(
        processor.run(delivery_id="turno-71", context=dict(CONTEXT), extra_system=(block,))
    )

    assert len(hermes.requests) == 1
    assert _result(tmp_path)["proposal"]["reply"] == reply  # type: ignore[index]


def test_the_correction_without_the_guard_is_the_one_of_always() -> None:
    assert retry_correction("too_many_questions") == (
        "Tu respuesta anterior no se pudo usar: `reply` tiene mas de una pregunta. "
        "Deja una sola. Devolve solo el objeto JSON."
    )


# --------------------------------------------------------- la derivacion y la nota


SECRET = "webhook-secret"
JID = "12025550123@s.whatsapp.net"
CONVERSATION_ID = 909
MESSAGE_ID = 9901
CONTENT = "Hola, quiero pagar el programa"  # mensaje de prueba, no de una persona
HANDOFF_REQUEST_ID = "6f1c2b3a-4d5e-4f60-8a7b-9c0d1e2f3a4b"


class _Chatwoot(StubChatwootClient):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.notes: list[dict[str, object]] = []

    async def ensure_marked_private_note(self, **kwargs: object) -> bool:
        self.notes.append(kwargs)
        return True


class _Supabase(StubInboundCommercialSupabase):
    async def request_inbound_human_handoff(self, **kwargs: object) -> object:
        self.handoff_calls.append(kwargs)
        return type(
            "HandoffResult",
            (),
            {"outcome": "requested", "handoff_request_id": HANDOFF_REQUEST_ID},
        )()


def _derive(tmp_path: Path, processor: HermesShadowProcessor) -> tuple[_Chatwoot, _Supabase]:
    chatwoot = _Chatwoot(
        messages=[
            {
                "id": MESSAGE_ID,
                "created_at": 1793158783,
                "message_type": 0,
                "private": False,
                "content": CONTENT,
                "sender": {"type": "contact", "id": 20},
            }
        ]
    )
    supabase = _Supabase()
    settings = Settings(  # type: ignore[call-arg]
        webhook_secret=SECRET,
        allowed_jid=JID,
        capture_dir=tmp_path / "captures",
        max_age_seconds=300,
        agent_bot_id=1,
        chatwoot_account_id=1,
        chatwoot_inbox_id=9,
        chatwoot_cut_b_admission_enabled=True,
        chatwoot_cut_b_scope_key="libre-de-ansiedad-inbound",
        chatwoot_cut_b_scope_version=2,
        chatwoot_cut_b_agent_enabled=True,
        automated_replies_enabled=True,
        human_handoff_admission_enabled=True,
        human_handoff_projection_enabled=True,
        handoff_projection_policy_key="lancemos-inbound-handoff",
        handoff_projection_policy_version=1,
        human_handoff_projection_worker_id="handoff-projection-test",
    )
    app = create_app(
        settings,
        chatwoot_client=chatwoot,
        shadow_processor=processor,
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    webhook = {
        "event": "message_created",
        "id": MESSAGE_ID,
        "content": CONTENT,
        "message_type": "incoming",
        "private": False,
        "account": {"id": 1},
        "inbox": {"id": 9},
        "conversation": {
            "id": CONVERSATION_ID,
            "inbox_id": 9,
            "contact_inbox": {"source_id": JID},
        },
    }
    raw_body = json.dumps(webhook, separators=(",", ":")).encode("utf-8")
    response = _post(app, raw_body, _signed_headers(raw_body, secret=SECRET, delivery="turno-9"))
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())
    return chatwoot, supabase


def test_a_blocked_link_derives_with_its_reason_and_a_note_for_the_team(tmp_path: Path) -> None:
    hermes = _Hermes(
        [
            _proposal(LINK_REPLY, "send_payment_link", "payment_link_requested"),
            _proposal(LINK_REPLY, "send_payment_link", "payment_link_requested"),
        ]
    )

    chatwoot, supabase = _derive(tmp_path, _processor(tmp_path, hermes, guard=True))

    assert chatwoot.reply_calls == []  # ni la respuesta ni el link salen
    assert [call["detail_reason_code"] for call in supabase.handoff_calls] == [
        "price_not_in_catalog"
    ]
    assert (CONVERSATION_ID, "automation_paused") in chatwoot.calls
    [note] = chatwoot.notes
    assert note["conversation_id"] == CONVERSATION_ID
    assert note["idempotency_marker"] == (
        f"{PRICE_GUARD_LINK_NOTE_MARKER_PREFIX}{HANDOFF_REQUEST_ID}]"
    )
    assert str(note["note_body"]).startswith("El agente pidió el link de pago y no salió")


def test_a_blocked_reply_without_the_link_derives_without_the_note(tmp_path: Path) -> None:
    hermes = _Hermes([_proposal(BAD_PRICE_REPLY), _proposal(BAD_PRICE_REPLY)])

    chatwoot, supabase = _derive(tmp_path, _processor(tmp_path, hermes, guard=True))

    assert chatwoot.reply_calls == []
    assert [call["detail_reason_code"] for call in supabase.handoff_calls] == [
        "price_not_in_catalog"
    ]
    assert chatwoot.notes == []


def test_with_the_flag_off_the_same_answers_are_sent_as_today(tmp_path: Path) -> None:
    hermes = _Hermes([_proposal(BAD_PRICE_REPLY)])

    chatwoot, supabase = _derive(tmp_path, _processor(tmp_path, hermes, guard=False))

    assert [call["content"] for call in chatwoot.reply_calls] == [BAD_PRICE_REPLY]
    assert supabase.handoff_calls == [] and chatwoot.notes == []


def test_the_product_of_the_note_is_named_from_the_manifest() -> None:
    settings = Settings(  # type: ignore[call-arg]
        webhook_secret="s", allowed_jid=None, capture_dir=Path("."), max_age_seconds=300,
        instance_manifest=MANIFEST,
    )
    assert app_module.price_guard_link_product_name(settings, None) == MANIFEST.product_name.strip()
    assert app_module.price_guard_link_product_name(
        Settings(webhook_secret="s", allowed_jid=None, capture_dir=Path("."), max_age_seconds=300),  # type: ignore[call-arg]
        None,
    ) is None


def test_the_real_client_accepts_the_marker_of_the_note() -> None:
    """La allowlist de marcadores de `ensure_marked_private_note` (chatwoot.py)."""
    posted: list[dict[str, object]] = []
    messages: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # La forma de las respuestas de Chatwoot de test_handoff_link_note.py.
        path = request.url.path
        if request.method == "GET" and path.endswith(f"/conversations/{CONVERSATION_ID}"):
            return httpx.Response(200, json={"id": CONVERSATION_ID, "inbox_id": 9})
        if request.method == "GET" and path.endswith("/messages"):
            if "before" in request.url.params:
                return httpx.Response(200, json={"payload": []})
            return httpx.Response(200, json={"payload": list(messages)})
        body = json.loads(request.content)
        posted.append(body)
        created = {
            "id": 99001,
            "conversation_id": CONVERSATION_ID,
            "inbox_id": 9,
            "message_type": 1,
            "private": body["private"],
            "content": body["content"],
        }
        messages.append(created)
        return httpx.Response(200, json=created)

    client = ChatwootClient(
        base_url="https://chatwoot.example",
        account_id=1,
        access_token="token",
        transport=httpx.MockTransport(handler),
    )
    body = link_note_body("Alimenta Tu Tiroides")
    marker = link_note_marker(HANDOFF_REQUEST_ID)
    for _ in range(2):
        assert asyncio.run(
            client.ensure_marked_private_note(
                conversation_id=CONVERSATION_ID,
                expected_inbox_id=9,
                note_body=body,
                idempotency_marker=marker,
            )
        )
    [post] = posted
    assert post["private"] is True
    assert post["content"] == f"{body}\n\n{marker}"
