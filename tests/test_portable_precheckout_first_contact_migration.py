"""Migracion 20261001000200: el primer contacto portable tras el formulario.

La migracion solo crea funciones nuevas; dos de ellas son copias de una
definicion vigente (el arranque del pago fallido y el status del piloto). Este
test verifica que cada copia sea la vigente salvo lo declarado, que ninguna
funcion existente se redefina, que los tres checks solo sumen un valor, la
forma del bloque protegido del entrypoint, que la reevaluacion delegue en la
compartida, que el renglon del plan no tenga columnas con datos de la persona
y que el ACL sea explicito. El comportamiento se prueba en
tests/sql/followup_engine/validate_portable_precheckout_first_contact.mjs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
BASELINE = ROOT / "supabase" / "baseline" / "20260803_public_schema.sql"
MIGRATION = MIGRATIONS / "20261001000200_portable_precheckout_first_contact.sql"
VALIDATOR = "validate_portable_precheckout_first_contact.mjs"
MARKED_BLOCK = re.compile(
    r"[ \t]*-- precheckout_first_contact: begin\n.*?-- precheckout_first_contact: end\n",
    re.DOTALL,
)
STOP = "_portable_precheckout_stop_reason"
FIND = "_find_portable_precheckout_contact"
ENSURE = "_ensure_portable_precheckout_contact"
PLAN = "_plan_portable_precheckout_first_contact"
ADMIT = "admit_and_plan_portable_lead_precheckout"
REEVALUATE = "reevaluate_portable_precheckout_action"
START = "mark_portable_precheckout_request_started"
STATUS = "get_portable_precheckout_pilot_runtime_status"
TABLE = "portable_precheckout_first_contact_plans"
SIGNATURES = {
    STOP: "(uuid,uuid)",
    FIND: "(uuid)",
    ENSURE: "(uuid,uuid)",
    PLAN: "(uuid,uuid,uuid,text,integer)",
    ADMIT: "(text,text,integer,text,jsonb,jsonb,text,integer)",
    REEVALUATE: (
        "(uuid,text,bigint,timestamptz,boolean,text,text,timestamptz,text,"
        "boolean,boolean,boolean,boolean,boolean)"
    ),
    START: "(uuid,uuid,text,bigint,timestamptz)",
    STATUS: "(text,integer,text,text,text)",
}
ENTRYPOINTS = {ADMIT, REEVALUATE, START, STATUS}
# Las dos copias: la funcion nueva, la vigente de la que sale, la migracion que
# la define, los literales que cambian (nuevo, vigente, ocurrencias) y la
# cantidad de bloques marcados que se suman.
COPIES = {
    START: (
        "mark_portable_payment_failure_request_started",
        "20260903000300_commercial_ally_payment_failure_recovery.sql",
        [
            ("and action.anchor_type = 'precheckout_intent'", "and action.anchor_type = 'payment_failure'", 1),
            ("detail = 'precheckout_intent_action_required';", "detail = 'payment_failure_action_required';", 1),
            ("'landing', 'PRECHECKOUT_FORM_SUBMITTED',", "'hotmart', 'PURCHASE_CANCELED',", 1),
        ],
        2,
    ),
    STATUS: (
        "get_lancemos_pilot_runtime_status",
        "20260929000200_pilot_scope_additional_source_events.sql",
        [
            ("or v_scope.source <> 'landing'", "or v_scope.source <> 'hotmart'", 1),
            ("'PRECHECKOUT_FORM_SUBMITTED'", "'PURCHASE_OUT_OF_SHOPPING_CART'", 2),
        ],
        1,
    ),
}


def _function(sql: str, name: str) -> str:
    pattern = re.compile(
        rf"create\s+or\s+replace\s+function\s+public\.{name}\s*\(.*?\$function\$;",
        re.DOTALL,
    )
    matches = pattern.findall(sql)
    assert len(matches) == 1, f"expected one definition of {name}"
    return matches[0]


def _normalized(sql: str) -> str:
    return " ".join(sql.split())


def _executable(sql: str) -> str:
    return _normalized(re.sub(r"--[^\n]*", "", sql))


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _new(name: str) -> str:
    return _function(_sql(), name)


def _earlier_sql() -> dict[str, str]:
    files = {BASELINE.name: BASELINE.read_text(encoding="utf-8")}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name < MIGRATION.name:
            files[path.name] = path.read_text(encoding="utf-8")
    return files


def test_migration_only_creates_new_functions() -> None:
    sql = _sql()
    defined = re.findall(r"create\s+(?:or\s+replace\s+)?function\s+public\.([a-z0-9_]+)\s*\(", sql)

    assert sorted(defined) == sorted(SIGNATURES)
    # Ninguna existia antes: ni definida en un archivo ni derivada con SQL
    # dinamico. Johanna no ejecuta nada de lo que esta migracion crea.
    for filename, earlier in _earlier_sql().items():
        for name in SIGNATURES:
            assert not re.search(rf"\bpublic\.{name}\s*\(", earlier), (filename, name)
    executable = re.sub(r"--[^\n]*", "", sql)
    assert "pg_get_functiondef" not in executable
    assert not re.search(r"\bexecute\s+replace\(", executable)
    assert not re.search(r"\balter\s+function\b", executable, re.IGNORECASE)
    assert not re.search(r"\bdrop\s+(function|table|column|trigger)\b", executable, re.IGNORECASE)
    assert not re.search(r"\bcreate\s+(or\s+replace\s+)?trigger\b", executable, re.IGNORECASE)


@pytest.mark.parametrize("name", sorted(COPIES))
def test_the_copied_definition_is_the_vigent_one(name: str) -> None:
    source, vigent, _, _ = COPIES[name]
    later = [
        path.name
        for path in sorted(MIGRATIONS.glob("*.sql"))
        if vigent < path.name < MIGRATION.name
        and re.search(
            rf"(create\s+(or\s+replace\s+)?function\s+public\.{source}\s*\("
            rf"|pg_get_functiondef\(\s*'public\.{source}\()",
            path.read_text(encoding="utf-8"),
        )
    ]

    assert later == []


@pytest.mark.parametrize("name", sorted(COPIES))
def test_copy_is_the_vigent_one_plus_the_declared_changes(name: str) -> None:
    source, vigent, replacements, marked = COPIES[name]
    original = _function((MIGRATIONS / vigent).read_text(encoding="utf-8"), source)
    new = _new(name)

    assert len(MARKED_BLOCK.findall(new)) == marked
    reverted = _normalized(MARKED_BLOCK.sub("", new))
    assert reverted.count(f"public.{name}(") == 1
    reverted = reverted.replace(f"public.{name}(", f"public.{source}(")
    for changed, vigent_text, count in replacements:
        assert reverted.count(changed) == count, changed
        reverted = reverted.replace(changed, vigent_text)

    assert "precheckout" not in reverted.replace("public.admit_", "")
    assert reverted == _normalized(original)


def test_request_start_rechecks_the_stops_under_both_opt_out_locks() -> None:
    new = _new(START)
    declarations, block = MARKED_BLOCK.findall(new)

    assert _executable(declarations) == "v_variant text; v_stop_reason text;"
    assert _executable(block) == (
        "if not v_replayed then "
        "for v_variant in select variant.phone from unnest( "
        "public._whatsapp_phone_variants(v_identity.external_user_id) ) as variant(phone) "
        "order by variant.phone loop "
        "perform pg_advisory_xact_lock(hashtextextended( "
        "concat_ws(':', 'chatwoot-opt-out-user', v_account_id, v_variant), 0 )); "
        "end loop; "
        f"v_stop_reason := public.{STOP}( "
        "v_binding.audience_purchase_intent_id, v_case.contact_id ); "
        "if v_stop_reason is not null then raise exception using errcode = '55000', "
        "message = 'pilot_request_start_rejected', detail = v_stop_reason; end if; "
        "end if;"
    )
    # El lock es el del opt-out compartido y el de mark_followup_request_started.
    shared = _normalized(
        _function(
            (MIGRATIONS / "20260810000300_lancemos_pilot_boundary_runtime.sql").read_text(
                encoding="utf-8"
            ),
            "mark_followup_request_started",
        )
    )
    assert "':', 'chatwoot-opt-out-user', v_account_id, v_external_user_id" in shared
    # Va despues de la autorizacion del piloto (el control antes que el lock de
    # opt-out, como los otros arranques) y antes del arranque compartido.
    order = [
        new.index("from public.authorize_lancemos_pilot_request_start("),
        new.index("if v_replayed then"),
        new.index(block),
        new.index("from public.mark_followup_request_started("),
    ]
    assert order == sorted(order)


def test_runtime_status_also_rejects_a_manual_cohort_scope() -> None:
    (block,) = MARKED_BLOCK.findall(_new(STATUS))

    assert _executable(block) == "or v_scope.audience_mode = 'manual_cohort'"


def _check_values(statement: str) -> list[str]:
    return sorted(re.findall(r"'([a-z_]+)'", statement))


def test_the_three_checks_only_gain_one_value() -> None:
    sql = re.sub(r"--[^\n]*", "", _sql())
    recovery = (MIGRATIONS / "20260903000300_commercial_ally_payment_failure_recovery.sql").read_text(
        encoding="utf-8"
    )
    baseline = BASELINE.read_text(encoding="utf-8")
    expected = {
        ("recovery_cases", "recovery_cases_source_check"): (
            re.search(r"source text not null default 'hotmart' check \(([^\n]*)\),", baseline),
            "landing",
        ),
        ("recovery_case_events", "recovery_case_events_event_role_check"): (
            re.search(
                r"add constraint recovery_case_events_event_role_check\s+check \((.*?)\);",
                recovery,
                re.DOTALL,
            ),
            "precheckout_intent",
        ),
        ("followup_sequences", "followup_sequences_reason_check"): (
            re.search(
                r"add constraint followup_sequences_reason_check\s+check \((.*?)\);",
                recovery,
                re.DOTALL,
            ),
            "precheckout_intent",
        ),
    }
    for (table, constraint), (vigent, added) in expected.items():
        assert vigent is not None, constraint
        if table == "recovery_cases":
            # El check de source nacio sin nombre: se quita por definicion, no
            # por el nombre que Postgres le puso solo (ver el test de abajo).
            assert f"drop constraint {constraint}" not in _normalized(sql)
        else:
            assert f"alter table public.{table} drop constraint {constraint};" in _normalized(sql)
        new = re.search(
            rf"alter table public\.{table}\s+add constraint {constraint}\s+check \((.*?)\);",
            sql,
            re.DOTALL,
        )
        assert new is not None, constraint
        before = _check_values(vigent.group(1))
        assert added not in before
        assert _check_values(new.group(1)) == sorted([*before, added])
        # Ninguna migracion posterior a la vigente volvio a tocar el check.
        assert not [
            path.name
            for path in sorted(MIGRATIONS.glob("*.sql"))
            if "20260903000300" < path.name[:14] < MIGRATION.name[:14]
            and constraint in path.read_text(encoding="utf-8")
        ]
    # No hay otros alter table: nada mas cambia en tablas existentes.
    assert len(re.findall(r"\balter\s+table\b", sql, re.IGNORECASE)) == 7
    assert f"alter table public.{TABLE} enable row level security;" in sql


def test_the_source_check_is_dropped_by_definition_not_by_name() -> None:
    sql = _sql()
    block = re.search(r"do \$source_check\$\n(.*?)\n\$source_check\$;", sql, re.DOTALL)
    assert block is not None
    body = _executable(block.group(1))

    # Ninguna migracion nombra el check: existe solo inline en el baseline, y
    # el nombre lo eligio Postgres. La migracion no puede depender de el.
    baseline = BASELINE.read_text(encoding="utf-8")
    assert "recovery_cases_source_check" not in baseline
    assert not [
        path.name
        for path in sorted(MIGRATIONS.glob("*.sql"))
        if path.name < MIGRATION.name
        and "recovery_cases_source_check" in path.read_text(encoding="utf-8")
    ]
    # Lo busca entre los checks de la tabla sobre la columna source, por los
    # valores que acepta, y exige exactamente uno.
    assert "from pg_constraint con where con.conrelid = 'public.recovery_cases'::regclass" in body
    assert "and con.contype = 'c'" in body
    assert "and att.attname = 'source'" in body
    assert "pg_get_constraintdef(con.oid)" in body
    assert ") = array['hotmart', 'simulator'];" in body
    assert (
        "if cardinality(v_names) <> 1 then raise exception using errcode = '55000', "
        "message = 'recovery_cases_source_check_not_found', "
        "detail = cardinality(v_names)::text; end if;"
    ) in body
    assert (
        "execute format( 'alter table public.recovery_cases drop constraint %I', v_names[1] );"
    ) in body
    # Y lo repone enseguida con el nombre de siempre, que es el que mira el
    # inventario de esquema.
    after = sql.split(block.group(0), 1)[1].lstrip()
    assert after.startswith(
        "alter table public.recovery_cases\n    add constraint recovery_cases_source_check\n"
    )


def test_plan_row_has_ids_and_codes_only() -> None:
    sql = _sql()
    table = re.search(rf"create table public\.{TABLE} \((.*?)\n\);", sql, re.DOTALL)
    assert table is not None
    columns = re.findall(r"^    ([a-z_]+) (uuid|text|integer|timestamptz)\b", table.group(1), re.MULTILINE)

    assert columns == [
        ("submission_id", "uuid"),
        ("purchase_intent_id", "uuid"),
        ("contact_id", "uuid"),
        ("recovery_case_id", "uuid"),
        ("scope_key", "text"),
        ("scope_version", "integer"),
        ("outcome", "text"),
        ("reason_code", "text"),
        ("error_sqlstate", "text"),
        ("created_at", "timestamptz"),
    ]
    body = _normalized(table.group(1))
    # Los dos textos que escribe la base estan acotados a un codigo.
    assert "outcome text not null check (outcome in ('planned', 'not_planned', 'plan_failed'))" in body
    assert "reason_code text not null check (reason_code ~ '^[a-z0-9_]{1,64}$')" in body
    assert "error_sqlstate text check (error_sqlstate ~ '^[0-9A-Z]{5}$')" in body
    assert "submission_id uuid primary key references public.precheckout_submissions(id)" in body
    # El ancla del caso lleva ids y la fecha del envio, nada de la persona.
    plan = _new(PLAN)
    anchor = re.search(r"insert into public\.webhook_events \((.*?)returning id into v_anchor_event_id;", plan, re.DOTALL)
    assert anchor is not None
    assert re.findall(r"'([a-z_]+)', ", anchor.group(1).split("jsonb_build_object(", 1)[1].split(")", 1)[0]) == [
        "purchase_intent_id",
        "precheckout_submission_id",
        "creation_date",
    ]
    assert "'precheckout-submission:' || p_submission_id::text" in anchor.group(1)
    assert "'system'" in anchor.group(1) and "'processed'" in anchor.group(1)


def test_entrypoint_never_loses_the_admission_to_the_plan() -> None:
    new = _new(ADMIT)
    compact = _executable(new)

    # El control se bloquea antes de la admision, y la admision es la funcion
    # vigente, sin tocar.
    order = [
        new.index("from public.pilot_runtime_controls control"),
        new.index("from public.admit_portable_observed_lead_precheckout("),
        new.index("if v_admission_outcome <> 'inserted' then"),
        new.index(f"from public.{FIND}(v_intent_id) found;"),
        new.index(f"v_reason := public.{STOP}("),
        new.index(f"v_contact_id := public.{ENSURE}("),
        new.index(f"from public.{PLAN}("),
        new.index("set constraints all immediate;"),
        new.index("exception when others then"),
        new.index("set constraints all deferred;"),
        new.index(f"insert into public.{TABLE} ("),
    ]
    assert order == sorted(order)
    # Todo lo que sigue a la admision vive en un unico bloque protegido: lo que
    # se decide sin crear nada, el contacto, el plan y el disparo de los
    # triggers diferidos.
    block = re.search(
        r"\n    begin\n(.*?)\n    exception when others then\n(.*?)\n    end;\n",
        new,
        re.DOTALL,
    )
    assert block is not None
    body, handler = block.groups()
    for inside in (
        "select intent.* into strict v_intent",
        f"from public.{FIND}(v_intent_id) found;",
        f"v_reason := public.{STOP}(",
        f"v_contact_id := public.{ENSURE}(",
        f"from public.{PLAN}(",
        "set constraints all immediate;",
    ):
        assert inside in body, inside
    # Antes del bloque, solo el lock del control, la admision y el retorno del
    # envio repetido; despues, el renglon del plan.
    before, after = new.split(block.group(0))
    assert "from public.admit_portable_observed_lead_precheckout(" in before
    assert f"insert into public.{TABLE} (" in after
    assert " strict " not in _executable(after)
    assert compact.count("set constraints all immediate;") == 1
    assert compact.count("set constraints all deferred;") == 1
    assert compact.count("exception when others then") == 1
    # Solo los transitorios se relanzan.
    assert _executable(handler).startswith(
        "get stacked diagnostics v_sqlstate = returned_sqlstate, v_message = message_text, "
        "v_detail = pg_exception_detail; "
        "if v_sqlstate like '40%' or v_sqlstate like '53%' or v_sqlstate like '57%' "
        "or v_sqlstate like '08%' or v_sqlstate = '55P03' then raise; end if; "
        "v_contact_id := v_found_contact_id; v_case_id := null;"
    )
    assert _executable(handler).count("raise;") == 1
    # Un rechazo del planificador es not_planned con su motivo; lo demas,
    # plan_failed, y el mensaje solo si tiene forma de codigo.
    assert (
        "if v_sqlstate = '55000' and v_message = 'pilot_scope_rejected' "
        "and v_detail ~ '^[a-z0-9_]{1,64}$' then v_plan_outcome := 'not_planned'; "
        "v_plan_reason := v_detail;"
    ) in _executable(handler)
    assert (
        "v_plan_outcome := 'plan_failed'; v_plan_reason := case "
        "when v_message ~ '^[a-z0-9_]{1,64}$' then v_message "
        "else 'plan_error_unclassified' end;"
    ) in _executable(handler)
    # El entrypoint no evalua el scope por su cuenta: lo hace el planificador,
    # que ata la audiencia.
    assert "evaluate_lancemos_pilot_scope" not in new
    # Sin scope es un error del llamador, antes de admitir nada.
    assert new.index("message = 'invalid_pilot_plan_parameters';") < order[0]


def test_planner_binds_the_scope_the_audience_and_the_stops() -> None:
    new = _new(PLAN)
    compact = _executable(new)

    order = [
        new.index("from public.pilot_runtime_controls control"),
        new.index("from public.evaluate_lancemos_pilot_scope("),
        new.index("from public._lancemos_pilot_audience_intent("),
        new.index("if v_audience_submission_id is distinct from p_submission_id then"),
        new.index(f"v_stop_reason := public.{STOP}(v_intent.id, p_contact_id);"),
        new.index("detail = 'precheckout_contact_already_planned';"),
        new.index("insert into public.webhook_events ("),
        new.index("insert into public.recovery_cases ("),
        new.index("insert into public.scheduled_actions ("),
        new.index("insert into public.contact_authorizations ("),
        new.index("insert into public.pilot_recovery_case_bindings ("),
    ]
    assert order == sorted(order)
    assert "'landing', 'PRECHECKOUT_FORM_SUBMITTED'," in compact
    assert "if v_scope.audience_mode = 'manual_cohort' then" in compact
    # La demora y el vencimiento corren desde el envio que dispara el plan, no
    # desde el submitted_at de la intencion.
    assert "v_submitted_at := (v_submission.canonical_payload #>> '{submitted_at}')::timestamptz;" in compact
    assert "v_intent.submitted_at" not in new
    assert compact.count("v_submitted_at + v_policy.grace_period") == 2
    assert compact.count("v_submitted_at + v_policy.expires_after") == 2
    assert "'first_contact_review', 'pending'," in compact
    assert "'first_contact', 'precheckout_intent'," in compact
    # El permiso solo se concede si no hay una fila activa, de cualquier estado.
    grant = new.index("insert into public.contact_authorizations (")
    guard = new.rindex("if not exists (", 0, grant)
    assert "authorization_status" not in new[guard:grant]
    assert "'allowed', 'system'," in compact
    for reason in (
        "pilot_scope_not_published",
        "pilot_source_event_mismatch",
        "precheckout_scope_audience_unsupported",
        "precheckout_submission_mismatch",
        "precheckout_binding_unavailable",
        "precheckout_policy_unavailable",
        "precheckout_submission_expired",
        "precheckout_submission_not_consented",
        "precheckout_contact_already_planned",
    ):
        assert f"detail = '{reason}'" in new, reason


def test_stop_reason_is_read_only_and_names_every_stop() -> None:
    new = _new(STOP)

    assert set(re.findall(r"return '([a-z_]+)';", new)) == {
        "precheckout_intent_not_live",
        "intent_purchased",
        "intent_purchase_ambiguous",
        "precheckout_binding_unavailable",
        "purchase_by_identity",
        "superseded_by_provider_event",
        "precheckout_prior_opt_out",
        "precheckout_conversation_handoff",
    }
    order = [
        new.index("return 'intent_purchased';"),
        new.index("return 'intent_purchase_ambiguous';"),
        new.index("return 'purchase_by_identity';"),
        new.index("return 'superseded_by_provider_event';"),
        new.index("return 'precheckout_prior_opt_out';"),
        new.index("return 'precheckout_conversation_handoff';"),
        new.index("return null;"),
    ]
    assert order == sorted(order)
    # La conversacion derivada, pausada, cerrada o bloqueada frena con el
    # criterio del primer toque de Johanna (blocked_handoff), copiado de las dos
    # funciones que lo aplican alla: al reservar y al arrancar.
    criterion = (
        "conversation.contact_id = {owner} and ( conversation.human_takeover "
        "or conversation.status in ( 'paused_human', 'closed', 'blocked' ) "
        "or conversation.automation_status in ( 'paused', 'disabled', 'restricted', 'error' ) )"
    )
    for johanna in (
        "20260829000300_precheckout_delayed_one_shot_reservation.sql",
        "20260829000400_precheckout_delayed_worker_sender.sql",
    ):
        assert criterion.format(owner="contact.id") in _executable(
            (MIGRATIONS / johanna).read_text(encoding="utf-8")
        ), johanna
    assert (
        "if p_contact_id is not null and exists ( select 1 from public.conversations conversation where "
        + criterion.format(owner="p_contact_id")
        + " ) then return 'precheckout_conversation_handoff'; end if;"
    ) in _executable(new)
    for helper in (STOP, FIND):
        text = _new(helper)
        assert "language plpgsql stable security invoker" in _normalized(text)
        assert not re.search(r"\b(insert|update|delete)\b", _executable(text), re.IGNORECASE)
        assert "for update" not in _executable(text).lower()
    # El opt-out previo usa los estados que frena el arranque compartido y las
    # dos formas del telefono; la compra por identidad no mira ninguna ventana.
    compact = _executable(new)
    assert (
        "optout.external_user_id = any( public._whatsapp_phone_variants(v_intent.normalized_phone) ) "
        "and optout.correlation_status in ( 'applied', 'unmatched', 'ambiguous', 'evidence_conflict' )"
    ) in compact
    assert "max_lookback" not in new and "submitted_at" not in new
    # El contacto se busca tambien por la identidad de WhatsApp de la cuenta.
    find = _executable(_new(FIND))
    assert "from public.channel_identities identity" in find
    assert find.count("public._whatsapp_phone_variants(v_intent.normalized_phone)") == 2
    assert "where lower(contact.email) = v_intent.normalized_email" in find


def test_reevaluation_delegates_to_the_shared_one_and_never_wins_the_case() -> None:
    new = _new(REEVALUATE)
    compact = _executable(new)
    shared = _function(
        (MIGRATIONS / "20260805000300_per_case_conversation_anchor.sql").read_text(encoding="utf-8"),
        "reevaluate_followup_action",
    )

    # La misma firma y el mismo retorno que la funcion compartida.
    def head(text: str) -> str:
        return _normalized(text.split("language plpgsql", 1)[0]).split("(", 1)[1].lower()

    assert head(new) == head(shared)
    assert "language plpgsql security definer set search_path = pg_catalog, public, pg_temp" in compact
    assert "if v_action.anchor_type <> 'precheckout_intent' then" in compact
    arguments = re.findall(r"^    (p_[a-z_]+) ", new.split("returns table", 1)[0], re.MULTILINE)
    assert len(arguments) == 14
    call = compact.split("from public.reevaluate_followup_action(", 1)[1].split(")", 1)[0]
    assert [argument.strip() for argument in call.split(",")] == arguments
    # El replay se mira antes que el lease y que los frenos.
    order = [
        new.index("-- Replay after a committed response was lost."),
        new.index("message = 'current_action_lease_not_found';"),
        new.index(f"v_reason := public.{STOP}("),
        new.index("from public._lancemos_pilot_audience_intent("),
        new.index("from public.reevaluate_followup_action("),
        new.index("set status = 'cancelled', terminal_reason = v_reason,"),
    ]
    assert order == sorted(order)
    # Cancela; nunca gana el caso ni le atribuye una compra (D2).
    assert "'won'" not in new and "purchase_event_id" not in new
    assert "set status = 'cancelled', closed_at = p_now, version = version + 1" in compact
    assert "v_reason := 'precheckout_authorization_lost';" in compact


def test_acl_is_explicit() -> None:
    sql = _sql()
    compact = _normalized(sql)
    executable = re.sub(r"--[^\n]*", "", sql)

    assert "rolname in ('anon','authenticated','service_role')" in compact
    assert f"revoke all on table public.{TABLE} from public;" in compact
    assert f"execute format('revoke all on table public.{TABLE} from %I',v_role);" in compact
    assert not re.search(rf"\bgrant\b[^;]*public\.{TABLE}\b", executable)
    for name, signature in SIGNATURES.items():
        assert f"revoke all on function public.{name}{signature} from public;" in compact
        assert (
            f"execute format('revoke all on function public.{name}{signature} from %I',v_role);"
            in compact
        )
        granted = f"grant execute on function public.{name}{signature} to service_role;"
        assert (granted in compact) is (name in ENTRYPOINTS), name
        if name not in ENTRYPOINTS:
            assert not re.search(rf"\bgrant\b[^;]*public\.{name}\(", executable), name
    # Toda funcion security definer fija su search_path.
    for name in SIGNATURES:
        text = _normalized(_new(name))
        assert "set search_path = " in text.split(" as $function$", 1)[0], name
    for name in (ENSURE, PLAN, ADMIT, REEVALUATE, START, STATUS):
        assert "security definer" in _normalized(_new(name)).split(" as $function$", 1)[0], name
    # Los grants van dentro del bloque que tolera que los roles no existan.
    roles_block = re.search(r"do \$roles\$.*?\$roles\$;", sql, re.DOTALL)
    assert roles_block is not None
    outside = sql.replace(roles_block.group(0), "")
    assert not re.search(r"^\s*grant\b", re.sub(r"--[^\n]*", "", outside), re.MULTILINE)
    assert "if exists(select 1 from pg_roles where rolname='service_role') then" in compact


def test_migration_is_transactional_and_seeds_nothing() -> None:
    sql = _sql()
    outside_functions = re.sub(r"\$function\$.*?\$function\$", "", sql, flags=re.DOTALL)
    statements = re.sub(r"--[^\n]*", "", outside_functions).split(";")

    assert sql.isascii()
    assert re.search(
        r"^begin;\nset local lock_timeout = '5s';\nset local statement_timeout = '30s';\n",
        sql,
        re.MULTILINE,
    )
    assert sql.rstrip().endswith("commit;")
    assert not [
        statement
        for statement in statements
        if re.match(r"\s*(insert|update|delete|truncate)\b", statement, re.IGNORECASE)
    ]
    # validate_handoff_postgres.py trata como funcion de derivaciones a toda la
    # que nombre esas tablas, y exige que service_role no la ejecute.
    assert "human_handoff" not in sql


def test_validator_and_fixture_are_registered() -> None:
    package = json.loads(
        (ROOT / "tests" / "sql" / "followup_engine" / "package.json").read_text(encoding="utf-8")
    )
    assert package["scripts"]["test"].endswith(f"&& node {VALIDATOR}")
    validator = (ROOT / "tests" / "sql" / "followup_engine" / VALIDATOR).read_text(encoding="utf-8")
    assert MIGRATION.name in validator
    # La reevaluacion real: el validador nunca inserta la decision a mano.
    assert "reevaluate_portable_precheckout_action(" in validator
    assert not re.search(r"insert\s+into\s+public\.conversation_events", validator, re.IGNORECASE)
    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "instances" / "att1" / "politica-piloto.json").read_text(
            encoding="utf-8"
        )
    )
    first_contact = fixture["first_contact"]
    assert first_contact["pilot_scope"]["source"] == "landing"
    assert first_contact["pilot_scope"]["source_event_type"] == "PRECHECKOUT_FORM_SUBMITTED"
    assert first_contact["pilot_scope"]["audience_mode"] == "consented_intent_in_cohort"
    assert first_contact["policy"]["grace_period"] == "60 minutes"
    assert [step["step_key"] for step in first_contact["policy"]["steps"]] == ["first_contact"]
    assert re.fullmatch(r"[0-9a-f]{40}", fixture["_fixture"]["source_commit"])
    assert re.fullmatch(r"[0-9a-f]{64}", fixture["_fixture"]["source_sha256"])
