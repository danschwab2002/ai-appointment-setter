"""E-107: una baja nueva se confirma con una sola linea, sin vender.

Hasta aca una baja nueva se registraba y no se contestaba (regla del 25/10).
Dan, 26/10: se confirma con una linea, por ejemplo «Listo, no te escribimos
mas». La manda el bridge con un texto que valida al arrancar (sin link, sin
cupon, sin precio y sin pregunta), una vez por conversacion y nunca a quien ya
estaba dado de baja; con un flag por instancia, apagado por defecto.

Datos:

* la baja real del boton «No más mensajes» de la conversacion 50 de ATT1
  (inbox 11, mensaje 3108, del 12/10), en
  ``chatwoot_conversation_inbox_11_conv_50_opt_out_paused_20261017.json``: la
  historia entera que capturo la API de Chatwoot de produccion;
* el webhook real de la conversacion 200 del inbox 9
  (``chatwoot_message_created_audio_inbox_9_conv_200_20260928.json``) con
  ``content`` cambiado por la frase de baja, y el manifiesto de ATT1, como el
  resto de los tests del entrante (``test_whatsapp_inbound_equivalence.py``);
* la forma de un mensaje del agente con su marca de decision sale del mensaje
  2769 de la conversacion 17 de ATT1 (inbox 11, 15/10), como en
  ``test_handoff_notice.py``.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from bridge.app import Settings, create_app
from bridge.opt_out_confirmation import (
    OPT_OUT_CONFIRMATION_DECISION,
    OPT_OUT_CONFIRMATION_DEFAULT_TEXT,
    opt_out_confirmation_already_sent,
    validate_opt_out_confirmation_text,
)
from bridge.supabase import InboundOptOutResult
from bridge.worker import OptOutProjectionWorker
from test_att1_production_settings import _start
from test_handoff_notice import _agent_bot_message
from test_webhook import (
    StubChatwootClient,
    StubShadowProcessor,
    _post,
    _signed_headers,
)
from test_whatsapp_inbound_equivalence import (
    CONV_50,
    FORM_ID,
    OPT_OUT_TEXT,
    SECRET,
    WA_ID,
    _blocked,
    _manifest_settings,
    _Supabase,
    _text_webhook,
)


def _confirmation_settings(
    tmp_path: Path, *, enabled: bool = True, text: str | None = None
) -> Settings:
    settings = replace(
        _manifest_settings(tmp_path), opt_out_confirmation_enabled=enabled
    )
    if text is not None:
        settings = replace(settings, opt_out_confirmation_text=text)
    return settings


def _opt_out(
    tmp_path: Path,
    *,
    supabase: _Supabase | None = None,
    settings: Settings | None = None,
    text: str = OPT_OUT_TEXT,
    history: list[dict[str, Any]] | None = None,
    prior: list[dict[str, Any]] | None = None,
    message_id: int | None = None,
) -> tuple[StubChatwootClient, StubShadowProcessor, _Supabase]:
    """El entrante de la captura 200 con una frase de baja."""
    supabase = supabase or _Supabase(identities=(FORM_ID,))
    webhook, own_history = _text_webhook(text)
    if message_id is not None:
        webhook["id"] = message_id
        webhook["conversation"]["messages"][0]["id"] = message_id
    shadow = StubShadowProcessor({"decision": "reply", "reply": "Te leemos."})
    chatwoot = StubChatwootClient(
        messages=[*(prior or []), *(history if history is not None else own_history)]
    )
    app = create_app(
        settings or _confirmation_settings(tmp_path),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    raw_body = json.dumps(webhook, separators=(",", ":")).encode("utf-8")
    response = _post(
        app, raw_body, _signed_headers(raw_body, secret=SECRET, delivery="e107-200")
    )
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())
    return chatwoot, shadow, supabase


def _confirmations(chatwoot: StubChatwootClient) -> list[dict[str, object]]:
    return [
        call
        for call, (decision, _) in zip(
            chatwoot.reply_calls, chatwoot.reply_markers, strict=False
        )
        if decision == OPT_OUT_CONFIRMATION_DECISION
    ]


# ---------------------------------------------------------------- el texto


def test_the_default_text_is_one_line_without_selling() -> None:
    text = OPT_OUT_CONFIRMATION_DEFAULT_TEXT
    assert validate_opt_out_confirmation_text(text) == text
    assert "\n" not in text
    assert not re.search(r"https?://|www\.|\d|%|\$|\?|¿", text)


@pytest.mark.parametrize(
    "text",
    [
        "Listo, no te escribimos más. Si cambias de idea: https://pay.hotmart.com/x",
        "Listo. Te dejo el 10% con PROTOCOLO10+ por si acaso",
        "Listo, no te escribimos más. ¿Te puedo ayudar con algo?",
        "Listo.\nNo te escribimos más.",
        "Listo, mira www.ejemplo.com",
        "",
        "x" * 161,
    ],
)
def test_a_text_that_sells_asks_or_breaks_the_line_is_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="OPT_OUT_CONFIRMATION_TEXT"):
        validate_opt_out_confirmation_text(text)


def test_the_ghl_text_of_maribel_is_accepted() -> None:
    # El que manda hoy el workflow de GHL de Maribel (E-093): una instancia
    # puede copiarlo tal cual.
    text = (
        "Hemos recibido tu solicitud te eliminaremos de todas nuestras bases "
        "de datos, Te deseamos lo mejor"
    )
    assert validate_opt_out_confirmation_text(text) == text


# ------------------------------------------------------------- el flag


def test_the_flag_is_off_by_default() -> None:
    fields = Settings.__dataclass_fields__
    assert fields["opt_out_confirmation_enabled"].default is False
    assert (
        fields["opt_out_confirmation_text"].default
        == OPT_OUT_CONFIRMATION_DEFAULT_TEXT
    )


def test_without_the_flag_the_opt_out_is_recorded_and_not_answered(
    tmp_path: Path,
) -> None:
    chatwoot, shadow, supabase = _opt_out(
        tmp_path, settings=_confirmation_settings(tmp_path, enabled=False)
    )

    assert len(supabase.opt_outs) == 1
    assert chatwoot.reply_calls == [] and shadow.calls == []


def test_att1_production_starts_with_the_flag_and_its_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _start(
        monkeypatch,
        tmp_path,
        extra={
            "OPT_OUT_CONFIRMATION_ENABLED": "true",
            "OPT_OUT_CONFIRMATION_TEXT": "Listo, te sacamos de la lista.",
        },
    )
    assert settings.opt_out_confirmation_enabled is True
    assert settings.opt_out_confirmation_text == "Listo, te sacamos de la lista."

    settings, _ = _start(monkeypatch, tmp_path / "sin")
    assert settings.opt_out_confirmation_enabled is False
    assert settings.opt_out_confirmation_text == OPT_OUT_CONFIRMATION_DEFAULT_TEXT


def test_a_text_with_a_link_in_the_environment_does_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="must not carry a link"):
        _start(
            monkeypatch,
            tmp_path,
            extra={
                "OPT_OUT_CONFIRMATION_ENABLED": "true",
                "OPT_OUT_CONFIRMATION_TEXT": "Listo. https://pay.hotmart.com/x",
            },
        )


def test_the_flag_without_the_durable_opt_out_does_not_start(tmp_path: Path) -> None:
    # Sin la baja durable la confirmacion no saldria nunca, y un flag prendido
    # que no hace nada no se nota.
    settings = Settings(
        webhook_secret=SECRET,
        allowed_jid="5210000000200@s.whatsapp.net",
        capture_dir=tmp_path / "captures",
        max_age_seconds=300,
        opt_out_confirmation_enabled=True,
    )
    with pytest.raises(ValueError, match="OPT_OUT_CONFIRMATION_ENABLED requires"):
        create_app(settings, chatwoot_client=StubChatwootClient())


# ------------------------------------------------------- la confirmacion


def test_a_new_opt_out_is_confirmed_once_with_one_line(tmp_path: Path) -> None:
    chatwoot, shadow, supabase = _opt_out(tmp_path)

    assert len(supabase.opt_outs) == 1
    [confirmation] = chatwoot.reply_calls
    assert confirmation["content"] == OPT_OUT_CONFIRMATION_DEFAULT_TEXT
    assert chatwoot.reply_markers == [(OPT_OUT_CONFIRMATION_DECISION, None)]
    # Responde al mensaje de la baja, en su conversacion y su inbox.
    assert confirmation["conversation_id"] == 200
    assert confirmation["trigger_message_id"] == supabase.opt_outs[0][
        "chatwoot_message_id"
    ]
    assert chatwoot.reply_expected_inbox_ids == [9]
    # Despues de la confirmacion nada mas: el agente no corre.
    assert shadow.calls == []


def test_the_real_button_of_conversation_50_is_confirmed(tmp_path: Path) -> None:
    # La historia capturada entera, con la conversacion devuelta al agente
    # (la admision da 'created'): la baja es la pulsacion real del boton
    # (3108, in_reply_to 2820).
    capture = json.loads(CONV_50.read_text(encoding="utf-8"))
    history = capture["conversation"]["messages"]
    [pressed] = [message for message in history if message["id"] == 3108]
    assert pressed["content"] == OPT_OUT_TEXT

    chatwoot, shadow, supabase = _opt_out(
        tmp_path, history=history, message_id=3108
    )

    [opt_out] = supabase.opt_outs
    assert opt_out["chatwoot_message_id"] == 3108
    [confirmation] = _confirmations(chatwoot)
    assert confirmation["trigger_message_id"] == 3108
    assert confirmation["content"] == OPT_OUT_CONFIRMATION_DEFAULT_TEXT
    assert shadow.calls == []


def test_the_instance_text_is_the_one_sent(tmp_path: Path) -> None:
    text = "Listo, no te vamos a escribir más."
    chatwoot, _, _ = _opt_out(
        tmp_path, settings=_confirmation_settings(tmp_path, text=text)
    )

    [confirmation] = chatwoot.reply_calls
    assert confirmation["content"] == text


def test_the_confirmation_carries_no_link_and_no_coupon(tmp_path: Path) -> None:
    chatwoot, _, _ = _opt_out(tmp_path)

    [confirmation] = chatwoot.reply_calls
    content = str(confirmation["content"])
    assert not re.search(r"https?://|www\.|hotmart|PROTOCOLO|\d|%", content)


def test_a_conversation_that_was_already_confirmed_is_not_confirmed_again(
    tmp_path: Path,
) -> None:
    prior = _agent_bot_message(
        content=OPT_OUT_CONFIRMATION_DEFAULT_TEXT,
        content_attributes={
            "appointment_setter_decision": OPT_OUT_CONFIRMATION_DECISION
        },
    )

    chatwoot, shadow, supabase = _opt_out(tmp_path, prior=[prior])

    assert len(supabase.opt_outs) == 1
    assert chatwoot.reply_calls == [] and shadow.calls == []


def test_a_second_opt_out_of_a_person_already_opted_out_is_not_confirmed(
    tmp_path: Path,
) -> None:
    # La misma persona ya tenia una baja (en esta u otra conversacion) y la
    # vuelve a pedir: se registra otra vez, no se le repite el mensaje.
    supabase = _Supabase(identities=(FORM_ID,), stopped_user_ids=(WA_ID,))

    chatwoot, shadow, supabase = _opt_out(tmp_path, supabase=supabase)

    assert supabase.reconciliations != []
    assert len(supabase.opt_outs) == 1
    assert chatwoot.reply_calls == [] and shadow.calls == []


def test_an_opt_out_the_base_does_not_recognize_is_not_confirmed(
    tmp_path: Path,
) -> None:
    class _Conflict(_Supabase):
        async def apply_chatwoot_inbound_opt_out(
            self, **kwargs: object
        ) -> InboundOptOutResult:
            self.opt_outs.append(kwargs)
            return InboundOptOutResult(
                outcome="evidence_conflict",
                opt_out_event_id="opt-out-event-200",
                contact_id=None,
                affected_cases=0,
                affected_actions=0,
                affected_attempts=0,
            )

    chatwoot, _, supabase = _opt_out(
        tmp_path, supabase=_Conflict(identities=(FORM_ID,))
    )

    assert len(supabase.opt_outs) == 1
    assert chatwoot.reply_calls == []


def test_a_message_that_is_not_an_opt_out_gets_no_confirmation(
    tmp_path: Path,
) -> None:
    chatwoot, shadow, supabase = _opt_out(tmp_path, text="Sí, quiero el enlace")

    assert supabase.opt_outs == []
    assert _confirmations(chatwoot) == []
    assert len(shadow.calls) == 1


def test_an_opt_out_in_a_blocked_admission_is_confirmed_through_chatwoot_guards(
    tmp_path: Path,
) -> None:
    # La admision da 'blocked' (la conversacion de la plantilla, o una que el
    # equipo tiene): la baja se registra igual y la confirmacion se intenta.
    # Si el equipo tiene la conversacion, la guarda del cliente de Chatwoot
    # (asignada o automation_paused) la frena; aca el stub no la frena.
    supabase = _blocked(identities=(FORM_ID,))

    chatwoot, shadow, supabase = _opt_out(tmp_path, supabase=supabase)

    assert len(supabase.opt_outs) == 1
    [confirmation] = _confirmations(chatwoot)
    assert confirmation["content"] == OPT_OUT_CONFIRMATION_DEFAULT_TEXT
    assert shadow.calls == []


def test_a_blocked_send_does_not_undo_the_opt_out(tmp_path: Path) -> None:
    supabase = _Supabase(identities=(FORM_ID,))
    webhook, history = _text_webhook(OPT_OUT_TEXT)
    chatwoot = StubChatwootClient(
        messages=history,
        reply_result={"status": "blocked", "reason": "human_assignee_present"},
    )
    shadow = StubShadowProcessor({"decision": "reply", "reply": "x"})
    app = create_app(
        _confirmation_settings(tmp_path),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    raw_body = json.dumps(webhook, separators=(",", ":")).encode("utf-8")
    response = _post(
        app, raw_body, _signed_headers(raw_body, secret=SECRET, delivery="e107-b")
    )
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())

    assert len(supabase.opt_outs) == 1
    assert shadow.calls == []
    assert app.state.chatwoot_worker is not None


# ------------------------------------------------- el reconocimiento de la marca


def test_only_an_agent_message_marked_as_confirmation_counts() -> None:
    marked = _agent_bot_message(
        content_attributes={
            "appointment_setter_decision": OPT_OUT_CONFIRMATION_DECISION
        }
    )
    other = _agent_bot_message(
        content_attributes={"appointment_setter_decision": "handoff"}
    )
    private = {**marked, "private": True}

    assert opt_out_confirmation_already_sent([marked]) is True
    assert opt_out_confirmation_already_sent([other, private]) is False


# ------------------------------------------- el candado con la proyeccion


class _ProjectionSupabase:
    def __init__(self) -> None:
        self.claims = 0

    async def claim_chatwoot_opt_out_projections(self, **_: object) -> list[object]:
        self.claims += 1
        return []


def test_the_projection_waits_while_the_confirmation_holds_the_lock() -> None:
    # La macro de la baja pone automation_paused, que bloquea todo envio del
    # AgentBot: mientras el entrante registra la baja y confirma, el worker no
    # proyecta.
    async def scenario() -> tuple[int, int]:
        lock = asyncio.Lock()
        supabase = _ProjectionSupabase()
        worker = OptOutProjectionWorker(
            supabase=supabase,  # type: ignore[arg-type]
            chatwoot=StubChatwootClient(),  # type: ignore[arg-type]
            worker_id="opt-out-projection-test",
            run_lock=lock,
        )
        await lock.acquire()
        task = asyncio.create_task(worker.run_once())
        await asyncio.sleep(0.05)
        while_held = supabase.claims
        lock.release()
        await task
        return while_held, supabase.claims

    assert asyncio.run(scenario()) == (0, 1)


def test_the_projection_runs_without_a_lock_as_before() -> None:
    async def scenario() -> int:
        supabase = _ProjectionSupabase()
        worker = OptOutProjectionWorker(
            supabase=supabase,  # type: ignore[arg-type]
            chatwoot=StubChatwootClient(),  # type: ignore[arg-type]
            worker_id="opt-out-projection-test",
        )
        await worker.run_once()
        return supabase.claims

    assert asyncio.run(scenario()) == 1
