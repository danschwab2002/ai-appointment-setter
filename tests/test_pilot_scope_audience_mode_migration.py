"""Migracion 20260930000300: la audiencia del scope del piloto.

Las cuatro funciones que toca se copian de su definicion vigente. Este test
verifica que cada copia, sin los bloques marcados ``pilot_audience``, sea
identica a la vigente (con los espacios normalizados), que en ``manual_cohort``
cada bloque sea inerte, y que el helper nuevo quede privado. El comportamiento
se prueba en tests/sql/followup_engine/validate_pilot_scope_audience_mode.mjs y
en validate_att1_portable_chain.mjs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20260930000300_pilot_scope_audience_mode.sql"
HELPER = "_lancemos_pilot_audience_intent"
HELPER_SIGNATURE = f"public.{HELPER}(text,integer,uuid,text,uuid,text)"
# Cada funcion copiada y la migracion que tiene su definicion vigente.
VIGENT = {
    "evaluate_lancemos_pilot_scope": "20260929000200_pilot_scope_additional_source_events.sql",
    "authorize_lancemos_pilot_request_start": "20260929000200_pilot_scope_additional_source_events.sql",
    "plan_lancemos_pilot_cart_recovery": "20260810000300_lancemos_pilot_boundary_runtime.sql",
    "plan_portable_payment_failure_recovery": "20260930000100_payment_failure_consented_intent_authorization.sql",
}
SIGNATURES = {
    "evaluate_lancemos_pilot_scope": "(text,integer,text,bigint,bigint,text,text,text,text,text,text,uuid)",
    "authorize_lancemos_pilot_request_start": "(text,integer,text,bigint,bigint,text,text,text,text,text,text,uuid,uuid,uuid,timestamptz)",
    "plan_lancemos_pilot_cart_recovery": "(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer)",
    "plan_portable_payment_failure_recovery": "(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer)",
}
MARKED_BLOCK = re.compile(
    r"[ \t]*-- pilot_audience: begin\n.*?-- pilot_audience: end\n",
    re.DOTALL,
)


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


def _new(name: str) -> str:
    return _function(MIGRATION.read_text(encoding="utf-8"), name)


@pytest.mark.parametrize("name", sorted(VIGENT))
def test_the_copied_definition_is_the_vigent_one(name: str) -> None:
    vigent = VIGENT[name]
    later = [
        path.name
        for path in sorted(MIGRATIONS.glob("*.sql"))
        if vigent < path.name < MIGRATION.name
        and re.search(
            rf"create\s+or\s+replace\s+function\s+public\.{name}\s*\(",
            path.read_text(encoding="utf-8"),
        )
    ]

    assert later == []


@pytest.mark.parametrize("name", sorted(VIGENT))
def test_body_without_the_audience_blocks_is_the_vigent_one(name: str) -> None:
    vigent = _function((MIGRATIONS / VIGENT[name]).read_text(encoding="utf-8"), name)
    new = _new(name)

    assert MARKED_BLOCK.findall(new), name
    assert _normalized(MARKED_BLOCK.sub("", new)) == _normalized(vigent)


def test_evaluate_skips_only_the_cohort_and_only_in_consented_intent() -> None:
    new = _new("evaluate_lancemos_pilot_scope")
    blocks = MARKED_BLOCK.findall(new)

    assert len(blocks) == 1
    assert _executable(blocks[0]) == (
        "elsif v_scope.audience_mode = 'consented_intent' then "
        "v_reason := 'pilot_scope_allowed';"
    )
    # Va despues de la oferta y antes de la cohorte: todo lo demas se sigue
    # chequeando en los tres modos.
    assert new.index("v_reason := 'pilot_offer_mismatch';") < new.index(blocks[0])
    assert new.index(blocks[0]) < new.index("v_reason := 'pilot_contact_not_in_cohort';")


def test_authorize_rechecks_the_intent_before_the_budget() -> None:
    new = _new("authorize_lancemos_pilot_request_start")
    blocks = MARKED_BLOCK.findall(new)
    compact = [_executable(block) for block in blocks]

    assert len(blocks) == 4
    assert "v_audience_data jsonb := '{}'::jsonb;" in compact[0]
    assert compact[1] == "v_scope.audience_mode <> 'consented_intent' and"
    assert new.index(blocks[1]) < new.index("'pilot_contact_not_in_cohort'::text")
    recheck = compact[2]
    assert recheck.startswith("if v_scope.audience_mode <> 'manual_cohort' then")
    assert "where binding.recovery_case_id = v_case.id" in recheck
    assert "or v_binding.audience_mode <> v_scope.audience_mode" in recheck
    assert (
        "from public.purchase_intents intent "
        "where intent.id = v_binding.audience_purchase_intent_id for share;"
    ) in recheck
    assert recheck.index("for share") < recheck.index(f"from public.{HELPER}(")
    assert "v_binding.audience_purchase_intent_id, v_identity.external_user_id" in recheck
    assert "if v_audience_reason is distinct from 'pilot_audience_allowed' then" in recheck
    # Despues de validar la identidad y antes de contar el presupuesto.
    assert new.index("v_identity.metadata ->> 'inbox_id'") < new.index(blocks[2])
    assert new.index(blocks[2]) < new.index("v_local_date := ")
    assert new.index(blocks[2]) < new.index("'pilot_total_budget_exhausted'")
    # El replay no re-chequea: sus returns estan antes del bloque.
    assert new.rindex("'pilot_request_start_authorized'::text,\n            v_existing") < (
        new.index(blocks[2])
    )
    assert compact[3] == "|| v_audience_data"
    assert new.index("jsonb_build_object('local_budget_date', v_local_date)") < (
        new.index(blocks[3])
    )


@pytest.mark.parametrize(
    "name", ["plan_lancemos_pilot_cart_recovery", "plan_portable_payment_failure_recovery"]
)
def test_planners_bind_the_intent_of_the_event_before_planning(name: str) -> None:
    new = _new(name)
    blocks = MARKED_BLOCK.findall(new)
    compact = [_executable(block) for block in blocks]

    assert len(blocks) == 4
    assert compact[0] == "v_audience_intent_id uuid; v_audience_reason text;"
    check = compact[1]
    assert check.startswith("if v_scope.audience_mode <> 'manual_cohort' then")
    assert f"from public.{HELPER}(" in check
    assert "p_external_user_id ) audience;" in check
    assert (
        "raise exception using errcode = '55000', message = 'pilot_scope_rejected', "
        "detail = coalesce(v_audience_reason, 'pilot_audience_intent_unresolved');"
    ) in check
    assert new.index("detail = 'pilot_policy_mismatch';") < new.index(blocks[1])
    assert new.index(blocks[1]) < new.index("_recovery_with_identity(")
    assert compact[2] == ", audience_mode, audience_purchase_intent_id"
    assert compact[3] == ", v_scope.audience_mode, v_audience_intent_id"
    if name == "plan_lancemos_pilot_cart_recovery":
        # La evidencia es la correlacion resuelta de ESTE evento de carrito.
        assert (
            "where correlation.webhook_event_id = p_webhook_event_id "
            "and correlation.event_type = 'PURCHASE_OUT_OF_SHOPPING_CART' "
            "and correlation.outcome = 'resolved';"
        ) in check
        assert "p_offer_code, v_audience_intent_id, p_external_user_id" in check
    else:
        # La intencion ya validada contra el evento, la oferta y el telefono.
        assert "p_offer_code, v_purchase_intent.id, p_external_user_id" in check
        assert new.index("message = 'payment_failure_recipient_mismatch'") < (
            new.index(blocks[1])
        )


def test_helper_ties_the_scope_and_applies_the_consented_intent_criterion() -> None:
    raw = _new(HELPER)
    helper = _executable(raw)

    assert "language plpgsql stable security invoker" in helper
    assert "set search_path = pg_catalog, public, pg_temp" in helper
    for predicate in (
        "scope.status = 'published'",
        "v_scope.audience_mode not in ('consented_intent_in_cohort', 'consented_intent')",
        "v_intent.tenant_ref is distinct from v_scope.tenant_key",
        "v_intent.offer_ref is distinct from p_offer_code",
        "p_offer_code <> all(v_scope.additional_offer_codes)",
        "mapping.active",
        "mapping.hotmart_product_id = v_scope.external_product_id",
        "mapping.offer_ref = v_intent.offer_ref",
        "from public._portable_consented_intent_reason( v_intent.id, p_contact_id, p_destination_phone )",
        "reason_code := 'pilot_audience_' || coalesce(v_consent_reason, 'consented_intent_unknown');",
    ):
        assert predicate in helper, predicate
    reasons = set(re.findall(r"reason_code := '([a-z_]+)';", raw))
    assert reasons == {
        "pilot_audience_input_invalid",
        "pilot_audience_intent_unresolved",
        "pilot_audience_intent_scope_mismatch",
        "pilot_audience_allowed",
    }
    for literal in ("johanna", "att1", "'lancemos'", "8104005", "5071808"):
        assert literal not in helper.lower(), literal


def test_ddl_keeps_every_existing_row_in_manual_cohort() -> None:
    compact = _executable(MIGRATION.read_text(encoding="utf-8"))

    assert (
        "alter table public.pilot_scope_versions "
        "add column audience_mode text not null default 'manual_cohort';"
    ) in compact
    assert (
        "audience_mode = 'manual_cohort' or ( audience_mode in "
        "('consented_intent_in_cohort', 'consented_intent') "
        "and source in ('hotmart', 'landing') )"
    ) in compact
    assert (
        "add column audience_mode text not null default 'manual_cohort', "
        "add column audience_purchase_intent_id uuid "
        "references public.purchase_intents(id) on delete restrict;"
    ) in compact
    assert (
        "(audience_mode = 'manual_cohort' and audience_purchase_intent_id is null) "
        "or ( audience_mode in ('consented_intent_in_cohort', 'consented_intent') "
        "and audience_purchase_intent_id is not null )"
    ) in compact


def test_acl_is_explicit() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    compact = _normalized(sql)
    executable = re.sub(r"--[^\n]*", "", sql)

    assert f"revoke all on function {HELPER_SIGNATURE} from public;" in compact
    assert (
        f"execute format('revoke all on function {HELPER_SIGNATURE} from %I',v_role);"
        in compact
    )
    assert "rolname in ('anon','authenticated','service_role')" in compact
    assert not re.search(rf"\bgrant\b[^;]*{HELPER}", executable)
    for name, signature in SIGNATURES.items():
        assert f"revoke all on function public.{name}{signature} from public;" in compact
        assert (
            f"execute format('revoke all on function public.{name}{signature} from %I',v_role);"
            in compact
        )
        granted = f"grant execute on function public.{name}{signature} to service_role;"
        # authorize no es un entrypoint: solo la llaman los mark_*_request_started.
        assert (granted in compact) is (name != "authorize_lancemos_pilot_request_start")


def test_migration_is_transactional_and_seeds_nothing() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    outside_functions = re.sub(r"\$function\$.*?\$function\$", "", sql, flags=re.DOTALL)

    assert re.search(r"^begin;\nset local lock_timeout = '5s';\n", sql, re.MULTILINE)
    assert sql.rstrip().endswith("commit;")
    # Ningun DML fuera de las funciones (el "on delete restrict" de la FK no es
    # DML): la migracion no siembra ni corrige filas.
    statements = re.sub(r"--[^\n]*", "", outside_functions).split(";")
    assert not [
        statement
        for statement in statements
        if re.match(r"\s*(insert|update|delete)\b", statement, re.IGNORECASE)
    ]
    assert "insert into public.pilot_scope_versions" not in sql
    assert not re.search(r"\bdrop\s+(table|function|constraint|column)\b", sql, re.IGNORECASE)
