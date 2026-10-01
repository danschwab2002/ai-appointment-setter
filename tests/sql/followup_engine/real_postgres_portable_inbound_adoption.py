"""Real PostgreSQL probe for the adoption of a pilot template conversation (H7).

Migration 20261001000400 lets the portable inbound admission
(admit_portable_inbound_commercial_case_v1) adopt the conversation that the
acceptance of a pilot template left in 'enabled', so the reply to the template
reaches the agent. PGlite covers the behavior
(validate_portable_inbound_template_adoption.mjs and case 10 of
validate_att1_portable_chain.mjs), but it has one session. This probe repeats,
with two real sessions, what depends on locks:

* two replies to the same template at once (two Chatwoot messages, the same
  admission): the second waits for the first in the advisory lock of the
  admission and gets already_exists; one adoption event, one inbound_sales
  case. Ordered with a sleep, and also unordered: both replies wait at the
  same time behind a barrier session that holds the same advisory lock, and
  the probe checks in pg_stat_activity that both were waiting before it is
  released;
* a reply while the reconciliation of a delivery_unknown attempt of the same
  person is being committed (the payment failure after the cart, accepted late
  in the cart conversation, as case 3 of
  validate_portable_inbound_template_adoption.mjs): the reply waits for the
  identity row the reconciliation holds and adopts with the newest template.
  Read without waiting, it would see the attempt still in delivery_unknown and
  refuse it (the pending-step brake), which is what a reply gives before the
  reconciliation starts: that control runs first;
* a reply while the reconciliation of the template itself is being committed
  (a first touch in delivery_unknown: the conversation does not exist yet):
  the reply waits and adopts the conversation the reconciliation creates;
* KNOWN LIMIT, pinned here so it does not go unnoticed: a reply that the
  admission commits BEFORE that reconciliation starts finds no conversation,
  so it creates one in draft_only (as the shared admission always did) and
  nothing is adopted. The late acceptance then attaches the recovery case to
  that conversation: cart_recovery + inbound_sales without the adoption
  event, and the lookups of 20261001000500 stay ambiguous there. The reply is
  not lost (the agent answers), but a handoff in that conversation cannot be
  marked attended. The day this is resolved, this block fails and has to
  change.

The sessions are ordered with sleeps: the session that holds its transaction
keeps it for HOLD_SECONDS and the other one arrives a second later. Nothing
here depends on a race being won, only on a lock being held while the other
session arrives; the probe checks in pg_stat_activity that it did wait.

Data: the ATT1 fixture (tests/fixtures/instances/att1: binding, offers,
Chatwoot, inbound scope, pilot scope and one-touch policy, with the documented
00:00-23:59 window of validate_att1_portable_chain.mjs). Cart: the capture
tests/fixtures/hotmart_cart_abandonment_rejected_v1.json with product, offer,
id, date and buyer replaced. The form and the payment failure use the inline
precedents of validate_att1_portable_chain.mjs (there is no captured
lead.precheckout 1.1.0 nor PURCHASE_CANCELED). Phones are synthetic and the
accepted text is a marker: the database keeps what the bridge passes.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import time
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
CAPTURED_CART = json.loads(
    (FIXTURES / "hotmart_cart_abandonment_rejected_v1.json").read_text(encoding="utf-8")
)["payload"]
INSTANCE = MANIFEST["instancia"]
HOTMART = MANIFEST["hotmart"]
OFFERS = HOTMART["ofertas"]
CHATWOOT = MANIFEST["chatwoot"]
INBOUND = MANIFEST["inbound"]
SCOPE = PILOT["pilot_scope"]
POLICY = PILOT["policy"]
NOW = datetime.now(timezone.utc).replace(microsecond=0)
ADOPTION_EVENT = "inbound_adopted_template_conversation"
WORKER = "att1-adoption-pg"
# How long a session keeps its transaction, and when the other one arrives.
# Wide on purpose: the CI runner starts a psql per session.
HOLD_SECONDS = 4
SECOND_ARRIVES = 1.0


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


def session(sql: str, *, delay: float = 0.0) -> subprocess.CompletedProcess[str]:
    """One real session, started after a delay; it never raises."""
    time.sleep(delay)
    return subprocess.run(
        args("-A", "-t", "-F", "|", "-c", sql),
        env=pg_env(),
        text=True,
        capture_output=True,
        check=False,
    )


def result_line(done: subprocess.CompletedProcess[str]) -> list[str]:
    """The first row a session printed (pg_sleep prints an empty one)."""
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    require(
        done.returncode == 0 and bool(lines),
        f"the session failed: {done.stdout.strip()} {done.stderr.strip()}",
    )
    return lines[0].split("|")


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


def waiting_on_lock(fragment: str, events: tuple[str, ...], sessions: int = 1) -> bool:
    """True once ``sessions`` sessions running ``fragment`` wait, at the same
    time, for one of ``events``."""
    wanted = ",".join(literal(event) for event in events)
    deadline = time.monotonic() + HOLD_SECONDS - 0.5
    while time.monotonic() < deadline:
        waiting = session(
            "select count(*) from pg_stat_activity "
            "where pid <> pg_backend_pid() and wait_event_type = 'Lock' "
            f"and wait_event in ({wanted}) and query like '%{fragment}%'"
        )
        if waiting.returncode == 0 and waiting.stdout.strip() == str(sessions):
            return True
        time.sleep(0.2)
    return False


ADVISORY = ("advisory",)
ROW_LOCK = ("transactionid", "tuple")


def install_schema() -> None:
    require(
        CONFIRMATION == "portable-inbound-adoption",
        "disposable database confirmation required",
    )
    database = query("select current_database()")
    require(database.startswith("portable_inbound_adoption"), "unexpected database name")
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
    fingerprints = {
        row[0]: row[-1]
        for row in rows(ROOT / "scripts/supabase_schema_inventory.sql")
        if row[0] in {"20261001000400", "20261001000500"}
    }
    require(
        fingerprints == {
            "20261001000400": "fingerprint_present",
            "20261001000500": "fingerprint_present",
        },
        f"schema fingerprint failed: {fingerprints}",
    )
    print("inbound_adoption_real_postgres_migrations=OK")


def seed() -> None:
    default_offer, *additional = OFFERS
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
    windows = [{**window, "start": "00:00", "end": "23:59"} for window in POLICY["business_windows"]]
    offer_codes = "array[" + ",".join(literal(offer["codigo"]) for offer in additional) + "]::text[]"
    extra_events = (
        "array[" + ",".join(literal(event) for event in SCOPE["additional_source_event_types"]) + "]::text[]"
    )
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
        {literal(INBOUND['scope_key'])},{INBOUND['scope_version']},
        {offer_codes},{literal(landings)});
      insert into public.hotmart_purchase_intent_scopes
        (tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref,
         offer_ref, max_lookback, active)
      select {literal(INSTANCE['tenant_ref'])},{literal(INSTANCE['funnel_ref'])},
             {literal(HOTMART['product_id'])},{literal(HOTMART['hotlink'])},offer,
             {literal(PILOT['purchase_intent_scope']['max_lookback'])}::interval,true
      from unnest(array[{literal(default_offer['codigo'])}]::text[] || {offer_codes}) offer;
      insert into public.commercial_ally_hotmart_purchase_policies
        (tenant_ref, funnel_ref, binding_version, enabled, max_lookback)
      values ({literal(INSTANCE['tenant_ref'])},{literal(INSTANCE['funnel_ref'])},
        {INSTANCE['binding_version']},true,
        {literal(PILOT['purchase_policy']['max_lookback'])}::interval);
      -- The inbound scope as despliegue/base/aprovisionar-att1.sql publishes it:
      -- the hotlink as external_product_id and the default offer.
      insert into public.inbound_commercial_scope_versions
        (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
         external_product_id, offer_code, approved_by, approved_at, published_at)
      values ({literal(INBOUND['scope_key'])},{INBOUND['scope_version']},'published',
        {literal(INSTANCE['tenant_ref'])},{CHATWOOT['account_id']},{CHATWOOT['inbox_id']},
        {literal(HOTMART['hotlink'])},{literal(default_offer['codigo'])},
        'operator-test',now(),now());
      insert into public.followup_policy_versions
        (policy_key, version, status, purpose, timezone, business_windows,
         grace_period, expires_after, max_automatic_messages, steps,
         approved_by, approved_at, published_at)
      values ({literal(POLICY['policy_key'])},{POLICY['version']},'published',
        {literal(POLICY['purpose'])},{literal(INSTANCE['zona_horaria'])},{literal(windows)},
        {literal(POLICY['grace_period'])}::interval,{literal(POLICY['expires_after'])}::interval,
        {POLICY['max_automatic_messages']},{literal(POLICY['steps'])},
        'operator-test',now(),now());
      insert into public.pilot_scope_versions
        (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
         channel, channel_provider, channel_account_ref, source, source_event_type,
         additional_source_event_types, external_product_id, offer_code,
         additional_offer_codes, purpose, policy_key, policy_version, timezone,
         max_cohort_contacts, max_outbound_request_starts_total,
         max_outbound_request_starts_per_day, approved_by, approved_at, published_at)
      values ({literal(SCOPE['scope_key'])},{SCOPE['version']},'published',
        {literal(INSTANCE['tenant_ref'])},{CHATWOOT['account_id']},{CHATWOOT['inbox_id']},
        'whatsapp',{literal(SCOPE['channel_provider'])},
        {literal(SCOPE['channel_account_ref_prefix'] + str(CHATWOOT['inbox_id']))},
        {literal(SCOPE['source'])},{literal(SCOPE['source_event_type'])},{extra_events},
        {literal(HOTMART['product_id'])},{literal(default_offer['codigo'])},{offer_codes},
        {literal(SCOPE['purpose'])},{literal(POLICY['policy_key'])},{POLICY['version']},
        {literal(INSTANCE['zona_horaria'])},{SCOPE['max_cohort_contacts']},
        {SCOPE['max_outbound_request_starts_total']},
        {SCOPE['max_outbound_request_starts_per_day']},
        'operator-test',now(),now());
      insert into public.pilot_runtime_controls
        (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
      values ({literal(SCOPE['scope_key'])},{SCOPE['version']},'inactive',0,
        'operator-test','default-off');
    """)
    armed = query(
        "select runtime_state from public.set_lancemos_pilot_runtime_state("
        f"{literal(SCOPE['scope_key'])},{SCOPE['version']},0,'armed','operator-test','controlled-test')"
    )
    require(armed == "armed", f"the recovery scope was not armed: {armed}")


def person(index: int) -> dict[str, str]:
    offer = OFFERS[index % len(OFFERS)]
    return {
        "name": f"Compradora ATT1 adopcion {index:02d}",
        "email": f"att1-adoption-pg-{index:02d}@example.test",
        "phone": f"120255507{index:02d}",
        "offer": offer["codigo"],
        "site": offer["site"],
        "landing_id": offer["landing_id"],
        "url": offer["url"],
    }


def admit_form(lead: dict[str, str]) -> None:
    """The form of the landing of the offer (inline precedent, with consent)."""
    submitted_at = (NOW - timedelta(minutes=50)).isoformat().replace("+00:00", ".000Z")
    external_id = f"att1-adoption-pg-form-{lead['email']}"
    checkout_url = (
        f"https://pay.hotmart.com/{HOTMART['hotlink']}?off={lead['offer']}&checkoutMode=10"
    )
    page = urlsplit(lead["url"])
    page_url = f"https://{page.netloc}{page.path}"
    copy_version = MANIFEST["consentimiento"]["copy_version"]
    raw = {
        "id": external_id,
        "event": "lead.precheckout",
        "version": "1.1.0",
        "created_at": submitted_at,
        "source": {
            "system": "landing", "site": lead["site"], "aliado": INSTANCE["marca"],
            "landing_id": lead["landing_id"], "page_url": page_url,
        },
        "data": {
            "buyer": {
                "name": lead["name"], "email": lead["email"], "phone": f"+{lead['phone']}",
                "phone_country_code": "1", "phone_national": lead["phone"][1:],
            },
            "product": {
                "hotlink": HOTMART["hotlink"], "id": None, "name": HOTMART["product_name"],
                "price": float(HOTMART["precio"]), "currency": HOTMART["moneda"],
            },
            "offer": {"code": lead["offer"]},
            "checkout_url": checkout_url,
            "checkout_country": {"iso": "US", "source": "phone_country_code"},
            "consent": {
                "marketing_optin": True, "whatsapp_contact": True, "copy_version": copy_version,
            },
        },
        "dedupe_key": f"{lead['site']}:{lead['offer']}:{lead['email']}",
    }
    canonical = {
        "external_submission_id": external_id,
        "event_type": "PRECHECKOUT_FORM_SUBMITTED",
        "contract_version": "1.1.0",
        "submitted_at": submitted_at,
        "source": {
            "tenant_ref": INSTANCE["tenant_ref"], "funnel_ref": INSTANCE["funnel_ref"],
            "landing_ref": lead["landing_id"], "page_url": page_url, "aliado": INSTANCE["marca"],
        },
        "identity": {
            "email": lead["email"], "phone": lead["phone"], "phone_valid": True,
            "phone_country_iso": "US",
        },
        "lead": {"full_name": lead["name"]},
        "commerce": {
            "product_ref": HOTMART["hotlink"], "product_name": HOTMART["product_name"],
            "offer_ref": lead["offer"], "price": str(HOTMART["precio"]),
            "currency": HOTMART["moneda"], "checkout_url": checkout_url,
        },
        "dedupe_key": raw["dedupe_key"],
        "consent": {
            "terms_accepted": False, "privacy_accepted": False, "marketing_optin": True,
            "whatsapp_contact": True, "copy_version": copy_version,
        },
        "assurance": {"provisional": False, "provider_observed": True, "activation_authorized": True},
    }
    outcome = query(
        "select outcome from public.admit_portable_observed_lead_precheckout("
        f"{literal(INSTANCE['tenant_ref'])},{literal(INSTANCE['funnel_ref'])},"
        f"{INSTANCE['binding_version']},{literal(external_id)},{literal(raw)},{literal(canonical)})"
    )
    require(outcome == "inserted", f"the form was not admitted: {outcome}")


def admit_cart(lead: dict[str, str]) -> tuple[str, str]:
    """The captured cart with product, offer, id, date and buyer replaced."""
    payload = copy.deepcopy(CAPTURED_CART)
    payload["id"] = f"att1-adoption-pg-cart-{lead['email']}"
    abandoned = NOW - timedelta(minutes=30)
    payload["creation_date"] = int(abandoned.timestamp() * 1000)
    payload["data"]["product"] = {"id": HOTMART["product_id"], "name": HOTMART["product_name"]}
    payload["data"]["offer"] = {"code": lead["offer"]}
    payload["data"]["buyer"] = {"name": lead["name"], "email": lead["email"], "phone": lead["phone"]}
    admitted = query(
        "select outcome, webhook_event_id from public.admit_portable_hotmart_cart_abandonment("
        f"{literal(INSTANCE['tenant_ref'])},{literal(INSTANCE['funnel_ref'])},"
        f"{INSTANCE['binding_version']},{literal(payload['id'])},{literal(payload)},"
        f"{literal(lead['email'])},{literal(lead['phone'])})"
    ).split("|")
    require(admitted[0] == "inserted", f"the cart was not admitted: {admitted[0]}")
    return admitted[1], abandoned.isoformat()


def admit_failure(lead: dict[str, str], transaction: str) -> tuple[str, str]:
    """A payment failure (inline precedent, without a capture)."""
    failed = NOW - timedelta(minutes=10)
    payload = {
        "id": f"att1-adoption-pg-failure-{lead['email']}",
        "creation_date": int(failed.timestamp() * 1000),
        "event": "PURCHASE_CANCELED",
        "version": "2.0.0",
        "data": {
            "buyer": {"name": lead["name"], "email": lead["email"], "checkout_phone": f"+{lead['phone']}"},
            "product": {"id": HOTMART["product_id"], "name": HOTMART["product_name"]},
            "purchase": {
                "transaction": transaction,
                "status": "CANCELED",
                "offer": {"code": lead["offer"]},
                "payment": {"refusal_reason": "insufficient_funds"},
            },
            "checkout_country": {"iso": "MX", "name": "México"},
        },
    }
    admitted = query(
        "select outcome, webhook_event_id from public.admit_portable_hotmart_payment_failure("
        f"{literal(INSTANCE['tenant_ref'])},{literal(INSTANCE['funnel_ref'])},"
        f"{INSTANCE['binding_version']},{literal(payload['id'])},{literal(payload)},"
        f"{literal(lead['email'])},{literal(lead['phone'])})"
    ).split("|")
    correlation = query(
        "select correlation_outcome from public.commercial_ally_payment_failure_details "
        f"where webhook_event_id={admitted[1]!r}::uuid"
    )
    require(
        admitted[0] == "inserted" and correlation == "resolved",
        f"the payment failure was not admitted and correlated: {admitted[0]} {correlation}",
    )
    return admitted[1], failed.isoformat()


def contact_for(lead: dict[str, str], event_id: str) -> str:
    """What resolve_event leaves before planning: a new contact and its points."""
    contact = query(
        "insert into public.contacts (full_name, email, phone, country_iso) values ("
        f"{literal(lead['name'])},{literal(lead['email'])},{literal(lead['phone'])},'MX') returning id"
    )
    query(
        "insert into public.contact_points "
        "(contact_id, type, raw_value, normalized_value, source, source_event_id) values "
        f"({contact!r}::uuid,'email',{literal(lead['email'])},{literal(lead['email'])},'hotmart',{event_id!r}::uuid),"
        f"({contact!r}::uuid,'phone',{literal(lead['phone'])},{literal(lead['phone'])},'hotmart',{event_id!r}::uuid)"
    )
    generation = query(
        "select generation from public.pilot_runtime_controls "
        f"where scope_key={literal(SCOPE['scope_key'])}"
    )
    member = query(
        "select member_status from public.set_lancemos_pilot_cohort_member("
        f"{literal(SCOPE['scope_key'])},{SCOPE['version']},{contact!r}::uuid,{generation},"
        "'active','operator-test','controlled-test')"
    )
    require(member == "active", f"the contact was not enrolled: {member}")
    return contact


def plan(rpc: str, lead: dict[str, str], contact: str, event_id: str, at: str) -> tuple[str, str]:
    """The plan with the arguments resolution.resolve_event builds."""
    planned = query(
        "select created, scheduled_action_id, recovery_case_id "
        f"from public.{rpc}({event_id!r}::uuid,{contact!r}::uuid,"
        f"{literal(HOTMART['product_id'])},{literal(HOTMART['product_name'])},{literal(lead['offer'])},"
        f"{literal(POLICY['policy_key'])},{POLICY['version']},{literal(at)}::timestamptz,"
        f"{CHATWOOT['account_id']},{CHATWOOT['inbox_id']},{literal(lead['phone'])},"
        f"{literal(SCOPE['scope_key'])},{SCOPE['version']})"
    ).split("|")
    require(planned[0] == "t", f"{rpc} did not create the case: {planned}")
    return planned[1], planned[2]


START_BY_RPC = {
    "plan_lancemos_pilot_cart_recovery": "mark_lancemos_pilot_request_started",
    "plan_portable_payment_failure_recovery": "mark_portable_payment_failure_request_started",
}


def start_send(action_id: str, start_rpc: str) -> dict[str, str]:
    """The dispatcher up to the request start: claim, real reevaluation,
    approved_template reservation and the start of the pilot."""
    claimed = [
        row.split("|")
        for row in query(
            "select id, lease_generation from public.claim_due_followup_actions("
            f"{literal(WORKER)}, clock_timestamp(), interval '5 minutes', 100)"
        ).splitlines()
    ]
    require(
        [row[0] for row in claimed] == [action_id],
        f"the claim did not return only this action: {claimed}",
    )
    lease = claimed[0][1]
    decision = query(
        "select decision, case_version, sequence_revision from public.reevaluate_followup_action("
        f"{action_id!r}::uuid, {literal(WORKER)}, {lease}, clock_timestamp())"
    ).split("|")
    require(decision[0] == "execute", f"the reevaluation did not execute: {decision}")
    attempt = query(
        "select id from public.reserve_followup_delivery_attempt("
        f"{action_id!r}::uuid, {literal(WORKER)}, {lease}, {decision[1]}, {decision[2]}, "
        "'whatsapp', 'approved_template', clock_timestamp())"
    )
    phase = query(
        f"select phase from public.{start_rpc}("
        f"{action_id!r}::uuid, {attempt!r}::uuid, {literal(WORKER)}, {lease}, clock_timestamp())"
    )
    require(phase == "request_started", f"the send did not start: {phase}")
    return {"action": action_id, "attempt": attempt, "lease": lease}


ACCEPTED = 0


def acceptance_sql(delivery: dict[str, str], chatwoot: int) -> str:
    """record_and_finalize_followup_acceptance: the acceptance, or the
    reconciliation of an attempt left in delivery_unknown."""
    global ACCEPTED
    ACCEPTED += 1
    return (
        "select status from public.record_and_finalize_followup_acceptance("
        f"{delivery['action']!r}::uuid, {delivery['attempt']!r}::uuid, {literal(WORKER)}, "
        f"{delivery['lease']}, {literal(str(chatwoot))}, {literal(f'att1-adoption-pg-wamid-{ACCEPTED}')}, "
        f"{literal(f'Plantilla aprobada de ATT1 ({ACCEPTED})')}, clock_timestamp())"
    )


def accept(delivery: dict[str, str], chatwoot: int) -> None:
    status = query(acceptance_sql(delivery, chatwoot))
    require(status == "accepted_by_chatwoot", f"the acceptance did not finalize: {status}")


def leave_unknown(delivery: dict[str, str]) -> None:
    status = query(
        "select status from public.finalize_followup_delivery_attempt("
        f"{delivery['action']!r}::uuid, {delivery['attempt']!r}::uuid, {literal(WORKER)}, "
        f"{delivery['lease']}, 'delivery_unknown', null, null, 'ambiguous_timeout', null, "
        "clock_timestamp() + interval '1 hour', clock_timestamp())"
    )
    require(status == "delivery_unknown", f"the attempt did not stay delivery_unknown: {status}")


def cart_case(index: int) -> tuple[dict[str, str], dict[str, str]]:
    """One person with a cart planned and its send started."""
    lead = person(index)
    admit_form(lead)
    event_id, abandoned_at = admit_cart(lead)
    lead["contact"] = contact_for(lead, event_id)
    action, _case = plan("plan_lancemos_pilot_cart_recovery", lead, lead["contact"], event_id, abandoned_at)
    return lead, start_send(action, START_BY_RPC["plan_lancemos_pilot_cart_recovery"])


def portable_sql(chatwoot: int, user: str) -> str:
    return (
        "select outcome, automation_status, commercial_case_id "
        "from public.admit_portable_inbound_commercial_case_v1("
        f"{literal(INBOUND['scope_key'])}, {INBOUND['scope_version']}, {chatwoot}, {literal(user)})"
    )


def reply_holding(chatwoot: int, user: str) -> str:
    """A reply whose transaction stays open for HOLD_SECONDS."""
    return (
        "set deadlock_timeout='100ms'; begin; set local role service_role; "
        f"{portable_sql(chatwoot, user)}; select pg_sleep({HOLD_SECONDS}); commit;"
    )


def reply(chatwoot: int, user: str) -> str:
    return (
        "set statement_timeout='20s'; set deadlock_timeout='100ms'; set role service_role; "
        f"{portable_sql(chatwoot, user)}; reset role;"
    )


def conversation(chatwoot: int) -> dict[str, object]:
    found = query(
        "select conversation.id, conversation.status, conversation.automation_status, "
        "conversation.version, "
        "(select string_agg(commercial_case.case_kind, '+' order by commercial_case.case_kind) "
        "   from public.commercial_cases commercial_case "
        "  where commercial_case.conversation_id = conversation.id), "
        "(select count(*) from public.conversation_events event "
        f"  where event.conversation_id = conversation.id and event.event_type = {literal(ADOPTION_EVENT)}), "
        "(select string_agg(coalesce(event.related_action_id::text, ''), ',') "
        "   from public.conversation_events event "
        f"  where event.conversation_id = conversation.id and event.event_type = {literal(ADOPTION_EVENT)}) "
        "from public.conversations conversation "
        "where conversation.commercial_context = jsonb_build_object("
        f"'chatwoot_conversation_id', {literal(str(chatwoot))})"
    )
    require(found and "\n" not in found, f"expected one conversation for {chatwoot}: {found!r}")
    values = found.split("|")
    return {
        "id": values[0],
        "state": f"{values[1]}/{values[2]}",
        "version": int(values[3]),
        "kinds": values[4],
        "adoptions": int(values[5]),
        "adopted_action": values[6],
    }


def concurrent(first: str, second: str, waiter: str, events: tuple[str, ...]) -> tuple[
    subprocess.CompletedProcess[str], subprocess.CompletedProcess[str], bool
]:
    """first holds its transaction; second arrives a second later and has to
    wait for a lock of ``events`` (checked in pg_stat_activity)."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(session, first)
        arriving = pool.submit(session, second, delay=SECOND_ARRIVES)
        time.sleep(SECOND_ARRIVES)
        waited = waiting_on_lock(waiter, events)
        held, arrived = holder.result(), arriving.result()
    require(
        "deadlock detected" not in held.stderr + arrived.stderr,
        f"the two sessions deadlocked: {held.stderr.strip()} {arrived.stderr.strip()}",
    )
    return held, arrived, waited


def main() -> None:
    install_schema()
    seed()

    # 1. Two replies to the same accepted template. The first one holds its
    #    transaction; the second waits in the advisory lock of the admission
    #    and reads the admission row the first one wrote.
    lead, delivery = cart_case(1)
    accept(delivery, 950001)
    before = conversation(950001)
    held, arrived, waited = concurrent(
        reply_holding(950001, lead["phone"]),
        reply(950001, lead["phone"]),
        "admit_portable_inbound_commercial_case_v1",
        ADVISORY,
    )
    first, second = result_line(held), result_line(arrived)
    after = conversation(950001)
    require(waited, "the second reply did not wait for the first one")
    require(
        before["state"] == "active/enabled" and before["adoptions"] == 0
        and first[:2] == ["created", "draft_only"]
        and second[:2] == ["already_exists", "draft_only"]
        and second[2] == first[2]
        and after["state"] == "active/draft_only"
        and after["kinds"] == "cart_recovery+inbound_sales"
        and after["adoptions"] == 1
        and after["adopted_action"] == delivery["action"],
        f"two replies to one template: {first[:2]} {second[:2]} {before} {after}",
    )
    print("inbound_adoption_two_replies_ordered_one_adoption=OK")

    #    The same two replies without ordering them against each other. A
    #    third session holds the first advisory lock of the admission (the
    #    same key) as a barrier; the two replies are launched and the probe
    #    waits until pg_stat_activity shows both of them waiting on it at the
    #    same time. Only then is the barrier released: whoever gets the lock
    #    first adopts, the other one reads its admission.
    lead, delivery = cart_case(2)
    accept(delivery, 950002)
    barrier = (
        "set deadlock_timeout='100ms'; begin; "
        "select pg_advisory_xact_lock(hashtextextended(concat_ws(':', "
        f"'inbound-commercial-case', {literal(INBOUND['scope_key'])}, "
        f"{INBOUND['scope_version']}, 950002), 0)); "
        f"select pg_sleep({HOLD_SECONDS}); commit;"
    )
    with ThreadPoolExecutor(max_workers=3) as pool:
        holder = pool.submit(session, barrier)
        time.sleep(SECOND_ARRIVES)
        replying = [
            pool.submit(session, reply(950002, lead["phone"])) for _ in range(2)
        ]
        both_waited = waiting_on_lock(
            "admit_portable_inbound_commercial_case_v1", ADVISORY, sessions=2
        )
        held = holder.result()
        outcomes = [future.result() for future in replying]
    require(held.returncode == 0, f"the barrier failed: {held.stderr.strip()}")
    require(both_waited, "the two replies were not waiting at the same time")
    replies = sorted(result_line(outcome)[0] for outcome in outcomes)
    after = conversation(950002)
    require(
        replies == ["already_exists", "created"]
        and len({result_line(outcome)[2] for outcome in outcomes}) == 1
        and after["adoptions"] == 1
        and after["kinds"] == "cart_recovery+inbound_sales",
        f"two simultaneous replies: {replies} {after}",
    )
    print("inbound_adoption_two_replies_unordered_one_adoption=OK")

    # 2. The reconciliation of another case of the same person. The cart was
    #    accepted in 950003; the payment failure planned after it stayed in
    #    delivery_unknown. Before the reconciliation, a reply is refused (the
    #    pending-step brake, then the 22000 of the shared admission): that is
    #    what the reply would read if it did not wait. The reconciliation then
    #    accepts the payment failure in the same conversation and holds its
    #    transaction; the reply arrives, waits for the identity row and adopts
    #    with the newest template (the payment failure's).
    lead, delivery = cart_case(3)
    accept(delivery, 950003)
    event_id, failed_at = admit_failure(lead, "HPATT1ADOPTPG3")
    failure_action, _failure_case = plan(
        "plan_portable_payment_failure_recovery", lead, lead["contact"], event_id, failed_at
    )
    failure = start_send(failure_action, START_BY_RPC["plan_portable_payment_failure_recovery"])
    leave_unknown(failure)
    refused = query(
        f"begin; set local role service_role; {portable_sql(950003, lead['phone'])}; rollback;",
        expect_failure=True,
    )
    require(
        "inbound_canonical_conversation_conflict" in refused
        and conversation(950003)["adoptions"] == 0,
        f"a reply with the payment failure in delivery_unknown was not refused: {refused}",
    )
    held, arrived, waited = concurrent(
        f"set deadlock_timeout='100ms'; begin; {acceptance_sql(failure, 950003)}; "
        f"select pg_sleep({HOLD_SECONDS}); commit;",
        reply(950003, lead["phone"]),
        "admit_portable_inbound_commercial_case_v1",
        ROW_LOCK,
    )
    reconciled, replied = result_line(held), result_line(arrived)
    after = conversation(950003)
    resolution = query(
        "select reconciliation_resolution from public.followup_delivery_attempts "
        f"where id={failure['attempt']!r}::uuid"
    )
    require(waited, "the reply did not wait for the reconciliation in flight")
    require(
        reconciled == ["accepted_by_chatwoot"]
        and resolution == "accepted_by_chatwoot"
        and replied[:2] == ["created", "draft_only"]
        and after["state"] == "active/draft_only"
        and after["adoptions"] == 1
        and after["adopted_action"] == failure["action"],
        f"the reply against the reconciliation of the other case: {reconciled} {replied[:2]} {resolution} {after}",
    )
    print("inbound_adoption_waits_for_the_reconciliation_of_another_case=OK")

    # 3. The reconciliation of the template itself: a first touch left in
    #    delivery_unknown, so the conversation does not exist yet. The
    #    reconciliation creates it (enabled) and holds its transaction; the
    #    reply waits and adopts it.
    lead, delivery = cart_case(4)
    leave_unknown(delivery)
    held, arrived, waited = concurrent(
        f"set deadlock_timeout='100ms'; begin; {acceptance_sql(delivery, 950004)}; "
        f"select pg_sleep({HOLD_SECONDS}); commit;",
        reply(950004, lead["phone"]),
        "admit_portable_inbound_commercial_case_v1",
        ROW_LOCK,
    )
    reconciled, replied = result_line(held), result_line(arrived)
    after = conversation(950004)
    require(waited, "the reply did not wait for the reconciliation of its template")
    require(
        reconciled == ["accepted_by_chatwoot"]
        and replied[:2] == ["created", "draft_only"]
        and after["state"] == "active/draft_only"
        and after["kinds"] == "cart_recovery+inbound_sales"
        and after["adoptions"] == 1
        and after["adopted_action"] == delivery["action"],
        f"the reply against the reconciliation of its template: {reconciled} {replied[:2]} {after}",
    )
    print("inbound_adoption_waits_for_the_reconciliation_of_its_template=OK")

    # 4. KNOWN LIMIT (see the docstring): the reply commits first. It finds no
    #    conversation and the shared admission creates one in draft_only; the
    #    reconciliation waits for it and attaches the cart case there. No
    #    adoption event, two cases, and the attendance mark is ambiguous.
    lead, delivery = cart_case(5)
    leave_unknown(delivery)
    held, arrived, waited = concurrent(
        reply_holding(950005, lead["phone"]),
        f"set deadlock_timeout='100ms'; {acceptance_sql(delivery, 950005)};",
        "record_and_finalize_followup_acceptance",
        ROW_LOCK,
    )
    replied, reconciled = result_line(held), result_line(arrived)
    after = conversation(950005)
    attendance = query(
        "begin; set local role service_role; select outcome from "
        "public.mark_human_handoff_attended(950005, clock_timestamp(), clock_timestamp()); rollback;",
        expect_failure=True,
    )
    require(waited, "the reconciliation did not wait for the reply in flight")
    require(
        replied[:2] == ["created", "draft_only"]
        and reconciled == ["accepted_by_chatwoot"]
        and after["state"] == "active/draft_only"
        and after["kinds"] == "cart_recovery+inbound_sales"
        and after["adoptions"] == 0
        and "mark_human_handoff_attended_ambiguous_case" in attendance,
        f"the known limit changed (reply before the reconciliation): {replied[:2]} {reconciled} {after} {attendance}",
    )
    print("inbound_adoption_known_limit_reply_before_the_reconciliation=PINNED")

    total = query(
        f"select count(*) from public.conversation_events where event_type={literal(ADOPTION_EVENT)}"
    )
    require(total == "4", f"expected four adoption events in total: {total}")


if __name__ == "__main__":
    main()
