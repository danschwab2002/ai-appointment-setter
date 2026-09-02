import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INTAKE = ROOT / "config" / "commercial-allies" / "att1" / "intake-v1.json"
APPROVAL = ROOT / "docs" / "design" / "att1-commercial-information-approval-v1.md"
SOURCE_REGISTER = ROOT / "docs" / "design" / "att1-source-register-v1.md"
CONVERSATION_RELEASE = ROOT / "docs" / "design" / "att1-conversation-release-v1.md"


def test_commercial_information_gate_is_explicit_and_fail_closed() -> None:
    intake = json.loads(INTAKE.read_text(encoding="utf-8"))
    approval = intake["commercial_information_approval"]

    assert approval == {
        "artifact_ref": "docs/design/att1-commercial-information-approval-v1.md",
        "status": "pending_external_approval",
        "approval_authority_ref": "marcela",
        "decision_evidence_ref": "operator-confirmation:2026-09-02-authority-content-health-language-countries",
        "decisions": {
            "materials": "pending_external_input",
            "content": "operator_confirmed_pending_marcela_ratification",
            "health_limits": "operator_confirmed_pending_marcela_ratification",
            "language": "operator_confirmed_pending_marcela_ratification",
            "countries": "operator_confirmed_pending_marcela_ratification",
            "discount": "pending_external_approval",
        },
        "approved_by": None,
        "approved_at": None,
    }
    assert intake["materials_received_and_sanitized"] is False
    assert intake["pilot_language"] == "spanish_latam_neutral"
    assert intake["pilot_countries"] == ["MX"]
    assert intake["health_safety_baseline"]["status"] == (
        "operator_confirmed_pending_marcela_ratification"
    )
    assert intake["pilot_scope_status"] == (
        "operator_confirmed_pending_marcela_ratification"
    )
    assert intake["conversation_release_approved"] is False
    assert intake["activation_authorized"] is False


def test_approval_packet_covers_every_macro_two_decision_without_inference() -> None:
    text = APPROVAL.read_text(encoding="utf-8")

    for decision_id in (
        "att1-commercial-001-materials",
        "att1-commercial-002-content",
        "att1-commercial-003-health-limits",
        "att1-commercial-004-language",
        "att1-commercial-005-countries",
        "att1-commercial-006-discount",
    ):
        assert f"`{decision_id}`" in text

    assert "Estado: Pendiente de aprobación externa" in text
    assert "No se infiere el alcance geográfico" in text
    assert "no publicada" in text
    assert "no autoriza activación" in text


def test_discount_document_keeps_every_policy_field_pending() -> None:
    text = (ROOT / "docs" / "design" / "att1-discount-recovery-sequences.md").read_text(
        encoding="utf-8"
    )

    assert "Decisiones de política pendientes" in text
    for field in (
        "porcentaje o importe exacto",
        "triggers autorizados",
        "posición existente",
        "cupón o fuente canónica",
        "inicio y duración exacta",
        "países y monedas",
        "texto permitido sobre urgencia",
        "approver y vigencia",
    ):
        assert field in text
    assert "sólo necesita resolverse si" not in text


def test_derived_documents_reflect_partial_commercial_confirmation() -> None:
    source = SOURCE_REGISTER.read_text(encoding="utf-8")
    release = CONVERSATION_RELEASE.read_text(encoding="utf-8")

    assert "operador confirmó después" in source
    assert "pendiente de ratificación de Marcela" in source
    assert "pilot_language: spanish_latam_neutral" in release
    assert "pilot_countries: [MX]" in release
    assert "Marcela debe ratificarlo" in release
    assert "El outcome observable confirmado es" not in release
    assert (
        "outcome observable confirmado por el operador y pendiente de ratificación de Marcela"
        in release
    )
    assert "- [ ] ratificación comercial de país e idioma" in release
    assert "- [ ] ratificación comercial del baseline sanitario" in release
    assert "conversation_release_approved: false" in release
    assert "activation_authorized: false" in release


def test_legacy_johanna_materials_cannot_be_used_as_att1_sources() -> None:
    text = SOURCE_REGISTER.read_text(encoding="utf-8")

    assert "Fuentes deliberadamente excluidas" in text
    assert "onboarding de Johanna" in text
    assert "no constituyen evidencia de ATT1" in text
