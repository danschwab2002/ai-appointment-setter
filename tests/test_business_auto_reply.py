"""M8.m1: el agente no le contesta a la respuesta automatica de un negocio.

Lo que paso (Maribel, conversacion 4, 27/10): el primer contacto (4551) salio a
las 22:15:44Z y a las 22:15:51Z llego el 4552, «Gracias por comunicarte con …»,
la respuesta automatica del WhatsApp Business de la persona, sin
``in_reply_to``. El agente lo tomo como una persona y contesto con tres globos.

Datos: ``chatwoot_business_auto_reply_inbox_13_conv_4_20261027.json``, el
webhook real del 4552 y los dos mensajes tal como los devuelve la API de
mensajes de Chatwoot (cuenta 3, inbox 13), saneados y con texto sintetico (ver
su ``_fixture``). El webhook llega al bridge por la puerta firmada y lo toma el
worker como en produccion. Los otros casos cambian solo ``content``,
``content_attributes`` o ``created_at`` de la misma captura.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_whatsapp_inbound_equivalence import (
    PAYMENT_LINK_PROPOSAL,
    SECRET,
    _PaymentLinkSupabase,
    _pending,
)
from test_webhook import (
    StubChatwootClient,
    StubShadowProcessor,
    _post,
    _signed_headers,
)

import bridge.app as app_module
from bridge.app import Settings, create_app
from bridge.business_auto_reply import (
    BUSINESS_AUTO_REPLY_WINDOW_SECONDS,
    batch_is_business_auto_reply,
    has_business_auto_reply_shape,
    last_template_at,
    looks_like_business_auto_reply,
)
from bridge.commercial_knowledge import CommercialKnowledge
from bridge.instance_manifest import InstanceManifest
from bridge.opt_out import detect_explicit_opt_out

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "chatwoot_business_auto_reply_inbox_13_conv_4_20261027.json"
)
ATT1 = Path(__file__).parent / "fixtures" / "instances" / "att1"
FORM_ID = "520000000004"  # el movil de la captura como lo guarda el formulario
TEMPLATE_ID = 4551
AUTO_REPLY_ID = 4552
CONVERSATION_ID = 4
HUMAN_TEXT = "Hola, si, me quedo una duda con el pago"


def _capture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _history() -> list[dict[str, Any]]:
    return copy.deepcopy(_capture()["history"])


def _message(history: list[dict[str, Any]], message_id: int) -> dict[str, Any]:
    [message] = [item for item in history if item["id"] == message_id]
    return message


def _auto_reply_variant(
    *,
    text: str | None = None,
    delay: int | None = None,
    in_reply_to: int | None = None,
    message_id: int = AUTO_REPLY_ID,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """El 4552 (webhook y mensaje del historial) con un cambio."""
    capture = _capture()
    webhook = copy.deepcopy(capture["webhook"])
    message = copy.deepcopy(_message(capture["history"], AUTO_REPLY_ID))
    template = _message(capture["history"], TEMPLATE_ID)
    for item in (webhook, webhook["conversation"]["messages"][0], message):
        item["id"] = message_id
        if text is not None:
            item["content"] = text
            if "processed_message_content" in item:
                item["processed_message_content"] = text
        if in_reply_to is not None:
            item["content_attributes"] = {
                "in_reply_to": in_reply_to,
                "in_reply_to_external_id": "wamid.SANEADO-PLANTILLA-4",
            }
    if delay is not None:
        created_at = template["created_at"] + delay
        message["created_at"] = created_at
        webhook["conversation"]["messages"][0]["created_at"] = created_at
    return webhook, message


def _settings(tmp_path: Path, *, guard: bool = True) -> Settings:
    # El arnes de ATT1 (el de LAN-128) con la cuenta y el inbox de Maribel, que
    # son los de la captura, y su agent bot (3).
    instance = tmp_path / "instancia"
    shutil.copytree(ATT1, instance)
    manifest_path = instance / "instancia.toml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8")
        .replace("account_id = 2\n", "account_id = 3\n")
        .replace("inbox_id = 11\n", "inbox_id = 13\n"),
        encoding="utf-8",
    )
    knowledge_path = instance / "conocimiento" / "knowledge-v1.toml"
    knowledge_path.write_text(
        knowledge_path.read_text(encoding="utf-8").replace(
            'estado = "borrador"',
            'estado = "aprobado"\naprobado_por = "test"\naprobado_el = 2026-09-28',
        ),
        encoding="utf-8",
    )
    manifest = InstanceManifest.from_toml_file(manifest_path)
    manifest = replace(manifest, flows={**manifest.flows, "inbound": True})
    assert (manifest.chatwoot_account_id, manifest.chatwoot_inbox_id) == (3, 13)
    config = manifest.to_commercial_ally_config()
    return Settings(
        webhook_secret=SECRET,
        allowed_jid=None,
        capture_dir=tmp_path / "captures",
        max_age_seconds=300,
        commercial_ally_config=config,
        commercial_ally_manifest_path=manifest_path,
        instance_manifest=manifest,
        hermes_model_name=manifest.agent_model_name,
        commercial_knowledge=CommercialKnowledge.from_toml_file(knowledge_path),
        agent_bot_id=3,
        chatwoot_account_id=3,
        chatwoot_inbox_id=13,
        chatwoot_scoped_inbound_senders_enabled=True,
        chatwoot_cut_b_admission_enabled=True,
        chatwoot_cut_b_scope_key=config.inbound_scope_key,
        chatwoot_cut_b_scope_version=config.inbound_scope_version,
        chatwoot_cut_b_agent_enabled=True,
        automated_replies_enabled=True,
        chatwoot_durable_opt_out_enabled=True,
        chatwoot_opt_out_macro_id=5,
        opt_out_projection_worker_id="opt-out-projection-test",
        chatwoot_human_pause_enabled=True,
        human_handoff_admission_enabled=True,
        human_handoff_projection_enabled=True,
        handoff_projection_policy_key="att1-derivacion",
        handoff_projection_policy_version=1,
        human_handoff_projection_worker_id="handoff-projection-test",
        payment_link_enabled=True,
        chatwoot_inbound_debounce_seconds=30.0,
        chatwoot_business_auto_reply_guard_enabled=guard,
    )


def _deliver(
    tmp_path: Path,
    webhooks: list[dict[str, Any]],
    history: list[dict[str, Any]],
    *,
    guard: bool = True,
    history_error: Exception | None = None,
) -> tuple[StubChatwootClient, StubShadowProcessor, _PaymentLinkSupabase, Any]:
    """Los webhooks por la puerta firmada; el worker los toma en un lote."""
    supabase = _PaymentLinkSupabase(identities=(FORM_ID,))
    shadow = StubShadowProcessor(PAYMENT_LINK_PROPOSAL)
    chatwoot = StubChatwootClient(messages=history)
    chatwoot.history_error = history_error
    app = create_app(
        _settings(tmp_path, guard=guard),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    for webhook in webhooks:
        raw_body = json.dumps(webhook, separators=(",", ":")).encode("utf-8")
        response = _post(
            app,
            raw_body,
            _signed_headers(raw_body, secret=SECRET, delivery=f"inbound-4-{webhook['id']}"),
        )
        assert response.status_code == 202
    worker = app.state.chatwoot_worker
    # Pasada la ventana del debounce, el worker toma el lote entero.
    worker._clock = lambda: time.time() + 60
    asyncio.run(worker.run_once())
    return chatwoot, shadow, supabase, app


# ------------------------------------------------------------- la captura


def test_the_capture_is_our_template_and_seven_seconds_later_a_text() -> None:
    capture = _capture()
    history = capture["history"]
    template = _message(history, TEMPLATE_ID)
    auto_reply = _message(history, AUTO_REPLY_ID)

    assert template["message_type"] == 1 and template["private"] is False
    assert template["sender"] == {**template["sender"], "type": "agent_bot", "id": 3}
    assert "recovery_first_touch_hash" in template["content_attributes"]
    assert auto_reply["message_type"] == 0 and auto_reply["sender"]["type"] == "contact"
    assert auto_reply["content_attributes"] == {}
    assert auto_reply["created_at"] - template["created_at"] == 7
    # El webhook es el mismo mensaje: mismo texto, sin in_reply_to, y en el
    # webhook created_at viene como string ISO (la hora sale del historial).
    webhook = capture["webhook"]
    assert webhook["id"] == AUTO_REPLY_ID and webhook["event"] == "message_created"
    assert webhook["content"] == auto_reply["content"]
    assert webhook["content_attributes"] == {}
    assert isinstance(webhook["created_at"], str)
    assert webhook["conversation"]["messages"][0]["created_at"] == auto_reply["created_at"]


# ------------------------------------------------------------- la regla


def test_the_captured_auto_reply_looks_like_one() -> None:
    history = _history()
    auto_reply = _message(history, AUTO_REPLY_ID)

    sent_at = last_template_at(history, until=auto_reply["created_at"])

    assert sent_at == _message(history, TEMPLATE_ID)["created_at"]
    assert looks_like_business_auto_reply(auto_reply, sent_at) is True
    assert batch_is_business_auto_reply(history, (AUTO_REPLY_ID,)) is True


def test_the_same_text_five_minutes_later_is_not_one() -> None:
    _, message = _auto_reply_variant(delay=300)
    history = [_message(_history(), TEMPLATE_ID), message]

    assert batch_is_business_auto_reply(history, (AUTO_REPLY_ID,)) is False


def test_the_window_is_one_minute_inclusive() -> None:
    _, at_the_edge = _auto_reply_variant(delay=BUSINESS_AUTO_REPLY_WINDOW_SECONDS)
    _, past_it = _auto_reply_variant(delay=BUSINESS_AUTO_REPLY_WINDOW_SECONDS + 1)
    template = _message(_history(), TEMPLATE_ID)

    assert batch_is_business_auto_reply([template, at_the_edge], (AUTO_REPLY_ID,))
    assert not batch_is_business_auto_reply([template, past_it], (AUTO_REPLY_ID,))


def test_a_button_three_seconds_later_is_not_one_even_with_the_shape() -> None:
    _, message = _auto_reply_variant(delay=3, in_reply_to=TEMPLATE_ID)
    history = [_message(_history(), TEMPLATE_ID), message]

    assert batch_is_business_auto_reply(history, (AUTO_REPLY_ID,)) is False


def test_a_person_who_answers_fast_without_the_shape_is_not_one() -> None:
    _, message = _auto_reply_variant(text=HUMAN_TEXT, delay=4)
    history = [_message(_history(), TEMPLATE_ID), message]

    assert batch_is_business_auto_reply(history, (AUTO_REPLY_ID,)) is False


@pytest.mark.parametrize(
    "text",
    [
        "No más mensajes",
        "no me escriban más por favor",
        "Hola, no me contacten más, gracias",
        "No me hablen más",
    ],
)
def test_an_opt_out_never_has_the_shape(text: str) -> None:
    # La baja sigue su camino: el detector de bajas la toma y esto no la toca.
    assert detect_explicit_opt_out([text]) is not None
    assert has_business_auto_reply_shape(text) is False


def test_without_our_template_there_is_nothing_to_measure_against() -> None:
    history = _history()
    template = _message(history, TEMPLATE_ID)
    # Una respuesta del agente no es una plantilla.
    template["content_attributes"] = {"appointment_setter_reply_hash": "0" * 64}

    assert batch_is_business_auto_reply(history, (AUTO_REPLY_ID,)) is False


@pytest.mark.parametrize(
    "marker",
    [
        "recovery_first_touch_hash",
        "recovery_followup_hash",
        "reactivation_command_key",
        "conversation_followup_command_key",
    ],
)
def test_every_template_the_bridge_sends_counts(marker: str) -> None:
    history = _history()
    _message(history, TEMPLATE_ID)["content_attributes"] = {marker: "x"}

    assert batch_is_business_auto_reply(history, (AUTO_REPLY_ID,)) is True


@pytest.mark.parametrize(
    "text",
    [
        "Gracias por comunicarte con Negocio Ejemplo. ¿En qué podemos ayudarte hoy?",
        "Hola! Gracias por enviarnos tu mensaje, te responderemos a la brevedad.",
        "Bienvenido a Negocio Ejemplo. Elegí una opción del menú.",
        "En este momento estamos fuera del horario de atención.",
        "Este es un mensaje automático.",
        "En breve un asesor te atiende.",
    ],
)
def test_the_shapes(text: str) -> None:
    assert has_business_auto_reply_shape(text) is True


@pytest.mark.parametrize(
    "text",
    [HUMAN_TEXT, "Hola", "Cuanto sale?", "gracias!", "Ya pague", "", None],
)
def test_what_is_not_a_shape(text: object) -> None:
    assert has_business_auto_reply_shape(text) is False


def test_a_batch_with_a_message_of_the_person_is_answered() -> None:
    history = _history()
    _, person = _auto_reply_variant(text=HUMAN_TEXT, delay=20, message_id=4553)
    history.append(person)

    assert batch_is_business_auto_reply(history, (AUTO_REPLY_ID, 4553)) is False


# ------------------------------------------------------------- el bridge


def test_the_flag_is_off_by_default_and_portable() -> None:
    assert Settings.__dataclass_fields__[
        "chatwoot_business_auto_reply_guard_enabled"
    ].default is False
    assert (
        "chatwoot_business_auto_reply_guard_enabled"
        in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    )


def test_with_the_flag_the_captured_auto_reply_does_not_reach_the_agent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="bridge.app")

    chatwoot, shadow, supabase, app = _deliver(
        tmp_path, [_capture()["webhook"]], _history()
    )

    assert shadow.calls == [] and chatwoot.reply_calls == []
    assert supabase.admission_calls == []
    assert _pending(app) == []
    assert (
        f"chatwoot_inbound_business_auto_reply_ignored conversation={CONVERSATION_ID}"
        in caplog.text
    )
    # Una sola lectura del historial, con el mensaje del lote.
    assert chatwoot.history_required_ids == [(AUTO_REPLY_ID,)]


def test_without_the_flag_the_agent_answers_it_as_today(tmp_path: Path) -> None:
    chatwoot, shadow, _, _ = _deliver(
        tmp_path, [_capture()["webhook"]], _history(), guard=False
    )

    assert shadow.calls != []
    assert chatwoot.reply_calls != []


def test_with_the_flag_the_same_text_five_minutes_later_is_answered(
    tmp_path: Path,
) -> None:
    webhook, message = _auto_reply_variant(delay=300)

    chatwoot, shadow, _, _ = _deliver(
        tmp_path, [webhook], [_message(_history(), TEMPLATE_ID), message]
    )

    assert shadow.calls != [] and chatwoot.reply_calls != []


def test_with_the_flag_a_person_who_answers_fast_is_answered_without_an_extra_read(
    tmp_path: Path,
) -> None:
    webhook, message = _auto_reply_variant(text=HUMAN_TEXT, delay=4)
    history = [_message(_history(), TEMPLATE_ID), message]

    with_guard = _deliver(tmp_path / "con", [webhook], history)
    without_guard = _deliver(tmp_path / "sin", [webhook], history, guard=False)

    assert with_guard[1].calls != [] and with_guard[0].reply_calls != []
    # Sin la forma, la guarda no lee el historial: las lecturas son las de hoy.
    assert with_guard[0].history_calls == without_guard[0].history_calls


def test_with_the_flag_an_auto_reply_and_then_the_person_in_one_batch_is_answered(
    tmp_path: Path,
) -> None:
    first, first_message = _auto_reply_variant()
    second, second_message = _auto_reply_variant(
        text=HUMAN_TEXT, delay=20, message_id=4553
    )
    history = [_message(_history(), TEMPLATE_ID), first_message, second_message]

    chatwoot, shadow, _, _ = _deliver(tmp_path, [first, second], history)

    assert shadow.calls != [] and chatwoot.reply_calls != []


def test_with_the_flag_an_unreadable_history_keeps_the_work_for_a_retry(
    tmp_path: Path,
) -> None:
    request = httpx.Request("GET", "https://chatwoot.example.test")

    chatwoot, shadow, supabase, app = _deliver(
        tmp_path,
        [_capture()["webhook"]],
        _history(),
        history_error=httpx.ConnectError("unavailable", request=request),
    )

    assert shadow.calls == [] and chatwoot.reply_calls == []
    assert supabase.admission_calls == []
    pending = _pending(app)
    assert pending and all(
        item["last_error_type"] == "RetryableChatwootWorkError"
        for item in pending
        if item.get("attempts")
    )
