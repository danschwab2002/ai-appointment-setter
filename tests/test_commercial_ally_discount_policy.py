from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "supabase/migrations/20260901000400_commercial_ally_discount_policies.sql"
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
