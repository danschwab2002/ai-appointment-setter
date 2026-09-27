"""Contract checks de la atencion de derivaciones.

El comportamiento se prueba ejecutando las RPC contra el esquema real en
``tests/sql/followup_engine/validate_handoff_attendance.mjs``; esto verifica
las propiedades estructurales que ese validador no puede observar, y que son
las que sostienen el guard: si el CHECK de ``status`` cambia, o si el guard
deja de mirar ``attended_at``, la conversacion derivada vuelve a recibir un
bot.
"""

from pathlib import Path


MIGRATIONS = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20260927000300_handoff_attendance_v1.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def _body(function_name: str) -> str:
    return (
        _sql()
        .split(f"create or replace function public.{function_name}", 1)[1]
        .split("$function$;", 1)[0]
    )


def test_migration_adds_the_attendance_column_without_touching_status() -> None:
    sql = _sql()
    assert "add column attended_at timestamptz" in sql
    # El CHECK de status lo leen el proyector de notas y el conector de Slack.
    # Un estado terminal nuevo les cambiaria el contrato; por eso la atencion
    # vive en una columna aparte.
    assert "check (status in (" not in sql
    assert "drop constraint" not in sql


def test_migration_does_not_widen_the_live_handoff_index() -> None:
    """El indice de unicidad define cuando se rechaza una derivacion nueva.

    Ampliarlo a 'projected' haria fallar con
    'inbound_handoff_live_request_conflict' derivaciones legitimas de una
    conversacion que ya tuvo una.
    """
    sql = _sql()
    assert "drop index" not in sql
    assert "create unique index" not in sql


def test_attendance_is_scoped_to_handoffs_that_already_existed() -> None:
    body = _body("mark_human_handoff_attended")
    # Sin esta comparacion, el primer mensaje del equipo marcaria como
    # atendidas tambien las derivaciones posteriores.
    assert "request.created_at <= v_attended_at" in body
    # Idempotente: solo toca lo que sigue esperando.
    assert "request.attended_at is null" in body
    # Un reloj adelantado no graba una atencion en el futuro.
    assert "least(p_attended_at" in body
    assert "mark_human_handoff_attended_ambiguous_case" in body


def test_reactivation_refuses_to_claim_over_an_unattended_handoff() -> None:
    body = _body("claim_conversation_reactivation")
    assert "blocked_pending_handoff" in body
    assert "request.attended_at is null" in body
    assert "from public.human_handoff_requests request" in body
    # El guard corre antes de insertar la reserva: un envio bloqueado no puede
    # dejar fila en conversation_reactivation_events.
    guard = body.split("blocked_pending_handoff", 1)[0]
    assert "insert into public.conversation_reactivation_events" not in guard


def test_resume_counts_a_projected_handoff_as_pending() -> None:
    body = _body("resume_paused_conversation")
    assert "request.status = 'projected' and request.attended_at is null" in body
    # Lo que ya bloqueaba sigue bloqueando.
    assert "request.status in ('requested', 'projection_failed')" in body


def test_the_three_functions_are_service_role_only() -> None:
    sql = _sql()
    for function_name in (
        "mark_human_handoff_attended",
        "claim_conversation_reactivation",
        "resume_paused_conversation",
    ):
        assert f"revoke execute on function public.{function_name}" in sql
        assert f"grant execute on function public.{function_name}" in sql
    assert "to service_role" in sql
    assert "to anon" not in sql
    assert "to authenticated" not in sql


def test_role_grants_tolerate_a_database_without_supabase_roles() -> None:
    """Los validadores PGlite ajenos no crean anon/authenticated/service_role.

    Un revoke a secas revienta ahi con 'role does not exist' y rompe
    validadores que no tienen nada que ver con esta migracion.
    """
    sql = _sql()
    assert "do $roles$" in sql
    assert "from pg_roles where rolname = 'anon'" in sql
    assert "from pg_roles where rolname = 'authenticated'" in sql
    assert "from pg_roles where rolname = 'service_role'" in sql


def test_migration_is_transactional() -> None:
    sql = _sql()
    assert sql.lstrip().startswith("--")
    assert "\nbegin;\n" in sql
    assert sql.rstrip().endswith("commit;")


def test_migration_is_the_tail_of_the_queue() -> None:
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    assert names[-1] == MIGRATION.name
