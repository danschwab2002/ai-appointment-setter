from __future__ import annotations

import hashlib
import json
import stat
from decimal import Decimal
from pathlib import Path

import pytest

from bridge.app import Settings, create_app
from bridge.commercial_ally import CommercialAllyConfig
from bridge.messaging import FinalMetaEffect, FinalMetaEffectGate


def _effect(**overrides: object) -> FinalMetaEffect:
    values: dict[str, object] = {
        "delivery_id": "attempt-001",
        "action_kind": "first_touch",
        "mode": "approved_template",
        "target_phone": "+525500000001",
        "content": "Hola, este contenido no debe persistirse en claro.",
        "template_name": "att1_inicio_conversacion_v1",
        "template_language": "es_MX",
    }
    values.update(overrides)
    return FinalMetaEffect(**values)  # type: ignore[arg-type]


def _evidence_path(root: Path, delivery_id: str = "attempt-001") -> Path:
    digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
    return root / f"{digest}.json"


def test_closed_final_meta_gate_records_private_sanitized_evidence(
    tmp_path: Path,
) -> None:
    gate = FinalMetaEffectGate(enabled=False, evidence_dir=tmp_path / "effects")
    effect = _effect()

    assert gate.authorize(effect) is False

    path = _evidence_path(tmp_path / "effects")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": 1,
        "status": "final_meta_gate_closed",
        "action_kind": "first_touch",
        "mode": "approved_template",
        "delivery_id_sha256": hashlib.sha256(b"attempt-001").hexdigest(),
        "target_sha256": hashlib.sha256(b"+525500000001").hexdigest(),
        "content_sha256": hashlib.sha256(
            "Hola, este contenido no debe persistirse en claro.".encode("utf-8")
        ).hexdigest(),
        "template_name": "att1_inicio_conversacion_v1",
        "template_language": "es_MX",
    }
    serialized = path.read_text(encoding="utf-8")
    assert "+525500000001" not in serialized
    assert "Hola," not in serialized
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_closed_final_meta_gate_replay_is_idempotent(tmp_path: Path) -> None:
    gate = FinalMetaEffectGate(enabled=False, evidence_dir=tmp_path / "effects")
    effect = _effect()

    assert gate.authorize(effect) is False
    original = _evidence_path(tmp_path / "effects").read_bytes()
    assert gate.authorize(effect) is False

    assert _evidence_path(tmp_path / "effects").read_bytes() == original
    assert len(list((tmp_path / "effects").glob("*.json"))) == 1


def test_closed_final_meta_gate_rejects_conflicting_replay(tmp_path: Path) -> None:
    gate = FinalMetaEffectGate(enabled=False, evidence_dir=tmp_path / "effects")
    assert gate.authorize(_effect()) is False

    with pytest.raises(ValueError, match="final_meta_effect_conflict"):
        gate.authorize(_effect(content="Contenido diferente"))


def test_open_final_meta_gate_authorizes_without_writing_evidence(
    tmp_path: Path,
) -> None:
    evidence_dir = tmp_path / "effects"
    gate = FinalMetaEffectGate(enabled=True, evidence_dir=evidence_dir)

    assert gate.authorize(_effect()) is True
    assert not evidence_dir.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("delivery_id", ""),
        ("action_kind", "unknown"),
        ("mode", "freeform"),
        ("target_phone", "5255-invalid"),
        ("content", ""),
        ("template_name", ""),
        ("template_language", ""),
    ],
)
def test_final_meta_gate_rejects_malformed_effect(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    gate = FinalMetaEffectGate(enabled=False, evidence_dir=tmp_path / "effects")

    with pytest.raises(ValueError, match="invalid_final_meta_effect"):
        gate.authorize(_effect(**{field: value}))

    assert not (tmp_path / "effects").exists()


def _required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_meta_final_effect_is_declared_default_off(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    env_example = (project_root / ".env.example").read_text()
    compose = (project_root / "compose.yaml").read_text()

    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid="12025550123@s.whatsapp.net",
        capture_dir=tmp_path,
        max_age_seconds=300,
    )

    assert settings.meta_final_effect_enabled is False
    assert settings.meta_final_effect_evidence_dir == Path(
        "./data/meta-final-effect-gate"
    )
    assert "META_FINAL_EFFECT_ENABLED=false" in env_example
    assert "META_FINAL_EFFECT_ENABLED:" in compose
    assert "${META_FINAL_EFFECT_ENABLED:-false}" in compose
    assert "META_FINAL_EFFECT_EVIDENCE_DIR:" in compose


def test_meta_final_effect_configuration_is_parsed_strictly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _required_env(monkeypatch)
    monkeypatch.setenv("META_FINAL_EFFECT_ENABLED", "true")
    monkeypatch.setenv("META_FINAL_EFFECT_EVIDENCE_DIR", str(tmp_path))

    settings = Settings.from_env()

    assert settings.meta_final_effect_enabled is True
    assert settings.meta_final_effect_evidence_dir == tmp_path


def test_meta_final_effect_configuration_rejects_invalid_boolean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _required_env(monkeypatch)
    monkeypatch.setenv("META_FINAL_EFFECT_ENABLED", "yes")

    with pytest.raises(
        ValueError,
        match="META_FINAL_EFFECT_ENABLED must be true or false",
    ):
        Settings.from_env()


def test_att1_runtime_rejects_open_final_meta_effect_gate(tmp_path: Path) -> None:
    att1 = CommercialAllyConfig(
        tenant_ref="att1",
        funnel_ref="att1-main",
        binding_version=1,
        ally_ref="att1",
        lead_ally_name="Dra. Nina Garza",
        lead_site="raizana",
        lead_landing_id="inscribirme-alimenta-tu-tiroides",
        lead_page_host="raizana.com.mx",
        lead_page_path="/inscribirme-alimenta-tu-tiroides",
        product_hotlink="D98014973Y",
        product_name="Alimenta Tu Tiroides",
        product_price=Decimal("47"),
        currency="USD",
        offer_code="83utgyow",
        consent_copy_version="att1-whatsapp-consent-v1",
        hotmart_product_id=5071808,
        chatwoot_account_id=42,
        chatwoot_inbox_id=24,
        inbound_scope_key="att1-inbound",
        inbound_scope_version=1,
    )
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid=None,
        capture_dir=tmp_path / "captures",
        max_age_seconds=300,
        commercial_ally_config=att1,
        commercial_ally_manifest_path=tmp_path / "att1.json",
        meta_final_effect_enabled=True,
    )

    with pytest.raises(ValueError, match="ATT1 final Meta effect must remain disabled"):
        create_app(settings)
