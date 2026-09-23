"""Contract checks for the durable resume transition.

El comportamiento se prueba ejecutando las RPC en
``tests/sql/followup_engine/validate_resume_paused_conversation.mjs``; esto
verifica lo que ese validador no puede: que la migracion no relaje la invariante
existente del caso comercial mientras agrega la inversa.
"""

from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260923000100_resume_paused_conversation_v1.sql"
)


def _trigger_body() -> str:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    return sql.split(
        "create or replace function public.protect_inbound_commercial_case()", 1
    )[1].split("$function$;", 1)[0]


def test_resume_keeps_the_original_pause_transition_intact() -> None:
    body = _trigger_body()

    # La transicion de ida sobrevive textualmente.
    assert "old.status = 'active'" in body
    assert "new.status = 'paused'" in body
    assert "new.automation_status = 'disabled'" in body
    # Y la inversa se agrega, no la reemplaza.
    assert "old.status = 'paused'" in body
    assert "new.status = 'active'" in body
    assert "new.automation_status = 'draft_only'" in body
    # El caso sigue siendo inmutable para cualquier otra cosa.
    assert "inbound_commercial_case_is_immutable" in body


def test_resume_transition_preserves_every_identity_field() -> None:
    body = _trigger_body()
    resume_branch = body.split("-- reanudacion", 1)[1]

    for invariant in (
        "new.version = old.version + 1",
        "new.updated_at > old.updated_at",
        "new.contact_id = old.contact_id",
        "new.conversation_id = old.conversation_id",
        "new.selected_channel_identity_id = old.selected_channel_identity_id",
        "new.created_at = old.created_at",
        "new.inbound_scope_key = old.inbound_scope_key",
        "new.tenant_ref = old.tenant_ref",
    ):
        assert invariant in resume_branch, invariant


def test_resume_rpc_fails_closed_on_every_documented_barrier() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    body = sql.split(
        "create or replace function public.resume_paused_conversation(", 1
    )[1].split("$function$;", 1)[0]

    # Quien pidio no ser contactado, un handoff a medio proyectar y el limite
    # anti-loop cortan antes de cualquier update.
    for outcome in (
        "'blocked_contact'",
        "'blocked_pending_handoff'",
        "'blocked_resume_limit'",
        "'already_active'",
        "'replayed'",
        "'not_found'",
    ):
        assert outcome in body, outcome
    assert "'opted_out', 'blocked', 'restricted'" in body
    for barrier in (
        "outcome := 'blocked_contact'",
        "outcome := 'blocked_pending_handoff'",
        "outcome := 'blocked_resume_limit'",
    ):
        assert body.index(barrier) < body.index("update public.conversations"), (
            f"{barrier} corre despues del update"
        )


def test_resume_rpc_is_service_role_only() -> None:
    compact = " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())
    signature = (
        "public.resume_paused_conversation( bigint, text, text, integer, "
        "integer, timestamptz )"
    )
    assert f"revoke execute on function {signature} from public;" in compact
    assert f"revoke execute on function {signature} from anon;" in compact
    assert f"revoke execute on function {signature} from authenticated;" in compact
    assert f"grant execute on function {signature} to service_role;" in compact
    assert "security definer" in compact
