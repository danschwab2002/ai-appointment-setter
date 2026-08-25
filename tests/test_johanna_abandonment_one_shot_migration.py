"""Contract tests for the Johanna controlled one-shot WABA migration."""

from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260825000200_johanna_abandonment_one_shot.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_one_shot_has_singleton_budget_and_exact_template() -> None:
    sql = _sql()

    assert "create table public.johanna_abandonment_one_shot_commands" in sql
    assert "johanna-abandonment-template-e2e-v1" in sql
    assert "unique" in sql
    assert "johanna_carrito_abandonado_01" in sql
    assert "johanna_compra_fallida_01" not in sql
    assert "'es_ec'" in sql
    assert "max_messages = 1" in sql
    assert "followups_allowed = 0" in sql


def test_begin_requires_v1_1_consent_allowlist_and_inactive_runtime() -> None:
    sql = _sql()

    assert "function public.begin_johanna_abandonment_one_shot" in sql
    assert "contract_version = '1.1.0'" in sql
    assert "{consent,whatsapp_contact}" in sql
    assert "johanna-precheckout-whatsapp-disclosure-v1" in sql
    assert "whatsapp_contact_authorized" in sql
    assert "activation_authorized" in sql
    assert "or intent.current_classification is not null" in sql
    assert "normalized_phone is distinct from p_allowed_external_user_id" in sql
    assert "'chatwoot-opt-out-user'" in sql
    assert "from public.contacts owner" in sql
    assert "for update of owner" in sql
    assert "from public.channel_identities identity" in sql
    assert "for update of identity" in sql
    assert "from public.contact_opt_out_events stop" in sql
    assert "runtime_state <> 'inactive'" in sql
    assert "generation is distinct from p_expected_generation" in sql


def test_command_is_durable_before_effect_and_replay_is_not_resendable() -> None:
    sql = _sql()

    assert "'request_started'" in sql
    assert "insert into public.johanna_abandonment_one_shot_commands" in sql
    assert "'replay'::text" in sql
    assert "delivery_unknown" in sql
    assert "create trigger johanna_abandonment_one_shot_commands_immutable" in sql


def test_rpc_is_service_role_only() -> None:
    sql = _sql()

    for signature in (
        "public.begin_johanna_abandonment_one_shot(text,uuid,text,bigint,bigint,text,integer,bigint)",
        "public.finish_johanna_abandonment_one_shot(uuid,text,bigint,bigint,text)",
    ):
        assert f"revoke all on function {signature} from public" in sql
        assert f"grant execute on function {signature} to service_role" in sql
    assert "security definer\nset search_path = pg_catalog, public, pg_temp" in sql
