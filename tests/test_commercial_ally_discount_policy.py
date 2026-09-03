import asyncio
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from bridge.supabase import (
    CommercialAllyDiscountPolicy,
    SupabaseClient,
    SupabaseError,
)


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "supabase/migrations/20260901000400_commercial_ally_discount_policies.sql"
)
INDEFINITE_MIGRATION = (
    ROOT
    / "supabase/migrations/20260903000200_commercial_ally_indefinite_discount.sql"
)
SCHEMA_INVENTORY = ROOT / "scripts/supabase_schema_inventory.sql"
PACKAGE = ROOT / "tests/sql/followup_engine/package.json"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_discount_policy_migration_is_the_versioned_default_off_boundary() -> None:
    sql = _sql()

    assert "create table public.commercial_ally_discount_policy_versions" in sql
    assert "foreign key (tenant_ref, funnel_ref, binding_version)" in sql
    assert "references public.commercial_ally_runtime_bindings" in sql
    assert "status in ('draft', 'approved', 'published', 'retired')" in sql
    assert "discount_kind in ('percentage', 'fixed_amount')" in sql
    assert "discount_kind = 'fixed_amount'" in sql
    assert "currency is not null" in sql
    assert "presentation_stage in ('first_touch', 'later_step')" in sql
    assert "where status = 'published'" in sql
    assert "insert into public.commercial_ally_discount_policy_versions" not in sql


def test_runtime_can_only_resolve_an_exact_published_discount_policy() -> None:
    sql = _sql()

    assert "create function public.resolve_commercial_ally_discount_policy" in sql
    assert "and b.status = 'active'" in sql
    assert "and p.status = 'published'" in sql
    assert "p.valid_from <= statement_timestamp()" in sql
    assert "p.valid_until is null or p.valid_until > statement_timestamp()" in sql
    assert "security definer" in sql
    assert "set search_path = ''" in sql


def test_service_role_has_no_discount_policy_dml_or_direct_table_read() -> None:
    sql = _sql()

    assert "revoke all on table public.commercial_ally_discount_policy_versions from service_role" in sql
    assert "grant select on table public.commercial_ally_discount_policy_versions to service_role" not in sql
    assert "grant execute on function public.resolve_commercial_ally_discount_policy" in sql
    for privilege in ("insert", "update", "delete", "truncate", "references", "trigger"):
        assert f"grant {privilege}" not in sql


def test_approved_discount_policy_versions_are_immutable_and_forward_only() -> None:
    sql = _sql()

    assert "create function public.guard_commercial_ally_discount_policy_version" in sql
    assert "commercial_ally_discount_policy_content_immutable" in sql
    assert "commercial_ally_discount_policy_approval_metadata_immutable" in sql
    assert "new.created_at is distinct from old.created_at" in sql
    assert "new.approved_at := statement_timestamp()" in sql
    assert "new.published_at := statement_timestamp()" in sql
    assert "old.published_at is distinct from new.published_at" in sql
    assert "old.status = 'approved' and new.status = 'published'" in sql
    assert "commercial_ally_discount_policy_status_transition_invalid" in sql
    assert "before insert or update or delete" in sql
    assert "old.status = 'draft' and new.status = 'approved'" in sql
    assert "old.status = 'approved' and new.status = 'published'" in sql
    assert "old.status in ('approved', 'published')" in sql
    assert "and new.status = 'retired'" in sql


def test_discount_policy_migration_is_in_release_and_sql_gates() -> None:
    inventory = SCHEMA_INVENTORY.read_text(encoding="utf-8")
    package = PACKAGE.read_text(encoding="utf-8")

    assert "20260901000400_commercial_ally_discount_policies.sql" in inventory
    assert "versioned_discount_policy_default_off" in inventory
    assert "validate_commercial_ally_discount_policies.mjs" in package


def test_forward_migration_represents_indefinite_offer_as_null() -> None:
    sql = INDEFINITE_MIGRATION.read_text(encoding="utf-8").lower()

    assert "alter column offer_valid_for drop not null" in sql
    assert "offer_valid_for is null" in sql
    assert "offer_valid_for > interval '0'" in sql
    assert "update public.commercial_ally_discount_policy_versions" not in sql
    assert "insert into public.commercial_ally_discount_policy_versions" not in sql


def test_forward_migration_adds_complete_transport_and_reply_semantics() -> None:
    sql = INDEFINITE_MIGRATION.read_text(encoding="utf-8").lower()

    for field in (
        "offer_expiration_mode",
        "requires_inbound_reply_after_initial_template",
        "coupon_delivery_mode",
        "urgency_copy_allowed",
        "channel_provider",
        "delivery_mode",
        "template_language",
        "template_category",
        "coupon_template_component",
        "coupon_template_parameter_index",
        "release_requires_exact_trigger_set",
    ):
        assert field in sql
    assert "coupon_delivery_mode = 'meta_template_variable'" in sql
    assert "delivery_mode = 'approved_template'" in sql
    assert "channel_provider = 'waba'" in sql


def test_forward_migration_supports_atomic_exact_three_trigger_release() -> None:
    sql = INDEFINITE_MIGRATION.read_text(encoding="utf-8").lower()

    assert "primary key" in sql
    assert "trigger_kind" in sql
    assert "deferrable initially deferred" in sql
    assert "payment_failure" in sql
    assert "confirmed_cart_abandonment" in sql
    assert "precheckout_without_purchase_signal" in sql
    assert "commercial_ally_discount_release_incomplete" in sql


def test_forward_migration_preserves_runtime_resolver_only_acl() -> None:
    sql = INDEFINITE_MIGRATION.read_text(encoding="utf-8").lower()

    assert "create function public.resolve_commercial_ally_discount_policy" in sql
    assert "security definer" in sql
    assert "set search_path = ''" in sql
    assert "revoke all on function public.resolve_commercial_ally_discount_policy" in sql
    assert "grant execute on function public.resolve_commercial_ally_discount_policy" in sql
    assert "grant select on table public.commercial_ally_discount_policy_versions" not in sql


def _resolved_policy_row() -> dict[str, object]:
    return {
        "policy_key": "att1-recovery-triplet",
        "policy_version": 1,
        "trigger_kind": "payment_failure",
        "discount_kind": "percentage",
        "discount_value": "10",
        "currency": None,
        "coupon_reference": "meta-variable",
        "offer_valid_for_seconds": None,
        "offer_expiration_mode": "indefinite",
        "presentation_stage": "later_step",
        "template_key": "att1_discount_later",
        "copy_version": "att1-discount-v1",
        "requires_inbound_reply_after_initial_template": True,
        "coupon_delivery_mode": "meta_template_variable",
        "urgency_copy_allowed": False,
        "channel_provider": "waba",
        "delivery_mode": "approved_template",
        "template_language": "es_MX",
        "template_category": "marketing",
        "coupon_template_component": "body",
        "coupon_template_parameter_index": 1,
        "valid_from": "2026-09-03T00:00:00+00:00",
        "valid_until": None,
        "release_requires_exact_trigger_set": True,
    }


def test_supabase_resolves_complete_typed_discount_policy() -> None:
    seen: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=[_resolved_policy_row()])

    client = SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="test-secret",
        transport=httpx.MockTransport(handler),
    )
    policy = asyncio.run(client.resolve_commercial_ally_discount_policy(
        tenant_ref="att1",
        funnel_ref="att1-main",
        binding_version=1,
        trigger_kind="payment_failure",
        expected_policy_key="att1-recovery-triplet",
        expected_policy_version=1,
    ))

    assert policy == CommercialAllyDiscountPolicy(
        **{**_resolved_policy_row(), "discount_value": Decimal("10")}
    )
    assert seen == [(
        "/rest/v1/rpc/resolve_commercial_ally_discount_policy",
        {
            "p_tenant_ref": "att1",
            "p_funnel_ref": "att1-main",
            "p_binding_version": 1,
            "p_trigger_kind": "payment_failure",
        },
    )]


@pytest.mark.parametrize("rows", [[], [_resolved_policy_row(), _resolved_policy_row()]])
def test_supabase_discount_policy_resolution_requires_exactly_one_row(
    rows: list[dict[str, object]],
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows)

    client = SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="test-secret",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SupabaseError, match="discount_policy_resolve_not_published"):
        asyncio.run(client.resolve_commercial_ally_discount_policy(
            tenant_ref="att1",
            funnel_ref="att1-main",
            binding_version=1,
            trigger_kind="payment_failure",
            expected_policy_key="att1-recovery-triplet",
            expected_policy_version=1,
        ))


def test_supabase_discount_policy_resolution_rejects_null_strict_transport() -> None:
    row = _resolved_policy_row()
    row["template_category"] = None
    row["coupon_template_component"] = None
    row["coupon_template_parameter_index"] = None

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[row])

    client = SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="test-secret",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SupabaseError, match="discount_policy_resolve_invalid_row"):
        asyncio.run(client.resolve_commercial_ally_discount_policy(
            tenant_ref="att1",
            funnel_ref="att1-main",
            binding_version=1,
            trigger_kind="payment_failure",
            expected_policy_key="att1-recovery-triplet",
            expected_policy_version=1,
        ))


def test_supabase_discount_policy_resolution_rejects_identity_drift() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_resolved_policy_row()])

    client = SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="test-secret",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SupabaseError, match="discount_policy_resolve_config_drift"):
        asyncio.run(client.resolve_commercial_ally_discount_policy(
            tenant_ref="att1",
            funnel_ref="att1-main",
            binding_version=1,
            trigger_kind="payment_failure",
            expected_policy_key="att1-recovery-triplet",
            expected_policy_version=2,
        ))


def test_supabase_discount_policy_resolution_rejects_trigger_drift() -> None:
    row = _resolved_policy_row()
    row["trigger_kind"] = "confirmed_cart_abandonment"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[row])

    client = SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="test-secret",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SupabaseError, match="discount_policy_resolve_config_drift"):
        asyncio.run(client.resolve_commercial_ally_discount_policy(
            tenant_ref="att1",
            funnel_ref="att1-main",
            binding_version=1,
            trigger_kind="payment_failure",
            expected_policy_key="att1-recovery-triplet",
            expected_policy_version=1,
        ))
