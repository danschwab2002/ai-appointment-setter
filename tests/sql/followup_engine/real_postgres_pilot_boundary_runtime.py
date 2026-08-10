"""Real PostgreSQL probe for atomic Lancemos pilot runtime wiring."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from real_postgres_pilot_boundary import (
    ROOT,
    args,
    pg_env,
    query,
    require,
    run_async,
)

CONFIRMATION = os.environ.get("ALLOW_DISPOSABLE_DATABASE")
CONTACT = "72000000-0000-0000-0000-000000000001"


def apply(path: Path) -> None:
    migration = path.read_text(encoding="utf-8")
    subprocess.run(
        args(),
        env=pg_env(),
        input=migration,
        text=True,
        check=True,
    )


def wait_for_pause_session() -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        active = query("""
          select count(*)
          from pg_stat_activity
          where application_name='pilot-runtime-pause-race'
            and state='active'
            and query like '%pg_sleep%'
        """)
        if active == "1":
            return
        time.sleep(0.02)
    raise RuntimeError("pause transaction did not reach the race barrier")


def main() -> None:
    require(
        CONFIRMATION == "pilot-boundary-runtime",
        "ALLOW_DISPOSABLE_DATABASE=pilot-boundary-runtime is required",
    )
    database = query("select current_database()")
    require(
        database.startswith("pilot_boundary_runtime"),
        "database name must start with pilot_boundary_runtime",
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
    print("pilot_runtime_real_postgres_migrations=OK")

    acl = query("""
      select concat_ws('|',
        has_function_privilege(
          'service_role',
          'public.get_lancemos_pilot_runtime_status(text,integer,text,text,text)',
          'execute'
        ),
        has_function_privilege(
          'service_role',
          'public.plan_lancemos_pilot_cart_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer)',
          'execute'
        ),
        has_function_privilege(
          'service_role',
          'public.mark_lancemos_pilot_request_started(uuid,uuid,text,bigint,timestamptz)',
          'execute'
        ),
        has_function_privilege(
          'service_role',
          'public.authorize_lancemos_pilot_request_start(text,integer,text,bigint,bigint,text,text,text,text,text,text,uuid,uuid,uuid,timestamptz)',
          'execute'
        ),
        has_function_privilege(
          'service_role',
          'public.plan_cart_recovery_with_identity(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text)',
          'execute'
        ),
        has_function_privilege(
          'anon',
          'public.mark_lancemos_pilot_request_started(uuid,uuid,text,bigint,timestamptz)',
          'execute'
        ),
        has_function_privilege(
          'service_role',
          'public.mark_followup_request_started(uuid,uuid,text,bigint,timestamptz)',
          'execute'
        )
      )
    """)
    require(acl == "t|t|t|f|f|f|f", f"unsafe ACL: {acl}")
    print("pilot_runtime_real_postgres_privileges=OK")

    query(f"""
      insert into public.followup_policy_versions (
        policy_key,version,status,purpose,timezone,business_windows,
        grace_period,expires_after,max_automatic_messages,steps,
        approved_by,approved_at,published_at
      ) values (
        'pilot-runtime-real',1,'published','cart_recovery','UTC',
        '[{{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}}]',
        interval '0 seconds',interval '30 days',1,
        '[{{"step_key":"first_contact","mode":"approved_template"}}]',
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
        'lancemos-cart-recovery',1,'published','lancemos',10,20,
        'whatsapp','waba','opaque-number-ref','hotmart',
        'PURCHASE_OUT_OF_SHOPPING_CART','3526906','offer-1','cart_recovery',
        'pilot-runtime-real',1,'UTC',1,5,5,
        'probe',clock_timestamp(),clock_timestamp()
      );
      insert into public.pilot_runtime_controls (
        scope_key,scope_version,runtime_state,generation,changed_by,change_reason
      ) values (
        'lancemos-cart-recovery',1,'inactive',0,'probe','default-off'
      );
      insert into public.contacts(id,full_name,email,phone) values (
        '{CONTACT}','Runtime Contact','runtime@example.com','5491100000200'
      );
    """)
    event_id = query("""
      select webhook_event_id
      from public.admit_hotmart_cart_abandonment(
        'runtime-real-event',
        jsonb_build_object(
          'id','runtime-real-event',
          'creation_date',(extract(epoch from timestamptz '2026-08-10 10:00:00+00') * 1000)::bigint,
          'event','PURCHASE_OUT_OF_SHOPPING_CART',
          'version','2.0.0',
          'data',jsonb_build_object(
            'buyer',jsonb_build_object(
              'email','runtime@example.com','phone','5491100000200'
            ),
            'product',jsonb_build_object('id',3526906,'name','Product One'),
            'offer',jsonb_build_object('code','offer-1')
          )
        )
      )
    """)
    query(f"""
      insert into public.contact_points(
        contact_id,type,raw_value,normalized_value,source,source_event_id
      ) values
        ('{CONTACT}','email','runtime@example.com','runtime@example.com','hotmart','{event_id}'),
        ('{CONTACT}','phone','5491100000200','5491100000200','hotmart','{event_id}')
    """)

    inactive_status = query("""
      select concat_ws('|',configured,runtime_state,runtime_generation,reason_code)
      from public.get_lancemos_pilot_runtime_status(
        'lancemos-cart-recovery',1,'lancemos','waba','opaque-number-ref'
      )
    """)
    require(
        inactive_status == "t|inactive|0|pilot_runtime_inactive",
        f"bad inactive readiness: {inactive_status}",
    )
    rejected = False
    try:
        query(f"""
          select * from public.plan_lancemos_pilot_cart_recovery(
            '{event_id}','{CONTACT}','3526906','Product One','offer-1',
            'pilot-runtime-real',1,'2026-08-10 10:00:00+00',
            10,20,'5491100000200','lancemos-cart-recovery',1
          )
        """)
    except RuntimeError as exc:
        rejected = "pilot_scope_rejected" in str(exc)
    require(rejected, "inactive runtime admitted a plan")
    require(
        query(f"select count(*) from public.recovery_cases where contact_id='{CONTACT}'")
        == "0",
        "rejected planning left a case",
    )
    print("pilot_runtime_real_postgres_default_off=OK")

    query("""
      select * from public.set_lancemos_pilot_runtime_state(
        'lancemos-cart-recovery',1,0,'armed','probe','controlled-test'
      );
    """)
    query(f"""
      select * from public.set_lancemos_pilot_cohort_member(
        'lancemos-cart-recovery',1,'{CONTACT}',1,'active',
        'probe','controlled-test'
      )
    """)
    action_id = query(f"""
      select scheduled_action_id
      from public.plan_lancemos_pilot_cart_recovery(
        '{event_id}','{CONTACT}','3526906','Product One','offer-1',
        'pilot-runtime-real',1,'2026-08-10 10:00:00+00',
        10,20,'5491100000200','lancemos-cart-recovery',1
      )
    """)
    claim = query("""
      select concat_ws('|',id,recovery_case_id,lease_generation,expected_case_version)
      from public.claim_due_followup_actions(
        'pilot-runtime-worker',clock_timestamp(),interval '5 minutes',1
      )
    """).split("|")
    require(len(claim) == 4 and claim[0] == action_id, "action claim failed")
    _, case_id, lease_generation, case_version = claim
    query(f"""
      insert into public.conversation_events(
        recovery_case_id,event_type,actor_type,related_action_id,data
      ) values (
        '{case_id}','followup_action_reevaluated','system','{action_id}',
        jsonb_build_object(
          'decision','execute','reason_code','eligible_for_execution',
          'worker_id','pilot-runtime-worker',
          'lease_generation',{lease_generation}::bigint,
          'case_version',{case_version}::bigint,
          'sequence_revision',1::bigint
        )
      )
    """)
    freeform_blocked = False
    try:
        query(f"""
          begin;
          with wrong_mode_attempt as (
            select id
            from public.reserve_followup_delivery_attempt(
              '{action_id}','pilot-runtime-worker',{lease_generation},
              {case_version},1,'whatsapp','freeform',clock_timestamp()
            )
          )
          select * from public.mark_lancemos_pilot_request_started(
            '{action_id}',(select id from wrong_mode_attempt),
            'pilot-runtime-worker',{lease_generation},clock_timestamp()
          );
          rollback;
        """)
    except RuntimeError as exc:
        freeform_blocked = (
            "pilot_request_start_rejected" in str(exc)
            and "pilot_delivery_mode_mismatch" in str(exc)
        )
    require(freeform_blocked, "WABA request-start accepted freeform mode")
    print("pilot_runtime_real_postgres_waba_freeform_blocked=OK")

    attempt_id = query(f"""
      select id
      from public.reserve_followup_delivery_attempt(
        '{action_id}','pilot-runtime-worker',{lease_generation},{case_version},1,
        'whatsapp','approved_template',clock_timestamp()
      )
    """)

    legacy_blocked = False
    try:
        query(f"""
          select * from public.mark_followup_request_started(
            '{action_id}','{attempt_id}','pilot-runtime-worker',
            {lease_generation},clock_timestamp()
          )
        """)
    except RuntimeError as exc:
        legacy_blocked = "pilot_request_authorization_required" in str(exc)
    require(legacy_blocked, "legacy request-start bypassed the pilot")

    pause = run_async("""
      begin;
      select * from public.set_lancemos_pilot_runtime_state(
        'lancemos-cart-recovery',1,2,'paused','probe','race-test'
      );
      select pg_sleep(1);
      commit;
    """, "pilot-runtime-pause-race")
    wait_for_pause_session()
    paused_blocked = False
    try:
        query(f"""
          select * from public.mark_lancemos_pilot_request_started(
            '{action_id}','{attempt_id}','pilot-runtime-worker',
            {lease_generation},clock_timestamp()
          )
        """)
    except RuntimeError as exc:
        paused_blocked = "pilot_request_start_rejected" in str(exc)
    pause_stdout, pause_stderr = pause.communicate(timeout=5)
    require(pause.returncode == 0, f"pause race failed: {pause_stdout} {pause_stderr}")
    require(paused_blocked, "request-start crossed a concurrently committed pause")
    require(
        query(f"select phase from public.followup_delivery_attempts where id='{attempt_id}'")
        == "reserved",
        "paused race changed the attempt",
    )
    require(
        query(f"select count(*) from public.pilot_outbound_request_authorizations where attempt_id='{attempt_id}'")
        == "0",
        "paused race consumed authorization",
    )
    print("pilot_runtime_real_postgres_pause_race=OK")

    query("""
      select * from public.set_lancemos_pilot_runtime_state(
        'lancemos-cart-recovery',1,3,'armed','probe','resume-test'
      )
    """)
    started = query(f"""
      select concat_ws('|',phase,pilot_authorization_replayed,
                       pilot_authorization_id is not null)
      from public.mark_lancemos_pilot_request_started(
        '{action_id}','{attempt_id}','pilot-runtime-worker',
        {lease_generation},clock_timestamp()
      )
    """)
    require(started == "request_started|f|t", f"start failed: {started}")
    replay = query(f"""
      select concat_ws('|',phase,pilot_authorization_replayed)
      from public.mark_lancemos_pilot_request_started(
        '{action_id}','{attempt_id}','pilot-runtime-worker',
        {lease_generation},clock_timestamp()
      )
    """)
    require(replay == "request_started|t", f"replay failed: {replay}")
    print("pilot_runtime_real_postgres_atomic_start=OK")


if __name__ == "__main__":
    main()
