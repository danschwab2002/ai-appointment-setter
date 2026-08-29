from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / (
    "20260829000400_precheckout_delayed_worker_sender.sql"
)


def sql() -> str:
    return " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())


def test_adds_compatible_v2_due_list_without_replacing_v1() -> None:
    migration = sql()

    assert "list_due_hotmart_abandonment_reevaluations_v2" in migration
    assert "p_include_precheckout boolean" in migration
    assert "timer.source_kind = 'hotmart_event'" in migration
    assert "p_include_precheckout and timer.source_kind = 'precheckout_intent'" in migration
    assert "command.status in ('reserved', 'request_started')" in migration
    assert "drop function public.list_due_hotmart_abandonment_reevaluations" not in migration


def test_projection_is_bound_to_timer_and_shared_command() -> None:
    migration = sql()

    assert "get_precheckout_delayed_one_shot_command" in migration
    assert "command.source_reevaluation_id = p_reevaluation_id" in migration
    assert "timer.outcome = 'command_reserved'" in migration
    assert "intent_submission.purchase_intent_id = intent.id" in migration
    assert "command.purchase_intent_id = intent.id" in migration
    assert "order by (candidate.canonical_payload #>> '{submitted_at}')::timestamptz desc" in migration
    assert "v_row.rollout_scope <> 'johanna-precheckout-delayed-first-touch-v1'" in migration
    assert "johanna_interes_precheckout_01" in migration
    assert "johanna-precheckout-delayed-first-touch-v1" in migration


def test_projection_returns_exact_sender_context_and_terminal_status() -> None:
    migration = sql()

    for marker in (
        "command_id",
        "command_status",
        "target_phone",
        "buyer_name",
        "buyer_email",
        "product_name",
        "template_name",
        "template_language",
        "template_category",
        "copy_version",
        "send_authorized",
        "authorization_reason",
    ):
        assert marker in migration
    assert "request_started" in migration
    assert "accepted_by_chatwoot" in migration
    assert "delivery_unknown" in migration


def test_projection_is_the_atomic_request_start_fence() -> None:
    migration = sql()

    assert "status in ('reserved', 'request_started'" in migration
    assert "'chatwoot-opt-out-user', 1, v_target_phone" in migration
    assert "for update of owner" in migration
    assert "for update of conversation" in migration
    assert "set status = 'request_started'" in migration
    assert "where command.id = v_row.command_id and command.status = 'reserved'" in migration
    assert "precheckout_inflight_recovered" in migration


def test_new_rpcs_are_service_role_only() -> None:
    migration = sql()

    signatures = (
        "public.list_due_hotmart_abandonment_reevaluations_v2(timestamptz,integer,boolean)",
        "public.get_precheckout_delayed_one_shot_command(uuid)",
    )
    for signature in signatures:
        assert f"revoke all on function {signature} from public" in migration
        assert f"grant execute on function {signature} to service_role" in migration
        assert f"revoke all on function {signature} from anon" in migration
        assert f"revoke all on function {signature} from authenticated" in migration


def test_task_does_not_create_parallel_ledger_or_external_effect() -> None:
    migration = sql()

    assert "create table" not in migration
    assert "insert into public.johanna_abandonment_one_shot_commands" not in migration
    assert "net.http_post" not in migration
    assert "send_first_message" not in migration
    assert "insert into public.messages" not in migration
