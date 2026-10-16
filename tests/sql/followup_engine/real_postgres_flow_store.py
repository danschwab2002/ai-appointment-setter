"""Real PostgreSQL races of the flow definition store (20261016000100).

PGlite runs one connection, so validate_flow_store.mjs proves the behaviour in
sequence. What only two real sessions can show is proven here, on the
PostgreSQL of the CI:

1. two publications of two proposals over the same base: exactly one
   publishes; the other waits for the controls of the instance and, once it
   has them, sees another base and returns flow_change_stale_base with its
   event; there is one new version and the generation went up by one;
2. two publications of the same proposal: one publishes and the other returns
   replayed, without a second version;
3. two approvals of the same proposal: one approves and the other returns
   replayed;
4. two publications over different flows of the same instance: publishing
   locks every control of the instance, so the second waits even though its
   flow is another one, and returns flow_change_stale_base (rules 8 and 10
   read the other flows); a lock on its own flows only would not wait;
5. a pause of the cart holds its control while a publication of the cart
   competes: the publication waits for the controls, sees the generation
   moved and returns flow_change_stale_base, and the flow stays paused;
6. a publication of the cart holds the controls while a pause with the
   generation it read competes: the pause waits for the control and returns
   flow_control_generation_mismatch (the CAS); without the lock it would land
   on a version it never saw;
7. two redos of the same draft with the same expected revision: the lock of
   the proposal puts them in line and the second returns
   flow_change_revision_mismatch; without it, the second would overwrite the
   items of the first.

The races use a barrier, not a fixed wait: the session that holds the lock is a
psql with its standard input open. It receives "begin;" and the call, and the
probe polls pg_stat_activity every 50 ms (for up to 10 s) until that session is
idle in transaction after the call: then it holds the lock. The second session
starts, and the probe waits until it shows up waiting on a lock. Only then the
first one commits. No race depends on who wins, and none takes longer than the
two functions. There is no loop of rounds and no pg_sleep.

The pause against a publication of the same flow also goes in sequence in
PGlite (step 4): it is the case that turns red without the generation in the
base. Here, races 5 and 6 are the two orders of the same pair with the locks
really held.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[3]
DATABASE_URL = os.environ.get("DATABASE_URL")
PSQL = os.environ.get("PSQL", "psql")
CONFIRMATION = os.environ.get("ALLOW_DISPOSABLE_DATABASE")
MIGRATION_VERSION = "20261016000100"
POLL_SECONDS = 0.05
BARRIER_SECONDS = 10.0
INSTANCE = {"tenant_ref": "lancemos", "ally_ref": "att1", "funnel_ref": "att1"}
# The rules and the mode (what ValidationResult.validator_json() emits) and the
# image. Recording and publishing require phase publish and the class of whoever
# asked for the proposal (here, the operator).
VALIDATOR = {
    "contract": "setter-flujo/v1",
    "rules_version": 1,
    "phase": "publish",
    "actor": "operador",
    "product_version": "v1.9.0",
    "git_sha": "abcdef0",
}
CLEAN = {"blocks": [], "warnings": []}


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
    result = subprocess.run(args("-f", str(path)), env=pg_env(), text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{path.name}: {result.stderr.strip()}")


def rows(path: Path) -> list[list[str]]:
    result = subprocess.run(
        args("-A", "-t", "-F", "|", "-f", str(path)),
        env=pg_env(),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"{path.name}: {result.stderr.strip()}")
    return [line.split("|") for line in result.stdout.splitlines() if line]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def literal(value: object) -> str:
    if isinstance(value, (dict, list)):
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True)
        return f"$json${encoded}$json$::jsonb"
    if value is None:
        return "null"
    return "'" + str(value).replace("'", "''") + "'"


def call(function: str, *params: object) -> dict:
    """An RPC of the store, as the owner, with its jsonb result."""
    rendered = ", ".join(literal(param) for param in params)
    return json.loads(query(f"select public.{function}({rendered})::text"))


def call_sql(function: str, *params: object) -> str:
    rendered = ", ".join(literal(param) for param in params)
    return f"select public.{function}({rendered})::text;"


def install_schema() -> None:
    require(CONFIRMATION == "flow-store", "ALLOW_DISPOSABLE_DATABASE=flow-store is required")
    database = query("select current_database()")
    require(database.startswith("flow_store"), "database name must start with flow_store")
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
      alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
      alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;
    """)
    apply(ROOT / "supabase/baseline/20260803_public_schema.sql")
    for migration in sorted((ROOT / "supabase/migrations").glob("*.sql")):
        apply(migration)
    acl = rows(ROOT / "scripts/supabase_acl_inventory.sql")
    require(bool(acl) and all(row[-1] == "ok" for row in acl), "ACL inventory failed")
    fingerprints = [row for row in rows(ROOT / "scripts/supabase_schema_inventory.sql") if row[0] == MIGRATION_VERSION]
    require(
        len(fingerprints) == 1 and fingerprints[0][-1] == "fingerprint_present",
        f"schema fingerprint failed: {fingerprints}",
    )
    print(f"flow_store_real_postgres_migrations=OK server={query('show server_version')}")


def seed() -> dict[str, str]:
    query("""
      set session_replication_role = replica;
      insert into public.commercial_ally_runtime_bindings (tenant_ref, funnel_ref, binding_version, status,
        ally_ref, lead_ally_name, lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
        product_name, product_price, currency, offer_code, consent_copy_version, hotmart_product_id,
        chatwoot_account_id, chatwoot_inbox_id, inbound_scope_key, inbound_scope_version)
      values ('lancemos', 'att1', 1, 'active', 'att1', 'Aliada de prueba', 'sitio', 'landing', 'prueba.example',
        '/landing', 'H00000000', 'Producto de prueba', 10, 'MXN', 'OFR11', 'copy-v1', 1011, 2, 11,
        'att1-inbound', 1);
    """)
    flows = {}
    for flow_key in ("carrito", "pago_fallido"):
        registered = call("operator_register_flow_v1", INSTANCE, "att1", flow_key, "dan", "alta del flujo")
        require(registered.get("ok") is True, f"register failed: {registered}")
        flows[flow_key] = registered["flow_id"]
    return flows


def definition(flow_key: str, **overrides: object) -> dict:
    """The cart block of the schema (flow-definition-v1.md, block 1), as a draft."""
    value = {
        "esquema": "setter-flujo/v1",
        "flujo": flow_key,
        "producto": "att1",
        "estado": "borrador",
        "nombre": "Carrito abandonado",
        "dispara": {"evento": "carrito"},
        "entra": {"permiso": "formulario", "contesto_a": []},
        "pasos": [{"plantilla": "att1_carrito_abandonado_01", "idioma": "es_MX", "variables": ["nombre", "producto"]}],
        "termina": {"si": ["compra"], "vence": "1d"},
        "horario": {"zona": "America/Mexico_City", "desde": "09:00", "hasta": "21:00"},
        "topes": {
            "bolsa": f"att1-{flow_key}",
            "por_dia": 80,
            "total": 1000,
            "repeticion": {"una_por": "persona_y_oferta", "otra_despues_de": "0m"},
        },
        "prioridad": {"le_gana_a": [], "no_arranca_mientras": []},
    }
    value.update(overrides)
    return value


def version_item(flow_id: str, flow_key: str, name: str) -> dict:
    return {
        "producto": "att1",
        "flujo": flow_key,
        "flow_id": flow_id,
        "tipo": "version",
        "definicion": definition(flow_key, nombre=name),
        "en_curso": "seguir",
    }


def draft_base(flow_ids: list[str]) -> dict:
    return json.loads(query(
        "select coalesce(jsonb_object_agg(flow_id::text, jsonb_build_object('active_version', active_version)), "
        f"'{{}}'::jsonb)::text from public.flow_controls where flow_id = any(array{flow_ids!r}::uuid[])"
    ))


def instance_base(flow_ids: list[str]) -> dict:
    return json.loads(query(
        f"select public._flow_store_base('lancemos', 'att1', array{flow_ids!r}::uuid[])::text"
    ))


def validated(items: list[dict]) -> tuple[str, str]:
    """A proposal saved and validated by the owner: (proposal_id, fingerprint)."""
    flow_ids = sorted({item["flow_id"] for item in items})
    saved = call("operator_save_flow_change_draft_v1", INSTANCE, None, None, items, draft_base(flow_ids),
                 "que el carrito salga a las 2 horas", "dan")
    require(saved.get("ok") is True, f"save failed: {saved}")
    recorded = call("operator_record_flow_change_validation_v1", INSTANCE, saved["proposal_id"], saved["revision"],
                    instance_base(flow_ids), VALIDATOR, CLEAN, {}, "dan")
    require(recorded.get("status") == "validada", f"validation failed: {recorded}")
    return saved["proposal_id"], recorded["fingerprint"]


def approved(items: list[dict]) -> tuple[str, str]:
    proposal_id, fingerprint = validated(items)
    result = call("operator_approve_flow_change_v1", INSTANCE, proposal_id, fingerprint, "dan")
    require(result.get("ok") is True and result.get("replayed") is False, f"approve failed: {result}")
    return proposal_id, fingerprint


def revalidation(fingerprint: str) -> dict:
    return {
        "fingerprint": fingerprint,
        "validator": VALIDATOR,
        "result": CLEAN,
        "catalog_sha256": "a" * 64,
        "validated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def wait_until(condition_sql: str, label: str) -> float:
    """Poll pg_stat_activity until the condition counts one session."""
    started = time.monotonic()
    while time.monotonic() - started < BARRIER_SECONDS:
        if query(condition_sql) == "1":
            return time.monotonic() - started
        time.sleep(POLL_SECONDS)
    raise RuntimeError(f"barrier not reached in {BARRIER_SECONDS}s: {label}")


def race(name: str, holder_sql: str, competitor_sql: str, function: str) -> tuple[dict, dict, dict]:
    """Holder takes the lock and keeps its transaction open; the competitor waits on it."""
    holder_app = f"flow_store_{name}_holder"
    competitor_app = f"flow_store_{name}_competitor"
    holder_env = pg_env()
    holder_env["PGAPPNAME"] = holder_app
    holder = subprocess.Popen(
        args("-A", "-t", "-F", "|"),
        env=holder_env,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    competitor = None
    try:
        assert holder.stdin is not None
        holder.stdin.write("begin;\n" + holder_sql + "\n")
        holder.stdin.flush()
        held = wait_until(
            "select count(*) from pg_stat_activity "
            f"where application_name = '{holder_app}' and state = 'idle in transaction' "
            f"and query like '%{function}%'",
            f"{name}: the holder ran {function} and kept its transaction open",
        )
        competitor_env = pg_env()
        competitor_env["PGAPPNAME"] = competitor_app
        competitor = subprocess.Popen(
            args("-A", "-t", "-F", "|", "-c", competitor_sql),
            env=competitor_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        waited = wait_until(
            "select count(*) from pg_stat_activity "
            f"where application_name = '{competitor_app}' and wait_event_type = 'Lock'",
            f"{name}: the competitor waits on a lock",
        )
        # communicate writes the commit and closes the standard input.
        holder_out, holder_err = holder.communicate(input="commit;\n", timeout=30)
        competitor_out, competitor_err = competitor.communicate(timeout=30)
    finally:
        for process in (holder, competitor):
            if process is not None and process.poll() is None:
                process.kill()
    require(holder.returncode == 0, f"{name}: holder failed: {holder_err.strip()}")
    require(competitor.returncode == 0, f"{name}: competitor failed: {competitor_err.strip()}")
    first = json.loads(holder_out.strip().splitlines()[-1])
    second = json.loads(competitor_out.strip().splitlines()[-1])
    timing = {"holder_barrier_s": round(held, 3), "lock_barrier_s": round(waited, 3)}
    return first, second, timing


def control(flow_id: str) -> tuple[str, int, str]:
    state, version, generation = query(
        f"select state, coalesce(active_version::text, ''), generation from public.flow_controls where flow_id = '{flow_id}'"
    ).split("|")
    return state, int(generation), version


def count(sql: str) -> int:
    return int(query(f"select count(*) from ({sql}) counted"))


def race_two_proposals_same_base(carrito: str) -> dict:
    first_id, first_fp = approved([version_item(carrito, "carrito", "Carrera A")])
    second_id, second_fp = approved([version_item(carrito, "carrito", "Carrera B")])
    _, generation_before, version_before = control(carrito)
    versions_before = count(f"select 1 from public.flow_definitions where flow_id = '{carrito}'")
    first, second, timing = race(
        "two_proposals",
        call_sql("operator_publish_flow_change_v1", INSTANCE, first_id, first_fp, revalidation(first_fp)),
        call_sql("operator_publish_flow_change_v1", INSTANCE, second_id, second_fp, revalidation(second_fp)),
        "operator_publish_flow_change_v1",
    )
    outcomes = [first, second]
    published = [result for result in outcomes if result.get("ok") is True and result.get("replayed") is False]
    stale = [result for result in outcomes if result.get("reason_code") == "flow_change_stale_base"]
    require(len(published) == 1 and len(stale) == 1, f"two proposals: expected one publication and one stale base: {outcomes}")
    loser_id = second_id if published[0] is first else first_id
    winner_id = first_id if loser_id == second_id else second_id
    require(
        count(
            "select 1 from public.flow_change_events "
            f"where proposal_id = '{loser_id}' and event_type = 'flow_change_publish_rejected' "
            "and reason_code = 'flow_change_stale_base'"
        ) == 1,
        "two proposals: the rejected publication leaves its event",
    )
    statuses = query(
        f"select string_agg(status, ',' order by status) from public.flow_change_proposals where id in ('{winner_id}', '{loser_id}')"
    )
    require(statuses == "aprobada,publicada", f"two proposals: the loser stays aprobada: {statuses}")
    state, generation_after, version_after = control(carrito)
    versions_after = count(f"select 1 from public.flow_definitions where flow_id = '{carrito}'")
    require(versions_after == versions_before + 1, "two proposals: exactly one new version")
    require(generation_after == generation_before + 1, "two proposals: the generation went up by one")
    require(int(version_after) == (int(version_before) if version_before else 0) + 1, "two proposals: the new version is active")
    return {"published": 1, "stale_base": 1, **timing}


def race_same_proposal_twice(carrito: str) -> dict:
    proposal_id, fingerprint = approved([version_item(carrito, "carrito", "Carrera C")])
    versions_before = count(f"select 1 from public.flow_definitions where flow_id = '{carrito}'")
    _, generation_before, _ = control(carrito)
    publish = call_sql("operator_publish_flow_change_v1", INSTANCE, proposal_id, fingerprint, revalidation(fingerprint))
    first, second, timing = race("same_proposal", publish, publish, "operator_publish_flow_change_v1")
    outcomes = [first, second]
    require(
        sum(1 for result in outcomes if result.get("ok") is True and result.get("replayed") is False) == 1
        and sum(1 for result in outcomes if result.get("ok") is True and result.get("replayed") is True) == 1,
        f"same proposal: expected one publication and one replay: {outcomes}",
    )
    require(
        count(f"select 1 from public.flow_definitions where flow_id = '{carrito}'") == versions_before + 1,
        "same proposal: no duplicate version",
    )
    require(
        count(f"select 1 from public.flow_definitions where proposal_id = '{proposal_id}'") == 1,
        "same proposal: one version for the proposal",
    )
    _, generation_after, _ = control(carrito)
    require(generation_after == generation_before + 1, "same proposal: the control moved once")
    require(
        count(f"select 1 from public.flow_change_events where proposal_id = '{proposal_id}' and event_type = 'flow_change_published'") == 1,
        "same proposal: one published event",
    )
    return {"published": 1, "replayed": 1, **timing}


def race_two_approvals(carrito: str) -> dict:
    proposal_id, fingerprint = validated([version_item(carrito, "carrito", "Carrera D")])
    approve = call_sql("operator_approve_flow_change_v1", INSTANCE, proposal_id, fingerprint, "dan")
    first, second, timing = race("two_approvals", approve, approve, "operator_approve_flow_change_v1")
    outcomes = [first, second]
    require(
        sum(1 for result in outcomes if result.get("ok") is True and result.get("replayed") is False) == 1
        and sum(1 for result in outcomes if result.get("ok") is True and result.get("replayed") is True) == 1,
        f"two approvals: expected one approval and one replay: {outcomes}",
    )
    require(
        count(f"select 1 from public.flow_change_events where proposal_id = '{proposal_id}' and event_type = 'flow_change_approved'") == 1,
        "two approvals: one approved event",
    )
    status = query(f"select status from public.flow_change_proposals where id = '{proposal_id}'")
    require(status == "aprobada", f"two approvals: the proposal is aprobada: {status}")
    return {"approved": 1, "replayed": 1, **timing}


def state_item(flow_id: str, flow_key: str, tipo: str) -> dict:
    return {"producto": "att1", "flujo": flow_key, "flow_id": flow_id, "tipo": tipo}


def publish_now(items: list[dict]) -> dict:
    """The whole cycle, in sequence: saved, validated, approved and published."""
    proposal_id, fingerprint = approved(items)
    result = call("operator_publish_flow_change_v1", INSTANCE, proposal_id, fingerprint, revalidation(fingerprint))
    require(result.get("ok") is True and result.get("replayed") is False, f"publish failed: {result}")
    return result


def stale_event(proposal_id: str) -> int:
    return count(
        "select 1 from public.flow_change_events "
        f"where proposal_id = '{proposal_id}' and event_type = 'flow_change_publish_rejected' "
        "and reason_code = 'flow_change_stale_base'"
    )


def race_two_proposals_other_flows(carrito: str, pago: str) -> dict:
    holder_id, holder_fp = approved([version_item(carrito, "carrito", "Carrera E")])
    competitor_id, competitor_fp = approved([version_item(pago, "pago_fallido", "Carrera F")])
    pago_before = control(pago)
    pago_versions = count(f"select 1 from public.flow_definitions where flow_id = '{pago}'")
    first, second, timing = race(
        "other_flows",
        call_sql("operator_publish_flow_change_v1", INSTANCE, holder_id, holder_fp, revalidation(holder_fp)),
        call_sql("operator_publish_flow_change_v1", INSTANCE, competitor_id, competitor_fp, revalidation(competitor_fp)),
        "operator_publish_flow_change_v1",
    )
    require(first.get("ok") is True and first.get("replayed") is False, f"other flows: the holder publishes: {first}")
    require(
        second == {"ok": False, "reason_code": "flow_change_stale_base"},
        f"other flows: the competitor waited and saw the cart move: {second}",
    )
    require(stale_event(competitor_id) == 1, "other flows: the rejected publication leaves its event")
    require(control(pago) == pago_before, "other flows: the payment flow did not move")
    require(
        count(f"select 1 from public.flow_definitions where flow_id = '{pago}'") == pago_versions,
        "other flows: no version of the payment flow",
    )
    status = query(f"select status from public.flow_change_proposals where id = '{competitor_id}'")
    require(status == "aprobada", f"other flows: the competitor stays aprobada: {status}")
    return {"published": 1, "stale_base": 1, **timing}


def race_pause_against_publication(carrito: str) -> dict:
    publish_now([state_item(carrito, "carrito", "prender")])
    proposal_id, fingerprint = approved([version_item(carrito, "carrito", "Carrera G")])
    state, generation, version = control(carrito)
    require(state == "prendido", f"pause against publication: the cart is on: {state}")
    versions_before = count(f"select 1 from public.flow_definitions where flow_id = '{carrito}'")
    first, second, timing = race(
        "pause_then_publish",
        call_sql("operator_pause_flow_v1", INSTANCE, "att1", "carrito", generation, "dan", "pausa en la carrera"),
        call_sql("operator_publish_flow_change_v1", INSTANCE, proposal_id, fingerprint, revalidation(fingerprint)),
        "operator_pause_flow_v1",
    )
    require(
        first == {"ok": True, "changed": True, "generation": generation + 1},
        f"pause against publication: the holder pauses: {first}",
    )
    require(
        second == {"ok": False, "reason_code": "flow_change_stale_base"},
        f"pause against publication: the publication sees the generation moved: {second}",
    )
    require(stale_event(proposal_id) == 1, "pause against publication: the rejected publication leaves its event")
    require(
        control(carrito) == ("pausado", generation + 1, version),
        f"pause against publication: the flow stays paused with its version: {control(carrito)}",
    )
    require(
        count(f"select 1 from public.flow_definitions where flow_id = '{carrito}'") == versions_before,
        "pause against publication: no new version",
    )
    return {"paused": 1, "stale_base": 1, **timing}


def race_publication_against_stale_pause(carrito: str) -> dict:
    publish_now([state_item(carrito, "carrito", "prender")])
    proposal_id, fingerprint = approved([version_item(carrito, "carrito", "Carrera H")])
    state, generation, version = control(carrito)
    require(state == "prendido", f"publication against pause: the cart is on: {state}")
    first, second, timing = race(
        "publish_then_pause",
        call_sql("operator_publish_flow_change_v1", INSTANCE, proposal_id, fingerprint, revalidation(fingerprint)),
        call_sql("operator_pause_flow_v1", INSTANCE, "att1", "carrito", generation, "dan", "pausa con la generacion vieja"),
        "operator_publish_flow_change_v1",
    )
    require(first.get("ok") is True and first.get("replayed") is False, f"publication against pause: the holder publishes: {first}")
    new_version = first["published"][0]["version"]
    require(new_version == int(version) + 1, f"publication against pause: the next version: {first}")
    require(
        second == {"ok": False, "reason_code": "flow_control_generation_mismatch", "generation": generation + 1},
        f"publication against pause: the pause waited and saw the generation moved: {second}",
    )
    require(
        control(carrito) == ("prendido", generation + 1, str(new_version)),
        f"publication against pause: the flow stays on with the new version: {control(carrito)}",
    )
    return {"published": 1, "generation_mismatch": 1, **timing}


def race_two_redos(carrito: str) -> dict:
    base = draft_base([carrito])
    saved = call("operator_save_flow_change_draft_v1", INSTANCE, None, None, [version_item(carrito, "carrito", "Rehacer 0")],
                 base, "un borrador para rehacer", "dan")
    require(saved.get("ok") is True and saved.get("revision") == 1, f"two redos: the draft: {saved}")
    proposal_id = saved["proposal_id"]
    redo = {
        name: call_sql("operator_save_flow_change_draft_v1", INSTANCE, proposal_id, 1,
                       [version_item(carrito, "carrito", f"Rehacer {name}")], base, f"rehacer {name}", "dan")
        for name in ("A", "B")
    }
    first, second, timing = race("two_redos", redo["A"], redo["B"], "operator_save_flow_change_draft_v1")
    require(
        first == {"ok": True, "proposal_id": proposal_id, "revision": 2, "status": "borrador"},
        f"two redos: the holder redoes: {first}",
    )
    require(
        second == {"ok": False, "reason_code": "flow_change_revision_mismatch", "revision": 2},
        f"two redos: the second sees the revision moved: {second}",
    )
    stored = query(
        "select revision || '|' || (items -> 0 -> 'definicion' ->> 'nombre') || '|' || purpose "
        f"from public.flow_change_proposals where id = '{proposal_id}'"
    )
    require(stored == "2|Rehacer A|rehacer A", f"two redos: the draft is the first redo: {stored}")
    return {"redone": 1, "revision_mismatch": 1, **timing}


def probe_permissions() -> None:
    """The owner path only: as PostgREST would, service_role cannot run or write."""
    for role in ("service_role", "anon"):
        denied = query(
            f"set role {role}; select public.operator_pause_flow_v1({literal(INSTANCE)}, 'att1', 'carrito', 0, 'dan', 'pausa');",
            expect_failure=True,
        )
        require("permission denied for function operator_pause_flow_v1" in denied, f"{role} executed the pause: {denied}")
        denied = query(f"set role {role}; select count(*) from public.flow_controls;", expect_failure=True)
        require("permission denied for table flow_controls" in denied, f"{role} read the controls: {denied}")


def main() -> None:
    started = time.monotonic()
    install_schema()
    installed = time.monotonic()
    flows = seed()
    carrito = flows["carrito"]
    # The v1 of the cart, by the whole cycle on a real server.
    proposal_id, fingerprint = approved([version_item(carrito, "carrito", "Carrito abandonado")])
    first = call("operator_publish_flow_change_v1", INSTANCE, proposal_id, fingerprint, revalidation(fingerprint))
    require(first.get("ok") is True and first["published"][0]["version"] == 1, f"cycle failed: {first}")
    probe_permissions()
    results = {
        "two_proposals_same_base": race_two_proposals_same_base(carrito),
        "same_proposal_twice": race_same_proposal_twice(carrito),
        "two_approvals": race_two_approvals(carrito),
        "two_proposals_other_flows": race_two_proposals_other_flows(carrito, flows["pago_fallido"]),
        "pause_against_publication": race_pause_against_publication(carrito),
        "publication_against_stale_pause": race_publication_against_stale_pause(carrito),
        "two_redos": race_two_redos(carrito),
    }
    deadlocks = query("select deadlocks from pg_stat_database where datname = current_database()")
    finished = time.monotonic()
    print(json.dumps({
        "races": results,
        "deadlocks": int(deadlocks),
        "chain_seconds": round(installed - started, 1),
        "races_seconds": round(finished - installed, 1),
        "total_seconds": round(finished - started, 1),
    }, sort_keys=True))
    require(int(deadlocks) == 0, f"deadlocks: {deadlocks}")
    print("flow_store_real_postgres=OK")


if __name__ == "__main__":
    main()
