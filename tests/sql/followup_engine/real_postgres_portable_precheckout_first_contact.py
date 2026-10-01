"""Real PostgreSQL probe for the portable first contact after the form.

Migration 20261001000200 keeps the form admission when the plan fails by
running the contact and the plan inside a protected block that also fires the
deferred triggers (``set constraints all immediate``). PGlite covers the
behavior (validate_portable_precheckout_first_contact.mjs); this probe repeats
the parts that depend on the server: the block and the deferred triggers under
real subtransactions, the entrypoints called as service_role with
Supabase-style default privileges, and two real sessions planning the same
person at once.

Data: the ATT1 fixture (tests/fixtures/instances/att1) and the two GHL
translator goldens (tests/fixtures/ghl/expected) with id, date and buyer
replaced. There is no captured PURCHASE_APPROVED: the purchase uses the inline
precedent of validate_commercial_ally_multi_offer.mjs. Phones are synthetic.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[3]
DATABASE_URL = os.environ.get("DATABASE_URL")
PSQL = os.environ.get("PSQL", "psql")
CONFIRMATION = os.environ.get("ALLOW_DISPOSABLE_DATABASE")
FIXTURES = ROOT / "tests" / "fixtures"
MANIFEST = tomllib.loads((FIXTURES / "instances/att1/instancia.toml").read_text(encoding="utf-8"))
PILOT = json.loads((FIXTURES / "instances/att1/politica-piloto.json").read_text(encoding="utf-8"))
GOLDENS = {
    "MX": json.loads(
        (
            FIXTURES / "ghl/expected/ghl_form_webhook_landing_d_derived_20260929.lead_precheckout.json"
        ).read_text(encoding="utf-8")
    ),
    "AR": json.loads(
        (
            FIXTURES / "ghl/expected/ghl_form_webhook_att1_ads_a_20260929.lead_precheckout.json"
        ).read_text(encoding="utf-8")
    ),
}
INSTANCE = MANIFEST["instancia"]
HOTMART = MANIFEST["hotmart"]
OFFERS = HOTMART["ofertas"]
CHATWOOT = MANIFEST["chatwoot"]
SCOPE_KEY = "att1-primer-contacto-pg"
SCOPE_VERSION = 1
NOW = datetime.now(timezone.utc).replace(microsecond=0)


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
        return result.stderr.strip()
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def apply(path: Path) -> None:
    subprocess.run(args("-f", str(path)), env=pg_env(), check=True)


def rows(path: Path) -> list[list[str]]:
    result = subprocess.run(
        args("-A", "-t", "-F", "|", "-f", str(path)),
        env=pg_env(),
        text=True,
        capture_output=True,
        check=True,
    )
    return [line.split("|") for line in result.stdout.splitlines() if line]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def literal(value: object) -> str:
    if isinstance(value, (dict, list)):
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True)
        return f"$json${encoded}$json$::jsonb"
    return "'" + str(value).replace("'", "''") + "'"


def person(index: int, country: str) -> dict[str, str]:
    golden = GOLDENS[country]["canonical_payload"]
    calling_code = "52" if country == "MX" else "54"
    national = f"{'55' if country == 'MX' else '11'}555506{index:02d}"
    return {
        "country": country,
        "calling_code": calling_code,
        "email": f"att1-first-contact-pg-{index:02d}@example.test",
        "plain": f"{calling_code}{national}",
        "whatsapp": f"{calling_code}{'1' if country == 'MX' else '9'}{national}",
        "offer": golden["commerce"]["offer_ref"],
    }


def form(lead: dict[str, str], *, landing: str | None = None, minutes_ago: int = 8) -> dict[str, object]:
    """The golden of the landing with id, date and buyer replaced.

    landing sends the form through the other landing: that golden with the
    country and the phone of the person (a derived variant, not a capture).
    """
    golden = GOLDENS[landing or lead["country"]]
    raw = copy.deepcopy(golden["raw_payload"])
    canonical = copy.deepcopy(golden["canonical_payload"])
    submitted_at = NOW - timedelta(minutes=minutes_ago)
    form.counter += 1  # type: ignore[attr-defined]
    external_id = f"01PGFIRSTCONTACT{form.counter:010d}"  # type: ignore[attr-defined]
    dedupe_key = f"{raw['source']['site']}:{raw['data']['offer']['code']}:{lead['email']}"
    raw["id"] = external_id
    raw["created_at"] = submitted_at.isoformat().replace("+00:00", ".000Z")
    raw["data"]["buyer"].update(
        email=lead["email"],
        phone=f"+{lead['plain']}",
        phone_country_code=lead["calling_code"],
        phone_national=lead["plain"][len(lead["calling_code"]) :],
    )
    raw["data"]["checkout_country"]["iso"] = lead["country"]
    raw["dedupe_key"] = dedupe_key
    canonical["external_submission_id"] = external_id
    canonical["submitted_at"] = submitted_at.isoformat().replace("+00:00", "Z")
    canonical["identity"].update(
        email=lead["email"], phone=lead["plain"], phone_country_iso=lead["country"]
    )
    canonical["dedupe_key"] = dedupe_key
    return {"id": external_id, "raw": raw, "canonical": canonical}


form.counter = 0  # type: ignore[attr-defined]


def admit_sql(submission: dict[str, object]) -> str:
    return (
        "select outcome,submission_id,purchase_intent_id,plan_outcome,plan_reason "
        "from public.admit_and_plan_portable_lead_precheckout("
        f"{literal(INSTANCE['tenant_ref'])},{literal(INSTANCE['funnel_ref'])},"
        f"{INSTANCE['binding_version']},{literal(submission['id'])},"
        f"{literal(submission['raw'])},{literal(submission['canonical'])},"
        f"{literal(SCOPE_KEY)},{SCOPE_VERSION})"
    )


def admit(submission: dict[str, object]) -> list[str]:
    """The entrypoint, as service_role: what the bridge calls."""
    sql = (
        "set statement_timeout='10s'; set deadlock_timeout='100ms'; "
        f"set role service_role; {admit_sql(submission)}; reset role;"
    )
    return query(sql).splitlines()[-1].split("|")


def plan_row(submission_id: str) -> list[str]:
    return query(
        "select outcome,reason_code,coalesce(error_sqlstate,''),coalesce(contact_id::text,''),"
        "coalesce(recovery_case_id::text,'') "
        f"from public.portable_precheckout_first_contact_plans where submission_id={submission_id!r}::uuid"
    ).split("|")


def shared_purchase(lead: dict[str, str], index: int) -> str:
    """A purchase admitted by the shared path: it stays in received."""
    approved_at = int((NOW - timedelta(minutes=5)).timestamp() * 1000)
    payload = {
        "id": f"att1-first-contact-pg-purchase-{index}",
        "creation_date": approved_at,
        "event": "PURCHASE_APPROVED",
        "version": "2.0.0",
        "data": {
            "product": {"id": HOTMART["product_id"], "ucode": "ATT1-FIRST-CONTACT-PG"},
            "buyer": {"email": lead["email"], "checkout_phone": f"+{lead['whatsapp']}"},
            "purchase": {
                "approved_date": approved_at,
                "status": "APPROVED",
                "transaction": f"HPATT1PG{index:04d}",
                "offer": {"code": lead["offer"]},
            },
        },
    }
    event_id = query(
        "select webhook_event_id from public.admit_and_correlate_hotmart_purchase_approved("
        f"{literal(payload['id'])},{literal(payload)},{literal(lead['email'])},{literal(lead['whatsapp'])})"
    )
    status = query(f"select processing_status from public.webhook_events where id={event_id!r}::uuid")
    require(status == "received", f"the shared purchase is not waiting in received: {status}")
    return event_id


def install_schema() -> None:
    require(
        CONFIRMATION == "portable-precheckout-first-contact",
        "disposable database confirmation required",
    )
    database = query("select current_database()")
    require(database.startswith("portable_precheckout_first_contact"), "unexpected database name")
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
      alter default privileges in schema public grant all on functions to service_role;
      alter default privileges in schema public grant all on tables to service_role;
    """)
    apply(ROOT / "supabase/baseline/20260803_public_schema.sql")
    for migration in sorted((ROOT / "supabase/migrations").glob("*.sql")):
        apply(migration)
    acl = rows(ROOT / "scripts/supabase_acl_inventory.sql")
    require(bool(acl) and all(row[-1] == "ok" for row in acl), "ACL inventory failed")
    fingerprints = [
        row for row in rows(ROOT / "scripts/supabase_schema_inventory.sql") if row[0] == "20261001000200"
    ]
    require(
        len(fingerprints) == 1 and fingerprints[0][-1] == "fingerprint_present",
        f"schema fingerprint failed: {fingerprints}",
    )
    print("first_contact_real_postgres_migrations=OK")


def seed() -> None:
    default_offer, *additional = OFFERS
    policy = PILOT["first_contact"]["policy"]
    scope = PILOT["first_contact"]["pilot_scope"]
    host_and_path = [urlsplit(offer["url"]) for offer in OFFERS]
    landings = [
        {
            "offer_code": offer["codigo"],
            "site": offer["site"],
            "landing_id": offer["landing_id"],
            "page_host": url.netloc,
            "page_path": url.path,
        }
        for offer, url in zip(additional, host_and_path[1:], strict=True)
    ]
    # The sending window uses the days of the fixture from 00:00 to 23:59, the
    # documented deviation of validate_att1_portable_chain.mjs.
    windows = [{**window, "start": "00:00", "end": "23:59"} for window in policy["business_windows"]]
    offer_codes = "array[" + ",".join(literal(offer["codigo"]) for offer in additional) + "]::text[]"
    query(f"""
      insert into public.commercial_ally_runtime_bindings
        (tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name,
         lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
         product_name, product_price, currency, offer_code, consent_copy_version,
         hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id,
         inbound_scope_key, inbound_scope_version, additional_offer_codes,
         additional_offer_landings)
      values ({literal(INSTANCE['tenant_ref'])},{literal(INSTANCE['funnel_ref'])},
        {INSTANCE['binding_version']},'active',{literal(INSTANCE['ally_ref'])},
        {literal(INSTANCE['marca'])},{literal(default_offer['site'])},
        {literal(default_offer['landing_id'])},{literal(host_and_path[0].netloc)},
        {literal(host_and_path[0].path)},{literal(HOTMART['hotlink'])},
        {literal(HOTMART['product_name'])},{literal(HOTMART['precio'])}::numeric,
        {literal(HOTMART['moneda'])},{literal(default_offer['codigo'])},
        {literal(MANIFEST['consentimiento']['copy_version'])},{HOTMART['product_id']},
        {CHATWOOT['account_id']},{CHATWOOT['inbox_id']},
        {literal(MANIFEST['inbound']['scope_key'])},{MANIFEST['inbound']['scope_version']},
        {offer_codes},{literal(landings)});
      insert into public.hotmart_purchase_intent_scopes
        (tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref,
         offer_ref, max_lookback, active)
      select {literal(INSTANCE['tenant_ref'])},{literal(INSTANCE['funnel_ref'])},
             {literal(HOTMART['product_id'])},{literal(HOTMART['hotlink'])},offer,
             {literal(PILOT['purchase_intent_scope']['max_lookback'])}::interval,true
      from unnest(array[{literal(default_offer['codigo'])}]::text[] || {offer_codes}) offer;
      insert into public.inbound_commercial_scope_versions
        (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
         external_product_id, offer_code, approved_by, approved_at, published_at)
      values ('att1-otro-inbox-pg',1,'published',{literal(INSTANCE['tenant_ref'])},
        {CHATWOOT['account_id']},{CHATWOOT['inbox_id'] + 88},{literal(HOTMART['product_id'])},
        {literal(default_offer['codigo'])},'operator-test',now(),now());
      insert into public.followup_policy_versions
        (policy_key, version, status, purpose, timezone, business_windows,
         grace_period, expires_after, max_automatic_messages, steps,
         approved_by, approved_at, published_at)
      values ({literal(policy['policy_key'])},{policy['version']},'published',
        {literal(policy['purpose'])},{literal(INSTANCE['zona_horaria'])},{literal(windows)},
        {literal(policy['grace_period'])}::interval,{literal(policy['expires_after'])}::interval,
        {policy['max_automatic_messages']},{literal(policy['steps'])},
        'operator-test',now(),now());
      insert into public.pilot_scope_versions
        (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
         channel, channel_provider, channel_account_ref, source, source_event_type,
         additional_source_event_types, external_product_id, offer_code,
         additional_offer_codes, purpose, policy_key, policy_version, timezone,
         max_cohort_contacts, max_outbound_request_starts_total,
         max_outbound_request_starts_per_day, audience_mode,
         approved_by, approved_at, published_at)
      values ({literal(SCOPE_KEY)},{SCOPE_VERSION},'published',{literal(INSTANCE['tenant_ref'])},
        {CHATWOOT['account_id']},{CHATWOOT['inbox_id']},'whatsapp',
        {literal(scope['channel_provider'])},
        {literal(scope['channel_account_ref_prefix'] + str(CHATWOOT['inbox_id']))},
        {literal(scope['source'])},{literal(scope['source_event_type'])},'{{}}'::text[],
        {literal(HOTMART['product_id'])},{literal(default_offer['codigo'])},{offer_codes},
        'cart_recovery',{literal(policy['policy_key'])},{policy['version']},
        {literal(INSTANCE['zona_horaria'])},5,20,20,'consented_intent',
        'operator-test',now(),now());
      insert into public.pilot_runtime_controls
        (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
      values ({literal(SCOPE_KEY)},{SCOPE_VERSION},'inactive',0,'operator-test','default-off');
    """)
    armed = query(
        "select runtime_state from public.set_lancemos_pilot_runtime_state("
        f"{literal(SCOPE_KEY)},{SCOPE_VERSION},0,'armed','operator-test','controlled-test')"
    )
    require(armed == "armed", f"the scope was not armed: {armed}")


def main() -> None:
    install_schema()
    seed()

    # The entrypoint as service_role, with the Supabase default privileges.
    first = person(1, "MX")
    planned = admit(form(first))
    require(
        planned[0] == "inserted" and planned[3:] == ["planned", "first_contact_scheduled"],
        f"the form was not planned: {planned[0]} {planned[3:]}",
    )
    planned_row = plan_row(planned[1])
    case_state = query(
        "select source,status,context->>'trigger_kind' from public.recovery_cases "
        f"where id={planned_row[4]!r}::uuid"
    )
    require(case_state == "landing|grace_period|precheckout_intent", f"unexpected case: {case_state}")
    denied = query(
        "set role service_role; select count(*) from public.portable_precheckout_first_contact_plans",
        expect_failure=True,
    )
    require("permission denied" in denied, f"service_role read the plan rows: {denied}")
    print("first_contact_planned_as_service_role=OK")

    # A known purchase closes the case inside the call: the deferred trigger
    # ran in the protected block, before the commit.
    known = person(2, "AR")
    known_purchase = shared_purchase(known, 2)
    inside = query(
        "begin; set local role service_role; "
        f"{admit_sql(form(known))}; reset role; "
        "select rc.status, rc.purchase_event_id from public.recovery_cases rc "
        "join public.contacts c on c.id = rc.contact_id "
        f"where c.email={literal(known['email'])}; commit;"
    ).splitlines()
    require(len(inside) == 2, f"unexpected output inside the transaction: {inside}")
    admission, case_inside = inside[0].split("|"), inside[1].split("|")
    require(
        admission[0] == "inserted" and admission[3:] == ["planned", "purchase_detected"],
        f"the known purchase did not close the plan: {admission[0]} {admission[3:]}",
    )
    require(case_inside == ["won", known_purchase], f"the trigger did not run inside the call: {case_inside}")
    print("first_contact_deferred_trigger_runs_inside_the_block=OK")

    # A failing deferred trigger: a purchase in received that a closed case
    # already holds (an inconsistent state set on purpose). The trigger hits
    # the unique index. The plan fails; the admission stays.
    broken = person(3, "MX")
    broken_purchase = shared_purchase(broken, 3)
    query(
        "update public.recovery_cases set status='won', won_at=clock_timestamp(), "
        f"purchase_event_id={broken_purchase!r}::uuid where id={planned_row[4]!r}::uuid"
    )
    contacts_before = query("select count(*) from public.contacts")
    failed = admit(form(broken))
    failed_row = plan_row(failed[1])
    require(
        failed[0] == "inserted" and failed[3:] == ["plan_failed", "plan_error_unclassified"],
        f"the failing trigger was not contained: {failed[0]} {failed[3:]}",
    )
    require(failed_row[:3] == ["plan_failed", "plan_error_unclassified", "23505"], f"plan row: {failed_row[:3]}")
    require(
        query("select count(*) from public.contacts") == contacts_before
        and query(
            "select count(*) from public.precheckout_submissions "
            f"where id={failed[1]!r}::uuid"
        ) == "1",
        "the failed plan left a contact or lost the admission",
    )
    print("first_contact_failing_deferred_trigger_contained=OK")

    # A planner error (23514): the WhatsApp identity of the phone belongs to
    # another inbox of the same account.
    other_inbox = person(4, "MX")
    query(
        "select outcome from public.admit_inbound_commercial_case_v2("
        f"'att1-otro-inbox-pg',1,990001,{literal(other_inbox['plain'])})"
    )
    mismatch = admit(form(other_inbox))
    mismatch_row = plan_row(mismatch[1])
    require(
        mismatch[0] == "inserted"
        and mismatch_row[:3] == ["plan_failed", "channel_identity_inbox_mismatch", "23514"],
        f"the planner error was not contained: {mismatch[0]} {mismatch_row[:3]}",
    )
    print("first_contact_planner_error_contained=OK")

    # Two real sessions, the same new person, one form per landing: one
    # contact, one live first contact, no error.
    twice = person(5, "MX")
    submissions = [form(twice), form(twice, landing="AR")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(admit, submissions))
    reasons = sorted(outcome[4] for outcome in outcomes)
    require(
        all(outcome[0] == "inserted" for outcome in outcomes)
        and reasons == ["first_contact_scheduled", "precheckout_contact_already_planned"],
        f"concurrent forms diverged: {[outcome[3:] for outcome in outcomes]}",
    )
    owners = query(
        "select count(*), (select count(*) from public.recovery_cases rc "
        "where rc.contact_id = min(c.id::text)::uuid) "
        f"from public.contacts c where c.email={literal(twice['email'])}"
    )
    require(owners == "1|1", f"concurrent forms left more than one contact or case: {owners}")
    print("first_contact_concurrent_forms_one_contact=OK")


if __name__ == "__main__":
    main()
