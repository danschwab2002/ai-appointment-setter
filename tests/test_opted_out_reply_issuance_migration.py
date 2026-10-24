"""El link para quien se dio de baja y escribe por su cuenta (migracion
20261024000400, F-036, decisiones D9 y D10 del 2026-10-24).

Lo que se puede leer en el texto de la migracion. El comportamiento lo prueban
contra la cadena completa (PGlite)
tests/sql/followup_engine/validate_opted_out_reply_issuance.mjs (el alcance y la
tabla de verdad del helper) y validate_whatsapp_phone_equivalence.mjs, seccion
10 e2 (la reserva y la autorizacion de punta a punta).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261024000400_opted_out_reply_checkout_issuance.sql"
PACKAGE = ROOT / "tests" / "sql" / "followup_engine" / "package.json"
HELPER = "_chatwoot_reply_follows_opt_out_v1"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return re.sub(r"--[^\n]*", "", _sql())


def test_it_is_one_transaction_and_comes_before_the_native_bridge() -> None:
    sql = _sql()
    assert "\nbegin;\n" in sql and sql.rstrip().endswith("commit;")
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    # Despues va solo el bridge del motor nativo (20261024000500), que no
    # toca el link.
    assert names[-1] == "20261024000500_flow_engine_native_bridge_v1.sql"
    assert names[-2] == MIGRATION.name
    assert names[-3] == "20261024000200_flow_engine_native_v1.sql"


def test_it_patches_the_four_link_functions_by_counted_anchor() -> None:
    code = _code()
    for signature in (
        "public.reserve_chatwoot_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,text,timestamptz)",
        "public.reserve_portable_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,text,timestamptz)",
        "public.reserve_portable_checkout_issuance_for_funnel_v1(uuid,text,bigint,bigint,bigint,text,text,text,timestamptz)",
        "public.authorize_chatwoot_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,timestamptz)",
    ):
        assert signature in code
    # Si la definicion viva no es la esperada, aborta en vez de derivar a ciegas.
    assert "unexpected_checkout_issuance_opt_out_definition" in code
    assert "checkout_issuance_opt_out_reply_not_applied" in code
    assert "pg_get_functiondef" in code


def test_blocked_and_restricted_contacts_still_stop_the_link() -> None:
    code = _code()
    assert "v_contact.contact_permission in (''blocked'', ''restricted'')" in code
    # La baja solo deja de frenar si el helper dice que el mensaje es posterior.
    assert "v_contact.contact_permission = ''opted_out''\\n'" in code
    assert f"public.{HELPER}(" in code


def test_the_helper_requires_an_opt_out_and_a_newer_message() -> None:
    code = _code()
    helper = code.split(f"create or replace function public.{HELPER}(", 1)[1].split("$function$;", 1)[0]
    assert "security invoker" in helper
    assert "optout.canonical_message_id >= v_trigger" in helper
    assert "_whatsapp_phone_variants(p_external_user_id)" in helper
    assert helper.count("from public.contact_opt_out_events optout") == 2


def test_no_api_role_executes_the_helper() -> None:
    code = _code()
    assert f"'public.{HELPER}(uuid,bigint,bigint,bigint,text,text)'" in code
    assert "array['anon', 'authenticated', 'service_role']" in code
    assert "opted_out_reply_helper_acl_unexpected" in code
    assert "grant execute" not in code


def test_it_does_not_touch_the_proactive_claims() -> None:
    code = _code()
    for name in (
        "claim_conversation_followup_v1",
        "claim_portable_conversation_followup_v1",
        "claim_conversation_reactivation",
        "apply_chatwoot_inbound_opt_out",
    ):
        assert name not in code


def test_the_validator_runs_in_the_pglite_chain() -> None:
    script = json.loads(PACKAGE.read_text(encoding="utf-8"))["scripts"]["test"]
    assert "node validate_opted_out_reply_issuance.mjs" in script
