"""LAN-128: una baja en cualquier mensaje del lote gana al atajo de «Quiero mi 10 %».

Lo que paso (ATT1, conversacion 536, 28/10 04:57Z): la persona toco el boton
«No más mensajes» de la plantilla y, 12 s despues, «Quiero mi 10 %». El
debounce de la conversacion los junto en un lote, que encabeza el ultimo (el
4611). El atajo de la renovacion miraba solo ``payload["content"]`` del que
encabeza: adelanto Z e hizo ``return`` antes de la admision y del barrido de
bajas del historial, y la baja no se registro. Al dia siguiente salio otro
seguimiento a quien habia pedido no recibir mas.

Datos: ``chatwoot_message_created_buttons_inbox_11_conv_536_20261028.json``,
los dos webhooks reales de esa conversacion (cuenta 2, inbox 11), saneados
(ver su ``_fixture``). Los dos llegan al bridge por el webhook firmado y el
worker los junta como en produccion (debounce de 30 s, reloj adelantado). El
orden inverso y el lote sin baja cambian solo ``content`` de la misma captura.
"""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_cart_followups_wiring import _Renewal
from test_whatsapp_inbound_equivalence import PAYMENT_LINK_PROPOSAL, SECRET, _pending
from test_webhook import (
    StubChatwootClient,
    StubShadowProcessor,
    _post,
    _signed_headers,
)

from bridge.app import Settings, create_app
from bridge.commercial_knowledge import CommercialKnowledge
from bridge.instance_manifest import InstanceManifest

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "chatwoot_message_created_buttons_inbox_11_conv_536_20261028.json"
)
ATT1 = Path(__file__).parent / "fixtures" / "instances" / "att1"
FORM_ID = "520000000536"  # el movil de la captura como lo guarda el formulario
OPT_OUT_TEXT = "No más mensajes"
BUTTON = "Quiero mi 10 %"
OPT_OUT_ID = 4610
BUTTON_ID = 4611


def _webhooks() -> dict[int, dict[str, Any]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {int(key): value for key, value in data["webhooks"].items()}


def _with_content(webhook: dict[str, Any], text: str) -> dict[str, Any]:
    webhook = copy.deepcopy(webhook)
    webhook["content"] = text
    for message in webhook["conversation"]["messages"]:
        message["content"] = text
        message["processed_message_content"] = text
    return webhook


def _settings(tmp_path: Path) -> Settings:
    # El manifiesto de ATT1 tal cual: la captura es de su cuenta (2) y su
    # inbox (11), asi que no hace falta cambiarle los ids.
    instance = tmp_path / "instancia"
    shutil.copytree(ATT1, instance)
    knowledge_path = instance / "conocimiento" / "knowledge-v1.toml"
    knowledge_path.write_text(
        knowledge_path.read_text(encoding="utf-8").replace(
            'estado = "borrador"',
            'estado = "aprobado"\naprobado_por = "test"\naprobado_el = 2026-09-28',
        ),
        encoding="utf-8",
    )
    manifest = InstanceManifest.from_toml_file(instance / "instancia.toml")
    manifest = replace(manifest, flows={**manifest.flows, "inbound": True})
    assert (manifest.chatwoot_account_id, manifest.chatwoot_inbox_id) == (2, 11)
    config = manifest.to_commercial_ally_config()
    return Settings(
        webhook_secret=SECRET,
        allowed_jid=None,
        capture_dir=tmp_path / "captures",
        max_age_seconds=300,
        commercial_ally_config=config,
        commercial_ally_manifest_path=instance / "instancia.toml",
        instance_manifest=manifest,
        hermes_model_name=manifest.agent_model_name,
        commercial_knowledge=CommercialKnowledge.from_toml_file(knowledge_path),
        agent_bot_id=1,
        chatwoot_account_id=2,
        chatwoot_inbox_id=11,
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
        recovery_cart_followups_enabled=True,
        chatwoot_inbound_debounce_seconds=30.0,
    )


def _batch(
    tmp_path: Path,
    supabase: _Renewal,
    contents: dict[int, str],
    *,
    history_error: Exception | None = None,
) -> tuple[StubChatwootClient, StubShadowProcessor, Any]:
    """Los webhooks de ``contents`` por la puerta firmada, juntos en un lote."""
    captured = _webhooks()
    webhooks = [
        _with_content(captured[message_id], text)
        for message_id, text in sorted(contents.items())
    ]
    history = [copy.deepcopy(webhook["conversation"]["messages"][0]) for webhook in webhooks]
    shadow = StubShadowProcessor(PAYMENT_LINK_PROPOSAL)
    chatwoot = StubChatwootClient(messages=history)
    chatwoot.history_error = history_error
    app = create_app(
        _settings(tmp_path),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    for webhook in webhooks:
        raw_body = json.dumps(webhook, separators=(",", ":")).encode("utf-8")
        response = _post(
            app,
            raw_body,
            _signed_headers(
                raw_body, secret=SECRET, delivery=f"inbound-536-{webhook['id']}"
            ),
        )
        assert response.status_code == 202
    worker = app.state.chatwoot_worker
    # Pasada la ventana del debounce, el worker toma el lote entero.
    worker._clock = lambda: time.time() + 60
    asyncio.run(worker.run_once())
    return chatwoot, shadow, app


def test_the_captured_batch_is_an_opt_out_and_then_the_button() -> None:
    captured = _webhooks()

    assert captured[OPT_OUT_ID]["content"] == OPT_OUT_TEXT
    assert captured[BUTTON_ID]["content"] == BUTTON
    # Las dos son respuesta rapida a la misma plantilla, a 12 s una de otra.
    replies = {
        webhook["content_attributes"]["in_reply_to"] for webhook in captured.values()
    }
    assert len(replies) == 1
    first, second = (
        captured[message_id]["conversation"]["messages"][0]["created_at"]
        for message_id in (OPT_OUT_ID, BUTTON_ID)
    )
    assert second - first == 12


def test_an_opt_out_before_the_button_is_recorded_and_z_is_not_brought_forward(
    tmp_path: Path,
) -> None:
    # La 536 tal como paso.
    supabase = _Renewal(identities=(FORM_ID,))

    chatwoot, shadow, app = _batch(
        tmp_path, supabase, {OPT_OUT_ID: OPT_OUT_TEXT, BUTTON_ID: BUTTON}
    )

    assert supabase.renewals == []
    [opt_out] = supabase.opt_outs
    assert opt_out["external_user_id"] == FORM_ID
    assert opt_out["chatwoot_conversation_id"] == 536
    assert opt_out["chatwoot_message_id"] == OPT_OUT_ID
    assert opt_out["rule_key"] == "stop_receiving_messages"
    assert shadow.calls == [] and chatwoot.reply_calls == []
    assert _pending(app) == []


def test_an_opt_out_after_the_button_is_recorded_too(tmp_path: Path) -> None:
    # El orden inverso: la baja encabeza el lote y el atajo nunca se mira.
    supabase = _Renewal(identities=(FORM_ID,))

    chatwoot, shadow, app = _batch(
        tmp_path, supabase, {OPT_OUT_ID: BUTTON, BUTTON_ID: OPT_OUT_TEXT}
    )

    assert supabase.renewals == []
    [opt_out] = supabase.opt_outs
    assert opt_out["chatwoot_message_id"] == BUTTON_ID
    assert opt_out["rule_key"] == "stop_receiving_messages"
    assert shadow.calls == [] and chatwoot.reply_calls == []
    assert _pending(app) == []


def test_a_batch_without_an_opt_out_still_brings_z_forward(tmp_path: Path) -> None:
    supabase = _Renewal(identities=(FORM_ID,))

    chatwoot, shadow, app = _batch(
        tmp_path, supabase, {OPT_OUT_ID: "Hola", BUTTON_ID: BUTTON}
    )

    [renewal] = supabase.renewals
    assert renewal["chatwoot_message_id"] == BUTTON_ID
    assert supabase.opt_outs == []
    assert supabase.admission_rpcs == []
    assert shadow.calls == [] and chatwoot.reply_calls == []
    assert _pending(app) == []
    # Para saber si hay una baja en el lote leyo el historial una vez.
    assert chatwoot.history_required_ids == [(OPT_OUT_ID, BUTTON_ID)]


def test_the_button_alone_does_not_read_the_history(tmp_path: Path) -> None:
    supabase = _Renewal(identities=(FORM_ID,))

    chatwoot, shadow, _ = _batch(tmp_path, supabase, {BUTTON_ID: BUTTON})

    assert len(supabase.renewals) == 1
    assert chatwoot.history_calls == []
    assert shadow.calls == []


def test_an_unreadable_batch_keeps_the_work_for_a_retry(tmp_path: Path) -> None:
    import httpx

    supabase = _Renewal(identities=(FORM_ID,))
    request = httpx.Request("GET", "https://chatwoot.example.test")

    chatwoot, shadow, app = _batch(
        tmp_path,
        supabase,
        {OPT_OUT_ID: OPT_OUT_TEXT, BUTTON_ID: BUTTON},
        history_error=httpx.ConnectError("unavailable", request=request),
    )

    assert supabase.renewals == [] and supabase.opt_outs == []
    assert shadow.calls == [] and chatwoot.reply_calls == []
    pending = _pending(app)
    assert pending and all(
        item["last_error_type"] == "RetryableChatwootWorkError"
        for item in pending
        if item.get("attempts")
    )


@pytest.mark.parametrize("text", [OPT_OUT_TEXT, "no me escriban más"])
def test_the_opt_out_in_the_batch_is_found_by_the_same_rules(
    tmp_path: Path, text: str
) -> None:
    supabase = _Renewal(identities=(FORM_ID,))

    _, _, _ = _batch(tmp_path, supabase, {OPT_OUT_ID: text, BUTTON_ID: BUTTON})

    assert supabase.renewals == []
    assert len(supabase.opt_outs) == 1
