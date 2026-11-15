"""E-571 (parte 2 de E-539): sacar `automation_paused` reanuda en el momento.

Diagnostico (E-539, 13/11/2026, Maribel conv 260): el equipo saco la etiqueta
y resolvio la conversacion; los dos `conversation_updated` llegaron al bridge
(`unsupported_event` y `chatwoot_conversation_label_change_captured`, entrega
`7afec902-...`) y ninguno se proceso. La conversacion del setter quedo
`paused_human` hasta que la persona volviera a escribir.

DEUDA DE FIXTURE (declarada en el PR): el payload real de la entrega
`7afec902-...` esta en la captura del bridge de Maribel (un volumen de
produccion, fuera del alcance de este encargo) y no se bajo. Los payloads de
aca se derivan del precedente mas cercano del repo, marcado: el
`webhook.conversation` capturado de la conversacion 173
(`chatwoot_paused_lead_label_removed_by_human_inbox_9_conv_173_20260925.json`),
que es el mismo `webhook_data` que Chatwoot v4.13.0 manda en la raiz de un
`conversation_updated` (`WebhookListener#conversation_updated`), mas
`changed_attributes` con la forma de `BaseListener#extract_changed_attributes`.
Es la misma derivacion que usa
`test_a_conversation_updated_with_a_label_change_is_captured_and_nothing_else`.
Cuando se capture el payload real anonimizado, estos tests pasan a usarlo.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

import httpx
import pytest

import bridge.app as app_module
from bridge.app import Settings, _resume_paused_conversation, create_app
from bridge.filtering import LabelRemoval, classify_chatwoot_label_removal
from bridge.supabase import ConversationResumeResult
from test_app import (
    AHORA_173,
    JID_DEL_LEAD_173,
    NUMERO_DE_PRUEBA,
    _Chatwoot173,
    _cliente_173,
    _fixture_173,
    _settings_resume,
    _SupabaseFalso,
    _webhook_firmado,
)

ENTREGA = "7afec902-0000-4000-8000-000000000173"  # forma de la entrega real, valor de prueba
CLAVE = (
    "resume:173:label:"
    + hashlib.sha256(ENTREGA.encode("utf-8")).hexdigest()[:32]
)


def _cambio_de_etiqueta(
    previo: list[str], actual: list[str], **extra: object
) -> dict[str, object]:
    """`conversation_updated` derivado del `webhook_data` capturado de la 173."""
    conversation = dict(_fixture_173()["webhook"]["conversation"])
    conversation["labels"] = list(actual)
    return {
        **conversation,
        "event": "conversation_updated",
        "changed_attributes": [
            {"label_list": {"previous_value": previo, "current_value": actual}}
        ],
        **extra,
    }


def _clasificar(payload: object, *, scoped: bool = True, allowed: str = NUMERO_DE_PRUEBA):
    return classify_chatwoot_label_removal(
        payload,
        label="automation_paused",
        expected_inbox_id=9,
        allowed_jid=allowed,
        allow_any_scoped_sender=scoped,
    )


# ------------------------------------------------------------- el clasificador


def test_the_removal_of_the_pause_label_is_recognized_with_the_lead_identity() -> None:
    removal = _clasificar(_cambio_de_etiqueta(["automation_paused"], []))
    assert removal == LabelRemoval(conversation_id=173, sender_jid=JID_DEL_LEAD_173)


@pytest.mark.parametrize(
    "payload",
    [
        # La etiqueta se puso, no se saco.
        _cambio_de_etiqueta([], ["automation_paused"]),
        # Se saco otra; la pausa sigue.
        _cambio_de_etiqueta(
            ["automation_paused", "cliente"], ["automation_paused"]
        ),
        # Otro atributo (asignacion), sin label_list.
        {
            **_cambio_de_etiqueta(["automation_paused"], []),
            "changed_attributes": [
                {"assignee_id": {"previous_value": None, "current_value": 7}}
            ],
        },
        # Otro inbox.
        _cambio_de_etiqueta(["automation_paused"], [], inbox_id=8),
        # Un mensaje no es un cambio de etiqueta.
        {**_cambio_de_etiqueta(["automation_paused"], []), "event": "message_created"},
        # Forma rota: falla cerrado.
        {
            **_cambio_de_etiqueta(["automation_paused"], []),
            "changed_attributes": [
                {"label_list": {"previous_value": "automation_paused", "current_value": []}}
            ],
        },
        {**_cambio_de_etiqueta(["automation_paused"], []), "id": True},
        {**_cambio_de_etiqueta(["automation_paused"], []), "contact_inbox": "x"},
        "no es un objeto",
    ],
)
def test_anything_else_is_not_a_label_removal(payload: object) -> None:
    assert _clasificar(payload) is None


def test_an_event_from_another_account_is_ignored() -> None:
    def clasificar(payload: object):
        return classify_chatwoot_label_removal(
            payload,
            label="automation_paused",
            expected_inbox_id=9,
            expected_account_id=1,
            allowed_jid=NUMERO_DE_PRUEBA,
            allow_any_scoped_sender=True,
        )

    base = _cambio_de_etiqueta(["automation_paused"], [])
    assert clasificar({**base, "account_id": 2}) is None
    assert clasificar({**base, "account": {"id": 2}}) is None
    assert clasificar({**base, "account_id": 1}) is not None
    # Sin la cuenta en el payload alcanza el inbox (id global en Chatwoot).
    assert clasificar(base) is not None


def test_without_scoped_senders_only_the_allowed_identity_counts() -> None:
    payload = _cambio_de_etiqueta(["automation_paused"], [])
    # El lead no es el numero permitido: se ignora.
    assert _clasificar(payload, scoped=False) is None
    # Con el JID del lead permitido, cuenta y la identidad la verifica el
    # cliente contra ALLOWED_WHATSAPP_JID (expected_jid None), como hoy.
    assert _clasificar(payload, scoped=False, allowed=JID_DEL_LEAD_173) == LabelRemoval(
        conversation_id=173, sender_jid=None
    )


# ------------------------------------------------- la reanudacion, sin mensaje


def test_the_label_removal_path_never_measures_the_team_silence() -> None:
    """Si al leer la conversacion la etiqueta volvio, no se reanuda nada."""
    fixture = _fixture_173()
    fixture["conversation"] = {**fixture["conversation"], "labels": ["automation_paused"]}
    transport = _Chatwoot173(fixture)
    supabase = _SupabaseFalso()
    resumed = asyncio.run(
        _resume_paused_conversation(
            control_client=_cliente_173(transport),
            supabase=supabase,
            settings=_settings_resume(),
            conversation_id=173,
            expected_jid=JID_DEL_LEAD_173,
            durable_pause_recorded=True,
            command_key=CLAVE,
            label_removal_only=True,
        )
    )
    assert resumed is False
    assert supabase.llamadas == []
    assert transport.macro_executed is False
    assert not [
        r for r in transport.requests if r.url.path.endswith("/173/messages")
        and r.url.params.get("limit") == "100"
    ], "no se mide el silencio del equipo"


def test_another_contact_in_the_conversation_is_never_resumed() -> None:
    """La defensa principal: el snapshot se verifica contra el JID del evento."""
    transport = _Chatwoot173(_fixture_173())
    supabase = _SupabaseFalso()
    resumed = asyncio.run(
        _resume_paused_conversation(
            control_client=_cliente_173(transport),
            supabase=supabase,
            settings=_settings_resume(),
            conversation_id=173,
            expected_jid="5210000000999@s.whatsapp.net",
            durable_pause_recorded=True,
            command_key=CLAVE,
            label_removal_only=True,
        )
    )
    assert resumed is False
    assert supabase.llamadas == []


def test_a_supabase_failure_is_retried_instead_of_swallowed() -> None:
    from bridge.app import LabelRemovalResumeTransientError
    from bridge.supabase import SupabaseError

    class _SupabaseCaido:
        async def resume_paused_conversation(self, **_: object):
            raise SupabaseError("conversation_resume_failed: HTTP 503")

    with pytest.raises(LabelRemovalResumeTransientError):
        asyncio.run(
            _resume_paused_conversation(
                control_client=_cliente_173(_Chatwoot173(_fixture_173())),
                supabase=_SupabaseCaido(),
                settings=_settings_resume(),
                conversation_id=173,
                expected_jid=JID_DEL_LEAD_173,
                durable_pause_recorded=True,
                command_key=CLAVE,
                label_removal_only=True,
            )
        )
    # El camino de hoy (por mensaje) sigue tragandola: devuelve False.
    assert (
        asyncio.run(
            _resume_paused_conversation(
                control_client=_cliente_173(_Chatwoot173(_fixture_173())),
                supabase=_SupabaseCaido(),
                settings=_settings_resume(),
                conversation_id=173,
                message_id=2373,
                expected_jid=JID_DEL_LEAD_173,
                durable_pause_recorded=True,
            )
        )
        is False
    )


def test_without_a_message_or_a_key_nothing_is_resumed() -> None:
    supabase = _SupabaseFalso()
    resumed = asyncio.run(
        _resume_paused_conversation(
            control_client=_cliente_173(_Chatwoot173(_fixture_173())),
            supabase=supabase,
            settings=_settings_resume(),
            conversation_id=173,
            expected_jid=JID_DEL_LEAD_173,
            durable_pause_recorded=True,
        )
    )
    assert resumed is False
    assert supabase.llamadas == []


# ------------------------------------------------------------ el handler entero


class _SupabaseConPausa:
    def __init__(self, outcome: str = "resumed") -> None:
        self.outcome = outcome
        self.admisiones: list[dict[str, object]] = []
        self.reanudaciones: list[dict[str, object]] = []

    async def admit_inbound_commercial_case(self, **kwargs: object) -> object:
        self.admisiones.append(kwargs)
        raise AssertionError("un cambio de etiqueta no abre ni admite un caso")

    async def resume_paused_conversation(self, **kwargs: object):
        self.reanudaciones.append(kwargs)
        return ConversationResumeResult(
            outcome=self.outcome,
            conversation_id="conversation-173",
            commercial_case_id="case-173",
            resume_event_id="event-173",
        )

    async def has_chatwoot_opt_out_stop(self, **_: object) -> bool:
        return False


class _SombraNula:
    async def run(self, *, delivery_id: str, context: object) -> None:
        return None

    def record_failure(self, *, delivery_id: str, reason: str) -> None:
        return None

    def has_result(self, *, delivery_id: str) -> bool:
        return True

    def get_completed_proposal(self, *, delivery_id: str) -> None:
        return None


def _app(tmp_path, transport, supabase, **flags: object):
    base: dict[str, object] = dict(
        webhook_secret="webhook-secret",
        allowed_jid=NUMERO_DE_PRUEBA,
        capture_dir=tmp_path,
        max_age_seconds=300,
        agent_bot_id=1,
        chatwoot_account_id=1,
        chatwoot_inbox_id=9,
        chatwoot_scoped_inbound_senders_enabled=True,
        chatwoot_cut_b_admission_enabled=True,
        chatwoot_cut_b_scope_key="libre-de-ansiedad-inbound",
        chatwoot_cut_b_scope_version=2,
        chatwoot_cut_b_agent_enabled=True,
        automated_replies_enabled=True,
        chatwoot_durable_opt_out_enabled=True,
        chatwoot_human_pause_enabled=True,
        chatwoot_opt_out_macro_id=2,
        opt_out_projection_worker_id="opt-out-test",
        human_handoff_admission_enabled=True,
        human_handoff_projection_enabled=True,
        handoff_projection_policy_key="lancemos-inbound-handoff",
        handoff_projection_policy_version=1,
        human_handoff_projection_worker_id="handoff-projection-test",
        conversation_resume_enabled=True,
        chatwoot_resume_macro_id=3,
    )
    base.update(flags)
    return create_app(
        Settings(**base),  # type: ignore[arg-type]
        chatwoot_client=_cliente_173(transport),
        shadow_processor=_SombraNula(),  # type: ignore[arg-type]
        supabase_client=supabase,  # type: ignore[arg-type]
    )


def _enviar(app, payload: dict[str, object], entregas: tuple[str, ...] = (ENTREGA,)):
    async def exercise() -> list[httpx.Response]:
        responses = []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            for entrega in entregas:
                raw_body, headers = _webhook_firmado(
                    payload, timestamp=AHORA_173, delivery_id=entrega
                )
                responses.append(
                    await client.post(
                        "/webhooks/chatwoot", content=raw_body, headers=headers
                    )
                )
                await app.state.chatwoot_worker.run_once()
        return responses

    return asyncio.run(exercise())


def test_with_the_flag_off_a_label_removal_is_only_captured_as_today(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA_173)
    transport = _Chatwoot173(_fixture_173())
    supabase = _SupabaseConPausa()
    app = _app(tmp_path, transport, supabase)
    [response] = _enviar(app, _cambio_de_etiqueta(["automation_paused"], []))
    assert response.status_code == 202
    assert response.json() == {
        "status": "captured",
        "reason": "conversation_label_change",
        "delivery_id": ENTREGA,
    }
    assert supabase.reanudaciones == []
    assert transport.requests == []


def test_the_flag_needs_the_durable_admission_to_be_on(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA_173)
    transport = _Chatwoot173(_fixture_173())
    supabase = _SupabaseConPausa()
    app = _app(
        tmp_path,
        transport,
        supabase,
        chatwoot_label_removal_resume_enabled=True,
        chatwoot_cut_b_admission_enabled=False,
        chatwoot_cut_b_agent_enabled=False,
        chatwoot_scoped_inbound_senders_enabled=False,
        human_handoff_admission_enabled=False,
        human_handoff_projection_enabled=False,
    )
    [response] = _enviar(app, _cambio_de_etiqueta(["automation_paused"], []))
    assert response.json()["status"] == "captured"
    assert supabase.reanudaciones == []


def test_the_flag_needs_the_resume_itself_to_be_on(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA_173)
    transport = _Chatwoot173(_fixture_173())
    supabase = _SupabaseConPausa()
    app = _app(
        tmp_path,
        transport,
        supabase,
        chatwoot_label_removal_resume_enabled=True,
        conversation_resume_enabled=False,
    )
    [response] = _enviar(app, _cambio_de_etiqueta(["automation_paused"], []))
    assert response.json()["status"] == "captured"
    assert supabase.reanudaciones == []


def test_a_person_removing_the_label_resumes_the_setter_right_away(
    tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """La 173 con la etiqueta sacada: `operator_request`, sin macro, sin caso."""
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA_173)
    transport = _Chatwoot173(_fixture_173())
    supabase = _SupabaseConPausa()
    app = _app(
        tmp_path, transport, supabase, chatwoot_label_removal_resume_enabled=True
    )
    caplog.set_level(logging.INFO)
    primera, repetida = _enviar(
        app, _cambio_de_etiqueta(["automation_paused"], []), (ENTREGA, ENTREGA)
    )
    assert primera.status_code == 202
    assert primera.json() == {
        "status": "accepted",
        "reason": "conversation_label_removal",
        "delivery_id": ENTREGA,
    }
    assert repetida.status_code == 200
    assert repetida.json()["status"] == "duplicate"
    assert [r["command_key"] for r in supabase.reanudaciones] == [CLAVE]
    assert supabase.reanudaciones[0]["reason_code"] == "operator_request"
    assert supabase.reanudaciones[0]["external_conversation_id"] == 173
    assert supabase.reanudaciones[0]["quiet_seconds"] is None
    assert supabase.admisiones == []
    assert transport.macro_executed is False, "no hay etiqueta que sacar"
    assert not [m for m in caplog.messages if "chatwoot_work_failed" in m]
    assert any(
        "chatwoot_label_removal_resume conversation=173 resumed=True" in m
        for m in caplog.messages
    )


def test_the_rpc_still_decides_an_unattended_handoff_stays_paused(
    tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Hoy la RPC exige `attended_at` aun con `operator_request` (parte 1 de E-539).

    Este camino no la rodea: con la derivacion sin atender, la RPC dice
    `blocked_pending_handoff` y la conversacion sigue con las personas.
    """
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA_173)
    transport = _Chatwoot173(_fixture_173())
    supabase = _SupabaseConPausa(outcome="blocked_pending_handoff")
    app = _app(
        tmp_path, transport, supabase, chatwoot_label_removal_resume_enabled=True
    )
    caplog.set_level(logging.INFO)
    [response] = _enviar(app, _cambio_de_etiqueta(["automation_paused"], []))
    assert response.status_code == 202
    assert len(supabase.reanudaciones) == 1
    assert any("outcome=blocked_pending_handoff" in m for m in caplog.messages)
    assert any("resumed=False" in m for m in caplog.messages)


def test_the_bridge_echo_after_its_own_resume_changes_nothing(
    tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """El bridge saca la etiqueta tras reanudar por silencio y le llega el eco.

    La RPC ya esta admisible ('already_active'): una sola llamada, sin macro y
    sin otra reanudacion. No hay loop.
    """
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA_173)
    transport = _Chatwoot173(_fixture_173())
    supabase = _SupabaseConPausa(outcome="already_active")
    app = _app(
        tmp_path, transport, supabase, chatwoot_label_removal_resume_enabled=True
    )
    caplog.set_level(logging.INFO)
    [response] = _enviar(app, _cambio_de_etiqueta(["automation_paused"], []))
    assert response.status_code == 202
    assert len(supabase.reanudaciones) == 1
    assert transport.macro_executed is False
    assert supabase.admisiones == []


def test_a_failed_resume_is_retried_by_the_worker(
    tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    from bridge.supabase import SupabaseError

    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA_173)
    transport = _Chatwoot173(_fixture_173())
    supabase = _SupabaseConPausa()

    async def caido(**kwargs: object):
        supabase.reanudaciones.append(kwargs)
        raise SupabaseError("conversation_resume_failed: HTTP 503")

    supabase.resume_paused_conversation = caido  # type: ignore[method-assign]
    app = _app(
        tmp_path, transport, supabase, chatwoot_label_removal_resume_enabled=True
    )
    caplog.set_level(logging.WARNING)
    [response] = _enviar(app, _cambio_de_etiqueta(["automation_paused"], []))
    assert response.status_code == 202
    assert len(supabase.reanudaciones) == 1
    fallos = [m for m in caplog.messages if "chatwoot_work_failed" in m]
    assert fallos and "LabelRemovalResumeTransientError" in fallos[0]
    # Queda admitida para el proximo intento (tope de 8), no completada.
    assert "status=admitted" in fallos[0]


def test_a_resolved_conversation_is_not_resumed(tmp_path, monkeypatch) -> None:
    """Las reglas de hoy: solo una conversacion abierta vuelve al agente (conv 260)."""
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA_173)
    fixture = _fixture_173()
    fixture["conversation"] = {**fixture["conversation"], "status": "resolved"}
    transport = _Chatwoot173(fixture)
    supabase = _SupabaseConPausa()
    app = _app(
        tmp_path, transport, supabase, chatwoot_label_removal_resume_enabled=True
    )
    [response] = _enviar(app, _cambio_de_etiqueta(["automation_paused"], []))
    assert response.status_code == 202
    assert supabase.reanudaciones == []


def test_a_label_being_added_keeps_todays_capture(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA_173)
    transport = _Chatwoot173(_fixture_173())
    supabase = _SupabaseConPausa()
    app = _app(
        tmp_path, transport, supabase, chatwoot_label_removal_resume_enabled=True
    )
    [response] = _enviar(app, _cambio_de_etiqueta([], ["automation_paused"]))
    assert response.json()["status"] == "captured"
    assert supabase.reanudaciones == []
    assert transport.requests == []


# ------------------------------------------------------------------ el arranque


def test_the_flag_is_off_by_default_and_portable(monkeypatch: pytest.MonkeyPatch) -> None:
    field = Settings.__dataclass_fields__["chatwoot_label_removal_resume_enabled"]
    assert field.default is False
    assert (
        "chatwoot_label_removal_resume_enabled"
        in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    )
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
    monkeypatch.delenv("CHATWOOT_LABEL_REMOVAL_RESUME_ENABLED", raising=False)
    assert Settings.from_env().chatwoot_label_removal_resume_enabled is False
    monkeypatch.setenv("CHATWOOT_LABEL_REMOVAL_RESUME_ENABLED", " TRUE ")
    assert Settings.from_env().chatwoot_label_removal_resume_enabled is True


