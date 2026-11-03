"""El cableado de E-316 en el bridge (segment-labels-v1): los flags y el turno.

El webhook es el entrante capturado de ``test_whatsapp_inbound_equivalence.py``
(la conversacion 200), por los dobles de ``test_customer_status.py``. La herencia
real necesita los segmentos de PR-A (``[[agente.segmentos]]`` y
``AGENT_SEGMENTS_ENABLED``), que todavia no estan en ``main``: aca va un doble
de ``SegmentLabelInheritance`` que registra el payload que recibe y devuelve
otro, para probar que lo que devuelve es el payload con el que sigue el turno.
Para verlo desde afuera, el doble le suma la etiqueta ``cliente`` (la que lee
``customer_status``); la herencia real nunca hereda ``cliente``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_customer_status import _ask_for_the_link, _Customer, _settings
from test_whatsapp_inbound_equivalence import FORM_ID, StubChatwootClient, _johanna_settings

from bridge import app as app_module
from bridge.app import Settings, create_app
from bridge.segment_labels import SegmentLabelsSetup, with_labels

PRODUCT = "Alimenta tu Tiroides"


class _SpyInheritance:
    def __init__(self) -> None:
        self.calls: list[tuple[int, list[str]]] = []

    async def apply(self, payload: dict[str, Any], *, conversation_id: int) -> dict[str, Any]:
        self.calls.append((conversation_id, list(payload["conversation"]["labels"])))
        return with_labels(payload, ["cliente"])


def test_the_turn_goes_on_with_the_payload_the_inheritance_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _SpyInheritance()
    monkeypatch.setattr(app_module, "_segment_labels_setup",
                        lambda settings, client: SegmentLabelsSetup(inheritance=spy))  # type: ignore[arg-type]
    supabase = _Customer(identities=(FORM_ID,))

    _, shadow, _ = _ask_for_the_link(tmp_path, supabase, labels=[])

    assert spy.calls == [(200, [])]
    [(_, context)] = shadow.calls
    assert context["customer_status"] == [{"product": PRODUCT, "status": "cliente", "source": "etiqueta"}]


def test_without_the_flags_the_turn_is_the_one_of_today(tmp_path: Path) -> None:
    supabase = _Customer(identities=(FORM_ID,))

    _, shadow, _ = _ask_for_the_link(tmp_path, supabase, labels=[])

    [(_, context)] = shadow.calls
    assert context.get("customer_status") in (None, [])


def test_list_flows_has_no_segments_without_the_flag() -> None:
    from test_automation_expert_native_reads import _flows_body, _reader

    body = _flows_body(_reader())

    assert body["contract_version"] == "1.5.0"
    assert "segments" not in body["data"] and "allowed_labels" not in body["data"]


def test_list_flows_brings_the_segments_with_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    from test_automation_expert_native_reads import _reader
    from test_automation_expert_read_app import AUTH, _client

    from bridge.product_label import ProductLabeler
    from bridge.segment_labels import NativeSegmentLabels, SegmentLabel

    segment = SegmentLabel(key="black-november", label="black-november", macro_id=41,
                           valid_from=datetime(2026, 11, 20, 6, tzinfo=UTC),
                           valid_until=datetime(2099, 12, 1, 6, tzinfo=UTC))
    native = NativeSegmentLabels(segments=[segment], labelers={
        "black-november": ProductLabeler(client=object(), label="black-november", macro_id=41)})  # type: ignore[arg-type]
    monkeypatch.setattr(app_module, "_segment_labels_setup",
                        lambda settings, client: SegmentLabelsSetup(segments=(segment,), native=native))
    client = _client(_reader())

    for path in ("/internal/automation-expert/v1/flows", "/internal/automation-expert/v1/flows/carrito"):
        response = client.get(path, headers=AUTH)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["allowed_labels"] == ["cliente", "black-november"]
        [item] = data["segments"]
        assert item["key"] == "black-november" and item["labelable"] is True
        assert item["labeled_by"] == []


@pytest.mark.parametrize(
    ("field", "env"),
    [
        ("flow_engine_segment_labels_enabled", "FLOW_ENGINE_SEGMENT_LABELS_ENABLED"),
        ("segment_label_inheritance_enabled", "SEGMENT_LABEL_INHERITANCE_ENABLED"),
    ],
)
def test_the_flags_are_off_by_default_and_portable(
    monkeypatch: pytest.MonkeyPatch, field: str, env: str
) -> None:
    assert Settings.__dataclass_fields__[field].default is False
    assert field in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
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
    monkeypatch.delenv(env, raising=False)
    assert getattr(Settings.from_env(), field) is False
    monkeypatch.setenv(env, "yes")
    assert getattr(Settings.from_env(), field) is False
    monkeypatch.setenv(env, " TRUE ")
    assert getattr(Settings.from_env(), field) is True


def test_without_a_manifest_the_flags_do_not_start(tmp_path: Path) -> None:
    for field in ("flow_engine_segment_labels_enabled", "segment_label_inheritance_enabled"):
        settings = replace(_johanna_settings(tmp_path), **{field: True})
        with pytest.raises(ValueError, match="requires an instance manifest"):
            create_app(settings)


def test_with_a_manifest_without_segments_the_flag_does_not_start(tmp_path: Path) -> None:
    # main todavia no tiene [[agente.segmentos]] (PR-A): prenderlo no puede quedar mudo.
    settings = replace(_settings(tmp_path), flow_engine_segment_labels_enabled=True)
    with pytest.raises(ValueError, match=r"requires \[\[agente.segmentos\]\]"):
        create_app(settings, chatwoot_client=StubChatwootClient(messages=[]))
