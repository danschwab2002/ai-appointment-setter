"""Migracion 20261001000100: equivalencia de telefonos de WhatsApp.

Las siete funciones que reemplaza se copian de su definicion vigente. Este test
verifica que cada copia sea la vigente salvo lo declarado: las comparaciones
del telefono que pasan a la forma canonica (se cuentan, se revierten a la
exacta y recien ahi se compara) y lo aditivo, que va entre comentarios
``whatsapp_phone_equivalence``. Tambien que el correlador portable se derive
del compartido con ocurrencias exactas, que los helpers nuevos queden privados
y que la migracion no redefina nada de lo que ejecuta Johanna. Los dos
arranques del piloto frenan con un opt-out en cualquiera de las formas. La
reserva portable del enlace de pago se deriva de la compartida con ocurrencias
exactas, como el correlador. El comportamiento se prueba en
tests/sql/followup_engine/validate_whatsapp_phone_equivalence.mjs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261001000100_whatsapp_phone_equivalence.sql"
MARKED_BLOCK = re.compile(
    r"[ \t]*-- whatsapp_phone_equivalence: begin\n.*?-- whatsapp_phone_equivalence: end\n",
    re.DOTALL,
)
CART = "admit_portable_hotmart_cart_abandonment"
PURCHASE = "admit_portable_hotmart_purchase_approved"
FAILURE = "admit_portable_hotmart_payment_failure"
HELPER = "_portable_consented_intent_reason"
PLAN = "plan_portable_payment_failure_recovery"
CANONICAL = "_whatsapp_phone_canonical"
VARIANTS = "_whatsapp_phone_variants"
PORTABLE_CORRELATOR = "_correlate_portable_hotmart_purchase_intent"
OPT_OUT_STOP = "_portable_chatwoot_opt_out_stop"
PILOT_START = "mark_lancemos_pilot_request_started"
FAILURE_START = "mark_portable_payment_failure_request_started"
SHARED_CORRELATOR = "correlate_hotmart_purchase_intent"
PORTABLE_RESERVE = "reserve_portable_checkout_issuance_v2"
SHARED_RESERVE = "reserve_chatwoot_checkout_issuance_v2"
# La definicion vigente de la reserva compartida (la ultima que la redefine).
SHARED_RESERVE_VIGENT = "20260927000200_sck_length_accepts_255_v1.sql"
# Cada funcion copiada y la migracion que tiene su definicion vigente.
VIGENT = {
    CART: "20260928000200_commercial_ally_additional_offers.sql",
    PURCHASE: "20260928000200_commercial_ally_additional_offers.sql",
    FAILURE: "20260928000200_commercial_ally_additional_offers.sql",
    HELPER: "20260930000100_payment_failure_consented_intent_authorization.sql",
    PLAN: "20260930000300_pilot_scope_audience_mode.sql",
    PILOT_START: "20260810000300_lancemos_pilot_boundary_runtime.sql",
    FAILURE_START: "20260903000300_commercial_ally_payment_failure_recovery.sql",
}
HOTMART_ADMISSION = "(text,text,integer,text,jsonb,text,text)"
REQUEST_START = "(uuid,uuid,text,bigint,timestamptz)"
SIGNATURES = {
    CANONICAL: "(text)",
    VARIANTS: "(text)",
    PORTABLE_CORRELATOR: "(uuid)",
    HELPER: "(uuid,uuid,text)",
    CART: HOTMART_ADMISSION,
    PURCHASE: HOTMART_ADMISSION,
    FAILURE: HOTMART_ADMISSION,
    PLAN: "(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer)",
    OPT_OUT_STOP: "(bigint,uuid,text)",
    PILOT_START: REQUEST_START,
    FAILURE_START: REQUEST_START,
    PORTABLE_RESERVE: "(uuid,text,bigint,bigint,bigint,text,text,timestamptz)",
}
ENTRYPOINTS = {CART, PURCHASE, FAILURE, PLAN, PILOT_START, FAILURE_START, PORTABLE_RESERVE}
# Por funcion: (comparacion nueva, comparacion vigente, ocurrencias). Todo con
# los espacios normalizados.
CANONICAL_COMPARISONS = {
    CART: [
        (
            f"from public.{PORTABLE_CORRELATOR}(v_event_id) correlation;",
            f"from public.{SHARED_CORRELATOR}(v_event_id) correlation;",
            1,
        ),
    ],
    FAILURE: [
        (
            f"from public.{PORTABLE_CORRELATOR}(v_event_id) correlation;",
            f"from public.{SHARED_CORRELATOR}(v_event_id) correlation;",
            1,
        ),
    ],
    PURCHASE: [
        (
            f"intent.normalized_phone = any(public.{VARIANTS}(v_phone))",
            "intent.normalized_phone = v_phone",
            2,
        ),
    ],
    HELPER: [
        (
            f"or public.{CANONICAL}(v_intent.normalized_phone) "
            f"is distinct from public.{CANONICAL}(p_destination_phone)",
            "or v_intent.normalized_phone <> p_destination_phone",
            1,
        ),
        (
            f"and point.normalized_value = any( public.{VARIANTS}(v_intent.normalized_phone) )",
            "and point.normalized_value = v_intent.normalized_phone",
            1,
        ),
    ],
    PLAN: [
        (
            f"and public.{CANONICAL}(intent.normalized_phone) "
            f"= public.{CANONICAL}(p_external_user_id);",
            "and intent.normalized_phone = p_external_user_id;",
            1,
        ),
        (
            "and point.normalized_value = any( "
            f"public.{VARIANTS}(v_purchase_intent.normalized_phone) )",
            "and point.normalized_value = v_purchase_intent.normalized_phone",
            1,
        ),
    ],
    # Los arranques no cambian ninguna comparacion: solo suman su bloque.
    PILOT_START: [],
    FAILURE_START: [],
}
MARKED_BLOCKS = {
    CART: 0,
    FAILURE: 0,
    PURCHASE: 0,
    HELPER: 2,
    PLAN: 1,
    PILOT_START: 1,
    FAILURE_START: 1,
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


@pytest.mark.parametrize("name", sorted(VIGENT))
def test_the_copied_definition_is_the_vigent_one(name: str) -> None:
    vigent = VIGENT[name]
    later = [
        path.name
        for path in sorted(MIGRATIONS.glob("*.sql"))
        if vigent < path.name < MIGRATION.name
        and re.search(
            rf"(create\s+(or\s+replace\s+)?function\s+public\.{name}\s*\("
            rf"|pg_get_functiondef\(\s*'public\.{name}\()",
            path.read_text(encoding="utf-8"),
        )
    ]

    assert later == []


@pytest.mark.parametrize("name", sorted(VIGENT))
def test_body_is_the_vigent_one_plus_the_declared_changes(name: str) -> None:
    vigent = _function((MIGRATIONS / VIGENT[name]).read_text(encoding="utf-8"), name)
    new = _new(name)

    assert len(MARKED_BLOCK.findall(new)) == MARKED_BLOCKS[name]
    reverted = _normalized(MARKED_BLOCK.sub("", new))
    for canonical, exact, count in CANONICAL_COMPARISONS[name]:
        assert reverted.count(canonical) == count, canonical
        reverted = reverted.replace(canonical, exact)

    # Nada de la equivalencia queda fuera de lo declarado arriba.
    assert "_whatsapp_phone_" not in reverted
    assert PORTABLE_CORRELATOR not in reverted
    assert OPT_OUT_STOP not in reverted
    assert reverted == _normalized(vigent)


def test_helper_adds_two_checks_in_the_planned_order() -> None:
    new = _new(HELPER)
    contact_phone, opt_out = MARKED_BLOCK.findall(new)

    # F1: el envio sale a contacts.phone. Va despues del motivo que ya existia
    # para el telefono (no lo cambia) y antes del binding.
    assert _executable(contact_phone) == (
        "if not exists ( select 1 from public.contacts contact "
        "where contact.id = p_contact_id "
        f"and public.{CANONICAL}(contact.phone) "
        f"= public.{CANONICAL}(v_intent.normalized_phone) ) then "
        "reason_code := 'consented_intent_contact_phone_mismatch'; return; end if;"
    )
    # Opt-out previo en cualquiera de las dos formas, en la cuenta del binding
    # y con los mismos estados que frena el arranque.
    assert _executable(opt_out) == (
        "if exists ( select 1 from public.contact_opt_out_events optout "
        "where optout.source = 'chatwoot' and optout.channel = 'whatsapp' "
        "and optout.canonical_account_id = v_binding.chatwoot_account_id "
        f"and optout.external_user_id = any( public.{VARIANTS}(v_intent.normalized_phone) ) "
        "and optout.correlation_status in ( "
        "'applied', 'unmatched', 'ambiguous', 'evidence_conflict' ) ) then "
        "reason_code := 'consented_intent_prior_opt_out'; return; end if;"
    )
    request_start = _function(
        (MIGRATIONS / "20260810000300_lancemos_pilot_boundary_runtime.sql").read_text(
            encoding="utf-8"
        ),
        "mark_followup_request_started",
    )
    assert (
        "optout.correlation_status in ( 'applied', 'unmatched', 'ambiguous', 'evidence_conflict' )"
        in _normalized(request_start)
    )

    order = [
        new.index("reason_code := 'consented_intent_phone_mismatch';"),
        new.index(contact_phone),
        new.index("reason_code := 'consented_intent_binding_unavailable';"),
        new.index(opt_out),
        new.index("select submission.id into precheckout_submission_id"),
        new.index("reason_code := 'consented_intent_submission_missing';"),
    ]
    assert order == sorted(order)
    # El envio del formulario y su intencion son la misma fuente: exacto.
    assert (
        "and submission.canonical_payload #>> '{identity,phone}' = v_intent.normalized_phone"
        in _normalized(new)
    )
    assert set(re.findall(r"reason_code := '([a-z_]+)'", new)) == {
        "consented_intent_input_invalid",
        "consented_intent_not_found",
        "consented_intent_not_live",
        "consented_intent_not_authorized",
        "consented_intent_phone_mismatch",
        "consented_intent_contact_phone_mismatch",
        "consented_intent_binding_unavailable",
        "consented_intent_prior_opt_out",
        "consented_intent_submission_missing",
        "consented_intent_ok",
    }
    # Sigue siendo de solo lectura.
    assert "language plpgsql stable security invoker" in _normalized(new)
    assert not re.search(r"\b(insert|update|delete)\b", _executable(new), re.IGNORECASE)
    assert "for update" not in _executable(new).lower()


def test_plan_records_the_phone_match_in_the_grant_evidence() -> None:
    new = _new(PLAN)
    (block,) = MARKED_BLOCK.findall(new)

    assert _executable(block) == (
        ", 'phone_match', case "
        "when v_purchase_intent.normalized_phone = p_external_user_id then 'exact' "
        "else 'whatsapp_equivalent' end"
    )
    grant = re.search(
        r"-- consented_intent_grant: begin\n(?!.*-- consented_intent_grant: begin\n).*?"
        r"-- consented_intent_grant: end\n",
        new,
        re.DOTALL,
    )
    assert grant is not None and block in grant.group(0)
    assert new.index("'recovery_case_id', v_recovery_case_id\n") < new.index(block)
    assert new.index(block) < new.index("            v_consent_now\n        );")


@pytest.mark.parametrize("name", [PILOT_START, FAILURE_START])
def test_request_starts_stop_on_an_opt_out_in_either_form(name: str) -> None:
    new = _new(name)
    (block,) = MARKED_BLOCK.findall(new)

    # El rechazo tiene la forma que el bridge toma como rechazo limpio
    # (_pilot_request_start_rejection): 55000, el mensaje del piloto y el
    # motivo como codigo en detail.
    assert _executable(block) == (
        "if not coalesce(v_replayed, false) "
        f"and public.{OPT_OUT_STOP}( v_account_id, v_case.contact_id, "
        "v_identity.external_user_id ) then "
        "raise exception using errcode = '55000', "
        "message = 'pilot_request_start_rejected', "
        "detail = 'pilot_chatwoot_opt_out_stop'; end if;"
    )
    # Despues de la autorizacion y del replay (el control antes que el lock de
    # opt-out, como el arranque del primer contacto) y antes del arranque
    # compartido, que despues toma otra vez el lock de la forma exacta.
    replay = new.index("\n    if v_replayed then\n")
    order = [
        new.index("from public.authorize_lancemos_pilot_request_start("),
        replay,
        new.index("\n    end if;\n", replay),
        new.index(block),
        new.index("from public.mark_followup_request_started("),
    ]
    assert order == sorted(order)


def test_opt_out_stop_locks_and_reads_every_form_like_the_shared_start() -> None:
    helper = _executable(_new(OPT_OUT_STOP))
    shared = _normalized(
        _function(
            (MIGRATIONS / "20260810000300_lancemos_pilot_boundary_runtime.sql").read_text(
                encoding="utf-8"
            ),
            "mark_followup_request_started",
        )
    )

    assert "returns boolean language plpgsql volatile security invoker" in helper
    assert "set search_path = pg_catalog, public, pg_temp" in helper
    # Las formas de la identidad y las de contacts.phone (adonde sale el
    # envio), sin repetir y en orden.
    assert "array_agg(distinct variant.user_id order by variant.user_id)" in helper
    assert f"coalesce(public.{VARIANTS}(p_external_user_id), array[]::text[])" in helper
    assert (
        f"select public.{VARIANTS}(contact.phone) from public.contacts contact "
        "where contact.id = p_contact_id"
    ) in helper
    # El lock de cada forma es el de apply_chatwoot_inbound_opt_out y el del
    # arranque compartido, con la misma clave.
    assert (
        "foreach v_user_id in array v_user_ids loop "
        "perform pg_advisory_xact_lock(hashtextextended( "
        "concat_ws(':', 'chatwoot-opt-out-user', p_account_id, v_user_id), 0 )); "
        "end loop;"
    ) in helper
    assert "':', 'chatwoot-opt-out-user', v_account_id, v_external_user_id" in shared
    # Los mismos estados que frena el arranque compartido, en la cuenta.
    states = "optout.correlation_status in ( 'applied', 'unmatched', 'ambiguous', 'evidence_conflict' )"
    assert states in shared
    assert (
        "return exists ( select 1 from public.contact_opt_out_events optout "
        "where optout.source = 'chatwoot' and optout.channel = 'whatsapp' "
        "and optout.canonical_account_id = p_account_id "
        f"and optout.external_user_id = any(v_user_ids) and {states} );"
    ) in helper
    # No escribe nada.
    assert not re.search(r"\b(insert|update|delete)\b", helper, re.IGNORECASE)
    assert "for update" not in helper.lower()


def test_canonical_form_rewrites_only_thirteen_digit_mexico_and_argentina() -> None:
    canonical = _executable(_new(CANONICAL))
    variants = _executable(_new(VARIANTS))

    for helper in (canonical, variants):
        assert "language sql immutable strict security invoker" in helper
        assert "set search_path = pg_catalog, public, pg_temp" in helper
    assert "returns text language" in canonical
    assert "returns text[] language" in variants
    assert "regexp_replace(p_phone, '[^0-9]', '', 'g')" in canonical
    assert re.findall(r"~ '([^']+)'", canonical) == ["^521[0-9]{10}$", "^549[0-9]{10}$"]
    assert "then '52' || substr(normalized.digits, 4)" in canonical
    assert "then '54' || substr(normalized.digits, 4)" in canonical
    assert "else nullif(normalized.digits, '')" in canonical
    assert re.findall(r"~ '([^']+)'", variants) == ["^52[0-9]{10}$", "^54[0-9]{10}$"]
    assert "array[canonical.phone, '521' || substr(canonical.phone, 3)]" in variants
    assert "array[canonical.phone, '549' || substr(canonical.phone, 3)]" in variants
    assert f"select public.{CANONICAL}(p_phone) as phone" in variants


def test_portable_correlator_is_derived_with_exact_occurrences() -> None:
    sql = _sql()
    (block,) = re.findall(r"do \$migration\$.*?\$migration\$;", sql, re.DOTALL)
    compact = _executable(block)

    assert (
        f"select pg_get_functiondef( 'public.{SHARED_CORRELATOR}(uuid)'::regprocedure ) "
        "into v_definition;"
    ) in compact
    assert f"v_shared_name constant text := '{SHARED_CORRELATOR}';" in compact
    assert f"v_shared_head constant text := 'public.{SHARED_CORRELATOR}(';" in compact
    assert f"v_portable_head constant text := 'public.{PORTABLE_CORRELATOR}(';" in compact
    assert "v_exact_phone constant text := 'intent.normalized_phone = v_phone';" in compact
    assert (
        "v_equivalent_phone constant text := "
        f"'intent.normalized_phone = any(public.{VARIANTS}(v_phone))';"
    ) in compact
    # El nombre una sola vez (la cabecera) y las dos comparaciones del telefono.
    assert (
        "length(v_definition) - length(replace(v_definition, v_shared_name, '')) "
        "<> length(v_shared_name)"
    ) in compact
    assert (
        "length(v_definition) - length(replace(v_definition, v_shared_head, '')) "
        "<> length(v_shared_head)"
    ) in compact
    assert (
        "length(v_definition) - length(replace(v_definition, v_exact_phone, '')) "
        "<> 2 * length(v_exact_phone)"
    ) in compact
    assert (
        "raise exception using errcode = '55000', "
        "message = 'unexpected_hotmart_intent_correlator_definition';"
    ) in compact
    assert (
        "execute replace( replace(v_definition, v_shared_head, v_portable_head), "
        "v_exact_phone, v_equivalent_phone );"
    ) in compact
    # La definicion de origen tiene esas dos comparaciones y un solo nombre.
    source = _function(
        (MIGRATIONS / "20260820000100_hotmart_purchase_intent_correlation.sql").read_text(
            encoding="utf-8"
        ),
        SHARED_CORRELATOR,
    )
    assert source.count("intent.normalized_phone = v_phone") == 2
    assert source.count(SHARED_CORRELATOR) == 1
    # Y se deriva despues de crear las variantes que la copia usa.
    assert sql.index(f"create or replace function public.{VARIANTS}(") < sql.index(block)


def test_portable_reserve_is_derived_with_exact_occurrences() -> None:
    sql = _sql()
    (block,) = re.findall(r"do \$reserve\$.*?\$reserve\$;", sql, re.DOTALL)
    compact = _executable(block)

    assert (
        f"select pg_get_functiondef( 'public.{SHARED_RESERVE}"
        "(uuid,text,bigint,bigint,bigint,text,text,timestamptz)'::regprocedure ) "
        "into v_definition;"
    ) in compact
    assert f"v_shared_name constant text := '{SHARED_RESERVE}';" in compact
    assert f"v_shared_head constant text := 'public.{SHARED_RESERVE}(';" in compact
    assert f"v_portable_head constant text := 'public.{PORTABLE_RESERVE}(';" in compact
    assert (
        "v_exact_intent constant text := 'intent.normalized_phone = p_external_user_id';"
    ) in compact
    assert (
        "v_equivalent_intent constant text := "
        f"'intent.normalized_phone = any(public.{VARIANTS}(p_external_user_id))';"
    ) in compact
    # El nombre una vez, la cabecera una vez, las tres busquedas de la
    # intencion y el chequeo del opt-out una vez; si no, 55000 y nada.
    for check in (
        "length(v_definition) - length(replace(v_definition, v_shared_name, '')) "
        "<> length(v_shared_name)",
        "length(v_definition) - length(replace(v_definition, v_shared_head, '')) "
        "<> length(v_shared_head)",
        "length(v_definition) - length(replace(v_definition, v_exact_intent, '')) "
        "<> 3 * length(v_exact_intent)",
        "length(v_definition) - length(replace(v_definition, v_exact_opt_out, '')) "
        "<> length(v_exact_opt_out)",
        "position('_whatsapp_phone_' in v_definition) > 0",
    ):
        assert check in compact, check
    assert (
        "raise exception using errcode = '55000', "
        "message = 'unexpected_checkout_issuance_reserve_definition';"
    ) in compact
    assert (
        "execute replace( replace( replace(v_definition, v_shared_head, v_portable_head), "
        "v_exact_intent, v_equivalent_intent ), v_exact_opt_out, v_equivalent_opt_out );"
    ) in compact

    # La definicion de origen: la vigente, con esos textos y esas cuentas. Los
    # E-strings del bloque son exactamente el llamado al opt-out de la fuente.
    vigent = [
        path.name
        for path in sorted(MIGRATIONS.glob("*.sql"))
        if path.name < MIGRATION.name
        and re.search(
            rf"create\s+(?:or\s+replace\s+)?function\s+public\.{SHARED_RESERVE}\s*\(",
            path.read_text(encoding="utf-8"),
        )
    ]
    assert vigent[-1] == SHARED_RESERVE_VIGENT
    source = _function(
        (MIGRATIONS / SHARED_RESERVE_VIGENT).read_text(encoding="utf-8"), SHARED_RESERVE
    )
    body = source.split("$function$", 1)[1]
    assert body.count("intent.normalized_phone = p_external_user_id") == 3
    assert body.count(SHARED_RESERVE) == 0
    opt_out_call = (
        "public.has_chatwoot_opt_out_stop(\n"
        "        p_chatwoot_account_id, p_chatwoot_inbox_id,\n"
        "        p_chatwoot_conversation_id, p_external_user_id\n"
        "    )"
    )
    assert body.count(opt_out_call) == 1
    escaped = (
        "E'public.has_chatwoot_opt_out_stop(\\n' "
        "|| E'        p_chatwoot_account_id, p_chatwoot_inbox_id,\\n' "
        "|| E'        p_chatwoot_conversation_id, p_external_user_id\\n' "
        "|| E'    )'"
    )
    assert _normalized(escaped) in compact
    # La identidad del caso y el replay quedan exactos.
    assert body.count("identity.external_user_id = p_external_user_id") == 2
    # Se deriva despues de crear las variantes que la copia usa.
    assert sql.index(f"create or replace function public.{VARIANTS}(") < sql.index(block)


def test_copies_keep_the_fingerprints_of_the_schema_inventory() -> None:
    # scripts/supabase_schema_inventory.sql busca estos literales dentro de las
    # funciones copiadas (filas 20260901000300, 20260903000100, 20260928000200,
    # 20260929000100, 20260930000100 y 20260930000300).
    helper = _new(HELPER)
    plan = _new(PLAN)
    cart = _new(CART)
    failure = _new(FAILURE)
    purchase = _new(PURCHASE)

    assert "consented_intent_submission_missing" in helper
    for literal in (
        "_portable_consented_intent_reason",
        "precheckout_whatsapp_consent",
        "point.source in ('hotmart', 'system')",
        "_lancemos_pilot_audience_intent",
        "all(v_runtime_binding.additional_offer_codes)",
    ):
        assert literal in plan, literal
    assert "security definer" in _normalized(plan)
    for literal in (
        "additional_offer_codes",
        "commercial_ally_runtime_bindings",
        "hotmart_purchase_intent_scopes",
        "commercial_ally_hotmart_event_bindings",
        "portable_hotmart_cart_replay_binding_mismatch",
    ):
        assert literal in cart, literal
    assert "additional_offer_codes" in failure
    assert "commercial_ally_runtime_bindings" in purchase
    assert "for update" in purchase.lower()
    # El negativo: la compra frena con cualquier oferta del producto, y el
    # inventario exige que el texto no aparezca ni en un comentario.
    assert "v_binding.offer_code" not in purchase


def test_acl_is_explicit() -> None:
    sql = _sql()
    compact = _normalized(sql)
    executable = re.sub(r"--[^\n]*", "", sql)

    assert "rolname in ('anon','authenticated','service_role')" in compact
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
    # Los grants van dentro del bloque que tolera que los roles no existan.
    roles_block = re.search(r"do \$roles\$.*?\$roles\$;", sql, re.DOTALL)
    assert roles_block is not None
    outside = sql.replace(roles_block.group(0), "")
    assert not re.search(r"^\s*grant\b", re.sub(r"--[^\n]*", "", outside), re.MULTILINE)
    assert "if exists(select 1 from pg_roles where rolname='service_role') then" in compact


def test_migration_redefines_only_the_portable_functions() -> None:
    sql = _sql()
    defined = re.findall(r"create\s+(?:or\s+replace\s+)?function\s+public\.([a-z0-9_]+)\s*\(", sql)

    assert sorted(defined) == sorted(
        [
            CANONICAL,
            VARIANTS,
            CART,
            PURCHASE,
            FAILURE,
            HELPER,
            PLAN,
            OPT_OUT_STOP,
            PILOT_START,
            FAILURE_START,
        ]
    )
    # Dos SQL dinamicos que crean funciones: el correlador portable y la
    # reserva portable del enlace. Ninguno redefine la compartida.
    executable = re.sub(r"--[^\n]*", "", sql)
    assert len(re.findall(r"\bexecute\s+replace\(", executable)) == 2
    assert f"create or replace function public.{SHARED_RESERVE}" not in sql
    assert PORTABLE_RESERVE not in defined
    assert not re.search(r"\balter\s+function\b", executable, re.IGNORECASE)
    assert not re.search(r"\bdrop\s+function\b", executable, re.IGNORECASE)


def test_migration_is_transactional_and_touches_no_table() -> None:
    sql = _sql()
    outside_functions = re.sub(r"\$function\$.*?\$function\$", "", sql, flags=re.DOTALL)
    statements = re.sub(r"--[^\n]*", "", outside_functions).split(";")

    assert sql.isascii()
    assert re.search(r"^begin;\nset local lock_timeout = '5s';\n", sql, re.MULTILINE)
    assert sql.rstrip().endswith("commit;")
    assert not [
        statement
        for statement in statements
        if re.match(r"\s*(insert|update|delete|truncate)\b", statement, re.IGNORECASE)
    ]
    assert not re.search(
        r"\b(create|alter|drop)\s+(table|index|trigger|type|policy)\b",
        re.sub(r"--[^\n]*", "", sql),
        re.IGNORECASE,
    )
