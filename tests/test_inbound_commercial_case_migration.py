"""Contract tests for inbound commercial-case draft-only Cut B."""

from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260816000200_inbound_commercial_case_draft_only.sql"
)


def _sql() -> str:
    assert MIGRATION.exists(), "missing inbound commercial-case Cut B migration"
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_scope_is_server_owned_versioned_and_not_seeded() -> None:
    sql = _sql()

    assert "create table public.inbound_commercial_scope_versions" in sql
    assert "status = any (array['draft', 'published'])" in sql
    assert "published_inbound_scope_is_immutable" in sql
    assert "insert into public.inbound_commercial_scope_versions" not in sql
    function = sql.split(
        "create function public.admit_inbound_commercial_case(", maxsplit=1
    )[1].split("$function$;", maxsplit=1)[0]
    assert "p_chatwoot_account_id" not in function
    assert "p_chatwoot_inbox_id" not in function
    assert "p_external_product_id" not in function
    assert "p_offer_code" not in function
    assert "scope.status = 'published'" in function


def test_admission_is_canonical_idempotent_and_conflict_durable() -> None:
    sql = _sql()

    assert "create table public.inbound_commercial_case_admissions" in sql
    assert "unique (scope_key, scope_version, external_conversation_id)" in sql
    assert "inbound_commercial_case_admission_mismatch" in sql
    assert "create table public.inbound_commercial_case_conflicts" in sql
    assert "on conflict do nothing" in sql
    assert "outcome := 'already_exists'" in sql
    assert "outcome := 'evidence_conflict'" in sql
    assert "insert into public.contacts (metadata)" in sql
    assert "insert into public.channel_identities" in sql
    assert "insert into public.conversations" in sql
    assert "commercial_context ->> 'chatwoot_conversation_id'" in sql
    assert "chatwoot-channel-identity" in sql
    assert "chatwoot-conversation-owner" in sql
    assert "v_identity.identity_status <> 'active'" in sql
    assert "inbound_external_conversation_ownership_ambiguous" in sql
    assert "v_anchor_conversation.commercial_context <> jsonb_build_object" in sql
    assert "identity.account_id = 'chatwoot:' || scope.chatwoot_account_id::text" in sql
    assert "identity.metadata ->> 'inbox_id' = scope.chatwoot_inbox_id::text" in sql
    assert "conversation.commercial_context ->> 'chatwoot_conversation_id'" in sql
    assert "~ '^[1-9][0-9]*$'" in sql


def test_inbound_root_is_physically_draft_only_and_immutable() -> None:
    sql = _sql()

    assert "create function public.protect_inbound_commercial_case()" in sql
    assert "new.automation_status <> 'draft_only'" in sql
    assert "new.identity_resolution_status <> 'resolved'" in sql
    assert "inbound_commercial_case_is_immutable" in sql
    assert "commercial_cases_live_inbound_conversation_scope_idx" in sql
    assert "add column inbound_scope_key text" in sql
    assert "add column inbound_scope_version integer" in sql
    assert "add column tenant_ref text" in sql
    assert "conversation_id, inbound_scope_key, inbound_scope_version" in sql
    assert "'active', 'draft_only', 'resolved', 'shadow', 1" in sql


def test_fixture_does_not_assert_client_specific_facts() -> None:
    fixture = (
        Path(__file__).parents[1]
        / "tests/sql/followup_engine/validate_inbound_commercial_case_draft_only.mjs"
    ).read_text(encoding="utf-8").lower()

    assert "johanna" not in fixture
    assert "f106691755g" not in fixture
    assert "bxjge6zq" not in fixture


def test_intent_correlation_is_separate_uncertain_and_closed() -> None:
    sql = _sql()

    assert "create table public.commercial_case_intent_correlations" in sql
    for status in ("resolved", "candidate", "ambiguous", "conflict", "unmatched"):
        assert f"'{status}'" in sql
    assert "commercial_case_intent_correlation_is_immutable" in sql
    assert "revoke all on table public.commercial_case_intent_correlations" in sql


def test_cut_b_adds_no_agent_handoff_scheduling_or_outbound_effect() -> None:
    sql = _sql()

    for forbidden in (
        "insert into public.followup_sequences",
        "insert into public.scheduled_actions",
        "insert into public.followup_delivery_attempts",
        "insert into public.human_handoff_requests",
        "request_human_handoff(",
        "hermes",
        "message_sender",
    ):
        assert forbidden not in sql


def test_only_bounded_rpc_is_executable_by_service_role() -> None:
    sql = _sql()

    assert "security definer" in sql
    assert "set search_path = pg_catalog, public, pg_temp" in sql
    assert (
        "grant execute on function public.admit_inbound_commercial_case(text, integer, bigint, text) to service_role"
        in sql
    )
    for table in (
        "inbound_commercial_scope_versions",
        "inbound_commercial_case_admissions",
        "inbound_commercial_case_conflicts",
        "commercial_case_intent_correlations",
    ):
        assert f"revoke all on table public.{table} from service_role" in sql
