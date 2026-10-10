"""Contract checks de la migracion del contexto identificado (ADR-0018).

El comportamiento de las RPC se prueba contra el esquema real en
``tests/sql/followup_engine/validate_daily_feedback_review_context.mjs``; esto
verifica lo estructural que ese validador no mira: que la migracion, los
inventarios de schema y ACL y el test de ACL cuenten la misma historia. Si
alguno queda afuera, el deploy se bloquea por un inventario desactualizado y
no por un bug real.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/20260927000100_daily_feedback_review_context_v2.sql"
ACL_INVENTORY = ROOT / "scripts/supabase_acl_inventory.sql"
SCHEMA_INVENTORY = ROOT / "scripts/supabase_schema_inventory.sql"
ACL_VALIDATOR = ROOT / "tests/sql/followup_engine/validate_acl_hardening.mjs"
PACKAGE_JSON = ROOT / "tests/sql/followup_engine/package.json"
CONTEXT_RPC = "get_daily_feedback_conversation_context_v1"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_messages_validator_accepts_v1_and_v2_shapes() -> None:
    sql = _sql()
    assert "create or replace function public.daily_feedback_messages_valid(p_messages jsonb)" in sql
    assert "array['actor','occurred_at','text']::text[]" in sql
    assert "array['actor','kind','meta','occurred_at','status','text']::text[]" in sql
    assert "m->>'actor' in ('prospect','agent','team','system')" in sql
    # el minimo baja a 1 mensaje: una conversacion sin respuesta del agente entra
    assert "jsonb_array_length(p_messages) between 1 and 1000" in sql
    assert "drop constraint if exists daily_feedback_items_messages_check" in sql
    assert "jsonb_array_length(messages) between 1 and 1000" in sql


def test_item_context_column_is_bounded_and_checked() -> None:
    sql = _sql()
    assert "create function public.daily_feedback_item_context_valid(p_context jsonb)" in sql
    assert "add column if not exists context jsonb not null default '{}'::jsonb" in sql
    assert "add constraint daily_feedback_items_context_valid" in sql
    assert "octet_length(p_context::text) <= 32768" in sql
    for key in (
        "chatwoot_conversation_id",
        "conversation_url",
        "contact",
        "conversation",
        "origin",
        "events",
        "payment_links",
        "prior_reviews",
        "summary",
    ):
        assert f"'{key}'" in sql
    assert "(javascript|vbscript):" in sql


def test_commit_accepts_items_with_and_without_context_and_page_returns_it() -> None:
    sql = _sql()
    commit = sql.split("create or replace function public.commit_daily_feedback_batch_v1(", 1)[1]
    commit = commit.split("create or replace function public.get_daily_feedback_review_page_v1", 1)[0]
    assert "array['apparent_objective','conversation_ref','display_label','messages','observed_outcome','release_id','release_version']::text[]" in commit
    assert "array['apparent_objective','context','conversation_ref','display_label','messages','observed_outcome','release_id','release_version']::text[]" in commit
    assert "v_context := coalesce(v_item->'context','{}'::jsonb);" in commit
    assert "raise exception 'invalid_daily_feedback_item_context'" in commit
    assert "observed_outcome,release_id,release_version,messages,context" in commit
    page = sql.split("create or replace function public.get_daily_feedback_review_page_v1", 1)[1]
    assert "'messages',v_item.messages,'context',v_item.context" in page


def test_context_rpc_is_scoped_service_role_only_and_fail_closed() -> None:
    sql = _sql()
    body = sql.split(f"create function public.{CONTEXT_RPC}(", 1)[1]
    assert "security definer" in body
    assert "set search_path = ''" in body
    assert "raise exception 'invalid_daily_feedback_context_request'" in body
    assert "raise exception 'daily_feedback_scope_not_configured'" in body
    assert "cardinality(p_conversation_ids) > 500" in body
    for table in (
        "public.human_handoff_requests",
        "public.conversation_reactivation_events",
        "public.conversation_resume_events",
        "public.contact_opt_out_events",
        "public.checkout_link_issuances",
        "public.checkout_offer_catalog",
        "public.daily_feedback_decisions",
    ):
        assert table in body
    assert "(i.context->>'chatwoot_conversation_id') = v_id::text" in body
    assert "and b.state <> 'purged'" in body
    signature = f"public.{CONTEXT_RPC}(text,text,bigint,bigint,bigint[])"
    # Los grants van guardados con to_regrole: validate_opt_out.mjs aplica todas las
    # migraciones en un PGlite sin los roles de Supabase (CI del PR #192, 27/09).
    assert f"revoke all on function {signature} from public;" in sql
    assert f"revoke all on function {signature} from anon;" in sql
    assert f"revoke all on function {signature} from authenticated;" in sql
    assert f"grant execute on function {signature} to service_role;" in sql
    assert "if to_regrole('service_role') is not null then" in sql


def test_helper_validators_are_not_executable_by_any_api_role() -> None:
    sql = _sql()
    for helper in (
        "public.daily_feedback_messages_valid(jsonb)",
        "public.daily_feedback_item_context_valid(jsonb)",
    ):
        for role in ("public", "anon", "authenticated", "service_role"):
            assert f"revoke all on function {helper} from {role};" in sql


def test_inventories_and_acl_validator_know_the_new_rpc() -> None:
    acl = ACL_INVENTORY.read_text(encoding="utf-8")
    assert f"('public.{CONTEXT_RPC}(text, text, bigint, bigint, bigint[])')" in acl
    validator = ACL_VALIDATOR.read_text(encoding="utf-8")
    assert f"('{CONTEXT_RPC}(text,text,bigint,bigint,bigint[])')" in validator
    assert "result.expected_count !== 120" in validator
    schema = SCHEMA_INVENTORY.read_text(encoding="utf-8")
    assert "'20260927000100_daily_feedback_review_context_v2.sql'" in schema
    assert "daily_feedback_identified_review_context" in schema
    assert f"to_regprocedure('public.{CONTEXT_RPC}(text,text,bigint,bigint,bigint[])')" in schema
    package = PACKAGE_JSON.read_text(encoding="utf-8")
    assert "node validate_daily_feedback_review_context.mjs" in package
