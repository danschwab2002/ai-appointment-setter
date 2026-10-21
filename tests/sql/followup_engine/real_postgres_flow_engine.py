"""Real PostgreSQL races of the flow engine routes (20261021000200, la T2.3a).

PGlite runs one connection, so validate_flow_engine_v1.mjs proves the routes in
sequence. What only two real sessions can show is proven here, on the
PostgreSQL of the CI, with the barrier of real_postgres_flow_store.py (a held
transaction and pg_stat_activity, never a fixed wait):

1. two operator_route_flow_v1 to motor with the same generation: the first
   takes the route FOR UPDATE and holds it; the second waits on that lock and,
   once it has the row, sees the generation moved and returns
   flow_route_generation_stale. One flow_route_moved event, generation 1;
2. a move of the route holds it while the legacy guard of a cart plan runs:
   the guard takes the route FOR SHARE, waits for the move, and then sees the
   route in motor (flow_engine_not_available). Without the lock it would have
   planned by the legacy with the route already moved.

In the T2.3a the engine executes no legacy_kind, so moving to motor always
returns flow_route_kind_not_supported. This disposable database replaces
_flow_engine_route_kinds() with {carrito}, as the T2.3b will, before the
races; the first check proves the T2.3a value is still the empty one.
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STORE_SPEC = importlib.util.spec_from_file_location(
    "real_postgres_flow_store", ROOT / "tests/sql/followup_engine/real_postgres_flow_store.py")
assert STORE_SPEC and STORE_SPEC.loader
store = importlib.util.module_from_spec(STORE_SPEC)
STORE_SPEC.loader.exec_module(store)
store.MIGRATION_VERSION = "20261021000200"

SCOPE = "att1-recuperacion"
VALIDATOR = {**store.VALIDATOR, "rules_version": 2}


def seed_scope() -> None:
    store.query(f"""
      insert into public.followup_policy_versions
        (policy_key, version, status, purpose, timezone, business_windows, grace_period, expires_after,
         max_automatic_messages, steps, approved_by, approved_at, published_at)
      values ('att1-carrito-motor', 1, 'published', 'cart_recovery', 'America/Mexico_City',
              '[{{"days": [1,2,3,4,5,6,7], "start": "00:00", "end": "23:59"}}]'::jsonb,
              interval '0 seconds', interval '1 day', 1,
              '[{{"mode": "freeform", "step_key": "first_contact"}}]'::jsonb, 'dan', now(), now());
      insert into public.pilot_scope_versions
        (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id, channel,
         channel_provider, channel_account_ref, source, source_event_type, external_product_id,
         offer_code, purpose, policy_key, policy_version, timezone, max_cohort_contacts,
         max_outbound_request_starts_total, max_outbound_request_starts_per_day, approved_by,
         approved_at, published_at)
      values ('{SCOPE}', 1, 'published', 'lancemos', 2, 11, 'whatsapp', 'waba', 'waba:11', 'hotmart',
              'PURCHASE_OUT_OF_SHOPPING_CART', '1011', 'OFR11', 'cart_recovery', 'att1-carrito-motor', 1,
              'America/Mexico_City', 20, 50, 50, 'dan', now(), now());
      insert into public.pilot_runtime_controls
        (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
      values ('{SCOPE}', 1, 'inactive', 0, 'dan', 'el scope de prueba');
      select * from public.set_lancemos_pilot_runtime_state('{SCOPE}', 1, 0, 'armed', 'dan', 'armado de prueba');
    """)


def revalidation(flow_id: str) -> dict:
    version, sha = store.query(f"""
      select d.version, d.definition_sha256 from public.flow_definitions d
        join public.flow_controls c on c.flow_id = d.flow_id and c.active_version = d.version
       where d.flow_id = '{flow_id}'""").split("|")
    return {"version": int(version), "definition_sha256": sha, "rules_version": 2, "capabilities": "v1",
            "blocks": [], "validator": VALIDATOR}


def route_sql(flow_id: str, generation: int) -> str:
    return store.call_sql("operator_route_flow_v1", store.INSTANCE, "att1", "carrito", "motor", generation,
                          "dan", "el carrito al motor", revalidation(flow_id))


def route_state(flow_id: str) -> tuple[str, int]:
    executor, generation = store.query(
        f"select executor, generation from public.flow_engine_routes where flow_id = '{flow_id}'").split("|")
    return executor, int(generation)


def moved_events(flow_id: str) -> int:
    return store.count(
        f"select 1 from public.flow_change_events where flow_id = '{flow_id}' and event_type = 'flow_route_moved'")


def race_two_moves_same_generation(carrito: str) -> dict:
    move = route_sql(carrito, 0)
    first, second, timing = store.race("two_route_moves", move, move, "operator_route_flow_v1")
    outcomes = [first, second]
    moved = [result for result in outcomes if result.get("ok") is True and (result.get("route") or {}).get("generation") == 1]
    stale = [result for result in outcomes if result.get("reason_code") == "flow_route_generation_stale"]
    store.require(len(moved) == 1 and len(stale) == 1,
                  f"two moves: expected one move and one stale generation: {outcomes}")
    store.require(stale[0].get("generation") == 1, f"two moves: the loser saw the new generation: {stale}")
    store.require(route_state(carrito) == ("motor", 1), f"two moves: the route is {route_state(carrito)}")
    store.require(moved_events(carrito) == 1, "two moves: exactly one flow_route_moved")
    return {"moved": 1, "stale": 1, **timing}


def race_guard_waits_for_the_move(carrito: str) -> dict:
    # De vuelta a legado (el scope esta armado), para que la guarda vea el cambio.
    back = store.call("operator_route_flow_v1", store.INSTANCE, "att1", "carrito", "legado", 1, "dan",
                      "de vuelta al legado", None)
    store.require(back.get("ok") is True and back["route"]["generation"] == 2, f"back to legacy: {back}")
    guard = (f"select json_build_object('stop', public._flow_engine_legacy_guard('{SCOPE}', 'carrito'))::text;")
    first, second, timing = store.race("guard_against_move", route_sql(carrito, 2), guard, "operator_route_flow_v1")
    store.require(first.get("ok") is True and first["route"]["executor"] == "motor", f"guard race: move {first}")
    store.require(second == {"stop": "flow_engine_not_available"}, f"guard race: the guard saw {second}")
    return {"guard": second["stop"], **timing}


def main() -> None:
    started = time.monotonic()
    store.install_schema()
    installed = time.monotonic()
    flows = store.seed()
    carrito = flows["carrito"]
    seed_scope()
    published = store.publish_now([
        store.version_item(carrito, "carrito", "Carrito abandonado"),
        store.state_item(carrito, "carrito", "prender"),
    ])
    store.require(published.get("ok") is True, f"publish failed: {published}")
    created = store.call("operator_create_flow_route_v1", store.INSTANCE, "att1", "carrito", "carrito", SCOPE,
                         "dan", "la ruta del carrito")
    store.require(created.get("ok") is True and created["route"]["executor"] == "legado", f"create: {created}")
    # La T2.3a: el motor no ejecuta nada todavia.
    refused = json.loads(store.query(route_sql(carrito, 0).rstrip(";")))
    store.require(refused == {"ok": False, "reason_code": "flow_route_kind_not_supported"}, f"T2.3a: {refused}")
    store.query("""
      create or replace function public._flow_engine_route_kinds() returns text[] language sql immutable
      security invoker set search_path = pg_catalog, pg_temp as $f$ select array['carrito']::text[] $f$
    """)
    results = {
        "two_moves_same_generation": race_two_moves_same_generation(carrito),
        "guard_waits_for_the_move": race_guard_waits_for_the_move(carrito),
    }
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
