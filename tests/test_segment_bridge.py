"""Segmentos por etiqueta en el bridge (E-311, PR-A): la entrada, el pedido, /ready y el arranque.

Diseno E-310 (defaults D1 a D15); contrato ``docs/contracts/segment-knowledge-v1.md``.

Datos: el webhook es la captura de la conversacion 200 del inbox 9 con
``content`` reemplazado (``_text_webhook`` de ``test_whatsapp_inbound_equivalence``),
y la etiqueta va en ``conversation.labels``, donde la manda Chatwoot (captura
``chatwoot_message_created_inbox_9_conv_158_20260923.json``, la misma que usa
``customer_status``). DEUDA: no hay una captura de un webhook con una etiqueta
de campana puesta por un macro; la forma de ``conversation.labels`` es la de esa
captura y la de ``cliente``.

El segmento es el FICTICIO de ``tests/fixtures/segments``; en el camino del
webhook la vigencia se reescribe a 2000-2999 (o al pasado / al futuro) para que
ningun test dependa del reloj.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from test_webhook import StubChatwootClient, StubShadowProcessor, _post, _signed_headers
from test_whatsapp_inbound_equivalence import (
    FORM_ID,
    SECRET,
    _johanna_settings,
    _manifest_settings,
    _Supabase,
    _text_webhook,
)

from bridge import app as app_module
from bridge.app import Settings, create_app
from bridge.hermes import HermesShadowProcessor
from bridge.instance_manifest import InstanceManifest
from bridge.segment_knowledge import SEGMENT_CONTEXT_KEY, load_segment_catalog

SEGMENT = Path(__file__).parent / "fixtures" / "segments" / "campana-de-prueba-v1.toml"
LABEL = "campana-de-prueba"
ALWAYS = ("2000-01-01T00:00:00-06:00", "2999-01-01T00:00:00-06:00")
PAST = ("2000-01-01T00:00:00-06:00", "2001-01-01T00:00:00-06:00")
FUTURE = ("2998-01-01T00:00:00-06:00", "2999-01-01T00:00:00-06:00")
DECLARATION = f"""
[[agente.segmentos]]
clave = "{LABEL}"
etiqueta = "{LABEL}"
conocimiento = "conocimiento/segmentos/{SEGMENT.name}"
prioridad = 10
"""


class _SegmentShadow(StubShadowProcessor):
    """El doble de siempre, que ademas guarda los `system` extra de cada turno."""

    def __init__(self, proposal: dict[str, object] | None = None) -> None:
        super().__init__(proposal)
        self.extra: list[tuple[Any, ...]] = []

    async def run(self, *, delivery_id: str, context: dict[str, object], **kwargs: Any) -> None:
        self.extra.append(tuple(kwargs.get("extra_system", ())))
        await super().run(delivery_id=delivery_id, context=context)


def _settings(tmp_path: Path, *, window: tuple[str, str] = ALWAYS, enabled: bool = True) -> Settings:
    base = _manifest_settings(tmp_path)
    instance = tmp_path / "instancia"
    folder = instance / "conocimiento" / "segmentos"
    folder.mkdir()
    text = SEGMENT.read_text(encoding="utf-8")
    text = text.replace("desde = 2026-11-20T00:00:00-06:00", f"desde = {window[0]}")
    text = text.replace("hasta = 2026-12-01T00:00:00-06:00", f"hasta = {window[1]}")
    (folder / SEGMENT.name).write_text(text, encoding="utf-8")
    manifest_path = instance / "instancia.toml"
    anchor = 'conocimiento = "conocimiento/knowledge-v1.toml"\n'
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(anchor, anchor + DECLARATION), encoding="utf-8"
    )
    assert base.instance_manifest is not None and base.commercial_knowledge is not None
    # Lo de _manifest_settings (inbound prendido, cuenta 1, inbox 9) con la declaracion nueva.
    manifest = replace(
        base.instance_manifest,
        agent_segments=InstanceManifest.from_toml_file(manifest_path).agent_segments,
    )
    return replace(
        base,
        instance_manifest=manifest,
        agent_segments_enabled=enabled,
        agent_segment_catalog=(
            load_segment_catalog(manifest, instance, base.commercial_knowledge) if enabled else None
        ),
    )


class _ReadySupabase(_Supabase):
    """La base falsa de la equivalencia, con el binding que /ready pregunta."""

    async def resolve_commercial_ally_runtime_binding(self, config: object) -> object:
        return config

    async def get_human_handoff_projection_status(self) -> object:
        return SimpleNamespace(pending_count=0, retryable_count=0, delivery_unknown_count=0,
                               conflict_count=0, dead_letter_count=0)


def _agent_reply_before(message: dict[str, Any]) -> dict[str, Any]:
    """Una respuesta del agente anterior al mensaje: la misma forma, del AgentBot 1."""

    reply = copy.deepcopy(message)
    reply["id"] = message["id"] - 1
    reply["created_at"] = message["created_at"] - 60
    reply["message_type"] = 1
    reply["content"] = "Hola, te leo."
    reply["sender"] = {"id": 1, "type": "agent_bot", "name": "Agente"}
    reply["sender_type"] = "AgentBot"
    reply["sender_id"] = 1
    return reply


def _turn(
    tmp_path: Path,
    *,
    settings: Settings,
    labels: list[str] | None,
    shadow: Any = None,
    agent_spoke_before: bool = False,
) -> tuple[_SegmentShadow, Any]:
    """Un mensaje de la persona por el webhook, como ``_process`` de la equivalencia."""

    webhook, history = _text_webhook("¿Qué precio tiene ahora?")
    if agent_spoke_before:
        history = [_agent_reply_before(history[0]), *history]
    if labels is not None:
        webhook["conversation"]["labels"] = labels
    shadow = shadow or _SegmentShadow({"decision": "reply", "reply": "Te leemos."})
    app = create_app(
        settings,
        chatwoot_client=StubChatwootClient(messages=history),
        shadow_processor=shadow,
        supabase_client=_ReadySupabase(identities=(FORM_ID,)),  # type: ignore[arg-type]
    )
    raw_body = json.dumps(webhook, separators=(",", ":")).encode("utf-8")
    response = _post(app, raw_body, _signed_headers(raw_body, secret=SECRET, delivery="inbound-200"))
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())
    return shadow, app


# ------------------------------------------------------------------ la entrada y el pedido


def test_a_labeled_conversation_in_force_gets_the_segment(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    settings = _settings(tmp_path)
    caplog.set_level(logging.INFO, logger="bridge")

    shadow, _ = _turn(tmp_path, settings=settings, labels=[LABEL, "otra"])

    [(_, context)] = shadow.calls
    assert context[SEGMENT_CONTEXT_KEY] == [{"key": LABEL, "version": 1}]
    [blocks] = shadow.extra
    assert [block.key for block in blocks] == [LABEL]
    assert blocks[0].rendered.startswith("# Segmento: Campaña de prueba (vigente hasta el 31/12 23:59")
    [line] = [record.getMessage() for record in caplog.records if record.getMessage().startswith("segment_context")]
    assert line.startswith(f"segment_context key={LABEL} version=1 sha256=") and line.endswith("conversation=200")


def test_a_segment_on_the_first_turn_is_not_late(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    shadow, _ = _turn(tmp_path, settings=_settings(tmp_path), labels=[LABEL])

    [blocks] = shadow.extra
    assert blocks[0].agent_replies_before == 0 and blocks[0].activated_late is False
    assert not any(r.getMessage().startswith("segment_after_agent_replies") for r in caplog.records)


def test_a_label_that_arrives_after_the_agent_spoke_is_measured(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Dan, 03/11: la etiqueta va antes del primer turno; si no, queda medido."""

    shadow, _ = _turn(tmp_path, settings=_settings(tmp_path), labels=[LABEL], agent_spoke_before=True)

    [(_, context)] = shadow.calls
    assert [m["actor"] for m in context["messages"]] == ["assistant", "prospect"]  # type: ignore[index]
    [blocks] = shadow.extra
    assert blocks[0].agent_replies_before == 1 and blocks[0].activated_late is True
    assert (
        f"segment_after_agent_replies key={LABEL} version=1 agent_replies_before=1 conversation=200"
        in [record.getMessage() for record in caplog.records]
    )
    # La entrada del agente no cambia por esto: el dato va solo a la procedencia.
    assert context[SEGMENT_CONTEXT_KEY] == [{"key": LABEL, "version": 1}]


def test_without_the_label_the_turn_is_as_before(tmp_path: Path) -> None:
    shadow, _ = _turn(tmp_path, settings=_settings(tmp_path), labels=["otra"])

    [(_, context)] = shadow.calls
    assert SEGMENT_CONTEXT_KEY not in context
    assert shadow.extra == [()]  # ni siquiera se pasa extra_system


def test_with_the_flag_off_the_label_does_nothing(tmp_path: Path) -> None:
    shadow, _ = _turn(tmp_path, settings=_settings(tmp_path, enabled=False), labels=[LABEL])

    [(_, context)] = shadow.calls
    assert SEGMENT_CONTEXT_KEY not in context and shadow.extra == [()]


@pytest.mark.parametrize(("window", "reason"), [(PAST, "expired"), (FUTURE, "not_started")])
def test_out_of_its_window_the_block_does_not_go(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, window: tuple[str, str], reason: str
) -> None:
    caplog.set_level(logging.INFO, logger="bridge")

    shadow, _ = _turn(tmp_path, settings=_settings(tmp_path, window=window), labels=[LABEL])

    [(_, context)] = shadow.calls
    assert SEGMENT_CONTEXT_KEY not in context and shadow.extra == [()]
    assert f"segment_inactive key={LABEL} version=1 reason={reason} conversation=200" in [
        record.getMessage() for record in caplog.records
    ]


def test_the_real_request_to_hermes_carries_base_then_segment_then_context(tmp_path: Path) -> None:
    """El pedido HTTP que arma el procesador real, desde el webhook."""

    settings = _settings(tmp_path)
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(503, json={"detail": "unavailable"})

    assert settings.commercial_knowledge is not None and settings.agent_segment_catalog is not None
    real = HermesShadowProcessor(
        base_url="https://hermes.example.test/v1",
        api_key="test-hermes-key",
        model_name=settings.hermes_model_name,
        shadow_dir=tmp_path / "shadow",
        transport=httpx.MockTransport(handler),
        system_prompt=settings.commercial_knowledge.render(),
    )

    class _Through(_SegmentShadow):
        async def run(self, *, delivery_id: str, context: dict[str, object], **kwargs: Any) -> None:
            await real.run(delivery_id=delivery_id, context=context, **kwargs)
            await super().run(delivery_id=delivery_id, context=context, **kwargs)

    _turn(tmp_path, settings=settings, labels=[LABEL], shadow=_Through({"decision": "reply", "reply": "Te leemos."}))

    [body] = requests
    roles = [message["role"] for message in body["messages"]]
    assert roles == ["system", "system", "user"]
    assert body["messages"][0]["content"] == settings.commercial_knowledge.render()
    assert body["messages"][1]["content"] == settings.agent_segment_catalog.segments[0].rendered
    assert json.loads(body["messages"][2]["content"])[SEGMENT_CONTEXT_KEY] == [{"key": LABEL, "version": 1}]


# ------------------------------------------------------------------ /ready


def _ready(app: Any) -> httpx.Response:
    async def get() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/ready")

    return asyncio.run(get())


def test_ready_shows_each_segment_with_its_state(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert settings.agent_segment_catalog is not None
    [segment] = settings.agent_segment_catalog.segments

    _, app = _turn(tmp_path, settings=settings, labels=None)
    response = _ready(app)

    assert response.status_code == 200
    assert response.json()["agent_segments"] == f"{LABEL}:v1:{segment.sha256}:vigente"


def test_ready_without_the_flag_has_no_segment_key(tmp_path: Path) -> None:
    _, app = _turn(tmp_path, settings=_settings(tmp_path, enabled=False), labels=None)

    assert "agent_segments" not in _ready(app).json()


# ------------------------------------------------------------------ el arranque


def test_the_flag_is_off_by_default_and_portable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings.__dataclass_fields__["agent_segments_enabled"].default is False
    assert "agent_segments_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
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
    monkeypatch.delenv("AGENT_SEGMENTS_ENABLED", raising=False)
    settings = Settings.from_env()
    assert settings.agent_segments_enabled is False and settings.agent_segment_catalog is None
    monkeypatch.setenv("AGENT_SEGMENTS_ENABLED", "yes")
    assert Settings.from_env().agent_segments_enabled is False
    monkeypatch.setenv("AGENT_SEGMENTS_ENABLED", " TRUE ")
    with pytest.raises(ValueError, match="requires INSTANCE_MANIFEST_PATH and COMMERCIAL_KNOWLEDGE_ENABLED"):
        Settings.from_env()


def test_without_a_manifest_it_does_not_start(tmp_path: Path) -> None:
    settings = replace(_johanna_settings(tmp_path), agent_segments_enabled=True)
    with pytest.raises(ValueError, match="AGENT_SEGMENTS_ENABLED requires an instance manifest"):
        create_app(settings)


def test_without_the_catalog_or_with_a_stray_one_it_does_not_start(tmp_path: Path) -> None:
    on = _settings(tmp_path)
    with pytest.raises(ValueError, match="requires the segment catalog"):
        create_app(replace(on, agent_segment_catalog=None))
    with pytest.raises(ValueError, match="the segment catalog requires AGENT_SEGMENTS_ENABLED"):
        create_app(replace(on, agent_segments_enabled=False))
    # Sin conocimiento base frenan antes otras compuertas; la de los segmentos tambien lo pide.
    with pytest.raises(ValueError, match="requires COMMERCIAL_KNOWLEDGE_ENABLED"):
        app_module._validate_agent_segments(replace(on, commercial_knowledge=None))


def test_a_catalog_measured_against_another_base_does_not_start(tmp_path: Path) -> None:
    on = _settings(tmp_path)
    assert on.agent_segment_catalog is not None
    with pytest.raises(ValueError, match="another knowledge"):
        create_app(replace(on, agent_segment_catalog=replace(on.agent_segment_catalog, base_bytes=1)))


def test_from_env_loads_the_catalog_of_the_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    on = _settings(tmp_path)
    instance = tmp_path / "instancia"
    for name, value in {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        # La cuenta y el inbox del manifiesto de prueba (en disco: 2 y 11).
        "CHATWOOT_ACCOUNT_ID": "2",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "CHATWOOT_INBOX_ID": "11",
        "INSTANCE_MANIFEST_PATH": str(instance / "instancia.toml"),
        "COMMERCIAL_KNOWLEDGE_ENABLED": "true",
        "AGENT_SEGMENTS_ENABLED": "true",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ALLOWED_WHATSAPP_JID", raising=False)

    settings = Settings.from_env()

    assert settings.agent_segment_catalog is not None and on.agent_segment_catalog is not None
    assert [s.sha256 for s in settings.agent_segment_catalog.segments] == [
        s.sha256 for s in on.agent_segment_catalog.segments
    ]
    # Un segmento en borrador no deja arrancar con el flag prendido.
    path = instance / "conocimiento" / "segmentos" / SEGMENT.name
    path.write_text(path.read_text(encoding="utf-8").replace('estado = "aprobado"', 'estado = "borrador"'),
                    encoding="utf-8")
    with pytest.raises(ValueError, match="borrador"):
        Settings.from_env()
    shutil.rmtree(instance / "conocimiento" / "segmentos")
    with pytest.raises(ValueError, match="no se pudo leer"):
        Settings.from_env()
