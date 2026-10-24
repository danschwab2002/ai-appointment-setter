"""F-034: al derivar, la persona recibe un aviso corto (decisiones D5 a D7).

Hasta F-034 el bridge descartaba la respuesta del agente al derivar y a la
persona no le llegaba nada (F-033, 15/10). En ATT1, 33 derivaciones en 30
dias; la mitad espero mas de 6 h al equipo (att1-db y Chatwoot, 24/10).

Dan, 24/10: el aviso nombra al equipo, sin plazo, sin nombre de persona, y no
dice que escribe una IA. Lo manda el bridge con un texto fijo, una vez por
conversacion, antes de la pausa; con un flag por instancia, apagado por
defecto (Johanna no cambia).

Datos:

* el webhook real de la conversacion 200 del inbox 9
  (``chatwoot_message_created_audio_inbox_9_conv_200_20260928.json``), con
  ``content`` cambiado por texto, y el manifiesto de ATT1, como el resto de los
  tests del entrante (``test_whatsapp_inbound_equivalence.py``);
* la forma de un mensaje del agente con su marca de decision sale del mensaje
  2769 de la conversacion 17 de ATT1 (inbox 11, 15/10), en
  ``automation_expert/chatwoot_messages_inbox_11_convs_50_17_52_20261015.json``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from bridge.app import Settings, create_app
from bridge.handoff_notice import (
    HANDOFF_NOTICE_DECISION,
    HANDOFF_NOTICE_TEXT,
    handoff_notice_already_sent,
)
from test_att1_production_settings import _start
from test_webhook import (
    StubChatwootClient,
    StubShadowProcessor,
    _post,
    _signed_headers,
)
from test_whatsapp_inbound_equivalence import (
    FORM_ID,
    PAYMENT_LINK_PROPOSAL,
    SECRET,
    _manifest_settings,
    _Supabase,
    _text_webhook,
)

FIXTURES = Path(__file__).parent / "fixtures"
AGENT_MESSAGE_FIXTURE = (
    FIXTURES
    / "automation_expert"
    / "chatwoot_messages_inbox_11_convs_50_17_52_20261015.json"
)
# Lo que el agente escribe hoy al derivar (el SOUL comun le pide decir que el
# caso requiere una revision humana). El aviso no lo usa.
HANDOFF = {
    "decision": "handoff",
    "qualification_status": "needs_human",
    "reason_code": "refund_request",
    "reply": "Te paso con una persona del equipo, te escribe en 2 horas.",
}


def _notice_settings(tmp_path: Path, *, enabled: bool = True) -> Settings:
    return replace(_manifest_settings(tmp_path), handoff_notice_enabled=enabled)


def _agent_bot_message(**changes: Any) -> dict[str, Any]:
    """El mensaje 2769 (ATT1, conv 17): la forma de un mensaje del agente."""
    data = json.loads(AGENT_MESSAGE_FIXTURE.read_text(encoding="utf-8"))
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("id") == 2769 and isinstance(node.get("sender"), dict):
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    message = copy.deepcopy(found[0])
    assert message["sender"]["type"] == "agent_bot"
    assert "appointment_setter_decision" in message["content_attributes"]
    message.update(changes)
    return message


def _derive(
    tmp_path: Path,
    *,
    text: str = "Quiero que me devuelvan el dinero",
    settings: Settings | None = None,
    proposal: dict[str, object] | None = None,
    prior: list[dict[str, Any]] | None = None,
    chatwoot_reply_result: dict[str, object] | None = None,
) -> tuple[StubChatwootClient, _Supabase]:
    """El entrante de la captura 200, por el mismo camino que ``_process``."""
    supabase = _Supabase(identities=(FORM_ID,))
    webhook, history = _text_webhook(text)
    shadow = StubShadowProcessor(proposal or HANDOFF)
    chatwoot = StubChatwootClient(
        messages=[*(prior or []), *history], reply_result=chatwoot_reply_result
    )
    app = create_app(
        settings or _notice_settings(tmp_path),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    raw_body = json.dumps(webhook, separators=(",", ":")).encode("utf-8")
    response = _post(
        app, raw_body, _signed_headers(raw_body, secret=SECRET, delivery="f034-200")
    )
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())
    return chatwoot, supabase


# ---------------------------------------------------------------- el texto


def test_the_notice_names_the_team_without_deadline_name_or_ai() -> None:
    text = HANDOFF_NOTICE_TEXT
    assert "equipo" in text
    # Sin plazo (D6): ni numeros ni palabras de tiempo.
    assert not re.search(r"\d", text)
    for word in ("hora", "minuto", "pronto", "hoy", "mañana", "enseguida", "breve"):
        assert word not in text.lower()
    # Sin nombre de persona: la unica mayuscula es la del comienzo.
    assert [w for w in text.split()[1:] if w[:1].isupper()] == []
    # Sin revelar que escribe una IA (el matiz de Dan sobre D5).
    for word in ("ia", "asistente", "bot", "virtual", "inteligencia", "automátic"):
        assert not re.search(rf"\b{word}", text.lower())
    # Corto, y sin link: tambien vale para salud y reclamos.
    assert len(text) <= 80
    assert "http" not in text


# ------------------------------------------------- con el flag: el aviso sale


def test_a_handoff_sends_the_notice_once_before_the_pause(tmp_path: Path) -> None:
    chatwoot, supabase = _derive(tmp_path)

    [reply] = chatwoot.reply_calls
    # El texto fijo, no el que escribio el agente (que prometia un plazo).
    assert reply["content"] == HANDOFF_NOTICE_TEXT
    assert reply["trigger_message_id"] == _text_webhook("x")[0]["id"]
    assert chatwoot.reply_markers == [(HANDOFF_NOTICE_DECISION, "refund_request")]
    # La derivacion se pide igual, y el aviso sale antes de la pausa, que
    # bloquearia el envio.
    assert len(supabase.handoff_calls) == 1
    assert chatwoot.events == ["reply", "label:automation_paused"]


def test_a_forced_health_handoff_also_gets_the_notice_without_link(
    tmp_path: Path,
) -> None:
    # El agente iba a contestar, pero el pedido es sobre medicacion: el bridge
    # fuerza la derivacion (direct_medication_guidance) y el aviso sale igual.
    chatwoot, supabase = _derive(
        tmp_path,
        text=(
            "Necesito saber si debo dejar mi medicación psiquiátrica para hacer "
            "el programa y qué dosis debería tomar"
        ),
        proposal={
            "decision": "ask_question",
            "qualification_status": "in_progress",
            "reason_code": "commercial_response",
            "reply": "Consultá a tu profesional.",
        },
    )

    [reply] = chatwoot.reply_calls
    assert reply["content"] == HANDOFF_NOTICE_TEXT
    assert "http" not in str(reply["content"])
    assert chatwoot.reply_markers == [
        (HANDOFF_NOTICE_DECISION, "direct_medication_guidance")
    ]
    assert len(supabase.handoff_calls) == 1


def test_a_link_that_cannot_go_out_derives_with_the_notice(tmp_path: Path) -> None:
    # Pidio el link y el link esta apagado: la derivacion del bridge
    # (payment_link_disabled) tambien avisa.
    chatwoot, supabase = _derive(
        tmp_path, text="Envíame el enlace", proposal=PAYMENT_LINK_PROPOSAL
    )

    [reply] = chatwoot.reply_calls
    assert reply["content"] == HANDOFF_NOTICE_TEXT
    assert chatwoot.reply_markers == [
        (HANDOFF_NOTICE_DECISION, "payment_link_disabled")
    ]
    assert len(supabase.handoff_calls) == 1


def test_a_second_handoff_in_the_same_conversation_does_not_repeat_it(
    tmp_path: Path,
) -> None:
    # La conversacion volvio al agente (reanudacion, F-037) y se deriva otra
    # vez: el aviso de la primera derivacion ya esta en el historial.
    prior = _agent_bot_message(
        id=1,
        conversation_id=200,
        created_at=1,
        content=HANDOFF_NOTICE_TEXT,
        content_attributes={
            "appointment_setter_reply_hash": "0" * 64,
            "appointment_setter_decision": HANDOFF_NOTICE_DECISION,
            "appointment_setter_reason_code": "refund_request",
        },
    )

    chatwoot, supabase = _derive(tmp_path, prior=[prior])

    assert chatwoot.reply_calls == []
    assert len(supabase.handoff_calls) == 1
    assert chatwoot.events == ["label:automation_paused"]


def test_when_the_team_already_has_the_conversation_no_notice_goes_out(
    tmp_path: Path,
) -> None:
    # La guarda de envio bloquea con una persona asignada (o con la pausa, o
    # con un mensaje del equipo despues del de la persona). El aviso no sale y
    # la derivacion sigue.
    chatwoot, supabase = _derive(
        tmp_path,
        chatwoot_reply_result={"status": "blocked", "reason": "human_assignee_present"},
    )

    # El bridge lo intento una vez por la guarda (la que bloquea con persona
    # asignada, con la pausa o con un mensaje del equipo: tests/test_chatwoot.py),
    # no insiste, y la derivacion termina con su pausa.
    assert [call["content"] for call in chatwoot.reply_calls] == [HANDOFF_NOTICE_TEXT]
    assert len(supabase.handoff_calls) == 1
    assert chatwoot.events[-1] == "label:automation_paused"


# ------------------------------------------------- sin el flag: como hasta hoy


def test_without_the_flag_the_handoff_stays_silent(tmp_path: Path) -> None:
    chatwoot, supabase = _derive(
        tmp_path, settings=_notice_settings(tmp_path, enabled=False)
    )

    assert chatwoot.reply_calls == []
    assert len(supabase.handoff_calls) == 1
    assert chatwoot.events == ["label:automation_paused"]


def test_the_flag_is_off_by_default() -> None:
    assert Settings.__dataclass_fields__["handoff_notice_enabled"].default is False


def test_att1_production_starts_with_the_flag_from_its_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # El bridge de ATT1 con su entorno de produccion (portable, con
    # manifiesto) mas HANDOFF_NOTICE_ENABLED: la capacidad es portable y
    # arranca. Sin la variable, apagado.
    settings, _ = _start(monkeypatch, tmp_path, extra={"HANDOFF_NOTICE_ENABLED": "true"})
    assert settings.handoff_notice_enabled is True

    settings, _ = _start(monkeypatch, tmp_path / "sin")
    assert settings.handoff_notice_enabled is False


def test_the_flag_without_the_handoff_admission_does_not_start(tmp_path: Path) -> None:
    settings = Settings(
        webhook_secret=SECRET,
        allowed_jid="5210000000200@s.whatsapp.net",
        capture_dir=tmp_path / "captures",
        max_age_seconds=300,
        handoff_notice_enabled=True,
    )
    with pytest.raises(ValueError, match="HANDOFF_NOTICE_ENABLED requires"):
        create_app(settings, chatwoot_client=StubChatwootClient())


# -------------------------------------------------- el reconocimiento del aviso


def test_only_an_agent_message_marked_as_handoff_counts_as_a_prior_notice() -> None:
    notice = _agent_bot_message(
        content=HANDOFF_NOTICE_TEXT,
        content_attributes={"appointment_setter_decision": "handoff"},
    )
    reply = _agent_bot_message(
        content_attributes={"appointment_setter_decision": "ask_question"},
    )
    private = {**notice, "private": True}
    from_team = {**notice, "sender": {"id": 4, "type": "user"}}

    assert handoff_notice_already_sent([notice]) is True
    assert handoff_notice_already_sent([reply, private, from_team]) is False
    assert handoff_notice_already_sent([]) is False
