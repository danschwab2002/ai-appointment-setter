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
            "discount": "operator_reported_marcela_approved_pending_template_and_runtime_support",
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
    assert intake["discount_policy_candidate"] == {
        "status": "operator_reported_marcela_approved_pending_template_and_runtime_support",
        "evidence_ref": "operator-confirmation:2026-09-02-discount",
        "approval_evidence_ref": (
            "operator-confirmation:2026-09-02-discount-approver-marcela"
        ),
        "approval_evidence_kind": "operator_reported",
        "discount_kind": "percentage",
        "discount_value": 10,
        "currency": None,
        "trigger_kinds": [
            "payment_failure",
            "confirmed_cart_abandonment",
            "precheckout_without_purchase_signal",
        ],
        "requires_inbound_reply_after_initial_meta_conversation_start_template": True,
        "presentation_stage": "later_step",
        "coupon_delivery": "meta_template_variable",
        "coupon_reference": None,
        "offer_expiration": "none",
        "urgency_copy_allowed": False,
        "country_restrictions": [],
        "currency_restrictions": [],
        "template_copy_status": "pending_external_approval",
        "approved_by_ref": "marcela",
        "runtime_policy_status": "unpublished",
        "runtime_schema_compatibility": (
            "blocked_offer_valid_for_requires_finite_interval"
        ),
    }
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
    assert "materiales y descuento permanecen abiertos" not in text
    assert "la decisión comercial del descuento ya no está abierta" in text
    assert "Descuento: porcentaje/importe" not in text


def test_discount_document_records_decision_without_publishing_policy() -> None:
    text = (ROOT / "docs" / "design" / "att1-discount-recovery-sequences.md").read_text(
        encoding="utf-8"
    )

    for decision in (
        "10 %",
        "payment_failure",
        "confirmed_cart_abandonment",
        "precheckout_without_purchase_signal",
        "later_step",
        "respuesta inbound posterior a la plantilla inicial de inicio de conversación de Meta",
        "variable de la plantilla de Meta",
        "no vence",
        "sin restricciones propias por país o moneda",
        "no se permite urgencia",
    ):
        assert decision in text
    assert "offer_valid_for" in text
    assert "incompatible" in text
    assert "ninguna política se publica" in text
    assert "texto final de la plantilla" in text
    assert "Marcela" in text
    assert "reportada por el operador" in text
    assert "identidad exacta del aprobador" not in text
    assert (
        "`payment_failure`, `confirmed_cart_abandonment` y "
        "`precheckout_without_purchase_signal`" in text
    )


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
