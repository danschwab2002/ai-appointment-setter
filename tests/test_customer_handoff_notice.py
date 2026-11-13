"""El aviso al derivar, solo a clientas (E-304, P-11, D12 de Dan, 02/11).

Al derivar, el bridge descarta el ``reply`` del modelo (F-033) y lo unico que
le puede llegar a la persona es el aviso fijo de F-034 («Le paso tu consulta al
equipo y te responden por este mismo chat.»), que con
``HANDOFF_NOTICE_ENABLED`` sale para todas o para nadie, y que esta apagado en
ATT1 y en Maribel (P-07, punto 6: Dan lo deja apagado en general).

D12: a una clienta (la entrada trae ``customer_status``, E-297) si le llega,
tambien cuando la derivacion es la de ``customer_link_request`` (D6). Con
``CUSTOMER_HANDOFF_NOTICE_ENABLED`` (apagado, portable) sale el mismo aviso,
por el mismo camino: una vez por conversacion, antes de la pausa, con las
guardas de envio, como respuesta al mensaje que la persona acaba de mandar.
Para quien no es clienta, y con ``HANDOFF_NOTICE_ENABLED``, nada cambia.

Datos: el webhook es la captura de la conversacion 200 del inbox 9 con
``content`` reemplazado (``_text_webhook``), la etiqueta en
``conversation.labels`` como la manda Chatwoot, y el manifiesto de ATT1, igual
que ``test_customer_status.py`` y ``test_handoff_notice.py``. El aviso previo
tiene la forma del mensaje 2769 de la conversacion 17 de ATT1.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_att1_production_settings import _start
from test_customer_status import _Customer, _status
from test_handoff_notice import HANDOFF, _agent_bot_message
from test_webhook import StubChatwootClient, StubShadowProcessor, _post, _signed_headers
from test_whatsapp_inbound_equivalence import (
    FORM_ID,
    PAYMENT_LINK_PROPOSAL,
    SECRET,
    WA_ID,
    _manifest_settings,
    _text_webhook,
)

from bridge import app as app_module
from bridge.app import Settings, create_app
from bridge.customer_status import CUSTOMER_LINK_REQUEST_REASON
from bridge.event_log import LOGGED_INFO_EVENTS
from bridge.handoff_notice import HANDOFF_NOTICE_DECISION, HANDOFF_NOTICE_TEXT


def _settings(
    tmp_path: Path, *, customer_notice: bool = True, general_notice: bool = False
) -> Settings:
    return replace(
        _manifest_settings(tmp_path),
        payment_link_enabled=True,
        agent_customer_status_enabled=True,
        customer_handoff_notice_enabled=customer_notice,
        handoff_notice_enabled=general_notice,
    )


def _derive(
    tmp_path: Path,
    supabase: _Customer,
    *,
    text: str = "Quiero que me devuelvan el dinero",
    settings: Settings | None = None,
    proposal: dict[str, object] | None = None,
    labels: list[str] | None = None,
    prior: list[dict[str, Any]] | None = None,
    chatwoot_reply_result: dict[str, object] | None = None,
) -> StubChatwootClient:
    """El entrante de la captura 200, por el mismo camino que ``_process``."""
    webhook, history = _text_webhook(text)
    if labels is not None:
        webhook["conversation"]["labels"] = labels
    chatwoot = StubChatwootClient(
        messages=[*(prior or []), *history], reply_result=chatwoot_reply_result
    )
    app = create_app(
        settings or _settings(tmp_path),
        chatwoot_client=chatwoot,
        shadow_processor=StubShadowProcessor(proposal or HANDOFF),
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    raw_body = json.dumps(webhook, separators=(",", ":")).encode("utf-8")
    response = _post(
        app, raw_body, _signed_headers(raw_body, secret=SECRET, delivery="e304-200")
    )
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())
    return chatwoot


def _customer(**overrides: Any) -> _Customer:
    return _Customer(statuses=(_status(**overrides),), identities=(FORM_ID,))


def _not_customer() -> _Customer:
    return _Customer(identities=(FORM_ID,))


# ------------------------------------------------ a una clienta, el aviso sale


def test_a_customer_who_is_handed_off_gets_the_notice_once_before_the_pause(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    supabase = _customer()

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        chatwoot = _derive(tmp_path, supabase)

    [reply] = chatwoot.reply_calls
    # El texto fijo, no el del modelo (que prometia un plazo).
    assert reply["content"] == HANDOFF_NOTICE_TEXT
    assert reply["trigger_message_id"] == _text_webhook("x")[0]["id"]
    assert chatwoot.reply_markers == [(HANDOFF_NOTICE_DECISION, "refund_request")]
    assert len(supabase.handoff_calls) == 1
    assert chatwoot.events == ["reply", "label:automation_paused"]
    assert (
        "customer_status_handoff_notice outcome=sent reason_code=refund_request "
        "conversation=200"
    ) in caplog.text
    assert FORM_ID not in caplog.text and WA_ID not in caplog.text


def test_the_customer_link_request_handoff_also_gets_the_notice(tmp_path: Path) -> None:
    # D6 con D12: pidio el link de lo que ya compro, el link no sale, el turno
    # se deriva y a ella le llega el aviso, no el texto del modelo.
    supabase = _customer()

    chatwoot = _derive(
        tmp_path, supabase, text="Pasame el link para pagar", proposal=PAYMENT_LINK_PROPOSAL
    )

    [reply] = chatwoot.reply_calls
    assert reply["content"] == HANDOFF_NOTICE_TEXT
    assert "http" not in str(reply["content"])
    assert chatwoot.reply_markers == [
        (HANDOFF_NOTICE_DECISION, CUSTOMER_LINK_REQUEST_REASON)
    ]
    assert supabase.candidate_calls == []
    [handoff] = supabase.handoff_calls
    assert handoff["detail_reason_code"] == CUSTOMER_LINK_REQUEST_REASON


@pytest.mark.parametrize("status", ["reembolsada", "contracargo"])
def test_a_refund_or_a_chargeback_is_a_customer_for_the_notice(
    tmp_path: Path, status: str
) -> None:
    supabase = _customer(status=status)

    chatwoot = _derive(tmp_path, supabase, text="Quiero volver a comprar", proposal=PAYMENT_LINK_PROPOSAL)

    assert [call["content"] for call in chatwoot.reply_calls] == [HANDOFF_NOTICE_TEXT]
    assert len(supabase.handoff_calls) == 1


def test_the_cliente_label_alone_is_a_customer_for_the_notice(tmp_path: Path) -> None:
    supabase = _not_customer()

    chatwoot = _derive(tmp_path, supabase, labels=["cliente"])

    assert [call["content"] for call in chatwoot.reply_calls] == [HANDOFF_NOTICE_TEXT]
    assert len(supabase.handoff_calls) == 1


def test_a_forced_health_handoff_of_a_customer_gets_the_notice(tmp_path: Path) -> None:
    supabase = _customer()

    chatwoot = _derive(
        tmp_path,
        supabase,
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

    assert chatwoot.reply_markers == [
        (HANDOFF_NOTICE_DECISION, "direct_medication_guidance")
    ]
    assert [call["content"] for call in chatwoot.reply_calls] == [HANDOFF_NOTICE_TEXT]


# ------------------------------------------------- una vez, y con las guardas


def test_a_customer_with_a_prior_notice_does_not_get_it_again(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
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
    supabase = _customer()

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        chatwoot = _derive(tmp_path, supabase, prior=[prior])

    assert chatwoot.reply_calls == []
    assert len(supabase.handoff_calls) == 1
    assert chatwoot.events == ["label:automation_paused"]
    assert "customer_status_handoff_notice outcome=already_sent" in caplog.text


def test_when_the_team_has_the_conversation_the_notice_does_not_go_out(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    supabase = _customer()

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        chatwoot = _derive(
            tmp_path,
            supabase,
            chatwoot_reply_result={"status": "blocked", "reason": "human_assignee_present"},
        )

    assert [call["content"] for call in chatwoot.reply_calls] == [HANDOFF_NOTICE_TEXT]
    assert len(supabase.handoff_calls) == 1
    assert chatwoot.events[-1] == "label:automation_paused"
    assert "customer_status_handoff_notice outcome=blocked" in caplog.text


# ---------------------------------------------- para las demas, como hasta hoy


def test_a_person_who_is_not_a_customer_gets_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    supabase = _not_customer()

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        chatwoot = _derive(tmp_path, supabase)

    assert chatwoot.reply_calls == []
    assert len(supabase.handoff_calls) == 1
    assert chatwoot.events == ["label:automation_paused"]
    assert "customer_status_handoff_notice" not in caplog.text


def test_with_the_flag_off_a_customer_gets_nothing_as_today(tmp_path: Path) -> None:
    supabase = _customer()

    chatwoot = _derive(tmp_path, supabase, settings=_settings(tmp_path, customer_notice=False))

    assert chatwoot.reply_calls == []
    assert len(supabase.handoff_calls) == 1


@pytest.mark.parametrize("is_customer", [True, False])
def test_with_the_general_notice_on_there_is_one_notice_for_everyone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, is_customer: bool
) -> None:
    supabase = _customer() if is_customer else _not_customer()

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        chatwoot = _derive(
            tmp_path, supabase, settings=_settings(tmp_path, general_notice=True)
        )

    # Uno solo, sea o no clienta: el general ya avisa y este no se suma.
    assert [call["content"] for call in chatwoot.reply_calls] == [HANDOFF_NOTICE_TEXT]
    assert "customer_status_handoff_notice" not in caplog.text


# ------------------------------------------------------------- el arranque


def test_the_flag_is_off_by_default_portable_and_logged() -> None:
    assert Settings.__dataclass_fields__["customer_handoff_notice_enabled"].default is False
    assert "customer_handoff_notice_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    assert "customer_status_handoff_notice" in LOGGED_INFO_EVENTS


def test_att1_production_starts_with_the_flag_from_its_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _start(
        monkeypatch,
        tmp_path,
        extra={
            "AGENT_CUSTOMER_STATUS_ENABLED": "true",
            "CUSTOMER_HANDOFF_NOTICE_ENABLED": " TRUE ",
        },
    )
    assert settings.customer_handoff_notice_enabled is True
    assert settings.handoff_notice_enabled is False

    settings, _ = _start(monkeypatch, tmp_path / "sin")
    assert settings.customer_handoff_notice_enabled is False


def test_without_the_customer_status_it_does_not_start(tmp_path: Path) -> None:
    settings = replace(
        _manifest_settings(tmp_path),
        agent_customer_status_enabled=False,
        customer_handoff_notice_enabled=True,
    )
    with pytest.raises(
        ValueError, match="CUSTOMER_HANDOFF_NOTICE_ENABLED requires AGENT_CUSTOMER_STATUS_ENABLED"
    ):
        create_app(
            settings,
            chatwoot_client=StubChatwootClient(),
            shadow_processor=StubShadowProcessor(HANDOFF),
            supabase_client=_not_customer(),  # type: ignore[arg-type]
        )


def test_without_the_handoff_admission_it_does_not_start(tmp_path: Path) -> None:
    settings = Settings(
        webhook_secret=SECRET,
        allowed_jid="5210000000200@s.whatsapp.net",
        capture_dir=tmp_path / "captures",
        max_age_seconds=300,
        customer_handoff_notice_enabled=True,
    )
    with pytest.raises(
        ValueError,
        match="CUSTOMER_HANDOFF_NOTICE_ENABLED requires HUMAN_HANDOFF_ADMISSION_ENABLED",
    ):
        create_app(settings, chatwoot_client=StubChatwootClient())


@pytest.fixture(autouse=True)
def _inside_service_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    """E-486: el aviso depende de la hora; estos tests derivan dentro del
    horario (martes 10/11, 11:00 de Ciudad de Mexico), con la frase sola."""
    from datetime import UTC, datetime

    from bridge import handoff_notice

    monkeypatch.setattr(
        handoff_notice, "current_time", lambda: datetime(2026, 11, 10, 17, 0, tzinfo=UTC)
    )
