"""La migracion del seguimiento con cupon, leida como texto.

La ejecucion real (reservar, emitir, autorizar, cerrar, reintentar, barreras)
la cubre ``tests/sql/followup_engine/validate_conversation_followup_discount.mjs``
en PGlite. Aca se fija lo que tiene que seguir siendo cierto del texto.
"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/20260928000400_conversation_followup_discount_v1.sql"
SQL = MIGRATION.read_text(encoding="utf-8")
# El codigo sin los comentarios, que explican cosas que el codigo no hace.
CODE = "\n".join(
    line for line in SQL.splitlines() if not line.lstrip().startswith("--")
)
COMPACT = " ".join(SQL.split())


def _function(name: str) -> str:
    start = SQL.index(f"create or replace function public.{name}(")
    return SQL[start : SQL.index("$function$;", start)]


def test_one_live_followup_per_conversation_and_failures_release() -> None:
    assert (
        "create unique index conversation_followup_events_one_live_per_conversation_idx "
        "on public.conversation_followup_events (conversation_id) where status <> 'failed';"
    ) in COMPACT


def test_the_link_is_issued_by_the_agents_rpc_anchored_on_our_last_message() -> None:
    claim = " ".join(_function("claim_conversation_followup_v1").split())
    assert "from public.reserve_chatwoot_checkout_issuance_v2(" in claim
    assert "p_last_outbound_message_id::text," in claim
    # Solo una reserva fresca se manda: una en vuelo o incierta no se repite.
    assert "if v_reservation.outcome is distinct from 'reserved'" in claim


def test_every_barrier_runs_before_anything_is_written() -> None:
    claim = _function("claim_conversation_followup_v1")
    insert_at = claim.index("insert into public.conversation_followup_events")
    reserve_at = claim.index("reserve_chatwoot_checkout_issuance_v2")
    for barrier in (
        "'blocked_followup_limit'",
        "'blocked_pending_handoff'",
        "'blocked_conversation'",
        "'blocked_contact'",
        "'purchase_already_approved'",
    ):
        assert claim.index(barrier) < reserve_at < insert_at, barrier


def test_a_purchase_is_detected_by_email_and_by_the_phone_without_country_code() -> None:
    claim = " ".join(_function("claim_conversation_followup_v1").split())
    assert "v_phone_tail := right(p_external_user_id, 9);" in claim
    assert "right(intent.normalized_phone, 9) = v_phone_tail" in claim
    assert "intent.normalized_email = v_email" in claim
    assert "right(identity.normalized_phone, 9) = v_phone_tail" in claim
    assert "event.event_type in ('PURCHASE_APPROVED', 'PURCHASE_COMPLETE')" in claim


def test_a_pending_handoff_uses_the_same_predicate_as_the_reactivation() -> None:
    claim = " ".join(_function("claim_conversation_followup_v1").split())
    assert (
        "request.status in ('requested', 'projected', 'projection_failed') "
        "and request.attended_at is null"
    ) in claim


def test_the_issuance_url_contract_is_not_touched() -> None:
    # El cupon va en el boton y en la fila del seguimiento; la URL de la
    # emision conserva el contrato que leen la correlacion y la revision diaria.
    assert "checkout_link_issuances_url_shape" not in SQL
    assert "offDiscount" not in CODE
    assert "alter table public.checkout_link_issuances" not in SQL


def test_both_functions_are_service_role_only_and_grants_are_guarded() -> None:
    assert CODE.count("\nsecurity definer\n") == 2
    assert "set search_path = pg_catalog, public, pg_temp" in SQL
    for role in ("anon", "authenticated", "service_role"):
        assert f"if to_regrole('{role}') is not null then" in SQL
    assert re.search(r"grant execute on function public\.claim_conversation_followup_v1\(", SQL)
    assert re.search(r"grant execute on function public\.settle_conversation_followup_v1\(", SQL)
    assert "revoke all on table public.conversation_followup_events from service_role;" in SQL
