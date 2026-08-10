"""Real PostgreSQL concurrency and privilege probe for the Lancemos pilot boundary."""

from __future__ import annotations

import os
import subprocess
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
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or not parsed.hostname
        or not database
    ):
        raise RuntimeError("DATABASE_URL must identify a PostgreSQL database")
    env = os.environ.copy()
    env.update(
        PGHOST=parsed.hostname,
        PGPORT=str(parsed.port or 5432),
        PGDATABASE=database,
    )
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
        env=pg_env(),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def apply(path: Path) -> None:
    subprocess.run(args("-f", str(path)), env=pg_env(), check=True)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run_async(sql: str, app_name: str) -> subprocess.Popen[str]:
    env = pg_env()
    env["PGAPPNAME"] = app_name
    return subprocess.Popen(
        args("-A", "-t", "-F", "|", "-c", sql),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def main() -> None:
    require(
        CONFIRMATION == "pilot-boundary-concurrency",
        "ALLOW_DISPOSABLE_DATABASE=pilot-boundary-concurrency is required",
    )
    database = query("select current_database()")
    require(
        database.startswith("pilot_boundary_concurrency"),
        "database name must start with pilot_boundary_concurrency",
    )
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
      alter default privileges in schema public
        grant execute on functions to anon, authenticated;
      alter default privileges in schema public
        grant all on tables to service_role;
    """)
    apply(ROOT / "supabase/baseline/20260803_public_schema.sql")
    for migration in sorted((ROOT / "supabase/migrations").glob("*.sql")):
        apply(migration)
    print("pilot_boundary_real_postgres_migrations=OK")

    leaked_api_functions = query("""
      select coalesce(string_agg(
        p.oid::regprocedure::text || ':' || role_name,
        ',' order by p.oid::regprocedure::text, role_name
      ), '')
      from pg_proc p
      cross join (values ('anon'), ('authenticated')) roles(role_name)
      where p.pronamespace='public'::regnamespace
        and p.proname in (
          'validate_pilot_scope_version',
          'validate_pilot_runtime_control_transition',
          'reject_pilot_append_only_mutation',
          'activate_lancemos_pilot_scope_version',
          'set_lancemos_pilot_runtime_state',
          'set_lancemos_pilot_cohort_member',
          'evaluate_lancemos_pilot_scope',
          'authorize_lancemos_pilot_request_start'
        )
        and has_function_privilege(role_name, p.oid, 'execute')
    """)
    require(
        leaked_api_functions == "",
        f"pilot function privilege leak: {leaked_api_functions}",
    )
    service_functions = query("""
      select string_agg(p.proname, ',' order by p.proname)
      from pg_proc p
      where p.pronamespace='public'::regnamespace
        and p.proname in (
          'validate_pilot_scope_version',
          'validate_pilot_runtime_control_transition',
          'reject_pilot_append_only_mutation',
          'activate_lancemos_pilot_scope_version',
          'set_lancemos_pilot_runtime_state',
          'set_lancemos_pilot_cohort_member',
          'evaluate_lancemos_pilot_scope',
          'authorize_lancemos_pilot_request_start'
        )
        and has_function_privilege('service_role', p.oid, 'execute')
    """)
    require(
        service_functions == (
            "activate_lancemos_pilot_scope_version,"
            "authorize_lancemos_pilot_request_start,"
            "evaluate_lancemos_pilot_scope,"
            "set_lancemos_pilot_cohort_member,"
            "set_lancemos_pilot_runtime_state"
        ),
        f"unexpected service_role function surface: {service_functions}",
    )
    direct_dml = query("""
      set role service_role;
      do $$
      declare table_name text;
      begin
        foreach table_name in array array[
          'pilot_scope_versions',
          'pilot_runtime_controls',
          'pilot_cohort_memberships',
          'pilot_outbound_request_authorizations',
          'pilot_control_events'
        ] loop
          begin
            execute format('select 1 from public.%I limit 0', table_name);
            raise exception 'direct_select_not_blocked:%', table_name;
          exception when insufficient_privilege then null;
          end;
          begin
            execute format('delete from public.%I where false', table_name);
            raise exception 'direct_delete_not_blocked:%', table_name;
          exception when insufficient_privilege then null;
          end;
        end loop;
      end $$;
      reset role;
      select 'blocked'
    """)
    require(direct_dml == "blocked", "service_role direct pilot DML was not blocked")
    print("pilot_boundary_effective_privileges=OK")

    query("""
      insert into public.followup_policy_versions (
        policy_key,version,status,purpose,timezone,business_windows,
        grace_period,expires_after,max_automatic_messages,steps,
        approved_by,approved_at,published_at
      ) values (
        'pilot-real',1,'published','cart_recovery','UTC','[]',
        interval '0 seconds',interval '30 days',4,
        '[{"step_key":"first_contact","mode":"freeform"}]',
        'probe',clock_timestamp(),clock_timestamp()
      );
      insert into public.pilot_scope_versions (
        scope_key,version,status,tenant_key,
        chatwoot_account_id,chatwoot_inbox_id,
        channel,channel_provider,channel_account_ref,
        source,source_event_type,external_product_id,offer_code,purpose,
        policy_key,policy_version,timezone,max_cohort_contacts,
        max_outbound_request_starts_total,max_outbound_request_starts_per_day,
        approved_by,approved_at,published_at
      ) values (
        'lancemos-real',1,'published','lancemos',10,20,
        'whatsapp','waba','opaque-number-ref','hotmart',
        'PURCHASE_OUT_OF_SHOPPING_CART','3526906','offer-1','cart_recovery',
        'pilot-real',1,'UTC',1,10,2,
        'probe',clock_timestamp(),clock_timestamp()
      ), (
        'lancemos-real',2,'published','lancemos',10,20,
        'whatsapp','waba','opaque-number-ref','hotmart',
        'PURCHASE_OUT_OF_SHOPPING_CART','3526906','offer-2','cart_recovery',
        'pilot-real',1,'UTC',1,10,2,
        'probe',clock_timestamp(),clock_timestamp()
      );
      insert into public.pilot_runtime_controls(
        scope_key,scope_version,runtime_state,generation,changed_by,change_reason
      ) values ('lancemos-real',1,'inactive',0,'probe','default-off');
      insert into public.contacts(id,full_name,email,phone) values
        ('51000000-0000-4000-8000-000000000001','Concurrent One',
         'pilot-real-one@example.com','5491100000000'),
        ('51000000-0000-4000-8000-000000000002','Concurrent Two',
         'pilot-real-two@example.com','5491100000001');
    """)
    activated = query("""
      set role service_role;
      select scope_version::text || '|' || runtime_state || '|' || generation::text
      from public.activate_lancemos_pilot_scope_version(
        'lancemos-real',2,0,'probe','activate-v2'
      );
      reset role;
    """)
    require(activated == "2|inactive|1", f"V2 activation failed: {activated}")
    rolled_back = query("""
      set role service_role;
      select scope_version::text || '|' || runtime_state || '|' || generation::text
      from public.activate_lancemos_pilot_scope_version(
        'lancemos-real',1,1,'probe','rollback-v1'
      );
      reset role;
    """)
    require(rolled_back == "1|inactive|2", f"V1 rollback failed: {rolled_back}")
    armed = query("""
      set role service_role;
      select runtime_state || '|' || generation::text
      from public.set_lancemos_pilot_runtime_state(
        'lancemos-real',1,2,'armed','probe','concurrency-test'
      );
      reset role;
    """)
    require(armed == "armed|3", f"runtime did not arm after rollback: {armed}")
    print("pilot_boundary_real_postgres_version_activation=OK")

    enroll_sql = (
        "set role service_role; "
        "select member_status,generation,changed,reason_code "
        "from public.set_lancemos_pilot_cohort_member("
        "'lancemos-real',1,'{contact}',3,'active','probe','concurrency-test')"
    )
    contacts = (
        "51000000-0000-4000-8000-000000000001",
        "51000000-0000-4000-8000-000000000002",
    )
    workers = [
        run_async(enroll_sql.format(contact=contact), f"pilot-enroll-{index}")
        for index, contact in enumerate(contacts, start=1)
    ]
    results = [worker.communicate(timeout=20) + (worker.returncode,) for worker in workers]
    successes = [result for result in results if result[2] == 0]
    failures = [result for result in results if result[2] != 0]
    require(
        len(successes) == 1 and len(failures) == 1,
        f"concurrent enrollment did not fence one writer: {results}",
    )
    require(
        "pilot_runtime_generation_mismatch" in failures[0][1],
        f"unexpected enrollment failure: {failures[0][1]}",
    )
    cohort = query("""
      select contact_id::text || '|' || count(*) over ()::text
      from public.pilot_cohort_memberships
      where scope_key='lancemos-real' and member_status='active'
    """)
    winner_contact, active_count = cohort.split("|")
    require(active_count == "1", "cohort cap was exceeded concurrently")
    loser_contact = contacts[1] if winner_contact == contacts[0] else contacts[0]
    winner_email = (
        "pilot-real-one@example.com"
        if winner_contact == contacts[0]
        else "pilot-real-two@example.com"
    )
    winner_phone = "5491100000000" if winner_contact == contacts[0] else "5491100000001"
    retry = query(f"""
      set role service_role;
      select changed::text || '|' || reason_code || '|' || active_member_count::text
      from public.set_lancemos_pilot_cohort_member(
        'lancemos-real',1,'{loser_contact}',4,'active','probe','retry-after-fence'
      );
      reset role;
    """)
    require(
        retry == "false|pilot_cohort_limit_reached|1",
        f"cohort retry did not hit cap: {retry}",
    )
    print("pilot_boundary_real_postgres_cohort_concurrency=OK")

    event_id = query(f"""
      set role service_role;
      select webhook_event_id
      from public.admit_hotmart_cart_abandonment(
        'pilot-real-event',
        jsonb_build_object(
          'id','pilot-real-event',
          'creation_date',floor(extract(epoch from date_trunc('second',clock_timestamp())) * 1000)::bigint,
          'event','PURCHASE_OUT_OF_SHOPPING_CART',
          'version','2.0.0',
          'data',jsonb_build_object(
            'buyer',jsonb_build_object('email','{winner_email}','phone','{winner_phone}'),
            'product',jsonb_build_object('id',3526906,'name','Product One'),
            'offer',jsonb_build_object('code','offer-1')
          )
        )
      );
      reset role;
    """)
    query(f"""
      insert into public.contact_points(
        contact_id,type,raw_value,normalized_value,source,source_event_id
      ) values
        ('{winner_contact}','email','{winner_email}','{winner_email}','hotmart','{event_id}'),
        ('{winner_contact}','phone','{winner_phone}','{winner_phone}','hotmart','{event_id}');
      set role service_role;
      select * from public.plan_cart_recovery_with_identity(
        '{event_id}','{winner_contact}',
        '3526906','Product One','offer-1','pilot-real',1,
        (select to_timestamp((payload->>'creation_date')::double precision / 1000)
         from public.webhook_events where id='{event_id}'),
        10,20,'{winner_phone}'
      );
      reset role;
      insert into public.followup_delivery_attempts(
        id,action_id,idempotency_key,attempt_number,channel,mode,phase,
        started_at,lease_generation,expected_case_version,expected_sequence_revision
      )
      select attempts.id,action.id,'pilot-real-' || attempts.number,
             attempts.number,'whatsapp','freeform','reserved',
             clock_timestamp(),attempts.number,1,1
      from public.scheduled_actions action
      join public.recovery_cases recovery_case on recovery_case.id=action.recovery_case_id
      cross join (values
        ('53000000-0000-4000-8000-000000000001'::uuid,1),
        ('53000000-0000-4000-8000-000000000002'::uuid,2),
        ('53000000-0000-4000-8000-000000000003'::uuid,3),
        ('53000000-0000-4000-8000-000000000004'::uuid,4)
      ) attempts(id,number)
      where recovery_case.contact_id='{winner_contact}';
    """)
    action_id = query(f"""
      select action.id
      from public.scheduled_actions action
      join public.recovery_cases recovery_case on recovery_case.id=action.recovery_case_id
      where recovery_case.contact_id='{winner_contact}'
    """)

    authorize_sql = """
      set role service_role;
      select authorized::text || '|' || reason_code || '|' || replayed::text
      from public.authorize_lancemos_pilot_request_start(
        'lancemos-real',1,'lancemos',10,20,'waba','opaque-number-ref',
        'hotmart','PURCHASE_OUT_OF_SHOPPING_CART','3526906','offer-1',
        '{contact}','{action}','{attempt}',clock_timestamp()
      )
    """
    replay_workers = [
        run_async(
            authorize_sql.format(
                contact=winner_contact,
                action=action_id,
                attempt="53000000-0000-4000-8000-000000000001",
            ),
            f"pilot-replay-{index}",
        )
        for index in (1, 2)
    ]
    replay_results = [
        worker.communicate(timeout=20) + (worker.returncode,)
        for worker in replay_workers
    ]
    require(
        all(result[2] == 0 for result in replay_results),
        f"replay workers failed: {replay_results}",
    )
    replay_outputs = sorted(result[0].strip() for result in replay_results)
    require(
        replay_outputs == [
            "true|pilot_request_start_authorized|false",
            "true|pilot_request_start_authorized|true",
        ],
        f"concurrent exact replay was not idempotent: {replay_outputs}",
    )
    require(
        query("select count(*) from public.pilot_outbound_request_authorizations") == "1",
        "exact replay wrote more than one authorization",
    )
    print("pilot_boundary_real_postgres_exact_replay_concurrency=OK")

    budget_workers = [
        run_async(
            authorize_sql.format(
                contact=winner_contact,
                action=action_id,
                attempt=attempt,
            ),
            f"pilot-budget-{index}",
        )
        for index, attempt in enumerate(
            (
                "53000000-0000-4000-8000-000000000002",
                "53000000-0000-4000-8000-000000000003",
            ),
            start=1,
        )
    ]
    budget_results = [
        worker.communicate(timeout=20) + (worker.returncode,)
        for worker in budget_workers
    ]
    require(
        all(result[2] == 0 for result in budget_results),
        f"budget workers failed: {budget_results}",
    )
    outputs = sorted(result[0].strip() for result in budget_results)
    require(
        outputs == [
            "false|pilot_daily_budget_exhausted|false",
            "true|pilot_request_start_authorized|false",
        ],
        f"concurrent daily budget was not atomic: {outputs}",
    )
    require(
        query("select count(*) from public.pilot_outbound_request_authorizations") == "2",
        "daily budget concurrency exceeded two authorizations",
    )
    print("pilot_boundary_real_postgres_budget_concurrency=OK")

    paused = query("""
      set role service_role;
      select runtime_state || '|' || generation::text
      from public.set_lancemos_pilot_runtime_state(
        'lancemos-real',1,4,'paused','probe','kill-switch'
      );
      reset role;
    """)
    require(paused == "paused|5", f"kill switch did not pause: {paused}")
    blocked = query(f"""
      set role service_role;
      select authorized::text || '|' || reason_code
      from public.authorize_lancemos_pilot_request_start(
        'lancemos-real',1,'lancemos',10,20,'waba','opaque-number-ref',
        'hotmart','PURCHASE_OUT_OF_SHOPPING_CART','3526906','offer-1',
        '{winner_contact}','{action_id}',
        '53000000-0000-4000-8000-000000000004',clock_timestamp()
      );
      reset role;
    """)
    require(
        blocked == "false|pilot_runtime_not_armed",
        f"kill switch allowed request start: {blocked}",
    )
    print("pilot_boundary_real_postgres_kill_switch=OK")


if __name__ == "__main__":
    main()
