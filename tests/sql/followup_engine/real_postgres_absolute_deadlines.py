#!/usr/bin/env python3
"""Verify absolute follow-up offsets on disposable PostgreSQL 17."""
from __future__ import annotations
import importlib.util,json,shutil,subprocess,tempfile,time
from pathlib import Path
from typing import Any

ROOT=Path(__file__).resolve().parents[3]
LAB=Path('/opt/data/projects/ai-appointment-setter-postgres17-lab/scripts/run_postgres17_disposable_lab.py')
PG_ROOT=Path('/opt/data/cache/postgres17-root')
SPEC=importlib.util.spec_from_file_location('pg17lab',LAB);assert SPEC and SPEC.loader
MODULE=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(MODULE)

def require(value:bool,message:str)->None:
    if not value: raise RuntimeError(message)

def sql(cluster:Any,database:str,statement:str)->str:
    try:
        return cluster.sql(database,statement)
    except subprocess.CalledProcessError as error:
        detail=(error.stderr or error.stdout or '').strip()
        raise RuntimeError(f'postgres_query_failed: {detail}') from error

def main()->None:
    workspace=Path(tempfile.mkdtemp(prefix='absolute_deadlines_pg17_'))
    cluster=MODULE.Cluster(PG_ROOT,workspace)
    database='postgres17_lab_absolute_deadlines'
    try:
        cluster.start()
        migrations=sorted((ROOT/'supabase/migrations').glob('*.sql'))
        absolute_matches=[
            migration
            for migration in migrations
            if migration.name=='20260813000100_absolute_followup_deadlines.sql'
        ]
        require(len(absolute_matches)==1,'absolute deadlines migration missing or duplicated')
        absolute_deadlines=absolute_matches[0]
        absolute_index=migrations.index(absolute_deadlines)
        before_absolute=migrations[:absolute_index]

        negative_database='postgres17_lab_absolute_negative_preflight'
        cluster.create_database(negative_database)
        MODULE._roles_and_defaults(cluster,negative_database)
        cluster.file(negative_database,ROOT/'supabase/baseline/20260803_public_schema.sql')
        for migration in before_absolute:
            cluster.file(negative_database,migration)
        cluster.sql(negative_database,"""
          insert into public.followup_policy_versions(
            policy_key,version,status,purpose,timezone,business_windows,
            grace_period,expires_after,max_automatic_messages,steps,
            approved_by,approved_at,published_at
          ) values(
            'negative-preflight',1,'published','cart_recovery','UTC',
            '[{"days":[1],"start":"00:00","end":"23:59"}]',
            interval '0',interval '1 day',2,
            '[{"step_key":"first_contact","mode":"freeform"},{"step_key":"followup_1","delay":"-2 seconds","mode":"freeform"}]',
            'probe',now(),now()
          )
        """)
        migration_rejected=False
        try:
            cluster.file(negative_database,absolute_deadlines)
        except subprocess.CalledProcessError as error:
            migration_rejected='existing_policy_step_offset_negative' in (error.stderr or '')
        require(migration_rejected,'negative existing policy did not reject migration')
        rollback_evidence=cluster.sql(negative_database,"""
          select
            to_regprocedure('public.validate_followup_policy_step_offsets()') is null,
            not exists(
              select 1 from pg_trigger
              where tgrelid='public.followup_policy_versions'::regclass
                and tgname='followup_policy_step_offsets_validate'
                and not tgisinternal
            ),
            position(
              'v_sequence_started_at'
              in pg_get_functiondef(
                'public._finalize_followup_delivery_attempt(uuid,uuid,text,bigint,text,text,uuid,text,timestamp with time zone,timestamp with time zone,timestamp with time zone)'::regprocedure
              )
            )=0
        """)
        require(rollback_evidence=='t|t|t','negative preflight migration did not roll back')

        cluster.create_database(database);MODULE._roles_and_defaults(cluster,database)
        cluster.file(database,ROOT/'supabase/baseline/20260803_public_schema.sql')
        for migration in migrations: cluster.file(database,migration)
        fingerprints=[line.split('|') for line in cluster.file(database,ROOT/'scripts/supabase_schema_inventory.sql',tuples=True).splitlines() if line]
        migration_versions={migration.name.split('_',1)[0] for migration in migrations}
        fingerprint_versions={row[0] for row in fingerprints}
        require(
            fingerprint_versions==migration_versions
            and all(row[-1]=='fingerprint_present' for row in fingerprints),
            'fingerprints failed',
        )
        cluster.sql(database,"""
          do $mutation$
          declare
            definition text;
          begin
            select pg_get_functiondef(
              'public._finalize_followup_delivery_attempt(uuid,uuid,text,bigint,text,text,uuid,text,timestamp with time zone,timestamp with time zone,timestamp with time zone)'::regprocedure
            ) into definition;
            if position(
              'v_next_due_at := v_sequence_started_at + v_next_delay'
              in definition
            )=0 then
              raise exception 'absolute_formula_missing_before_mutation';
            end if;
            execute replace(
              definition,
              'v_next_due_at := v_sequence_started_at + v_next_delay',
              'v_next_due_at := p_now + v_next_delay'
            );
          end;
          $mutation$
        """)
        mutated_fingerprint=[
            row.split('|') for row in cluster.file(
                database,ROOT/'scripts/supabase_schema_inventory.sql',tuples=True
            ).splitlines() if row.startswith('20260813000100|')
        ]
        require(
            len(mutated_fingerprint)==1
            and mutated_fingerprint[0][-1]!='fingerprint_present',
            'fingerprint accepted chained deadline mutation',
        )
        cluster.file(database,absolute_deadlines)
        restored_fingerprint=[
            row.split('|') for row in cluster.file(
                database,ROOT/'scripts/supabase_schema_inventory.sql',tuples=True
            ).splitlines() if row.startswith('20260813000100|')
        ]
        require(
            len(restored_fingerprint)==1
            and restored_fingerprint[0][-1]=='fingerprint_present',
            'migration did not restore absolute fingerprint',
        )
        acl=[line.split('|') for line in cluster.file(database,ROOT/'scripts/supabase_acl_inventory.sql',tuples=True).splitlines() if line]
        require(bool(acl) and all(row[-1]=='ok' for row in acl),'ACL failed')
        require(sum(row[4]=='t' for row in acl)==27,'service role allowlist changed')
        cluster.sql(database,"""
          insert into public.followup_policy_versions(policy_key,version,status,purpose,timezone,business_windows,grace_period,expires_after,max_automatic_messages,steps,approved_by,approved_at,published_at)
          values('absolute-pg17',1,'published','cart_recovery','UTC','[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]',interval '0',interval '7 days',3,'[{"step_key":"first_contact","mode":"freeform"},{"step_key":"followup_1","delay":"2 seconds","mode":"freeform"},{"step_key":"followup_2","delay":"5 seconds","mode":"freeform"}]','probe',now(),now());
          insert into public.webhook_events(id,source,external_event_id,event_type,payload) values(
            '81000000-0000-0000-0000-000000000001','hotmart','absolute-pg17','PURCHASE_OUT_OF_SHOPPING_CART',
            jsonb_build_object(
              'id','absolute-pg17',
              'creation_date',(extract(epoch from clock_timestamp())*1000)::bigint,
              'event','PURCHASE_OUT_OF_SHOPPING_CART','version','2.0.0',
              'data',jsonb_build_object(
                'buyer',jsonb_build_object('email','absolute@example.test','phone','5531999999999'),
                'product',jsonb_build_object('id',3526906,'name','Product'),
                'offer',jsonb_build_object('code','offer')
              )
            )
          );
          insert into public.contacts(id,full_name,email,phone) values(
            '81000000-0000-0000-0000-000000000002','Absolute Probe',
            'absolute@example.test','5531999999999'
          );
          insert into public.contact_points(
            contact_id,type,raw_value,normalized_value,source,source_event_id
          ) values
            (
              '81000000-0000-0000-0000-000000000002','email',
              'absolute@example.test','absolute@example.test','hotmart',
              '81000000-0000-0000-0000-000000000001'
            ),
            (
              '81000000-0000-0000-0000-000000000002','phone',
              '5531999999999','5531999999999','hotmart',
              '81000000-0000-0000-0000-000000000001'
            );
          insert into public.pilot_scope_versions(
            scope_key,version,status,tenant_key,chatwoot_account_id,chatwoot_inbox_id,
            channel,channel_provider,channel_account_ref,source,source_event_type,
            external_product_id,offer_code,purpose,policy_key,policy_version,timezone,
            max_cohort_contacts,max_outbound_request_starts_total,
            max_outbound_request_starts_per_day,approved_by,approved_at,published_at
          ) values(
            'absolute-pg17',1,'published','lancemos',1,7,'whatsapp','evolution',
            'absolute-account','hotmart','PURCHASE_OUT_OF_SHOPPING_CART','3526906',
            'offer','cart_recovery','absolute-pg17',1,'UTC',1,10,10,
            'probe',now(),now()
          );
          insert into public.pilot_runtime_controls(
            scope_key,scope_version,runtime_state,generation,changed_by,change_reason
          ) values('absolute-pg17',1,'inactive',0,'probe','setup');
          update public.pilot_runtime_controls
          set runtime_state='armed',generation=1,changed_by='probe',change_reason='execute disposable test'
          where scope_key='absolute-pg17';
          insert into public.pilot_cohort_memberships(
            scope_key,scope_version,contact_id,member_status,enrolled_by,
            enrollment_reason,last_runtime_generation
          ) values(
            'absolute-pg17',1,'81000000-0000-0000-0000-000000000002',
            'active','probe','execute disposable test',1
          );
        """)
        plan=sql(cluster,database,"""select recovery_case_id::text||'|'||followup_sequence_id::text from public.plan_lancemos_pilot_cart_recovery('81000000-0000-0000-0000-000000000001','81000000-0000-0000-0000-000000000002','3526906','Product','offer','absolute-pg17',1,(select to_timestamp((payload->>'creation_date')::numeric/1000) from public.webhook_events where id='81000000-0000-0000-0000-000000000001'),1,7,'5531999999999','absolute-pg17',1)""").split('|')
        require(len(plan)==2,'plan failed');case_id,sequence_id=plan
        cluster.sql(database,"""insert into public.contact_authorizations(contact_id,channel,purpose,authorization_status,authorization_source,valid_from) values('81000000-0000-0000-0000-000000000002','whatsapp','cart_recovery','allowed','system',clock_timestamp()-interval '1 minute');""")
        for index in (1,2):
            if index == 2:
                time.sleep(3)
            when=cluster.sql(database,"select clock_timestamp()")
            action=cluster.sql(database,f"select id||'|'||lease_generation||'|'||expected_case_version from public.claim_due_followup_actions('absolute-worker',timestamptz '{when}',interval '5 minutes',1)").split('|')
            require(len(action)==3,f'claim {index} failed');action_id,generation,case_version=action
            revision=cluster.sql(database,f"select revision from public.followup_sequences where id='{sequence_id}'")
            cluster.sql(database,f"""insert into public.conversation_events(recovery_case_id,event_type,actor_type,related_action_id,data) values('{case_id}','followup_action_reevaluated','system','{action_id}',jsonb_build_object('decision','execute','reason_code','absolute_probe','worker_id','absolute-worker','lease_generation',{generation}::bigint,'case_version',{case_version}::bigint,'sequence_revision',{revision}::bigint));""")
            attempt=cluster.sql(database,f"select id from public.reserve_followup_delivery_attempt('{action_id}','absolute-worker',{generation},{case_version},{revision},'whatsapp','freeform',timestamptz '{when}')")
            sql(cluster,database,f"select phase from public.mark_lancemos_pilot_request_started('{action_id}','{attempt}','absolute-worker',{generation},timestamptz '{when}')")
            cluster.sql(database,f"select status from public.record_and_finalize_followup_acceptance('{action_id}','{attempt}','absolute-worker',{generation},'absolute-conversation','absolute-message-{index}','Message {index}',timestamptz '{when}')")
        seconds=int(cluster.sql(database,f"""select extract(epoch from (next_action.due_at-first_attempt.accepted_at))::int from public.scheduled_actions next_action cross join lateral (select min(a.accepted_at) accepted_at from public.followup_delivery_attempts a join public.scheduled_actions s on s.id=a.action_id where s.followup_sequence_id=next_action.followup_sequence_id and a.outcome='accepted_by_chatwoot') first_attempt where next_action.followup_sequence_id='{sequence_id}' and next_action.status='pending' and next_action.step_key='followup_2'"""))
        require(seconds==5,f'expected 5 seconds, got {seconds}')
        print(json.dumps({'postgres_major':17,'migrations':len(migrations),'fingerprints':len(fingerprints),'acl_ok':True,'service_entrypoints':27,'absolute_offset_seconds':seconds,'negative_preflight_rollback':True,'fingerprint_mutation_rejected':True,'status':'pass'},sort_keys=True))
    finally:
        cluster.stop();shutil.rmtree(workspace,ignore_errors=True)

if __name__=='__main__': main()
