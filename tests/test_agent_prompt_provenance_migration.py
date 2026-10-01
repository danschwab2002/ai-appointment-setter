"""Contrato estructural de 20260928000100.

El comportamiento se prueba contra el esquema real en
`tests/sql/followup_engine/validate_agent_prompt_provenance.mjs`. Aca se fija lo
que un diff futuro no puede cambiar sin que alguien lo decida: que la migracion
no toque las tablas de la revision diaria, que lo que no es entrypoint no quede
con `service_role`, y que la confianza de la atribucion se calcule contra el
release siguiente en vez de afirmarse.
"""

from __future__ import annotations

from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20260928000100_agent_prompt_provenance_v1.sql"
RAIZ = Path(__file__).resolve().parents[1]


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_the_migration_is_transactional() -> None:
    sql = _sql().strip()
    assert sql.startswith("--")
    assert "\nbegin;\n" in sql
    assert sql.endswith("commit;")


def test_it_comes_after_the_handoff_attendance_one() -> None:
    versions = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    posicion = versions.index(MIGRATION.name)
    assert versions[posicion - 1] == "20260927000300_handoff_attendance_v1.sql"


def test_it_creates_the_two_tables_and_drops_nothing() -> None:
    """Se agrega, no se reestructura.

    Las tablas de la revision diaria las lee el proyector de notas y el conector
    de Slack; esta migracion no las toca, solo redefine un validador.
    """
    sql = _sql()
    assert "create table public.agent_prompt_releases (" in sql
    assert "create table public.agent_turn_provenance (" in sql
    assert "drop table" not in sql
    assert "drop function" not in sql
    assert "drop index" not in sql
    assert "alter table public.daily_feedback" not in sql
    assert "alter column" not in sql


def test_a_release_is_immutable() -> None:
    """Un release es un hecho observado: no se corrige, se observa de nuevo.

    Sin esto, un turno viejo podria terminar apuntando a un prompt que alguien
    edito despues, y la procedencia diria algo que nunca fue cierto.
    """
    sql = _sql()
    assert "agent_prompt_releases_immutable_guard" in sql
    assert "before update or delete on public.agent_prompt_releases" in sql
    assert "raise exception 'agent_prompt_release_is_immutable'" in sql


def test_the_same_release_observed_twice_is_not_an_event() -> None:
    """El cron corre seguido y lo normal es que nada haya cambiado."""
    sql = _sql()
    assert "'outcome', 'unchanged'" in sql
    assert "unique (tenant_ref, scope_ref, release_digest)" in sql
    assert "unique (tenant_ref, scope_ref, release_ordinal)" in sql


def test_a_turn_is_recorded_once_even_if_the_bridge_retries() -> None:
    sql = _sql()
    assert "turn_digest text not null unique" in sql
    assert "'outcome', 'replayed'" in sql


def test_a_turn_without_a_release_is_still_recorded() -> None:
    """Un turno sin procedencia es un dato, no una falla.

    Si la insercion fallara cuando el registrador del perfil todavia no corrio,
    el bridge perderia el rastro de TODOS los turnos hasta la primera corrida ---
    y el indice parcial no tendria nada que contar.
    """
    sql = _sql()
    assert "'recorded_without_release'" in sql
    assert "agent_turn_provenance_without_release_idx" in sql
    assert "where release_id is null" in sql


def test_the_release_attribution_is_whole_or_absent() -> None:
    """Media procedencia miente peor que ninguna."""
    sql = _sql()
    assert "agent_turn_provenance_release_is_whole" in sql
    assert "release_id is null and release_digest is null and release_ordinal is null" in sql


def test_the_active_release_is_the_newest_observed_before_the_turn() -> None:
    sql = _sql()
    assert "observed_at <= v_occurred" in sql
    assert "order by observed_at desc, release_ordinal desc" in sql


def test_the_confidence_is_computed_against_the_next_release() -> None:
    """La atribucion NO se afirma: se compara con el mtime del release siguiente.

    El registrador corre por cron, asi que entre dos observaciones el SOUL pudo
    haber cambiado. Si los artefactos del release siguiente ya estaban
    modificados antes del turno, el turno corrio algo mas nuevo que lo
    atribuido, y eso se llama 'misattributed' en vez de taparse.
    """
    sql = _sql()
    assert "agent_turn_release_confidence_v1" in sql
    assert "r.release_ordinal > p_release_ordinal" in sql
    assert "then 'verified'" in sql
    assert "else 'misattributed'" in sql
    assert "then 'open'" in sql
    assert "then 'no_release'" in sql


def test_the_soul_text_is_stored_whole_not_just_hashed() -> None:
    """Era el pedido: guardar el prompt, no un identificador del prompt."""
    sql = _sql()
    assert "soul_text text not null" in sql
    assert "char_length(soul_text) between 1 and 200000" in sql


def test_only_the_three_entrypoints_reach_service_role() -> None:
    """Lo que no es entrypoint se le revoca a mano.

    En Supabase los privilegios por defecto del esquema le dan execute a
    `service_role` sobre TODA funcion nueva. Sin estos revokes, el guard del
    trigger y la funcion de confianza aparecen como dos puertas de servicio que
    nadie declaro --- y el inventario de ACL lo detecta, que es como se encontro.
    """
    sql = _sql()
    for entrypoint in (
        "grant execute on function public.register_agent_prompt_release_v1(",
        "grant execute on function public.record_agent_turn_provenance_v1(",
        "grant execute on function public.get_agent_turn_provenance_v1(",
    ):
        assert sql.count(entrypoint) == 1
    assert sql.count("grant execute on function") == 3

    for revocada in (
        "revoke all on function public.agent_prompt_releases_immutable_guard()\n            from service_role;",
        "revoke all on function public.agent_turn_release_confidence_v1(",
        "revoke all on function public.daily_feedback_item_context_valid(jsonb)\n            from service_role;",
    ):
        assert revocada in sql


def test_the_tables_are_not_reachable_from_the_api() -> None:
    sql = _sql()
    for tabla in ("agent_prompt_releases", "agent_turn_provenance"):
        assert f"alter table public.{tabla} enable row level security" in sql
        assert f"revoke all on table public.{tabla} from public" in sql
        assert f"revoke all on table public.{tabla} from anon" in sql
        assert f"revoke all on table public.{tabla} from authenticated" in sql


def test_the_role_block_tolerates_roles_that_do_not_exist() -> None:
    """PGlite y un Postgres vacio no tienen los roles de la API de Supabase."""
    sql = _sql()
    assert "do $roles$" in sql
    assert sql.count("if exists (select 1 from pg_roles where rolname =") == 3


def test_the_review_context_gains_one_key_and_keeps_the_rest() -> None:
    """El validador se extrajo textualmente de 20260927000100.

    Si se hubiera reescrito a mano, las otras reglas --- la forma de
    `conversation_url`, el limite de tamano, el veto a `javascript:` --- podrian
    haberse perdido en silencio al agregar una clave.
    """
    sql = _sql()
    assert "create or replace function public.daily_feedback_item_context_valid" in sql
    assert "'agent_release'\n" in sql
    # Reglas que venian del original y tienen que seguir estando.
    assert "octet_length(p_context::text) <= 32768" in sql
    assert "(p_context->>'conversation_url') ~ '^https://[^[:space:]\"''<>\\\\]+$'" in sql
    assert "p_context::text !~* '(javascript|vbscript):'" in sql
    # Y la regla nueva, con la confianza incluida.
    assert "(p_context->'agent_release'->>'release_digest') ~ '^[a-f0-9]{64}$'" in sql
    assert "'verified', 'open', 'misattributed', 'no_release'" in sql


def test_the_digest_columns_only_accept_a_sha256() -> None:
    sql = _sql()
    assert sql.count("~ '^[a-f0-9]{64}$'") >= 4


def test_the_inventory_cascade_knows_the_three_new_entrypoints() -> None:
    """Toda migracion nueva mueve los inventarios; esto lo deja fijado."""
    acl = (RAIZ / "scripts" / "supabase_acl_inventory.sql").read_text(encoding="utf-8")
    validador = (
        RAIZ / "tests" / "sql" / "followup_engine" / "validate_acl_hardening.mjs"
    ).read_text(encoding="utf-8")
    huella = (RAIZ / "scripts" / "supabase_schema_inventory.sql").read_text(encoding="utf-8")

    for nombre in (
        "register_agent_prompt_release_v1",
        "record_agent_turn_provenance_v1",
        "get_agent_turn_provenance_v1",
    ):
        assert nombre in acl
        assert nombre in validador
    assert "expected_count !== 118" in validador
    assert "agent_prompt_provenance_per_turn" in huella
    assert "'20260928000100'" in huella


def test_the_new_behavioural_validator_is_in_the_chain() -> None:
    """Un validador que no corre en la cadena no protege nada."""
    paquete = (
        RAIZ / "tests" / "sql" / "followup_engine" / "package.json"
    ).read_text(encoding="utf-8")
    assert "node validate_agent_prompt_provenance.mjs" in paquete
    assert (
        RAIZ / "tests" / "sql" / "followup_engine" / "validate_agent_prompt_provenance.mjs"
    ).is_file()
