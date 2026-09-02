import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
INTAKE = ROOT / "config" / "commercial-allies" / "att1" / "intake-v1.json"


def _load() -> dict[str, Any]:
    return json.loads(INTAKE.read_text(encoding="utf-8"))


def test_att1_intake_preserves_received_business_scope_without_activation() -> None:
    intake = _load()

    assert intake["schema_version"] == 1
    assert intake["customer_ref"] == "att1"
    assert intake["status"] == "received_unverified"
    assert intake["ally_public_identity_reported"] == "Dra. Nina Garza"
    assert intake["offer"] == {
        "public_name": "Alimenta Tu Tiroides",
        "base_price": "47",
        "currency": "USD",
    }
    assert intake["success_outcome"] == "purchase_observed"
    assert intake["human_handoff"]["recipient_ref"] == "mariana-marin"
    assert intake["human_handoff"]["chatwoot_target_verified"] is False
    assert intake["human_handoff"]["schedule_and_sla_verified"] is False
    assert intake["materials_received_and_sanitized"] is False
    assert intake["canonical_provider_scope_verified"] is False
    assert intake["conversation_release_approved"] is False
    assert intake["activation_authorized"] is False


def test_att1_intake_preserves_audience_and_material_inventory_as_reported() -> None:
    intake = _load()
    audience = intake["audience"]

    assert audience["age_min"] == 35
    assert audience["age_max"] == 55
    assert audience["primary_gender"] == "women"
    assert audience["occupational_profiles"] == ["workers", "entrepreneurs"]
    assert audience["own_income_reported"] is True
    assert audience["reported_conditions"] == ["hypothyroidism", "hashimoto"]
    assert audience["market_share_percent"] == {
        "MX": 60,
        "US": 15,
        "CO": 10,
        "CA": 3,
        "ES": 3,
    }
    assert sum(audience["market_share_percent"].values()) == 91

    materials = intake["materials_reported_available"]
    assert materials == [
        "ally_history_and_authority",
        "offer_transformation_curriculum_price_and_order_bumps",
        "detailed_audience_profile",
        "copy_promise_pillars_and_core_messages",
        "web_pages",
        "ads",
        "upsell_and_post_offer",
    ]
    assert intake["materials_received_and_sanitized"] is False


def test_att1_intake_records_partial_commercial_confirmation_without_activation() -> None:
    intake = _load()

    assert intake["pilot_language"] == "spanish_latam_neutral"
    assert intake["pilot_countries"] == ["MX"]
    assert intake["commercial_approval_authority_ref"] == "marcela"
    assert intake["health_safety_baseline"] == {
        "policy_ref": "docs/design/att1-commercial-information-approval-v1.md#att1-commercial-003-health-limits--límites-sanitarios",
        "status": "operator_confirmed_pending_marcela_ratification",
    }
    assert intake["business_owner_ref"] is None
    assert intake["operational_owner_ref"] is None
    assert intake["canonical_provider_scope_verified"] is False
    assert intake["source_refs"] == [
        "operator-interview:2026-09-01-mariana-01",
        "operator-confirmation:2026-09-02-authority-content-health-language-countries",
    ]
    assert set(intake["open_questions"]) == {
        "business_owner_scope",
        "operational_owner_scope",
        "canonical_hotmart_product_and_offer",
        "chatwoot_handoff_target_and_sla",
        "materials_custody_sanitization_and_vigency",
        "discount_policy",
        "conversation_release_content_and_approval",
    }
    assert intake["conversation_release_approved"] is False
    assert intake["activation_authorized"] is False
