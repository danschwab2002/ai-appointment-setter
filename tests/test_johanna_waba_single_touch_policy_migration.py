"""Contract tests for Johanna's automatic WABA single-touch policy."""

from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260825000400_johanna_waba_single_touch_policy.sql"
)


def test_policy_v2_is_published_as_one_approved_template() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "'johanna-abandonment-single-touch-e2e'" in sql
    assert "2" in sql
    assert "'published'" in sql
    assert "'cart_recovery'" in sql
    assert "max_automatic_messages," in sql
    assert "1," in sql
    assert '\"mode\":\"approved_template\"' in sql
    assert '\"step_key\":\"first_contact\"' in sql
    assert "freeform" not in sql


def test_policy_v2_does_not_modify_or_retire_v1() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "insert into public.followup_policy_versions" in sql
    assert "update public.followup_policy_versions" not in sql
    assert "delete from public.followup_policy_versions" not in sql


def test_scope_v2_pins_policy_v2_and_preserves_single_contact_budget() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "insert into public.pilot_scope_versions" in sql
    assert "'johanna-abandonment-template-e2e'" in sql
    assert "'johanna-abandonment-single-touch-e2e'" in sql
    assert "'purchase_out_of_shopping_cart'" in sql
    assert "'8104005'" in sql
    assert "'bxjge6zq'" in sql
    assert "'chatwoot-inbox:9'" in sql
    assert "values (\n    'johanna-abandonment-template-e2e', 2" in sql
    assert "1, 1, 1" in sql
