"""Real PostgreSQL concurrency and privilege probe for inbound opt-out."""

from __future__ import annotations

import os
import subprocess
import time
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


def query(sql: str) -> str:
    result = subprocess.run(
        args("-A", "-t", "-F", "|", "-c", sql),
        env=pg_env(), text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def apply(path: Path) -> None:
    subprocess.run(args("-f", str(path)), env=pg_env(), check=True)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> None:
    require(CONFIRMATION == "optout-concurrency", "ALLOW_DISPOSABLE_DATABASE=optout-concurrency is required")
    database = query("select current_database()")
    require(database.startswith("optout_concurrency"), "database name must start with optout_concurrency")
    existing = query("""
      select
        (select count(*) from pg_namespace where nspname not in ('public','information_schema') and nspname not like 'pg_%')
        + (select count(*) from pg_class where relnamespace='public'::regnamespace)
        + (select count(*) from pg_proc where pronamespace='public'::regnamespace)
    """)
    require(existing == "0", "refusing non-empty database")
    query("""
      do $$ begin
        if not exists (select 1 from pg_roles where rolname='anon') then create role anon nologin; end if;
        if not exists (select 1 from pg_roles where rolname='authenticated') then create role authenticated nologin; end if;
        if not exists (select 1 from pg_roles where rolname='service_role') then create role service_role nologin; end if;
      end $$
      ;
      alter default privileges in schema public
        grant execute on functions to anon, authenticated;
    """)
    apply(ROOT / "supabase/baseline/20260803_public_schema.sql")
    for migration in sorted((ROOT / "supabase/migrations").glob("*.sql")):
        apply(migration)
    print("optout_real_postgres_migrations=OK")

    privileges = query("""
      select concat_ws('|',
        has_table_privilege('service_role','public.followup_delivery_attempts','select')::text,
        has_table_privilege('service_role','public.followup_delivery_attempts','insert')::text,
        has_table_privilege('service_role','public.followup_delivery_attempts','update')::text,
        has_table_privilege('service_role','public.followup_delivery_attempts','delete')::text,
        has_function_privilege('service_role','public.mark_followup_request_started(uuid,uuid,text,bigint,timestamptz)','execute')::text,
        has_function_privilege('service_role','public._mark_followup_request_started_without_opt_out_guard(uuid,uuid,text,bigint,timestamptz)','execute')::text,
        has_function_privilege('anon','public.apply_chatwoot_inbound_opt_out(bigint,bigint,bigint,bigint,text,timestamptz,text)','execute')::text,
        has_function_privilege('authenticated','public.apply_chatwoot_inbound_opt_out(bigint,bigint,bigint,bigint,text,timestamptz,text)','execute')::text,
        has_function_privilege('anon','public.mark_followup_request_started(uuid,uuid,text,bigint,timestamptz)','execute')::text,
        has_function_privilege('authenticated','public.mark_followup_request_started(uuid,uuid,text,bigint,timestamptz)','execute')::text,
        has_function_privilege('anon','public.reconcile_followup_delivery_attempt(uuid,uuid,bigint,text,text,uuid,timestamptz,text,timestamptz)','execute')::text,
        has_function_privilege('authenticated','public.reconcile_followup_delivery_attempt(uuid,uuid,bigint,text,text,uuid,timestamptz,text,timestamptz)','execute')::text,
        has_function_privilege('anon','public._finalize_opted_out_followup_not_applied(uuid,uuid,bigint,timestamptz)','execute')::text,
        has_function_privilege('authenticated','public._finalize_opted_out_followup_not_applied(uuid,uuid,bigint,timestamptz)','execute')::text
      )
    """)
    require(
        privileges == "true|false|false|false|true|false|false|false|false|false|false|false|false|false",
        f"unexpected privileges: {privileges}",
    )
    leaked_api_definers = query("""
      select coalesce(string_agg(
        p.oid::regprocedure::text || ':' || r.role_name,
        ',' order by p.oid::regprocedure::text, r.role_name
      ), '')
      from pg_proc p
      cross join (values ('anon'), ('authenticated')) as r(role_name)
      where p.pronamespace = 'public'::regnamespace
        and p.prosecdef
        and has_function_privilege(r.role_name, p.oid, 'execute')
    """)
    require(
        leaked_api_definers == "",
        f"API roles can execute SECURITY DEFINER functions: {leaked_api_definers}",
    )
    direct_dml = query("""
      set role service_role;
      do $$
      begin
        begin
          insert into public.followup_delivery_attempts default values;
          raise exception 'direct_insert_not_blocked';
        exception when insufficient_privilege then null;
        end;
        begin
          update public.followup_delivery_attempts set reason_code='bypass' where false;
          raise exception 'direct_update_not_blocked';
        exception when insufficient_privilege then null;
        end;
        begin
          delete from public.followup_delivery_attempts where false;
          raise exception 'direct_delete_not_blocked';
        exception when insufficient_privilege then null;
        end;
      end $$;
      reset role;
      select 'blocked'
    """)
    require(direct_dml == "blocked", "service_role direct DML was not blocked")
    print("optout_effective_privileges=OK")

    query("""
      insert into public.followup_policy_versions (
        policy_key, version, status, purpose, timezone, business_windows,
        grace_period, expires_after, max_automatic_messages, steps,
        approved_by, approved_at, published_at
      ) values (
        'optout-real',1,'published','cart_recovery','UTC','{}',interval '1 hour',
        interval '7 days',3,'[]','probe',clock_timestamp(),clock_timestamp()
      );
      insert into public.webhook_events(id,source,external_event_id,event_type,payload,processing_status)
      values
        ('91000000-0000-4000-8000-000000000001','hotmart','optout-real-1','PURCHASE_OUT_OF_SHOPPING_CART','{}','received'),
        ('91000000-0000-4000-8000-000000000002','hotmart','optout-real-2','PURCHASE_OUT_OF_SHOPPING_CART','{}','received');
    """)

    unmatched = query("""
      select outcome from public.apply_chatwoot_inbound_opt_out(
        1,7,7001,8001,'5531000000001',clock_timestamp(),'unsubscribe'
      )
    """)
    require(unmatched == "recorded_unmatched", "stop-first admission was not unmatched")
    query("""
      insert into public.contacts(id,full_name) values ('92000000-0000-4000-8000-000000000001','Reverse Probe');
      select * from public.plan_cart_recovery_with_identity(
        '91000000-0000-4000-8000-000000000001','92000000-0000-4000-8000-000000000001',
        'product','Product','offer','optout-real',1,clock_timestamp(),1,7,'5531000000001'
      );
      insert into public.followup_delivery_attempts(
        action_id,idempotency_key,attempt_number,channel,mode,phase,started_at,
        lease_generation,expected_case_version,expected_sequence_revision
      ) select action.id,action.idempotency_key,1,'whatsapp','freeform','reserved',clock_timestamp(),1,
               recovery_case.version,sequence.revision
        from public.scheduled_actions action
        join public.recovery_cases recovery_case on recovery_case.id=action.recovery_case_id
        join public.followup_sequences sequence on sequence.id=action.followup_sequence_id
        where recovery_case.contact_id='92000000-0000-4000-8000-000000000001';
    """)
    reverse_action_id, reverse_attempt_id = query("""
      select action.id||'|'||attempt.id
      from public.scheduled_actions action
      join public.followup_delivery_attempts attempt on attempt.action_id=action.id
      join public.recovery_cases recovery_case on recovery_case.id=action.recovery_case_id
      where recovery_case.contact_id='92000000-0000-4000-8000-000000000001'
    """).split("|")
    reverse = query(f"""
      set role service_role;
      do $$ begin
        begin
          perform public.mark_followup_request_started(
            '{reverse_action_id}','{reverse_attempt_id}','probe',1,clock_timestamp()
          );
          raise exception 'request_start_was_not_blocked';
        exception when sqlstate '55000' then
          if sqlerrm <> 'pending_chatwoot_opt_out_stop' then raise; end if;
        end;
      end $$;
      reset role;
      select 'blocked'
    """)
    require(reverse == "blocked", "reverse-order stop did not block request-start")
    print("optout_reverse_order_request_start=OK")

    query("""
      insert into public.contacts(id,full_name) values ('92000000-0000-4000-8000-000000000002','Concurrent Probe');
      select * from public.plan_cart_recovery_with_identity(
        '91000000-0000-4000-8000-000000000002','92000000-0000-4000-8000-000000000002',
        'product','Product','offer','optout-real',1,clock_timestamp(),1,7,'5531000000002'
      );
      insert into public.followup_delivery_attempts(
        action_id,idempotency_key,attempt_number,channel,mode,phase,started_at,
        lease_generation,expected_case_version,expected_sequence_revision
      ) select action.id,action.idempotency_key,1,'whatsapp','freeform','reserved',clock_timestamp(),1,
               recovery_case.version,sequence.revision
        from public.scheduled_actions action
        join public.recovery_cases recovery_case on recovery_case.id=action.recovery_case_id
        join public.followup_sequences sequence on sequence.id=action.followup_sequence_id
        where recovery_case.contact_id='92000000-0000-4000-8000-000000000002';
      create unlogged table public.optout_probe_sessions(label text primary key,pid integer unique not null);
      create function public.optout_probe_delay() returns trigger language plpgsql as $$
      begin perform pg_sleep(2); return new; end $$;
      create trigger optout_probe_delay before insert on public.contact_opt_out_events
      for each row execute function public.optout_probe_delay();
    """)
    action_attempt = query("""
      select action.id||'|'||attempt.id
      from public.scheduled_actions action
      join public.followup_delivery_attempts attempt on attempt.action_id=action.id
      join public.recovery_cases recovery_case on recovery_case.id=action.recovery_case_id
      where recovery_case.contact_id='92000000-0000-4000-8000-000000000002'
    """).split("|")
    action_id, attempt_id = action_attempt
    stop_sql = "set role service_role; select outcome from public.apply_chatwoot_inbound_opt_out(1,7,7002,8002,'5531000000002',clock_timestamp(),'unsubscribe')"
    start_sql = f"""
      set role service_role;
      do $$ begin
        begin
          perform public.mark_followup_request_started('{action_id}','{attempt_id}','probe',1,clock_timestamp());
          raise exception 'request_start_was_not_blocked';
        exception when sqlstate '55000' then
          if sqlerrm <> 'pending_chatwoot_opt_out_stop' then raise; end if;
        end;
      end $$;
      reset role;
      select 'blocked'
    """
    workers: list[subprocess.Popen[str]] = []
    try:
        for label, sql in (("stop", stop_sql), ("start", start_sql)):
            env = pg_env(); env["PGAPPNAME"] = f"optout-{label}"
            register = f"insert into public.optout_probe_sessions values ('{label}',pg_backend_pid())"
            workers.append(subprocess.Popen(args("-A","-t","-c",register,"-c",sql), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            if label == "stop":
                time.sleep(0.25)
        lock_seen = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = query("""
              select count(*) filter(where activity.state='active'),
                     count(*) filter(where activity.wait_event_type='Lock')
              from public.optout_probe_sessions probe
              join pg_stat_activity activity on activity.pid=probe.pid
            """)
            active, waiting = map(int, state.split("|"))
            lock_seen = lock_seen or (active == 2 and waiting >= 1)
            if lock_seen:
                break
            time.sleep(0.05)
        outputs = [worker.communicate(timeout=10) for worker in workers]
        require(all(worker.returncode == 0 for worker in workers), f"worker failure: {outputs}")
        require(lock_seen, "no exact backend advisory-lock wait observed")
        require("applied" in outputs[0][0] and "blocked" in outputs[1][0], f"unexpected outcomes: {outputs}")
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.terminate()
                try: worker.wait(timeout=2)
                except subprocess.TimeoutExpired: worker.kill(); worker.wait()
        query("drop trigger if exists optout_probe_delay on public.contact_opt_out_events; drop function if exists public.optout_probe_delay(); drop table if exists public.optout_probe_sessions")
    print("optout_real_postgres_concurrency=OK")


if __name__ == "__main__":
    main()
