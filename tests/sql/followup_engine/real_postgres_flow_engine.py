"""Real PostgreSQL races of the flow engine (20261021000200 la T2.3a, 20261022000100 la T2.3b).

PGlite runs one connection, so validate_flow_engine_v1.mjs and
validate_flow_engine_motor_v1.mjs prove the routes and the engine path in
sequence. What only two real sessions can show is proven here, on the
PostgreSQL of the CI, with the barrier of real_postgres_flow_store.py (a held
transaction and pg_stat_activity, never a fixed wait):

1. two operator_route_flow_v1 to motor with the same generation: the first
   takes the route FOR UPDATE and holds it; the second waits on that lock and,
   once it has the row, sees the generation moved and returns
   flow_route_generation_stale. One flow_route_moved event, generation 1;
2. a move of the route holds it while the legacy guard of a cart plan runs:
   the guard takes the route FOR SHARE and waits for the move. With the T2.3b
   the cart in motor has the same guard as in legacy (paused and off), so the
   guard answers null once it has the row;
3. (c) the route held FOR UPDATE by another session: the admission waits for
   its 3 s lock_timeout, records a transitorio divergence (sqlstate 55P03) and
   falls to the legacy with the caller's lock_timeout, which waits for the
   holder and plans: legado_por_divergencia;
4. (d) two carts of the same person at once: the first holds the control of
   the scope; the second waits, and once it has it, sees the open case of the
   first and does not start (ya_en_curso);
5. (f) turning the flow off against a start, in both orders: a start that
   holds the control first starts and the publication waits; a publication
   that holds the control first turns it off and the start that waited ends
   the run (terminado, flujo_apagado);
6. (e) the legacy and the engine for the last unit of the shared pool: the
   legacy start holds the control of the pool; the engine start waits and,
   once it has it, counts the legacy authorization and rejects
   (tope_del_flujo). Exactly one authorization.

No deadlock in the whole run.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STORE_SPEC = importlib.util.spec_from_file_location(
    "real_postgres_flow_store", ROOT / "tests/sql/followup_engine/real_postgres_flow_store.py")
assert STORE_SPEC and STORE_SPEC.loader
store = importlib.util.module_from_spec(STORE_SPEC)
STORE_SPEC.loader.exec_module(store)
store.MIGRATION_VERSION = "20261022000100"

SCOPE = "att1-recuperacion"
VALIDATOR = {**store.VALIDATOR, "rules_version": 2}
TENANT, FUNNEL, PRODUCT_ID, HOTLINK, OFFER = "lancemos", "att1", "1011", "H00000000", "OFR11"
ACCOUNT, INBOX, COPY = 2, 11, "copy-v1"
LEGACY_POLICY = "att1-carrito-motor"
CART = json.loads((ROOT / "tests/fixtures/hotmart_cart_abandonment_rejected_v1.json").read_text(encoding="utf-8"))["payload"]


def seed_scope() -> None:
    """The legacy scope with consented intent and what the portable admissions read."""
    store.query(f"""
      insert into public.followup_policy_versions
        (policy_key, version, status, purpose, timezone, business_windows, grace_period, expires_after,
         max_automatic_messages, steps, approved_by, approved_at, published_at)
      values ('{LEGACY_POLICY}', 1, 'published', 'cart_recovery', 'America/Mexico_City',
              '[{{"days": [1,2,3,4,5,6,7], "start": "00:00", "end": "23:59"}}]'::jsonb,
              interval '0 seconds', interval '1 day', 1,
              '[{{"mode": "freeform", "step_key": "first_contact"}}]'::jsonb, 'dan', now(), now());
      insert into public.pilot_scope_versions
        (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id, channel,
         channel_provider, channel_account_ref, source, source_event_type, external_product_id,
         offer_code, purpose, policy_key, policy_version, timezone, max_cohort_contacts,
         max_outbound_request_starts_total, max_outbound_request_starts_per_day, audience_mode, approved_by,
         approved_at, published_at)
      values ('{SCOPE}', 1, 'published', '{TENANT}', {ACCOUNT}, {INBOX}, 'whatsapp', 'waba', 'chatwoot-inbox:{INBOX}',
              'hotmart', 'PURCHASE_OUT_OF_SHOPPING_CART', '{PRODUCT_ID}', '{OFFER}', 'cart_recovery',
              '{LEGACY_POLICY}', 1, 'America/Mexico_City', 20, 1000, 50, 'consented_intent', 'dan', now(), now());
      insert into public.pilot_runtime_controls
        (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
      values ('{SCOPE}', 1, 'inactive', 0, 'dan', 'el scope de prueba');
      select * from public.set_lancemos_pilot_runtime_state('{SCOPE}', 1, 0, 'armed', 'dan', 'armado de prueba');
      insert into public.hotmart_purchase_intent_scopes
        (tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref, offer_ref, max_lookback, active)
      values ('{TENANT}', '{FUNNEL}', '{PRODUCT_ID}', '{HOTLINK}', '{OFFER}', interval '24 hours', true);
      insert into public.checkout_offer_catalog (
        tenant_ref, funnel_ref, scope_key, scope_version, product_ref, landing_ref, offer_code,
        checkout_base_url, checkout_mode, default_for_inbound, status, version, approved_by, approved_at)
      values ('{TENANT}', '{FUNNEL}', 'att1-inbound', 1, '{HOTLINK}', 'landing', '{OFFER}',
              'https://pay.hotmart.com/{HOTLINK}', 10, true, 'active', 1, 'dan', now());
      insert into public.commercial_ally_hotmart_purchase_policies
        (tenant_ref, funnel_ref, binding_version, enabled, max_lookback)
      values ('{TENANT}', '{FUNNEL}', 1, true, interval '7 days');
      insert into public.inbound_commercial_scope_versions
        (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
         external_product_id, offer_code, approved_by, approved_at, published_at)
      values ('att1-inbound', 1, 'published', '{TENANT}', {ACCOUNT}, {INBOX}, '{HOTLINK}', '{OFFER}', 'dan', now(), now());
    """)


def cart_definition() -> dict:
    """The cart of one send, with the pool of the legacy scope and its daily cap.

    All day, like the legacy policy of seed_scope: with the real 09:00-21:00 of
    ATT1 the reevaluation defers (business_window_closed) whenever the CI runs
    outside those hours in Mexico City, and the races never start.
    """
    return store.definition("carrito", horario={"zona": "America/Mexico_City", "desde": "00:00", "hasta": "23:59"},
                            topes={
                                "bolsa": SCOPE, "por_dia": 50, "total": 1000,
                                "repeticion": {"una_por": "persona_y_oferta", "otra_despues_de": "0m"}})


def row(function: str, *params: object) -> dict:
    rendered = ", ".join(store.literal(param) for param in params)
    return json.loads(store.query(f"select row_to_json(r)::text from public.{function}({rendered}) r"))


def row_sql(function: str, *params: object) -> str:
    rendered = ", ".join(store.literal(param) for param in params)
    return f"select row_to_json(r)::text from public.{function}({rendered}) r;"


def db_now() -> str:
    return store.query("select clock_timestamp()::text")


_people = 0


def person() -> dict:
    global _people
    _people += 1
    suffix = f"{_people:04d}"
    return {"name": f"Persona Motor {suffix}", "email": f"motor-real-{suffix}@example.test",
            "phone": f"1202555{suffix}", "chatwoot": str(970000 + _people)}


def form(lead: dict) -> None:
    submitted = store.query("select to_char((now() - interval '50 minutes') at time zone 'UTC', "
                            "'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"')")
    form_id = f"motor-real-form-{lead['email']}-{uuid.uuid4().hex[:8]}"
    page_url = "https://prueba.example/landing"
    checkout = f"https://pay.hotmart.com/{HOTLINK}?off={OFFER}&checkoutMode=10"
    raw = {
        "id": form_id, "event": "lead.precheckout", "version": "1.1.0", "created_at": submitted,
        "source": {"system": "landing", "site": "sitio", "aliado": "Aliada de prueba", "landing_id": "landing",
                   "page_url": page_url},
        "data": {
            "buyer": {"name": lead["name"], "email": lead["email"], "phone": f"+{lead['phone']}",
                      "phone_country_code": "1", "phone_national": lead["phone"][1:]},
            "product": {"hotlink": HOTLINK, "id": None, "name": "Producto de prueba", "price": 10, "currency": "MXN"},
            "offer": {"code": OFFER}, "checkout_url": checkout,
            "checkout_country": {"iso": "US", "source": "phone_country_code"},
            "consent": {"marketing_optin": True, "whatsapp_contact": True, "copy_version": COPY},
        },
        "dedupe_key": f"sitio:{OFFER}:{lead['email']}",
    }
    canonical = {
        "external_submission_id": form_id, "event_type": "PRECHECKOUT_FORM_SUBMITTED", "contract_version": "1.1.0",
        "submitted_at": submitted,
        "source": {"tenant_ref": TENANT, "funnel_ref": FUNNEL, "landing_ref": "landing", "page_url": page_url,
                   "aliado": "Aliada de prueba"},
        "identity": {"email": lead["email"], "phone": lead["phone"], "phone_valid": True, "phone_country_iso": "US"},
        "lead": {"full_name": lead["name"]},
        "commerce": {"product_ref": HOTLINK, "product_name": "Producto de prueba", "offer_ref": OFFER, "price": "10",
                     "currency": "MXN", "checkout_url": checkout},
        "dedupe_key": raw["dedupe_key"],
        "consent": {"terms_accepted": False, "privacy_accepted": False, "marketing_optin": True,
                    "whatsapp_contact": True, "copy_version": COPY},
        "assurance": {"provisional": False, "provider_observed": True, "activation_authorized": True},
    }
    admitted = row("admit_portable_observed_lead_precheckout", TENANT, FUNNEL, 1, form_id, raw, canonical)
    store.require(admitted.get("outcome") == "inserted", f"form: {admitted}")


def cart(lead: dict) -> list[object]:
    """A form and a cart of the person; returns the arguments of the admission."""
    form(lead)
    payload = json.loads(json.dumps(CART))
    payload["id"] = f"motor-real-cart-{uuid.uuid4().hex}"
    payload["creation_date"] = int(store.query("select (extract(epoch from now() - interval '30 minutes') * 1000)::bigint"))
    payload["data"]["product"] = {"id": int(PRODUCT_ID), "name": "Producto de prueba"}
    payload["data"]["offer"] = {"code": OFFER}
    payload["data"]["buyer"] = {"name": lead["name"], "email": lead["email"], "phone": lead["phone"]}
    admitted = row("admit_portable_hotmart_cart_abandonment", TENANT, FUNNEL, 1, payload["id"], payload,
                   lead["email"], lead["phone"])
    store.require(admitted.get("outcome") == "inserted", f"cart: {admitted}")
    event = admitted["webhook_event_id"]
    if "contact" not in lead:
        lead["contact"] = store.query(
            f"insert into public.contacts (full_name, email, phone, country_iso) values "
            f"({store.literal(lead['name'])}, {store.literal(lead['email'])}, {store.literal(lead['phone'])}, 'MX') "
            "returning id")
        store.query(
            "insert into public.contact_points (contact_id, type, raw_value, normalized_value, source, source_event_id) "
            f"values ('{lead['contact']}', 'email', {store.literal(lead['email'])}, {store.literal(lead['email'])}, "
            f"'hotmart', '{event}'), ('{lead['contact']}', 'phone', {store.literal(lead['phone'])}, "
            f"{store.literal(lead['phone'])}, 'hotmart', '{event}')")
    abandoned = store.query(f"select to_timestamp({payload['creation_date']} / 1000.0)::text")
    return [event, lead["contact"], PRODUCT_ID, "Producto de prueba", OFFER, abandoned, ACCOUNT, INBOX, lead["phone"],
            SCOPE, 1, LEGACY_POLICY, 1, False]


def admit_sql(arguments: list[object]) -> str:
    return row_sql("admit_flow_engine_hotmart_v1", *arguments)


def plan_legacy(arguments: list[object]) -> dict:
    event, contact, product, name, offer, abandoned, account, inbox, phone, scope, version, policy, policy_version, _ = arguments
    return row("plan_lancemos_pilot_cart_recovery", event, contact, product, name, offer, policy, policy_version,
               abandoned, account, inbox, phone, scope, version)


def reserve(action_id: str, label: str) -> dict:
    """The claim, the reevaluation and the reserved attempt of one action."""
    store.query(f"""
      update public.scheduled_actions
         set due_at = least(now() + interval '1 day', expires_at - interval '1 minute')
       where id <> '{action_id}' and status in ('pending', 'deferred', 'retryable_failed')
         and due_at <= now() + interval '1 minute';
      update public.scheduled_actions set due_at = least(due_at, now()), next_attempt_at = null where id = '{action_id}';
    """)
    worker = f"motor-real-{label}"
    now = db_now()
    claimed = [json.loads(line) for line in store.query(
        f"select row_to_json(c)::text from public.claim_due_followup_actions('{worker}', '{now}', "
        "interval '5 minutes', 100) c").splitlines() if line]
    store.require(len(claimed) == 1 and claimed[0]["id"] == action_id, f"{label}: the claim took {claimed}")
    lease = claimed[0]["lease_generation"]
    decision = row("reevaluate_followup_action", action_id, worker, lease, now)
    store.require(decision["decision"] == "execute", f"{label}: reevaluation {decision}")
    attempt = row("reserve_followup_delivery_attempt", action_id, worker, lease, decision["case_version"],
                  decision["sequence_revision"], "whatsapp", "approved_template", now)
    return {"action": action_id, "attempt": attempt["id"], "worker": worker, "lease": lease}


def start_sql(function: str, reserved: dict) -> str:
    return (f"select row_to_json(r)::text from public.{function}('{reserved['action']}', '{reserved['attempt']}', "
            f"'{reserved['worker']}', {reserved['lease']}, clock_timestamp()) r;")


def route_sql(flow_id: str, generation: int, executor: str = "motor") -> str:
    revalidation = None
    if executor == "motor":
        version, sha = store.query(f"""
          select d.version, d.definition_sha256 from public.flow_definitions d
            join public.flow_controls c on c.flow_id = d.flow_id and c.active_version = d.version
           where d.flow_id = '{flow_id}'""").split("|")
        revalidation = {"version": int(version), "definition_sha256": sha, "rules_version": 2, "capabilities": "v1",
                        "blocks": [], "validator": VALIDATOR}
    return store.call_sql("operator_route_flow_v1", store.INSTANCE, "att1", "carrito", executor, generation,
                          "dan", "mover la ruta del carrito", revalidation)


def route_state(flow_id: str) -> tuple[str, int]:
    executor, generation = store.query(
        f"select executor, generation from public.flow_engine_routes where flow_id = '{flow_id}'").split("|")
    return executor, int(generation)


def hold_and_compete(name: str, holder_sql: str, holder_marker: str, competitor_sql: str,
                     barriers: list[tuple[str, str]]) -> tuple[dict, dict, dict]:
    """The race of the store with more than one barrier before the holder commits.

    Each barrier is a query over pg_stat_activity and pg_locks with the
    placeholder {competitor} for the application name of the competitor.
    """
    holder_app = f"flow_engine_{name}_holder"
    competitor_app = f"flow_engine_{name}_competitor"
    holder_env = store.pg_env()
    holder_env["PGAPPNAME"] = holder_app
    holder = subprocess.Popen(store.args("-A", "-t", "-F", "|"), env=holder_env, text=True,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    competitor = None
    timing: dict[str, float] = {}
    try:
        assert holder.stdin is not None
        holder.stdin.write("begin;\n" + holder_sql + "\n")
        holder.stdin.flush()
        timing["holder_barrier_s"] = round(store.wait_until(
            "select count(*) from pg_stat_activity "
            f"where application_name = '{holder_app}' and state = 'idle in transaction' "
            f"and query like '%{holder_marker}%'",
            f"{name}: the holder ran and kept its transaction open"), 3)
        competitor_env = store.pg_env()
        competitor_env["PGAPPNAME"] = competitor_app
        competitor = subprocess.Popen(store.args("-A", "-t", "-F", "|", "-c", competitor_sql), env=competitor_env,
                                      text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for label, condition in barriers:
            timing[f"{label}_s"] = round(store.wait_until(condition.format(competitor=competitor_app),
                                                          f"{name}: {label}"), 3)
        holder_out, holder_err = holder.communicate(input="commit;\n", timeout=30)
        competitor_out, competitor_err = competitor.communicate(timeout=30)
    finally:
        for process in (holder, competitor):
            if process is not None and process.poll() is None:
                process.kill()
    store.require(holder.returncode == 0, f"{name}: holder failed: {holder_err.strip()}")
    store.require(competitor.returncode == 0, f"{name}: competitor failed: {competitor_err.strip()}")
    return (json.loads(holder_out.strip().splitlines()[-1]), json.loads(competitor_out.strip().splitlines()[-1]),
            timing)


WAITS_ON_A_LOCK = ("waits_on_a_lock", "select count(*) from pg_stat_activity "
                   "where application_name = '{competitor}' and wait_event_type = 'Lock'")


def race_two_moves_same_generation(carrito: str) -> dict:
    move = route_sql(carrito, 0)
    first, second, timing = store.race("two_route_moves", move, move, "operator_route_flow_v1")
    outcomes = [first, second]
    moved = [result for result in outcomes if result.get("ok") is True and (result.get("route") or {}).get("generation") == 1]
    stale = [result for result in outcomes if result.get("reason_code") == "flow_route_generation_stale"]
    store.require(len(moved) == 1 and len(stale) == 1, f"two moves: expected one move and one stale generation: {outcomes}")
    store.require(stale[0].get("generation") == 1, f"two moves: the loser saw the new generation: {stale}")
    store.require(route_state(carrito) == ("motor", 1), f"two moves: the route is {route_state(carrito)}")
    store.require(store.count(f"select 1 from public.flow_change_events where flow_id = '{carrito}' "
                              "and event_type = 'flow_route_moved'") == 1, "two moves: exactly one flow_route_moved")
    return {"moved": 1, "stale": 1, **timing}


def race_guard_waits_for_the_move(carrito: str) -> dict:
    back = store.call("operator_route_flow_v1", store.INSTANCE, "att1", "carrito", "legado", 1, "dan",
                      "de vuelta al legado", None)
    store.require(back.get("ok") is True and back["route"]["generation"] == 2, f"back to legacy: {back}")
    guard = f"select json_build_object('stop', public._flow_engine_legacy_guard('{SCOPE}', 'carrito'))::text;"
    first, second, timing = store.race("guard_against_move", route_sql(carrito, 2), guard, "operator_route_flow_v1")
    store.require(first.get("ok") is True and first["route"]["executor"] == "motor", f"guard race: move {first}")
    # El carrito en motor tiene la misma guarda que en legado: prendido, null.
    store.require(second == {"stop": None}, f"guard race: the guard saw {second}")
    return {"guard": second["stop"], **timing}


def race_route_held_falls_to_legacy(carrito: str) -> dict:
    """(c) The route held FOR UPDATE: the engine waits 3 s and falls to the legacy."""
    lead = person()
    arguments = cart(lead)
    holder = f"select 1 as flow_engine_routes_held from public.flow_engine_routes where flow_id = '{carrito}' for update;"
    # Despues del lock_timeout, la divergencia ya esta escrita (la transaccion
    # tiene su propio id) y el legado espera la ruta otra vez.
    wrote = ("divergence_written_and_waiting_again", """
      select count(*) from pg_stat_activity a
       where a.application_name = '{competitor}' and a.wait_event_type = 'Lock'
         and exists (select 1 from pg_locks l where l.pid = a.pid and l.locktype = 'transactionid'
                      and l.granted and l.mode = 'ExclusiveLock'
                      and l.transactionid::text = a.backend_xid::text)""")
    _, admitted, timing = hold_and_compete("route_held", holder, "flow_engine_routes_held", admit_sql(arguments),
                                           [WAITS_ON_A_LOCK, wrote])
    store.require(admitted["outcome"] == "legado_por_divergencia" and admitted["divergence_first"] is True,
                  f"route held: {admitted}")
    reason, sqlstate = store.query(
        f"select reason, sqlstate from public.flow_engine_divergences where id = '{admitted['divergence_id']}'").split("|")
    store.require((reason, sqlstate) == ("transitorio:sqlstate_55p03", "55P03"), f"route held: {reason} {sqlstate}")
    store.require(store.count(f"select 1 from public.flow_engine_runs where recovery_case_id = '{admitted['recovery_case_id']}'") == 0,
                  "route held: the legacy case has no engine run")
    return {"outcome": admitted["outcome"], "reason": reason, **timing}


def race_two_carts_same_person() -> dict:
    """(d) Two carts of the same person at once: one starts, the other is ya_en_curso."""
    lead = person()
    first_args = cart(lead)
    second_args = cart(lead)
    first, second, timing = hold_and_compete("two_carts", admit_sql(first_args), "admit_flow_engine_hotmart_v1",
                                             admit_sql(second_args), [WAITS_ON_A_LOCK])
    store.require(first["outcome"] == "motor_arranco" and first["created"] is True, f"two carts: first {first}")
    store.require(second["outcome"] == "motor_no_arranca" and second["reason"] == "ya_en_curso",
                  f"two carts: second {second}")
    return {"first": first["outcome"], "second": second["reason"], **timing}


def race_last_unit_of_the_pool(legacy: dict, engine: dict, flow_id: str) -> dict:
    """(e) The legacy and the engine for the last unit of the pool: exactly one."""
    used = int(store.query(f"""
      select (select count(*) from public.pilot_outbound_request_authorizations where scope_key = '{SCOPE}'
                and local_budget_date = (now() at time zone 'America/Mexico_City')::date)
           + (select count(*) from public.flow_engine_request_authorizations where pool = '{SCOPE}'
                and local_budget_date = (now() at time zone 'America/Mexico_City')::date)"""))
    store.query(f"""
      insert into public.flow_engine_request_authorizations
        (flow_id, version, pool, legacy_scope_key, action_id, attempt_id, contact_id, local_budget_date, flow_generation)
      select '{flow_id}', 1, '{SCOPE}', '{SCOPE}', gen_random_uuid(), gen_random_uuid(), gen_random_uuid(),
             (now() at time zone 'America/Mexico_City')::date, 0
        from generate_series(1, {49 - used})""")
    first, second, timing = hold_and_compete(
        "last_unit", start_sql("mark_lancemos_pilot_request_started", legacy), "mark_lancemos_pilot_request_started",
        start_sql("mark_flow_engine_request_started_v1", engine), [WAITS_ON_A_LOCK])
    store.require(first["phase"] == "request_started", f"last unit: the legacy {first}")
    store.require(second["outcome"] == "rechazado" and second["reason"] == "tope_del_flujo", f"last unit: the engine {second}")
    store.require(store.count(f"select 1 from public.flow_engine_request_authorizations where attempt_id = '{engine['attempt']}'") == 0,
                  "last unit: the engine did not authorize")
    return {"legacy": first["phase"], "engine": second["reason"], **timing}


def publish_off_sql(flow_id: str) -> str:
    proposal_id, fingerprint = store.approved([store.state_item(flow_id, "carrito", "apagar")])
    return store.call_sql("operator_publish_flow_change_v1", store.INSTANCE, proposal_id, fingerprint,
                          store.revalidation(fingerprint))


def race_turn_off_against_a_start(flow_id: str, started_first: dict, ended_second: dict) -> dict:
    """(f) The start against turning the flow off, in both orders."""
    start, published, timing_start = hold_and_compete(
        "start_then_off", start_sql("mark_flow_engine_request_started_v1", started_first),
        "mark_flow_engine_request_started_v1", publish_off_sql(flow_id), [WAITS_ON_A_LOCK])
    store.require(start["outcome"] == "request_started", f"start then off: the start {start}")
    store.require(published.get("ok") is True, f"start then off: the publication {published}")
    store.require(store.control(flow_id)[0] == "apagado", "start then off: the flow is off")
    store.publish_now([store.state_item(flow_id, "carrito", "prender")])
    published, ended, timing_off = hold_and_compete(
        "off_then_start", publish_off_sql(flow_id), "operator_publish_flow_change_v1",
        start_sql("mark_flow_engine_request_started_v1", ended_second), [WAITS_ON_A_LOCK])
    store.require(published.get("ok") is True, f"off then start: the publication {published}")
    store.require(ended["outcome"] == "terminado" and ended["reason"] == "flujo_apagado", f"off then start: {ended}")
    status = store.query("select rc.status from public.recovery_cases rc join public.scheduled_actions a "
                         f"on a.recovery_case_id = rc.id where a.id = '{ended_second['action']}'")
    store.require(status == "cancelled", f"off then start: the case is {status}")
    return {"start_then_off": start["outcome"], "off_then_start": ended["outcome"],
            "start_then_off_s": timing_start, "off_then_start_s": timing_off}


def main() -> None:
    started = time.monotonic()
    store.install_schema()
    installed = time.monotonic()
    flows = store.seed()
    carrito = flows["carrito"]
    seed_scope()
    published = store.publish_now([
        {**store.version_item(carrito, "carrito", "Carrito abandonado"), "definicion": cart_definition()},
        store.state_item(carrito, "carrito", "prender"),
    ])
    store.require(published.get("ok") is True, f"publish failed: {published}")
    created = store.call("operator_create_flow_route_v1", store.INSTANCE, "att1", "carrito", "carrito", SCOPE,
                         "dan", "la ruta del carrito")
    store.require(created.get("ok") is True and created["route"]["executor"] == "legado", f"create: {created}")
    # La T2.3b: el motor ejecuta el carrito.
    store.require(store.query("select public._flow_engine_route_kinds()::text") == "{carrito}", "route kinds")
    # Un caso del legado, planificado con la ruta en legado, para la bolsa (e).
    legacy_lead = person()
    legacy_args = cart(legacy_lead)
    legacy_plan = plan_legacy(legacy_args)
    store.require(legacy_plan["created"] is True, f"legacy plan: {legacy_plan}")

    results = {"two_moves_same_generation": race_two_moves_same_generation(carrito)}
    results["guard_waits_for_the_move"] = race_guard_waits_for_the_move(carrito)
    store.require(route_state(carrito) == ("motor", 3), f"the route is {route_state(carrito)}")
    results["route_held_falls_to_legacy"] = race_route_held_falls_to_legacy(carrito)
    results["two_carts_same_person"] = race_two_carts_same_person()

    def engine_case() -> dict:
        lead = person()
        admitted = row("admit_flow_engine_hotmart_v1", *cart(lead))
        store.require(admitted["outcome"] == "motor_arranco", f"engine case: {admitted}")
        return reserve(admitted["scheduled_action_id"], f"motor-{lead['chatwoot']}")

    results["turn_off_against_a_start"] = race_turn_off_against_a_start(carrito, engine_case(), engine_case())
    store.publish_now([store.state_item(carrito, "carrito", "prender")])
    engine = engine_case()
    legacy = reserve(legacy_plan["scheduled_action_id"], "legado")
    results["last_unit_of_the_pool"] = race_last_unit_of_the_pool(legacy, engine, carrito)

    deadlocks = store.query("select deadlocks from pg_stat_database where datname = current_database()")
    finished = time.monotonic()
    print(json.dumps({
        "races": results,
        "deadlocks": int(deadlocks),
        "chain_seconds": round(installed - started, 1),
        "races_seconds": round(finished - installed, 1),
    }, sort_keys=True))
    store.require(int(deadlocks) == 0, f"deadlocks: {deadlocks}")
    print("flow_engine_real_postgres=OK")


if __name__ == "__main__":
    main()
