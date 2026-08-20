"""Real PostgreSQL 17 probe for Hotmart purchase-intent correlation."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[3]
DATABASE_URL = os.environ.get("DATABASE_URL")
PSQL = os.environ.get("PSQL", "psql")
CONFIRMATION = os.environ.get("ALLOW_DISPOSABLE_DATABASE")
OBSERVED_AT = datetime(2026, 8, 20, 14, 0, tzinfo=UTC)
OBSERVED_MS = int(OBSERVED_AT.timestamp() * 1000)
SUBMITTED_AT = (OBSERVED_AT - timedelta(minutes=5)).isoformat()


def pg_env() -> dict[str, str]:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required")
    parsed = urlsplit(DATABASE_URL)
    database = unquote(parsed.path.lstrip("/"))
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not database:
        raise RuntimeError("DATABASE_URL must identify a PostgreSQL database")
    env = os.environ.copy()
    env.update(PGHOST=parsed.hostname, PGPORT=str(parsed.port or 5432), PGDATABASE=database)
    if parsed.username:
        env["PGUSER"] = unquote(parsed.username)
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    sslmode = parse_qs(parsed.query).get("sslmode")
    if sslmode:
        env["PGSSLMODE"] = sslmode[-1]
    return env


def args(*extra: str) -> list[str]:
    return [PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", *extra]


def query(sql: str, *, expect_failure: bool = False) -> str:
    result = subprocess.run(
        args("-A", "-t", "-F", "|", "-c", sql),
        env=pg_env(),
        text=True,
        capture_output=True,
        check=False,
    )
    if expect_failure:
        if result.returncode == 0:
            raise RuntimeError("expected SQL failure")
    elif result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def apply(path: Path) -> None:
    subprocess.run(args("-f", str(path)), env=pg_env(), check=True)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def json_literal(value: dict[str, object]) -> str:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True)
    return f"$json${encoded}$json$::jsonb"


def sql_text(value: str | None) -> str:
    return "null" if value is None else repr(value)


def install_schema() -> None:
    require(
        CONFIRMATION == "hotmart-intent-correlation",
        "disposable database confirmation required",
    )
    database = query("select current_database()")
    require(database.startswith("hotmart_intent_correlation"), "unexpected database name")
    existing = query("""
      select
        (select count(*) from pg_namespace
         where nspname not in ('public','information_schema')
           and nspname not like 'pg_%')
        + (select count(*) from pg_class where relnamespace='public'::regnamespace)
        + (select count(*) from pg_proc where pronamespace='public'::regnamespace)
    """)
    require(existing == "0", "refusing non-empty database")
    query("""
      do $$ begin
        if not exists (select 1 from pg_roles where rolname='anon') then
          create role anon nologin;
        end if;
        if not exists (select 1 from pg_roles where rolname='authenticated') then
          create role authenticated nologin;
        end if;
        if not exists (select 1 from pg_roles where rolname='service_role') then
          create role service_role nologin bypassrls;
        else
          alter role service_role bypassrls;
        end if;
      end $$;
      alter default privileges in schema public grant execute on functions to anon, authenticated;
      alter default privileges in schema public grant all on tables to service_role;
    """)
    apply(ROOT / "supabase/baseline/20260803_public_schema.sql")
    migrations = sorted((ROOT / "supabase/migrations").glob("*.sql"))
    for migration in migrations:
        if migration.name == "20260820000300_hotmart_confirmed_abandonment.sql":
            query(
                "insert into public.purchase_intents ("
                "tenant_ref,funnel_ref,landing_ref,product_ref,offer_ref,"
                "normalized_email,normalized_phone,submitted_at,lifecycle_state,"
                "current_classification,whatsapp_contact_authorized,provisional,"
                "provider_observed,activation_authorized"
                ") values ("
                "'lancemos','psicologajohanna','ads-a','F106691755G','bxjge6zq',"
                "'backfill@example.test','12025550199',clock_timestamp(),"
                "'waiting_for_purchase','abandonment_candidate',false,false,true,false)"
            )
        apply(migration)
        if migration.name == "20260820000300_hotmart_confirmed_abandonment.sql":
            require(
                query(
                    "select count(*) filter (where current_classification="
                    "'confirmed_abandonment')||'|'||count(*) filter (where "
                    "current_classification='abandonment_candidate') from "
                    "public.purchase_intents where normalized_email="
                    "'backfill@example.test'"
                ) == "1|0",
                "confirmed abandonment backfill failed",
            )
            print("hotmart_intent_confirmed_abandonment_backfill=OK")
    print(f"hotmart_intent_correlation_migrations={len(migrations)}")


def insert_intent(
    email: str | None,
    phone: str | None,
    *,
    product_ref: str = "F106691755G",
) -> str:
    return query(
        "insert into public.purchase_intents ("
        "tenant_ref,funnel_ref,landing_ref,product_ref,offer_ref,"
        "normalized_email,normalized_phone,submitted_at,lifecycle_state,"
        "current_classification,whatsapp_contact_authorized,provisional,"
        "provider_observed,activation_authorized"
        ") values ("
        f"'lancemos','psicologajohanna','ads-a',{product_ref!r},'bxjge6zq',"
        f"{sql_text(email)}::text,{sql_text(phone)}::text,{repr(SUBMITTED_AT)}::timestamptz,"
        "'waiting_for_purchase',null,false,false,true,false"
        ") returning id"
    ).splitlines()[-1]


def cart_payload(event_id: str, *, email: str | None, phone: str | None) -> dict[str, object]:
    buyer: dict[str, object] = {"name": "Fixture Buyer"}
    if email is not None:
        buyer["email"] = email
    if phone is not None:
        buyer["phone"] = phone
    return {
        "id": event_id,
        "creation_date": OBSERVED_MS,
        "event": "PURCHASE_OUT_OF_SHOPPING_CART",
        "version": "2.0.0",
        "data": {
            "affiliate": False,
            "buyer": buyer,
            "product": {"id": 8104005, "name": "Libre de Ansiedad"},
            "offer": {"code": "bxjge6zq"},
            "checkout_country": {"iso": "CO", "name": "Colombia"},
        },
    }


def purchase_payload(
    event_id: str,
    transaction: str,
    *,
    email: str | None,
    phone: str | None,
) -> dict[str, object]:
    buyer: dict[str, object] = {"name": "Fixture Buyer"}
    if email is not None:
        buyer["email"] = email
    if phone is not None:
        buyer["checkout_phone"] = phone
    return {
        "id": event_id,
        "creation_date": OBSERVED_MS,
        "event": "PURCHASE_APPROVED",
        "version": "2.0.0",
        "data": {
            "buyer": buyer,
            "product": {"id": 8104005, "name": "Libre de Ansiedad"},
            "purchase": {
                "status": "APPROVED",
                "transaction": transaction,
                "approved_date": OBSERVED_MS,
                "offer": {"code": "bxjge6zq"},
            },
        },
    }


def insert_event(payload: dict[str, object]) -> str:
    data = payload["data"]
    assert isinstance(data, dict)
    buyer = data["buyer"]
    assert isinstance(buyer, dict)
    email_value = buyer.get("email")
    email = email_value.strip().lower() if isinstance(email_value, str) else None
    phone_value = buyer.get("phone") or buyer.get("checkout_phone")
    phone = None
    if isinstance(phone_value, str):
        digits = re.sub(r"\D", "", phone_value)
        phone = f"+{digits}"
    event_type = str(payload["event"])
    rpc = (
        "admit_and_correlate_hotmart_purchase_approved"
        if event_type == "PURCHASE_APPROVED"
        else "admit_and_correlate_hotmart_cart_abandonment"
    )
    return query(
        "select webhook_event_id from public."
        f"{rpc}({repr(str(payload['id']))},{json_literal(payload)},"
        f"{sql_text(email)}::text,{sql_text(phone)}::text)"
    ).splitlines()[-1]


def correlation(event_id: str) -> str:
    return query(
        "select outcome,purchase_intent_id,matched_by,candidate_count,"
        "manual_handoff_required from public.hotmart_purchase_intent_correlations "
        f"where webhook_event_id={event_id!r}::uuid"
    )


def main() -> None:
    install_schema()

    resolved_intent = insert_intent("resolved@example.test", "573001111111")
    abandonment_payload = cart_payload(
        "corr-cart-resolved-001",
        email="resolved@example.test",
        phone="+57 (300) 111-1111",
    )
    abandonment_event = insert_event(abandonment_payload)
    require(
        correlation(abandonment_event)
        == f"resolved|{resolved_intent}|email_and_phone|1|f",
        "abandonment did not resolve exactly",
    )
    state = query(
        "select lifecycle_state,current_classification,activation_authorized,"
        "whatsapp_contact_authorized from public.purchase_intents "
        f"where id={resolved_intent!r}::uuid"
    )
    require(
        state == "waiting_for_purchase|confirmed_abandonment|f|f",
        f"unsafe abandonment state: {state}",
    )
    replay = query(
        "set role service_role; select outcome,purchase_intent_id,matched_by,"
        "candidate_count,manual_handoff_required from "
        f"public.correlate_hotmart_purchase_intent({abandonment_event!r}::uuid); reset role"
    ).splitlines()[-1]
    require(
        replay == f"resolved|{resolved_intent}|email_and_phone|1|f",
        "exact RPC replay changed",
    )
    query(
        "select * from public.admit_and_correlate_hotmart_cart_abandonment("
        f"'corr-cart-resolved-001',{json_literal(abandonment_payload)},"
        "'resolved@example.test','573009999999')",
        expect_failure=True,
    )
    preserved_identity = query(
        "select normalized_email,normalized_phone from "
        "public.hotmart_purchase_intent_event_identities "
        f"where webhook_event_id={abandonment_event!r}::uuid"
    )
    require(
        preserved_identity == "resolved@example.test|573001111111",
        "conflicting canonical replay changed the event identity",
    )
    spoofed_payload = cart_payload(
        "corr-cart-spoofed-001",
        email="attacker@example.test",
        phone=None,
    )
    query(
        "select * from public.admit_and_correlate_hotmart_cart_abandonment("
        f"'corr-cart-spoofed-001',{json_literal(spoofed_payload)},"
        "'resolved@example.test',null)",
        expect_failure=True,
    )
    require(
        query(
            "select count(*) from public.webhook_events "
            "where external_event_id='corr-cart-spoofed-001'"
        ) == "0",
        "payload/identity mismatch did not roll back admission",
    )
    print("hotmart_intent_payload_identity_binding=OK")
    print("hotmart_intent_resolved_abandonment=OK")

    purchase_event = insert_event(
        purchase_payload(
            "corr-purchase-resolved-001",
            "HPCORR000001",
            email="resolved@example.test",
            phone="+57 (300) 111-1111",
        )
    )
    require(
        correlation(purchase_event)
        == f"resolved|{resolved_intent}|email_and_phone|1|f",
        "purchase did not resolve exact abandoned intent",
    )
    state = query(
        "select lifecycle_state,current_classification,activation_authorized "
        f"from public.purchase_intents where id={resolved_intent!r}::uuid"
    )
    require(state == "purchased||f", f"purchase did not supersede abandonment: {state}")
    print("hotmart_intent_purchase_supersedes_abandonment=OK")

    unmatched_event = insert_event(
        cart_payload(
            "corr-cart-unmatched-001",
            email="unmatched@example.test",
            phone="573009999999",
        )
    )
    require(
        correlation(unmatched_event) == "unmatched|||0|t",
        "unmatched outcome invalid",
    )
    print("hotmart_intent_unmatched=OK")

    ambiguous_a = insert_intent("ambiguous-a@example.test", "573002222222")
    ambiguous_b = insert_intent(
        "ambiguous-b@example.test",
        "573002222222",
        product_ref="f106691755g",
    )
    ambiguous_event = insert_event(
        cart_payload(
            "corr-cart-ambiguous-001",
            email=None,
            phone="573002222222",
        )
    )
    require(
        correlation(ambiguous_event) == "ambiguous|||2|t",
        "ambiguous outcome invalid",
    )
    ambiguous_state = query(
        "select count(*),bool_and(current_classification='tracking_incomplete'),"
        "bool_and(not activation_authorized) from public.purchase_intents "
        f"where id in ({ambiguous_a!r}::uuid,{ambiguous_b!r}::uuid)"
    )
    require(ambiguous_state == "2|t|t", f"ambiguous candidates unsafe: {ambiguous_state}")
    print("hotmart_intent_ambiguous=OK")

    conflict_a = insert_intent("conflict-a@example.test", "573003333331")
    conflict_b = insert_intent("conflict-b@example.test", "573003333332")
    conflict_event = insert_event(
        cart_payload(
            "corr-cart-conflict-001",
            email="conflict-a@example.test",
            phone="573003333332",
        )
    )
    require(
        correlation(conflict_event) == "conflict|||2|t",
        "conflict outcome invalid",
    )
    conflict_state = query(
        "select count(*),bool_and(current_classification='identity_conflict'),"
        "bool_and(not activation_authorized) from public.purchase_intents "
        f"where id in ({conflict_a!r}::uuid,{conflict_b!r}::uuid)"
    )
    require(conflict_state == "2|t|t", f"conflict candidates unsafe: {conflict_state}")
    print("hotmart_intent_conflict=OK")

    email_intent = insert_intent("email-only@example.test", None)
    email_purchase = insert_event(
        purchase_payload(
            "corr-purchase-email-001",
            "HPCORR000002",
            email="email-only@example.test",
            phone=None,
        )
    )
    require(
        correlation(email_purchase) == f"resolved|{email_intent}|email|1|f",
        "email-only purchase did not resolve",
    )
    print("hotmart_intent_resolved_email_only=OK")

    effects = query(
        "select (select count(*) from public.recovery_cases),"
        "(select count(*) from public.followup_sequences),"
        "(select count(*) from public.scheduled_actions)"
    )
    require(effects == "0|0|0", f"correlation created commercial effects: {effects}")
    print("hotmart_intent_zero_effects=OK")

    acl = query("""
      select
        has_function_privilege('anon','public.correlate_hotmart_purchase_intent(uuid)','execute'),
        has_function_privilege('authenticated','public.correlate_hotmart_purchase_intent(uuid)','execute'),
        has_function_privilege('service_role','public.correlate_hotmart_purchase_intent(uuid)','execute'),
        has_table_privilege('service_role','public.hotmart_purchase_intent_correlations','select')
    """)
    require(acl == "f|f|t|f", f"unexpected ACL: {acl}")
    legacy_acl = query("""
      select
        has_function_privilege(
          'service_role',
          'public.admit_hotmart_purchase_approved(text,jsonb)',
          'execute'
        ),
        has_function_privilege(
          'service_role',
          'public.admit_hotmart_cart_abandonment(text,jsonb)',
          'execute'
        )
    """)
    require(legacy_acl == "f|f", f"legacy shims still executable: {legacy_acl}")
    base_search_paths = query("""
      select string_agg(
        p.proname||'='||array_to_string(p.proconfig,','),
        '|' order by p.proname
      )
      from pg_proc p join pg_namespace n on n.oid=p.pronamespace
      where n.nspname='public'
        and p.oid in (
          to_regprocedure(
            'public._admit_hotmart_purchase_approved_base(text,jsonb)'
          ),
          to_regprocedure(
            'public._admit_hotmart_cart_abandonment_base(text,jsonb)'
          )
        )
    """)
    require(
        base_search_paths == (
            "_admit_hotmart_cart_abandonment_base="
            "search_path=pg_catalog, public, pg_temp|"
            "_admit_hotmart_purchase_approved_base="
            "search_path=pg_catalog, public, pg_temp"
        ),
        f"base search paths are not catalog-first: {base_search_paths}",
    )
    print("hotmart_intent_base_search_paths=OK")
    legacy_payload = cart_payload(
        "corr-cart-legacy-shim-001",
        email="legacy@example.test",
        phone=None,
    )
    legacy_count_before = query(
        "select count(*) from public.webhook_events where external_event_id="
        "'corr-cart-legacy-shim-001'"
    )
    query(
        "set role service_role; select outcome||'|'||webhook_event_id "
        "from public.admit_hotmart_cart_abandonment("
        f"'corr-cart-legacy-shim-001',{json_literal(legacy_payload)})",
        expect_failure=True,
    )
    require(
        legacy_count_before == "0" and query(
            "select count(*) from public.webhook_events where external_event_id="
            "'corr-cart-legacy-shim-001'"
        ) == "0",
        "revoked legacy shim created durable state",
    )
    print("hotmart_intent_contract_legacy_denied=OK")
    require(
        query(
            "select public._normalize_hotmart_purchase_intent_phone(' +573009999999') "
            "is null"
        ) == "t",
        "SQL accepted phone whitespace rejected by Python",
    )
    numeric_email_identity = query(
        "select coalesce(normalized_email,'NULL')||'|'||coalesce(normalized_phone,'NULL') "
        "from public._hotmart_purchase_intent_payload_identity("
        "'PURCHASE_OUT_OF_SHOPPING_CART',"
        "'{\"data\":{\"buyer\":{\"email\":123,\"phone\":\" +573009999999\","
        "\"checkout_phone\":\"573008888888\"}}}'::jsonb)"
    )
    require(
        numeric_email_identity == "NULL|573008888888",
        f"Python/SQL identity rules diverged: {numeric_email_identity}",
    )
    print("hotmart_intent_payload_identity_edge_cases=OK")
    print("hotmart_intent_python_sql_identity_parity=OK")
    query(
        "update public.hotmart_purchase_intent_correlations set reason_code='tampered' "
        f"where webhook_event_id={abandonment_event!r}::uuid",
        expect_failure=True,
    )
    print("hotmart_intent_acl_and_immutability=OK")
    print("HOTMART_PURCHASE_INTENT_CORRELATION_REAL_POSTGRES_OK")


if __name__ == "__main__":
    main()
