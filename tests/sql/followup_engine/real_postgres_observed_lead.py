"""Real PostgreSQL probe for observed lead.precheckout durable admission."""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[3]
DATABASE_URL = os.environ.get("DATABASE_URL")
PSQL = os.environ.get("PSQL", "psql")
CONFIRMATION = os.environ.get("ALLOW_DISPOSABLE_DATABASE")


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


def payloads(
    external_id: str,
    email: str,
    *,
    name: str = "Maria Example",
    phone: str | None = "573001234567",
) -> tuple[dict[str, object], dict[str, object]]:
    raw: dict[str, object] = {
        "id": external_id,
        "event": "lead.precheckout",
        "version": "1.0.0",
        "created_at": "2026-08-19T11:00:00Z",
        "data": {"lead": {"email": email}},
    }
    identity: dict[str, object] = {"email": email, "phone_valid": phone is not None}
    if phone is not None:
        identity["phone"] = phone
    canonical: dict[str, object] = {
        "external_submission_id": external_id,
        "event_type": "PRECHECKOUT_FORM_SUBMITTED",
        "contract_version": "1.0.0",
        "submitted_at": "2026-08-19T11:00:00Z",
        "source": {
            "tenant_ref": "lancemos",
            "funnel_ref": "psicologajohanna",
            "landing_ref": "ads-a",
        },
        "identity": identity,
        "lead": {"full_name": name},
        "commerce": {
            "product_ref": "F106691755G",
            "offer_ref": "bxjge6zq",
            "price": "49",
            "currency": "USD",
        },
        "consent": {"marketing_optin": False, "whatsapp_contact": False},
        "assurance": {
            "provisional": False,
            "provider_observed": True,
            "activation_authorized": False,
        },
    }
    return raw, canonical


def json_literal(value: dict[str, object]) -> str:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True)
    return f"$json${encoded}$json$::jsonb"


def admit(
    external_id: str,
    raw_payload: dict[str, object],
    canonical_payload: dict[str, object],
) -> list[str]:
    sql = (
        "set statement_timeout='5s'; set deadlock_timeout='100ms'; "
        "set role service_role; "
        "select outcome,submission_id,purchase_intent_id "
        "from public.admit_observed_lead_precheckout("
        f"{external_id!r},{json_literal(raw_payload)},{json_literal(canonical_payload)}"
        "); reset role;"
    )
    return query(sql).splitlines()[-1].split("|")


def install_schema() -> None:
    require(CONFIRMATION == "observed-lead-precheckout", "disposable database confirmation required")
    database = query("select current_database()")
    require(database.startswith("observed_lead_precheckout"), "unexpected database name")
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
    for migration in sorted((ROOT / "supabase/migrations").glob("*.sql")):
        apply(migration)
    print("observed_lead_real_postgres_migrations=OK")


def main() -> None:
    install_schema()

    raw1, canonical1 = payloads(
        "01K3LEADPGREAL000000000001",
        "pg-real-1@example.test",
        phone="573001234561",
    )
    inserted = admit(str(raw1["id"]), raw1, canonical1)
    duplicate = admit(str(raw1["id"]), raw1, canonical1)
    require(inserted[0] == "inserted", f"unexpected insert: {inserted}")
    require(duplicate == ["duplicate", inserted[1], inserted[2]], f"unexpected replay: {duplicate}")
    print("observed_lead_insert_and_replay=OK")

    conflicting = json.loads(json.dumps(canonical1))
    conflicting["lead"]["full_name"] = "Different Name"
    first_conflict = admit(str(raw1["id"]), raw1, conflicting)
    second_conflict = admit(str(raw1["id"]), raw1, conflicting)
    require(first_conflict[0] == "semantic_conflict", f"unexpected conflict: {first_conflict}")
    require(second_conflict == first_conflict, "conflict replay changed")
    conflict_count = query(
        "select count(*) from public.precheckout_submission_conflicts "
        f"where existing_submission_id={inserted[1]!r}::uuid"
    )
    require(conflict_count == "1", f"conflict duplicated: {conflict_count}")
    print("observed_lead_semantic_conflict_replay=OK")

    raw2, canonical2 = payloads(
        "01K3LEADPGREAL000000000002",
        "pg-real-1@example.test",
        phone="573001234561",
    )
    second = admit(str(raw2["id"]), raw2, canonical2)
    require(second[0] == "inserted" and second[2] == inserted[2], "email did not reuse intent")
    print("observed_lead_double_submit_one_intent=OK")

    raw_phone, canonical_phone = payloads(
        "01K3LEADPGREAL000000000009",
        "pg-real-other-email@example.test",
        phone="573001234561",
    )
    same_phone = admit(str(raw_phone["id"]), raw_phone, canonical_phone)
    require(same_phone[0] == "inserted" and same_phone[2] == inserted[2], "phone did not reuse intent")
    phone_state = query(
        "select current_classification,whatsapp_contact_authorized "
        f"from public.purchase_intents where id={inserted[2]!r}::uuid"
    )
    require(phone_state == "identity_conflict|f", f"unsafe phone conflict: {phone_state}")
    print("observed_lead_same_phone_changed_email_fail_closed=OK")

    email_only_raw, email_only_canonical = payloads(
        "01K3LEADPGREAL000000000030",
        "pg-real-backfill@example.test",
        phone=None,
    )
    email_only = admit(str(email_only_raw["id"]), email_only_raw, email_only_canonical)
    enriched_raw, enriched_canonical = payloads(
        "01K3LEADPGREAL000000000031",
        "pg-real-backfill@example.test",
        phone="573001234570",
    )
    enriched = admit(str(enriched_raw["id"]), enriched_raw, enriched_canonical)
    require(enriched[2] == email_only[2], "phone enrichment created a second intent")
    enriched_state = query(
        "select normalized_phone,current_classification,whatsapp_contact_authorized "
        f"from public.purchase_intents where id={email_only[2]!r}::uuid"
    )
    require(enriched_state == "573001234570||f", f"phone was not backfilled safely: {enriched_state}")
    changed_email_raw, changed_email_canonical = payloads(
        "01K3LEADPGREAL000000000032",
        "pg-real-backfill-other@example.test",
        phone="573001234570",
    )
    changed_email = admit(
        str(changed_email_raw["id"]),
        changed_email_raw,
        changed_email_canonical,
    )
    require(changed_email[2] == email_only[2], "backfilled phone did not correlate")
    changed_state = query(
        "select current_classification,whatsapp_contact_authorized "
        f"from public.purchase_intents where id={email_only[2]!r}::uuid"
    )
    require(changed_state == "identity_conflict|f", f"backfilled identity was not fail-closed: {changed_state}")
    print("observed_lead_email_only_phone_backfill=OK")

    known_raw, known_canonical = payloads(
        "01K3LEADPGREAL000000000040",
        "pg-real-known-phone@example.test",
        phone="573001234571",
    )
    known = admit(str(known_raw["id"]), known_raw, known_canonical)
    missing_raw, missing_canonical = payloads(
        "01K3LEADPGREAL000000000041",
        "pg-real-known-phone@example.test",
        phone=None,
    )
    missing = admit(str(missing_raw["id"]), missing_raw, missing_canonical)
    require(missing[2] == known[2], "missing-phone replay changed intent")
    known_state = query(
        "select normalized_phone,current_classification,whatsapp_contact_authorized "
        f"from public.purchase_intents where id={known[2]!r}::uuid"
    )
    require(known_state == "573001234571||f", f"known phone was degraded: {known_state}")
    print("observed_lead_known_phone_not_degraded=OK")

    raw3, canonical3 = payloads(
        "01K3LEADPGREAL000000000003",
        "pg-real-null@example.test",
        phone=None,
    )
    nullable = admit(str(raw3["id"]), raw3, canonical3)
    nullable_state = query(
        "select normalized_phone is null,whatsapp_contact_authorized,current_classification,"
        "provider_observed,activation_authorized from public.purchase_intents "
        f"where id={nullable[2]!r}::uuid"
    )
    require(nullable_state == "t|f|tracking_incomplete|t|f", f"unsafe nullable phone: {nullable_state}")
    print("observed_lead_nullable_phone_fail_closed=OK")

    raw4, canonical4 = payloads(
        "01K3LEADPGREAL000000000004",
        "pg-real-race@example.test",
        phone="573001234564",
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(
            row[0]
            for row in pool.map(
                lambda _: admit(str(raw4["id"]), raw4, canonical4),
                range(2),
            )
        )
    require(outcomes == ["duplicate", "inserted"], f"unexpected replay race: {outcomes}")
    print("observed_lead_exact_replay_concurrency=OK")

    def distinct_submission(index: int) -> list[str]:
        external_id = f"01K3LEADPGREAL00000000001{index}"
        raw, canonical = payloads(
            external_id,
            "pg-real-double-race@example.test",
            phone="573001234565",
        )
        return admit(external_id, raw, canonical)

    with ThreadPoolExecutor(max_workers=2) as pool:
        distinct = list(pool.map(distinct_submission, (0, 1)))
    require([row[0] for row in distinct] == ["inserted", "inserted"], "distinct submissions failed")
    require(len({row[2] for row in distinct}) == 1, "distinct submissions created multiple intents")
    print("observed_lead_distinct_submit_concurrency=OK")

    cross_a_raw, cross_a_canonical = payloads(
        "01K3LEADPGREAL000000000050",
        "pg-real-cross-a@example.test",
        phone="573001234572",
    )
    cross_b_raw, cross_b_canonical = payloads(
        "01K3LEADPGREAL000000000051",
        "pg-real-cross-b@example.test",
        phone="573001234573",
    )
    cross_a = admit(str(cross_a_raw["id"]), cross_a_raw, cross_a_canonical)
    cross_b = admit(str(cross_b_raw["id"]), cross_b_raw, cross_b_canonical)

    def crossed(index: int) -> list[str]:
        if index == 0:
            external_id = "01K3LEADPGREAL000000000052"
            raw, canonical = payloads(
                external_id,
                "pg-real-cross-a@example.test",
                phone="573001234573",
            )
        else:
            external_id = "01K3LEADPGREAL000000000053"
            raw, canonical = payloads(
                external_id,
                "pg-real-cross-b@example.test",
                phone="573001234572",
            )
        return admit(external_id, raw, canonical)

    with ThreadPoolExecutor(max_workers=2) as pool:
        crossed_rows = list(pool.map(crossed, (0, 1)))
    require([row[0] for row in crossed_rows] == ["inserted", "inserted"], "crossed admissions failed")
    cross_state = query(
        "select count(*) filter (where current_classification='identity_conflict'),"
        "bool_and(not whatsapp_contact_authorized) from public.purchase_intents "
        f"where id in ({cross_a[2]!r}::uuid,{cross_b[2]!r}::uuid)"
    )
    require(cross_state == "2|t", f"crossed identity state unsafe: {cross_state}")
    print("observed_lead_crossed_identity_concurrency=OK")

    acl = query("""
      select
        has_function_privilege('anon','public.admit_observed_lead_precheckout(text,jsonb,jsonb)','execute'),
        has_function_privilege('authenticated','public.admit_observed_lead_precheckout(text,jsonb,jsonb)','execute'),
        has_function_privilege('service_role','public.admit_observed_lead_precheckout(text,jsonb,jsonb)','execute')
    """)
    require(acl == "f|f|t", f"unexpected function ACL: {acl}")
    direct_dml = query("""
      set role service_role;
      do $$ begin
        begin
          delete from public.precheckout_submissions where false;
          raise exception 'direct_delete_not_blocked';
        exception when insufficient_privilege then null;
        end;
      end $$;
      reset role;
      select 'blocked'
    """).splitlines()[-1]
    require(direct_dml == "blocked", "direct DML leaked")
    print("observed_lead_acl=OK")

    rollback_id = "01K3LEADPGREAL000000000020"
    rollback_raw, rollback_canonical = payloads(
        rollback_id,
        "pg-real-rollback@example.test",
        phone="573001234566",
    )
    query(
        "begin; set role service_role; "
        "select * from public.admit_observed_lead_precheckout("
        f"{rollback_id!r},{json_literal(rollback_raw)},{json_literal(rollback_canonical)}"
        "); reset role; select 1/0; commit;",
        expect_failure=True,
    )
    remaining = query(
        "select count(*) from public.precheckout_submissions "
        f"where external_submission_id={rollback_id!r}"
    )
    require(remaining == "0", "late failure left durable residue")
    print("observed_lead_late_failure_rollback=OK")
    print("OBSERVED_LEAD_PRECHECKOUT_REAL_POSTGRES_OK")


if __name__ == "__main__":
    main()
