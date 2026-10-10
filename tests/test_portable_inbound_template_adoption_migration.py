"""Migracion 20261001000400: la respuesta a una plantilla del piloto entra a la admision.

La migracion crea UNA funcion nueva, admit_portable_inbound_commercial_case_v1,
que adopta la conversacion que abrio una plantilla aceptada del piloto
(enabled -> draft_only) y despues delega siempre en
admit_inbound_commercial_case_v2, sin tocarla. Este test verifica que no
redefina ninguna funcion existente, que su salida sea la de la v2, que tome
los mismos advisory locks que la admision base y en el orden identidad ->
conversacion, los tres frenos que corrigen el borrador del diseno (los
estados vivos del constraint, el contacto dado de baja y la cadena de la
plantilla hasta el binding del piloto), la baja de Chatwoot de ese movil
aunque no haya quedado aplicada al contacto, que lo unico que escribe sea la
conversacion y su evento, y que el ACL sea explicito. El comportamiento se
prueba en tests/sql/followup_engine/validate_portable_inbound_template_adoption.mjs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
BASELINE = ROOT / "supabase" / "baseline" / "20260803_public_schema.sql"
MIGRATION = MIGRATIONS / "20261001000400_portable_inbound_adopts_template_conversation.sql"
BASE_ADMISSION = MIGRATIONS / "20260816000200_inbound_commercial_case_draft_only.sql"
V2_ADMISSION = MIGRATIONS / "20260826000200_inbound_paused_replay_guard.sql"
FOLLOWUP_ENGINE = MIGRATIONS / "20260803000100_followup_engine_v1.sql"
VALIDATOR = ROOT / "tests" / "sql" / "followup_engine" / "validate_portable_inbound_template_adoption.mjs"
NAME = "admit_portable_inbound_commercial_case_v1"
SIGNATURE = "(text,integer,bigint,text)"
V2 = "admit_inbound_commercial_case_v2"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _normalized(sql: str) -> str:
    return " ".join(re.sub(r"--[^\n]*", "", sql).split())


def _function(sql: str, name: str, *, replace: bool = False) -> str:
    create = r"create\s+or\s+replace\s+function" if replace else r"create\s+function"
    matches = re.findall(rf"{create}\s+public\.{name}\s*\(.*?\$function\$;", sql, re.DOTALL)
    assert len(matches) == 1, name
    return matches[0]


def _new() -> str:
    return _function(_sql(), NAME)


def _body() -> str:
    return _normalized(_new()).split(" as $function$ ", 1)[1]


def test_migration_creates_one_new_function_and_replaces_none() -> None:
    sql = _sql()
    defined = re.findall(r"create\s+(?:or\s+replace\s+)?function\s+public\.([a-z0-9_]+)\s*\(", sql)

    assert defined == [NAME]
    assert re.search(rf"^create function public\.{NAME}\(", sql, re.MULTILINE)
    # No existia antes en ningun archivo: Johanna no ejecuta nada de lo que
    # esta migracion crea, y su bridge sin manifiesto sigue llamando a la v2.
    earlier = {BASELINE.name: BASELINE.read_text(encoding="utf-8")}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name < MIGRATION.name:
            earlier[path.name] = path.read_text(encoding="utf-8")
        elif path.name > MIGRATION.name:
            assert not re.search(
                rf"create\s+(?:or\s+replace\s+)?function\s+public\.{NAME}\s*\(",
                path.read_text(encoding="utf-8"),
            ), path.name
    for filename, text in earlier.items():
        assert NAME not in text, filename
    executable = re.sub(r"--[^\n]*", "", sql)
    assert "pg_get_functiondef" not in executable
    for forbidden in (
        r"\balter\b",
        r"\bdrop\b",
        r"\bcreate\s+(or\s+replace\s+)?trigger\b",
        r"\bcreate\s+table\b",
        r"\bcreate\s+(unique\s+)?index\b",
        r"\bexecute\s+replace\(",
    ):
        assert not re.search(forbidden, executable, re.IGNORECASE), forbidden


def test_the_output_is_the_v2_output_and_the_v2_is_untouched() -> None:
    new_header = _normalized(_new()).split(" as $function$ ", 1)[0]
    v2_header = _normalized(_function(V2_ADMISSION.read_text(encoding="utf-8"), V2)).split(
        " as $function$ ", 1
    )[0]

    assert new_header == v2_header.replace(f"public.{V2}(", f"public.{NAME}(")
    assert new_header.endswith(
        "language plpgsql security definer set search_path = pg_catalog, public, pg_temp"
    )
    # La v2 vigente sigue siendo la de 20260826000200: nadie la redefine.
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name > V2_ADMISSION.name:
            assert not re.search(
                rf"create\s+(?:or\s+replace\s+)?function\s+public\.{V2}\s*\(",
                path.read_text(encoding="utf-8"),
            ), path.name


def test_it_always_ends_delegating_to_the_v2() -> None:
    body = _body()

    assert body.count(f"public.{V2}(") == 1
    assert body.endswith(
        "return query select result.outcome, result.commercial_case_id, result.contact_id, "
        "result.channel_identity_id, result.conversation_id, result.automation_status "
        f"from public.{V2}( p_scope_key, p_scope_version, p_external_conversation_id, "
        "p_external_user_id ) result; end; $function$;"
    )
    # Ningun return antes de la delegacion, ni un raise propio: si no adopta,
    # el resultado y los errores son los de hoy.
    before_delegation = body.rsplit("return query", 1)[0]
    assert not re.search(r"\breturn\b", before_delegation)
    assert "raise" not in body
    assert "admit_inbound_commercial_case_base" not in body
    assert "public.admit_inbound_commercial_case(" not in body


def test_the_advisory_locks_are_the_base_ones_in_the_same_order() -> None:
    lock = re.compile(r"perform pg_advisory_xact_lock\(hashtextextended\(.*?\), 0 \)\);")
    base = _normalized(_function(BASE_ADMISSION.read_text(encoding="utf-8"), "admit_inbound_commercial_case"))

    assert lock.findall(_body()) == lock.findall(base)
    assert len(lock.findall(base)) == 3


def test_lock_order_is_identity_then_conversation_and_nothing_else() -> None:
    body = _body()

    # Cada sentencia que bloquea, con la ultima tabla que nombra su from.
    locks = [
        (re.findall(r"from public\.([a-z_]+) ", statement)[-1], mode.group(1))
        for statement in body.split(";")
        if (mode := re.search(r" for (update|share)$", statement))
    ]
    assert locks == [
        ("inbound_commercial_scope_versions", "share"),
        ("channel_identities", "update"),
        ("conversations", "update"),
    ]
    assert body.count(" for update") == 2
    assert " for share" in body and body.count(" for share") == 1


def test_the_conversation_is_the_enabled_one_without_human_takeover() -> None:
    body = _body()

    assert (
        "and identity.metadata ->> 'inbox_id' = v_scope.chatwoot_inbox_id::text "
        "and identity.identity_status = 'active' for update;"
    ) in body
    assert (
        "and conversation.automation_status = 'enabled' and conversation.status in "
        "( 'active', 'awaiting_agent', 'awaiting_contact', 'snoozed' ) "
        "and not conversation.human_takeover for update;"
    ) in body
    assert "paused_human" not in body
    # Solo si todavia no hay fila de admision de esa conversacion de Chatwoot.
    assert "if not exists ( select 1 from public.inbound_commercial_case_admissions admission" in body


def test_the_template_chain_reaches_the_pilot_binding() -> None:
    body = _body()

    assert (
        "from public.messages message "
        "join public.followup_delivery_attempts attempt "
        "on attempt.accepted_message_id = message.id "
        "and attempt.outcome = 'accepted_by_chatwoot' "
        "join public.scheduled_actions action on action.id = attempt.action_id "
        "join public.recovery_cases recovery on recovery.id = action.recovery_case_id "
        "join public.pilot_recovery_case_bindings binding "
        "on binding.recovery_case_id = recovery.id "
        "where message.conversation_id = v_conversation.id "
        "and message.direction = 'outbound' "
        "and message.actor_type = 'ai_agent' "
        "and message.semantic_metadata ->> 'strategy' = 'durable_followup' "
        "and recovery.conversation_id = v_conversation.id "
        "and recovery.contact_id = v_identity.contact_id "
        "and recovery.selected_channel_identity_id = v_identity.id "
        "order by message.occurred_at desc, message.created_at desc, message.id desc limit 1;"
    ) in body
    assert "left join" not in body


def test_the_contact_and_the_live_actions_stop_the_adoption() -> None:
    body = _body()

    assert (
        "and v_contact.contact_permission not in ( 'opted_out', 'blocked', 'restricted' ) "
        "and v_contact.lifecycle_status <> 'do_not_contact'"
    ) in body
    # Y ninguna baja de Chatwoot de ese movil, aunque no haya quedado aplicada
    # al contacto: las formas del que contesta y las de contacts.phone, con
    # los estados que frena el arranque del piloto. Sin el advisory lock de
    # opt-out (la adopcion ya tiene la identidad bloqueada).
    assert (
        "and not exists ( select 1 from public.contact_opt_out_events optout "
        "where optout.source = 'chatwoot' and optout.channel = 'whatsapp' "
        "and optout.canonical_account_id = v_scope.chatwoot_account_id "
        "and optout.external_user_id = any( coalesce( "
        "public._whatsapp_phone_variants(p_external_user_id), array[]::text[] ) "
        "|| coalesce( public._whatsapp_phone_variants(v_contact.phone), array[]::text[] ) ) "
        "and optout.correlation_status in ( 'applied', 'unmatched', 'ambiguous', "
        "'evidence_conflict' ) )"
    ) in body
    stop = _normalized(
        (MIGRATIONS / "20261001000100_whatsapp_phone_equivalence.sql").read_text(encoding="utf-8")
    )
    assert (
        "and optout.correlation_status in ( 'applied', 'unmatched', 'ambiguous', "
        "'evidence_conflict' )"
    ) in stop
    assert "chatwoot-opt-out-user" not in body
    live = re.search(r"and action\.status in \(([^)]*)\)", body)
    assert live is not None
    states = [state.strip().strip("'") for state in live.group(1).split(",")]
    assert states == ["pending", "deferred", "retryable_failed", "delivery_unknown"]
    # Son los estados vivos del constraint de 20260803000100: los del indice
    # de una accion viva por secuencia.
    engine = _normalized(FOLLOWUP_ENGINE.read_text(encoding="utf-8"))
    index = re.search(
        r"create unique index scheduled_actions_one_live_per_sequence_idx .*? where status = any \(array\[(.*?)\]\);",
        engine,
    )
    assert index is not None
    assert [state.strip().strip("'") for state in index.group(1).split(",")] == states
    for gone in ("scheduled", "claimed", "validating", "generating", "sending"):
        assert f"'{gone}'" not in body
    # El paso pendiente de cualquier caso de la persona en esta conversacion o
    # todavia sin conversacion (el pago fallido planificado despues del
    # carrito).
    assert (
        "where recovery.contact_id = v_identity.contact_id and ( "
        "recovery.conversation_id = v_conversation.id or recovery.conversation_id is null )"
    ) in body


def test_the_only_writes_are_the_conversation_and_its_event() -> None:
    body = _body()

    writes = re.findall(r"\b(insert into|update|delete from)\s+public\.([a-z_]+)", body)
    assert writes == [("update", "conversations"), ("insert into", "conversation_events")]
    assert (
        "update public.conversations conversation set automation_status = 'draft_only', "
        "version = conversation.version + 1 where conversation.id = v_conversation.id "
        "and conversation.automation_status = 'enabled' "
        "returning conversation.version into strict v_conversation_version;"
    ) in body
    assert (
        "values ( v_conversation.id, v_recovery_case_id, "
        "'inbound_adopted_template_conversation', 'integration', "
        "v_template_message_id, v_template_action_id,"
    ) in body
    assert "template_reply_admitted_inbound" not in body
    assert "'prospect'" not in body


def test_migration_is_transactional_ascii_and_never_names_handoffs() -> None:
    sql = _sql()

    assert sql.isascii()
    assert re.search(
        r"^begin;\nset local lock_timeout = '5s';\nset local statement_timeout = '30s';\n",
        sql,
        re.MULTILINE,
    )
    assert sql.rstrip().endswith("commit;")
    # validate_handoff_postgres.py trata como funcion de derivaciones a toda la
    # que nombre esas tablas, y exige que service_role no la ejecute.
    assert "human_handoff" not in sql


def test_acl_is_explicit() -> None:
    sql = _sql()
    compact = _normalized(sql)

    assert "rolname in ('anon','authenticated','service_role')" in compact
    assert f"revoke all on function public.{NAME}{SIGNATURE} from public;" in compact
    assert (
        f"execute format('revoke all on function public.{NAME}{SIGNATURE} from %I',v_role);"
        in compact
    )
    assert f"grant execute on function public.{NAME}{SIGNATURE} to service_role;" in compact
    roles_block = re.search(r"do \$roles\$.*?\$roles\$;", sql, re.DOTALL)
    assert roles_block is not None
    outside = re.sub(r"--[^\n]*", "", sql.replace(roles_block.group(0), ""))
    assert not re.search(r"^\s*grant\b", outside, re.MULTILINE)
    assert "if exists(select 1 from pg_roles where rolname='service_role') then" in compact
    assert compact.count("grant ") == 1


def test_inventories_and_validators_know_the_new_entrypoint() -> None:
    acl = (ROOT / "scripts" / "supabase_acl_inventory.sql").read_text(encoding="utf-8")
    assert f"('public.{NAME}(text, integer, bigint, text)')" in acl
    hardening = (
        ROOT / "tests" / "sql" / "followup_engine" / "validate_acl_hardening.mjs"
    ).read_text(encoding="utf-8")
    assert f"('{NAME}{SIGNATURE}')" in hardening
    assert "result.expected_count !== 120" in hardening
    schema = (ROOT / "scripts" / "supabase_schema_inventory.sql").read_text(encoding="utf-8")
    assert f"'{MIGRATION.name}'" in schema
    assert f"to_regprocedure('public.{NAME}{SIGNATURE}')" in schema
    package = json.loads(
        (ROOT / "tests" / "sql" / "followup_engine" / "package.json").read_text(encoding="utf-8")
    )
    assert f"node {VALIDATOR.name}" in package["scripts"]["test"]
    validator = VALIDATOR.read_text(encoding="utf-8")
    assert f"public.{NAME}($1,$2,$3,$4)" in validator
    assert f"public.{V2}($1,$2,$3,$4)" in validator
    for role in ("service_role", "anon", "authenticated"):
        assert f"'{role}'" in validator
    assert "42501" in validator
