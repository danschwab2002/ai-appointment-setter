from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    PROJECT_ROOT
    / "supabase"
    / "migrations"
    / "20260901000100_commercial_ally_portability.sql"
)
SCHEMA_INVENTORY = PROJECT_ROOT / "scripts" / "supabase_schema_inventory.sql"
ACL_VALIDATOR = (
    PROJECT_ROOT / "tests" / "sql" / "followup_engine" / "validate_acl_hardening.mjs"
)


def test_portability_migration_creates_fail_closed_versioned_binding() -> None:
    sql = MIGRATION.read_text().lower()

    assert "create table public.commercial_ally_runtime_bindings" in sql
    assert "primary key (tenant_ref, funnel_ref, binding_version)" in sql
    assert "where status = 'active'" in sql
    assert "enable row level security" in sql
    assert "revoke all on table public.commercial_ally_runtime_bindings" in sql
    assert "grant select on table public.commercial_ally_runtime_bindings to service_role" in sql


def test_portability_binding_revokes_default_service_role_dml_and_inventories_it() -> None:
    migration = MIGRATION.read_text().lower()
    inventory = SCHEMA_INVENTORY.read_text().lower()
    fingerprint = inventory.split("'20260901000100'", 1)[1].split(")\nselect", 1)[0]

    assert (
        "revoke all on table public.commercial_ally_runtime_bindings from service_role"
        in migration
    )
    for privilege in ("insert", "update", "delete", "truncate", "references", "trigger"):
        assert f"'{privilege}'" in fingerprint
    assert "to_regclass('public.commercial_ally_runtime_bindings') is not null" in fingerprint
    assert "to_regprocedure(" in fingerprint
    assert ") is not null" in fingerprint
    assert "not has_table_privilege(" in fingerprint
    assert "proconfig @> array['search_path=\"\"']" in fingerprint


def test_canonical_acl_validator_includes_the_portable_binding_reader() -> None:
    validator = ACL_VALIDATOR.read_text()

    assert "resolve_commercial_ally_runtime_binding(text,text,integer)" in validator
    assert "result.expected_count !== 55" in validator


def test_portability_migration_exposes_only_active_exact_binding_resolution() -> None:
    sql = MIGRATION.read_text().lower()

    assert "function public.resolve_commercial_ally_runtime_binding" in sql
    assert "p_tenant_ref text" in sql
    assert "p_funnel_ref text" in sql
    assert "p_binding_version integer" in sql
    assert "b.status = 'active'" in sql
    assert "revoke all on function public.resolve_commercial_ally_runtime_binding" in sql
    assert "grant execute on function public.resolve_commercial_ally_runtime_binding" in sql
    assert "to service_role" in sql


def test_portability_migration_does_not_seed_customer_specific_state() -> None:
    sql = MIGRATION.read_text().lower()

    for forbidden in (
        "johanna",
        "psicologajohanna",
        "libre de ansiedad",
        "bxjge6zq",
        "8104005",
        "chatwoot-inbox:9",
    ):
        assert forbidden not in sql
    assert "insert into public.commercial_ally_runtime_bindings" not in sql
