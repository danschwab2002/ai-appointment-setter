#!/usr/bin/env python3
"""Verify both request-start/handoff race orderings on disposable PostgreSQL."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = Path(__file__).with_name("fixtures") / "handoff_race_setup.sql"
CONTACT_ID = "73000000-0000-0000-0000-000000000002"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pg-bin", type=Path, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--admin-db", default="postgres")
    return parser.parse_args()


class PostgresHarness:
    def __init__(self, args: argparse.Namespace) -> None:
        self.psql = args.pg_bin / "psql"
        self.createdb = args.pg_bin / "createdb"
        self.dropdb = args.pg_bin / "dropdb"
        self.host = args.host
        self.port = str(args.port)
        self.admin_db = args.admin_db
        self.env = os.environ.copy()
        for parent in args.pg_bin.parents:
            lib_dir = parent / "lib" / "x86_64-linux-gnu"
            if lib_dir.exists():
                self.env["LD_LIBRARY_PATH"] = str(lib_dir)
                break

    def base(self, executable: Path, database: str) -> list[str]:
        return [
            str(executable),
            "-h",
            self.host,
            "-p",
            self.port,
            database,
        ]

    def create_database(self) -> str:
        database = f"handoff_race_{uuid.uuid4().hex[:12]}"
        subprocess.run(
            self.base(self.createdb, database),
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        return database

    def drop_database(self, database: str) -> None:
        subprocess.run(
            self.base(self.dropdb, database),
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )

    def psql_command(self, database: str) -> list[str]:
        return self.base(self.psql, database) + ["-v", "ON_ERROR_STOP=1"]

    def sql(
        self,
        database: str,
        statement: str,
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.psql_command(database) + ["-At", "-c", statement],
            env=self.env,
            check=check,
            capture_output=True,
            text=True,
        )

    def apply_schema_and_fixture(self, database: str) -> None:
        self.sql(
            database,
            """
            do $roles$
            begin
              if not exists (select 1 from pg_roles where rolname = 'anon') then
                create role anon nologin;
              end if;
              if not exists (
                select 1 from pg_roles where rolname = 'authenticated'
              ) then
                create role authenticated nologin;
              end if;
              if not exists (
                select 1 from pg_roles where rolname = 'service_role'
              ) then
                create role service_role nologin;
              end if;
            end
            $roles$;
            """,
        )
        files = [
            ROOT / "supabase/baseline/20260803_public_schema.sql",
            *sorted((ROOT / "supabase/migrations").glob("*.sql")),
            FIXTURE,
        ]
        for path in files:
            result = subprocess.run(
                self.psql_command(database) + ["-f", str(path)],
                env=self.env,
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"failed to apply {path.name}: {result.stderr}")


def ids() -> str:
    return f"""
    select
      (select cases.id from public.recovery_cases cases
       where cases.contact_id='{CONTACT_ID}'::uuid) as recovery_case_id,
      (select attempt.action_id from public.followup_delivery_attempts attempt
       limit 1) as action_id,
      (select attempt.id from public.followup_delivery_attempts attempt limit 1)
        as attempt_id,
      (select attempt.lease_generation
       from public.followup_delivery_attempts attempt limit 1)
        as lease_generation
    """


def handoff_sql(
    *,
    command_key: str,
    source_fenced: bool,
    reason_code: str = "commercial_exception",
    sleep: int = 0,
) -> str:
    requested_by = "agent" if source_fenced else "system"
    source = """
      ids.action_id, ids.attempt_id, 'handoff-race-worker', ids.lease_generation
    """ if source_fenced else "null::uuid, null::uuid, null::text, null::bigint"
    delay = f"select pg_sleep({sleep});" if sleep else ""
    return f"""
    begin;
    with ids as ({ids()})
    select handoff.*
    from ids
    cross join lateral public.request_human_handoff(
      ids.recovery_case_id, '{command_key}', '{reason_code}',
      '{requested_by}', 'handoff-race-projection', 1, {source}, clock_timestamp()
    ) handoff;
    {delay}
    commit;
    """


def request_start_sql() -> str:
    return f"""
    with ids as ({ids()})
    select started.*
    from ids
    cross join lateral public.mark_lancemos_pilot_request_started(
      ids.action_id, ids.attempt_id, 'handoff-race-worker',
      ids.lease_generation, clock_timestamp()
    ) started;
    """


def wait_until_transaction_is_sleeping(
    harness: PostgresHarness,
    database: str,
    process: subprocess.Popen[str],
    *,
    marker: str,
) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(f"race winner exited early: {stdout}\n{stderr}")
        result = harness.sql(
            database,
            f"""
            select count(*) from pg_stat_activity
            where datname = current_database()
              and pid <> pg_backend_pid()
              and query like '%{marker}%'
              and wait_event = 'PgSleep'
            """,
        )
        if result.stdout.strip() == "1":
            return
        time.sleep(0.05)
    raise RuntimeError("race winner never reached its locked sleep")


def verify_handoff_wins(harness: PostgresHarness) -> None:
    database = harness.create_database()
    try:
        harness.apply_schema_and_fixture(database)
        winner = subprocess.Popen(
            harness.psql_command(database)
            + ["-c", handoff_sql(
                command_key="race-handoff-wins",
                source_fenced=True,
                sleep=3,
            )],
            env=harness.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        wait_until_transaction_is_sleeping(
            harness,
            database,
            winner,
            marker="race-handoff-wins",
        )
        loser = harness.sql(database, request_start_sql(), check=False)
        stdout, stderr = winner.communicate(timeout=10)
        if winner.returncode != 0:
            raise RuntimeError(f"handoff winner failed: {stdout}\n{stderr}")
        rejection = loser.stdout + loser.stderr
        if loser.returncode == 0:
            raise RuntimeError(f"request-start was not rejected: {rejection}")
        state = harness.sql(
            database,
            """
            select case when
              attempt.phase = 'completed'
              and attempt.outcome = 'failed_before_request'
              and attempt.request_started_at is null
              and attempt.reason_code = 'human_handoff_requested'
              and action.status = 'cancelled'
              and action.terminal_reason = 'human_handoff_requested'
              and cases.status = 'paused'
              and sequence.status = 'paused'
              and conversation.status = 'paused_human'
              and conversation.automation_status = 'paused'
              and not exists (
                select 1 from public.pilot_outbound_request_authorizations auth
                where auth.attempt_id = attempt.id
              )
              then 'HANDOFF_COMMIT_BLOCKED_REQUEST_START_OK'
              else 'INVALID'
            end
            from public.followup_delivery_attempts attempt
            join public.scheduled_actions action on action.id = attempt.action_id
            join public.recovery_cases cases on cases.id = action.recovery_case_id
            join public.followup_sequences sequence
              on sequence.id = action.followup_sequence_id
            join public.conversations conversation
              on conversation.id = cases.conversation_id
            limit 1
            """,
        )
        if state.stdout.strip() != "HANDOFF_COMMIT_BLOCKED_REQUEST_START_OK":
            raise RuntimeError(f"unexpected handoff-wins state: {state.stdout}")
        print("HANDOFF_COMMIT_BLOCKED_REQUEST_START_OK")
    finally:
        harness.drop_database(database)


def verify_request_start_wins(harness: PostgresHarness) -> None:
    database = harness.create_database()
    try:
        harness.apply_schema_and_fixture(database)
        request_start_winner_sql = f"""
        begin;
        /* race-request-start-winner */
        {request_start_sql()}
        select pg_sleep(3);
        commit;
        """
        winner = subprocess.Popen(
            harness.psql_command(database) + ["-c", request_start_winner_sql],
            env=harness.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        wait_until_transaction_is_sleeping(
            harness,
            database,
            winner,
            marker="race-request-start-winner",
        )
        harness.sql(
            database,
            handoff_sql(
                command_key="race-request-start-wins",
                source_fenced=False,
            ),
        )
        stdout, stderr = winner.communicate(timeout=10)
        if winner.returncode != 0 or "request_started" not in stdout:
            raise RuntimeError(f"request-start winner failed: {stdout}\n{stderr}")
        unknown = harness.sql(
            database,
            """
            select case when
              attempt.phase = 'completed'
              and attempt.outcome = 'delivery_unknown'
              and attempt.request_started_at is not null
              and action.status = 'delivery_unknown'
              and cases.status = 'paused'
              then 'STARTED_REQUEST_PRESERVED_UNKNOWN_OK'
              else 'INVALID'
            end
            from public.followup_delivery_attempts attempt
            join public.scheduled_actions action on action.id = attempt.action_id
            join public.recovery_cases cases on cases.id = action.recovery_case_id
            limit 1
            """,
        )
        if unknown.stdout.strip() != "STARTED_REQUEST_PRESERVED_UNKNOWN_OK":
            raise RuntimeError(f"started request was not preserved: {unknown.stdout}")
        accepted = harness.sql(
            database,
            f"""
            with ids as ({ids()})
            select accepted.*
            from ids
            cross join lateral public.record_and_finalize_followup_acceptance(
              ids.action_id, ids.attempt_id, 'handoff-race-worker',
              ids.lease_generation, '9001', '9100', 'accepted race message',
              clock_timestamp() + interval '30 days'
            ) accepted
            """,
        )
        if "accepted_by_chatwoot" not in accepted.stdout:
            raise RuntimeError(f"late acceptance failed: {accepted.stdout}")
        final_state = harness.sql(
            database,
            """
            select case when
              attempt.outcome = 'accepted_by_chatwoot'
              and action.status = 'accepted_by_chatwoot'
              and cases.status = 'paused'
              and not exists (
                select 1 from public.scheduled_actions successor
                where successor.recovery_case_id = cases.id
                  and successor.id <> action.id
              )
              then 'LATE_ACCEPTANCE_NO_SUCCESSOR_OK'
              else 'INVALID'
            end
            from public.followup_delivery_attempts attempt
            join public.scheduled_actions action on action.id = attempt.action_id
            join public.recovery_cases cases on cases.id = action.recovery_case_id
            limit 1
            """,
        )
        if final_state.stdout.strip() != "LATE_ACCEPTANCE_NO_SUCCESSOR_OK":
            raise RuntimeError(f"late acceptance reopened workflow: {final_state.stdout}")
        print("STARTED_REQUEST_PRESERVED_UNKNOWN_OK")
        print("LATE_ACCEPTANCE_NO_SUCCESSOR_OK")
    finally:
        harness.drop_database(database)


def verify_concurrent_command_replay(harness: PostgresHarness) -> None:
    database = harness.create_database()
    try:
        harness.apply_schema_and_fixture(database)
        winner = subprocess.Popen(
            harness.psql_command(database) + ["-c", handoff_sql(
                command_key="concurrent-command-replay",
                source_fenced=True,
                sleep=3,
            )],
            env=harness.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        wait_until_transaction_is_sleeping(
            harness,
            database,
            winner,
            marker="concurrent-command-replay",
        )
        exact = harness.sql(
            database,
            handoff_sql(
                command_key="concurrent-command-replay",
                source_fenced=True,
            ),
            check=False,
        )
        stdout, stderr = winner.communicate(timeout=10)
        if winner.returncode != 0 or "requested" not in stdout:
            raise RuntimeError(f"concurrent replay winner failed: {stdout}\n{stderr}")
        if exact.returncode != 0 or "already_requested" not in exact.stdout:
            raise RuntimeError(
                f"exact concurrent replay was not idempotent: {exact.stdout}\n{exact.stderr}"
            )

        conflict = harness.sql(
            database,
            handoff_sql(
                command_key="concurrent-command-replay",
                source_fenced=True,
                reason_code="explicit_human_request",
            ),
            check=False,
        )
        rejection = conflict.stdout + conflict.stderr
        if conflict.returncode == 0 or "human_handoff_command_conflict" not in rejection:
            raise RuntimeError(f"conflicting replay was not rejected: {rejection}")
        print("HANDOFF_CONCURRENT_EXACT_REPLAY_OK")
        print("HANDOFF_CONCURRENT_CONFLICT_REJECTED_OK")
    finally:
        harness.drop_database(database)


def verify_effective_acls(harness: PostgresHarness) -> None:
    database = harness.create_database()
    try:
        harness.apply_schema_and_fixture(database)
        result = harness.sql(
            database,
            """
            with api_roles(role_name) as (
              values ('anon'), ('authenticated'), ('service_role')
            ), table_privileges(privilege_name) as (
              values ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE')
            ), handoff_tables as (
              select c.oid
              from pg_class c
              join pg_namespace n on n.oid = c.relnamespace
              where n.nspname = 'public'
                and c.relkind in ('r', 'p', 'v')
                and c.relname like 'human_handoff%'
            ), table_check as (
              select bool_and(not has_table_privilege(
                role_name, oid, privilege_name
              )) as safe
              from api_roles cross join table_privileges cross join handoff_tables
            ), handoff_functions as (
              select p.oid, p.proname
              from pg_proc p
              join pg_namespace n on n.oid = p.pronamespace
              where n.nspname = 'public'
                and (
                  p.proname like '%human_handoff%'
                  or p.proname like 'protect_human_handoff%'
                  or case when p.prokind = 'f'
                    then pg_get_functiondef(p.oid) like '%human_handoff%'
                    else false
                  end
                )
            ), function_check as (
              select bool_and(
                case
                  when role_name = 'service_role'
                   and proname in (
                     'request_inbound_human_handoff',
                     'request_human_handoff',
                     'claim_human_handoff_projection_effects',
                     'finalize_human_handoff_projection_effect',
                     'get_human_handoff_projection_status',

                   ) then has_function_privilege(role_name, oid, 'EXECUTE')
                  else not has_function_privilege(role_name, oid, 'EXECUTE')
                end
              ) as safe
              from api_roles cross join handoff_functions
            ), inventory_check as (
              select bool_or(
                proname = 'protect_handoff_projection_effect_identity'
              ) as omitted_helper_covered
              from handoff_functions
            )
            select case when table_check.safe
              and function_check.safe
              and inventory_check.omitted_helper_covered
              then 'HANDOFF_EFFECTIVE_ACL_EXHAUSTIVE_OK'
              else 'INVALID'
            end
            from table_check cross join function_check cross join inventory_check
            """,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"handoff ACL query failed: {result.stderr}")
        if result.stdout.strip() != "HANDOFF_EFFECTIVE_ACL_EXHAUSTIVE_OK":
            raise RuntimeError(f"unsafe effective handoff ACLs: {result.stdout}")
        print("HANDOFF_EFFECTIVE_ACL_EXHAUSTIVE_OK")
    finally:
        harness.drop_database(database)


def main() -> int:
    args = parse_args()
    harness = PostgresHarness(args)
    verify_effective_acls(harness)
    verify_concurrent_command_replay(harness)
    verify_handoff_wins(harness)
    verify_request_start_wins(harness)
    return 0


if __name__ == "__main__":
    sys.exit(main())
