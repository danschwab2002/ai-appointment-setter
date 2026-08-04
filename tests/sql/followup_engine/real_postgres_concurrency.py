#!/usr/bin/env python3
"""Real PostgreSQL multi-session acceptance concurrency probe.

Requires an empty disposable database supplied through DATABASE_URL and a
`psql` executable supplied through PSQL (or available on PATH). The script
applies the repository baseline and follow-up migration before running two
simultaneous canonical acceptance finalizations.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, unquote, urlsplit


ROOT = Path(__file__).resolve().parents[3]
BASELINE = ROOT / "supabase/baseline/20260803_public_schema.sql"
MIGRATION = ROOT / "supabase/migrations/20260803000100_followup_engine_v1.sql"
DATABASE_URL = os.environ.get("DATABASE_URL")
PSQL = os.environ.get("PSQL", "psql")
DISPOSABLE_CONFIRMATION = os.environ.get("ALLOW_DISPOSABLE_DATABASE")


def psql_env() -> dict[str, str]:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required")
    parsed = urlsplit(DATABASE_URL)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise RuntimeError("DATABASE_URL must be a PostgreSQL URL with a host")
    database_name = unquote(parsed.path.lstrip("/"))
    if not database_name:
        raise RuntimeError("DATABASE_URL must include a database name")
    env = os.environ.copy()
    env["PGHOST"] = parsed.hostname
    env["PGPORT"] = str(parsed.port or 5432)
    env["PGDATABASE"] = database_name
    if parsed.username is not None:
        env["PGUSER"] = unquote(parsed.username)
    if parsed.password is not None:
        env["PGPASSWORD"] = unquote(parsed.password)
    sslmode = parse_qs(parsed.query).get("sslmode")
    if sslmode:
        env["PGSSLMODE"] = sslmode[-1]
    return env


def psql_args(*extra: str) -> list[str]:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required")
    return [PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", *extra]


def query(sql: str) -> str:
    result = subprocess.run(
        psql_args("-A", "-t", "-F", "|", "-c", sql),
        check=False,
        capture_output=True,
        text=True,
        env=psql_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(f"psql query failed: {result.stderr.strip()}")
    return result.stdout.strip()


def apply_sql(path: Path) -> None:
    subprocess.run(psql_args("-f", str(path)), check=True, env=psql_env())


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> None:
    require(bool(DATABASE_URL), "DATABASE_URL is required")
    require(
        DISPOSABLE_CONFIRMATION == "followup-concurrency",
        "ALLOW_DISPOSABLE_DATABASE=followup-concurrency is required",
    )
    database_name = query("select current_database()")
    require(
        database_name.startswith("followup_concurrency"),
        "database name must start with followup_concurrency",
    )
    existing = query("""
        select
          (select count(*) from pg_namespace
           where nspname <> 'public'
             and nspname <> 'information_schema'
             and nspname not like 'pg_%')
          +
          (select count(*) from pg_class
           where relnamespace = 'public'::regnamespace)
          +
          (select count(*) from pg_proc
           where pronamespace = 'public'::regnamespace)
    """)
    require(existing == "0", "refusing to run against a non-empty disposable database")

    baseline = BASELINE.read_text(encoding="utf-8").replace(
        "create extension if not exists pgcrypto;",
        "-- pgcrypto omitted: gen_random_uuid is built into PostgreSQL 17",
    )
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as handle:
        handle.write(baseline)
        baseline_path = Path(handle.name)
    try:
        apply_sql(baseline_path)
    finally:
        baseline_path.unlink(missing_ok=True)
    apply_sql(MIGRATION)
    print("real_postgres_migration_apply=OK")

    query("""
        insert into public.followup_policy_versions (
          policy_key, version, status, purpose, timezone, business_windows,
          grace_period, expires_after, max_automatic_messages, steps,
          approved_by, approved_at, published_at
        ) values (
          'concurrency-test', 1, 'published', 'cart_recovery', 'UTC',
          '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]'::jsonb,
          interval '1 hour', interval '7 days', 3,
          '[{"step_key":"first_contact","mode":"freeform"},'
          '{"step_key":"followup_1","delay":"24 hours","mode":"freeform"}]'::jsonb,
          'operator-test', now(), now()
        );
        insert into public.webhook_events (
          id, source, external_event_id, event_type, payload
        ) values (
          '10000000-0000-0000-0000-000000000001', 'hotmart',
          'real-pg-concurrency', 'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb
        );
        insert into public.contacts (id, full_name) values (
          '10000000-0000-0000-0000-000000000002', 'Concurrency Probe'
        );
        insert into public.channel_identities (
          id, contact_id, channel, account_id, external_user_id, identity_status
        ) values (
          '10000000-0000-0000-0000-000000000003',
          '10000000-0000-0000-0000-000000000002',
          'whatsapp', 'chatwoot:concurrency', 'authorized-concurrency-user', 'active'
        );
    """)

    plan = query("""
        select recovery_case_id, followup_sequence_id
        from public.plan_cart_recovery(
          '10000000-0000-0000-0000-000000000001',
          '10000000-0000-0000-0000-000000000002',
          'product-concurrency', 'Product Concurrency', 'offer-concurrency',
          'concurrency-test', 1, now() - interval '2 hours'
        )
    """).split("|")
    require(len(plan) == 2, "planning did not return aggregate IDs")
    case_id, sequence_id = plan

    query(f"""
        update public.recovery_cases
        set selected_channel_identity_id='10000000-0000-0000-0000-000000000003',
            identity_resolution_status='resolved'
        where id='{case_id}'::uuid
    """)
    action_id = query("""
        select id from public.claim_due_followup_actions(
          'real-pg-worker', now(), interval '5 minutes', 1
        )
    """)
    require(bool(action_id), "claim did not return an action")

    query(f"""
        insert into public.contact_authorizations (
          contact_id, channel, purpose, authorization_status,
          authorization_source, valid_from
        ) values (
          '10000000-0000-0000-0000-000000000002',
          'whatsapp', 'cart_recovery', 'allowed', 'system', now() - interval '1 minute'
        );
        insert into public.conversation_events (
          recovery_case_id, event_type, actor_type, related_action_id, data
        ) values (
          '{case_id}'::uuid, 'followup_action_reevaluated', 'system',
          '{action_id}'::uuid,
          jsonb_build_object(
            'decision', 'execute', 'reason_code', 'real_pg_concurrency',
            'worker_id', 'real-pg-worker', 'lease_generation', 1,
            'case_version', 1, 'sequence_revision', 1
          )
        )
    """)
    attempt_id = query(f"""
        select id from public.reserve_followup_delivery_attempt(
          '{action_id}'::uuid, 'real-pg-worker', 1, 1, 1,
          'whatsapp', 'freeform', now()
        )
    """)
    require(bool(attempt_id), "reservation did not return an attempt")
    phase = query(f"""
        select phase from public.mark_followup_request_started(
          '{action_id}'::uuid, '{attempt_id}'::uuid,
          'real-pg-worker', 1, now()
        )
    """)
    require(phase == "request_started", "request start failed")

    query("""
        create unlogged table public.concurrency_probe_sessions (
          worker_label text primary key,
          backend_pid integer not null unique
        );
        create function public._concurrency_probe_delay()
        returns trigger language plpgsql as $$
        begin
          perform pg_sleep(2);
          return new;
        end;
        $$;
        create trigger concurrency_probe_delay
        before insert on public.conversations
        for each row execute function public._concurrency_probe_delay();
    """)

    acceptance_sql = f"""
        set lock_timeout='10s';
        set statement_timeout='15s';
        select status
        from public.record_and_finalize_followup_acceptance(
          '{action_id}'::uuid, '{attempt_id}'::uuid,
          'real-pg-worker', 1,
          'real-pg-conversation', 'real-pg-message',
          'Mensaje canónico concurrente', now()
        );
    """
    workers: list[subprocess.Popen[str]] = []
    results: list[str] = []
    overlap_observed = False
    lock_wait_observed = False
    exact_backends_observed = False
    try:
        for number in (1, 2):
            env = psql_env()
            worker_label = f"worker-{number}"
            env["PGAPPNAME"] = f"followup-concurrency-{number}"
            register_sql = (
                "insert into public.concurrency_probe_sessions "
                f"values ('{worker_label}', pg_backend_pid())"
            )
            workers.append(
                subprocess.Popen(
                    psql_args(
                        "-A", "-t", "-F", "|",
                        "-c", register_sql,
                        "-c", acceptance_sql,
                    ),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
            )

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            activity = query("""
                select
                  count(distinct s.backend_pid),
                  count(*) filter (where a.state='active'),
                  count(*) filter (
                    where a.state='active' and a.wait_event_type='Lock'
                  )
                from public.concurrency_probe_sessions s
                join pg_stat_activity a on a.pid = s.backend_pid
                where s.worker_label in ('worker-1', 'worker-2')
            """)
            registered, active, waiting = (
                int(value) for value in activity.split("|")
            )
            exact_backends_observed = exact_backends_observed or registered == 2
            overlap_observed = overlap_observed or (registered == 2 and active == 2)
            lock_wait_observed = lock_wait_observed or (registered == 2 and waiting >= 1)
            if overlap_observed and lock_wait_observed:
                break
            time.sleep(0.05)

        for worker in workers:
            stdout, stderr = worker.communicate(timeout=20)
            require(
                worker.returncode == 0,
                f"concurrent worker failed: {stderr.strip()}",
            )
            rows = [line for line in stdout.splitlines() if line.strip()]
            require(len(rows) == 1, f"unexpected worker result: {stdout!r}")
            results.append(rows[0].strip())
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.terminate()
                try:
                    worker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait(timeout=5)
        query("""
            drop trigger if exists concurrency_probe_delay on public.conversations;
            drop function if exists public._concurrency_probe_delay();
            drop table if exists public.concurrency_probe_sessions;
        """)

    require(exact_backends_observed, "the two exact PostgreSQL backends were not observed")
    require(overlap_observed, "two active PostgreSQL sessions were not observed")
    require(lock_wait_observed, "a real PostgreSQL lock wait was not observed")
    require(
        all(result == "accepted_by_chatwoot" for result in results),
        f"acceptance statuses differ: {results}",
    )
    evidence = query(f"""
        select
          (select count(*)
           from public.followup_delivery_attempts attempt
           join public.messages message
             on message.id = attempt.accepted_message_id
           join public.conversations conversation
             on conversation.id = message.conversation_id
           join public.channel_identities identity
             on identity.id = conversation.channel_identity_id
           join public.scheduled_actions successor
             on successor.followup_sequence_id = '{sequence_id}'::uuid
            and successor.action_type = 'no_reply_review'
            and successor.status = 'pending'
            and successor.conversation_id = conversation.id
            and successor.anchor_type = 'accepted_outbound_message'
            and successor.anchor_subject_internal_id = message.id
            and successor.anchor_checkpoint ->> 'attempt_id' = attempt.id::text
            and successor.anchor_checkpoint ->> 'remote_message_id' = message.external_message_id
           where attempt.id = '{attempt_id}'::uuid
             and attempt.outcome = 'accepted_by_chatwoot'
             and attempt.remote_message_id = 'real-pg-message'
             and message.external_message_id = 'real-pg-message'
             and message.direction = 'outbound'
             and message.actor_type = 'ai_agent'
             and message.delivery_status = 'accepted'
             and message.content = 'Mensaje canónico concurrente'
             and message.semantic_metadata ->> 'attempt_id' = attempt.id::text
             and message.semantic_metadata ->> 'action_id' = '{action_id}'::uuid::text
             and conversation.commercial_context ->> 'chatwoot_conversation_id'
                   = 'real-pg-conversation'
             and conversation.last_message_id = message.id
             and conversation.last_message_direction = 'outbound'
             and identity.id = '10000000-0000-0000-0000-000000000003'::uuid
             and identity.external_conversation_id = 'real-pg-conversation'),
          (select count(*) from public.conversations
           where commercial_context ->> 'chatwoot_conversation_id'='real-pg-conversation'),
          (select count(*) from public.messages
           where external_message_id='real-pg-message')
    """)
    counts = [int(value) for value in evidence.split("|")]
    require(counts == [1, 1, 1], f"canonical relational evidence mismatch: {counts}")

    print("real_postgres_two_active_sessions=OK")
    print("real_postgres_lock_wait=OK")
    print("serialized_concurrent_acceptance_replay=OK")
    print("canonical_rows_and_successor=OK")


if __name__ == "__main__":
    main()
