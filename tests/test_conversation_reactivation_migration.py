"""Contract checks del registro de reactivaciones.

El comportamiento se prueba ejecutando las RPC contra el esquema real en
``tests/sql/followup_engine/validate_conversation_reactivation.mjs``; esto
verifica las propiedades estructurales que ese validador no puede observar, y
que son justamente las que hacen que el sistema no le mande dos veces la misma
plantilla al mismo lead.
"""

from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260923000200_conversation_reactivation_v1.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def _claim_body() -> str:
    return (
        _sql()
        .split(
            "create or replace function public.claim_conversation_reactivation",
            1,
        )[1]
        .split("$function$;", 1)[0]
    )


def test_the_idempotency_index_is_unique_and_excludes_failures() -> None:
    """Un envio fallido tiene que poder reintentarse; uno entregado, no.

    Si el indice fuera total, un fallo transitorio de Chatwoot quemaria esa
    conversacion para siempre. Si fuera no unico, dos barridos simultaneos le
    mandarian la plantilla dos veces al mismo lead.
    """
    sql = _sql()
    assert (
        "create unique index conversation_reactivation_events_command_key_idx"
        in sql
    )
    index = sql.split(
        "create unique index conversation_reactivation_events_command_key_idx",
        1,
    )[1].split(";", 1)[0]
    assert "on public.conversation_reactivation_events (command_key)" in index
    assert "where status <> 'failed'" in index


def test_every_barrier_runs_before_the_row_is_inserted() -> None:
    """Las tres barreras cierran antes del insert, no despues.

    Una barrera que corre despues de reservar deja una fila 'claimed' que
    bloquea el reintento aunque el envio nunca haya estado autorizado.
    """
    body = _claim_body()
    insert_at = body.index("insert into public.conversation_reactivation_events")
    for barrier in (
        "'replayed'",
        "'not_found'",
        "'blocked_contact'",
        "'blocked_reactivation_limit'",
    ):
        assert body.index(barrier) < insert_at, barrier


def test_the_limit_counts_reserved_and_delivered_rows() -> None:
    """A quien ya recibio la plantilla y no contesto no se le insiste.

    Contar solo las entregadas dejaria que una reserva colgada habilitara un
    segundo envio.
    """
    body = _claim_body()
    limit_block = body.split("blocked_reactivation_limit", 1)[0]
    assert "status in ('claimed', 'sent')" in limit_block


def test_the_opt_out_barrier_is_the_canonical_one() -> None:
    body = _claim_body()
    assert "contact_permission in (" in body
    for permission in ("'opted_out'", "'blocked'", "'restricted'"):
        assert permission in body


def test_an_ambiguous_commercial_case_fails_closed() -> None:
    # Un `select ... into` de plpgsql con dos filas toma una arbitraria en
    # silencio: reactivar el caso equivocado seria un error invisible.
    body = _claim_body()
    assert "claim_conversation_reactivation_ambiguous_case" in body


def test_the_reactivation_never_touches_the_pause() -> None:
    """Mandar una plantilla no despausa nada.

    La pausa la levanta resume_paused_conversation cuando el lead contesta, con
    su propia auditoria. Si esta migracion tambien la tocara, habria dos
    caminos escribiendo human_takeover y ninguna auditoria seria completa.
    """
    sql = _sql()
    assert "human_takeover = false" not in sql
    assert "update public.conversations" not in sql
    assert "update public.commercial_cases" not in sql


def test_both_functions_are_service_role_only() -> None:
    sql = _sql()
    for signature in (
        "public.claim_conversation_reactivation(",
        "public.settle_conversation_reactivation(",
    ):
        assert f"revoke execute on function {signature}" in sql
    assert "from public;" in sql
    assert "from anon;" in sql
    assert "from authenticated;" in sql
    assert "to service_role;" in sql
    # security definer con search_path fijado: sin eso, un search_path hostil
    # resuelve public.contacts a otra tabla. Se cuenta la declaracion de la
    # funcion, no la palabra suelta: la migracion tambien la nombra en prosa.
    assert sql.count("language plpgsql\nsecurity definer") == 2
    assert sql.count("set search_path = pg_catalog, public, pg_temp") == 2


def test_the_settlement_only_closes_a_live_reservation() -> None:
    body = (
        _sql()
        .split(
            "create or replace function public.settle_conversation_reactivation",
            1,
        )[1]
        .split("$function$;", 1)[0]
    )
    assert "event.status = 'claimed'" in body
    assert "for update" in body
    # Una entrega no puede llevar motivo de falla ni al reves.
    assert "p_status not in ('sent', 'failed')" in body
    assert "p_status = 'failed' and p_provider_message_id is not null" in body


def test_the_row_state_machine_is_enforced_by_a_check() -> None:
    sql = _sql()
    assert "check (status in ('claimed', 'sent', 'failed'))" in sql
    # 'claimed' no puede tener cierre; 'sent' no puede tener motivo de falla.
    assert "status = 'claimed' and settled_at is null" in sql
    assert "status = 'sent' and settled_at is not null" in sql
    assert "status = 'failed' and settled_at is not null" in sql
