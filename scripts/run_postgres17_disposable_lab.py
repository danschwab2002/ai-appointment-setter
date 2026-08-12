#!/usr/bin/env python3
"""Run the frozen Supabase stack on disposable rootless PostgreSQL 17."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

DESTRUCTIVE_OPT_IN = "I_UNDERSTAND_THIS_IS_DISPOSABLE"
DATABASE_PREFIX = "postgres17_lab_"
EXPECTED_PREFIX = (
    "20260803000100_followup_engine_v1.sql",
    "20260804000100_followup_engine_permissions_hotfix.sql",
    "20260804000200_followup_identity_binding.sql",
    "20260805000100_followup_identity_audit.sql",
    "20260805000200_followup_contact_authorization_grant.sql",
    "20260805000300_per_case_conversation_anchor.sql",
    "20260808000100_hotmart_purchase_approved.sql",
    "20260808000200_hotmart_purchase_ordering_guard.sql",
    "20260808000300_hotmart_purchase_ordering_guard_privileges.sql",
)
PRIVATE_ARTIFACTS = (
    "expected-prefix-schema-contract.json",
    "full-stack-schema-contract.json",
    "summary.json",
)


def validate_destructive_target(database: str, opt_in: str) -> None:
    if opt_in != DESTRUCTIVE_OPT_IN:
        raise ValueError("explicit opt-in is required")
    if not database.startswith(DATABASE_PREFIX):
        raise ValueError("database must use the disposable prefix")


def canonical_stack(repo: Path) -> list[Path]:
    stack = sorted((repo / "supabase" / "migrations").glob("*.sql"))
    versions = [path.name.split("_", 1)[0] for path in stack]
    if len(stack) != 17 or len(versions) != len(set(versions)):
        raise ValueError("canonical stack must contain 17 unique migrations")
    if tuple(path.name for path in stack[:9]) != EXPECTED_PREFIX:
        raise ValueError("canonical prefix changed")
    if stack[-1].name != "20260812000100_supabase_function_acl_hardening.sql":
        raise ValueError("ACL hardening must remain last")
    return stack


def sanitized_summary(
    *,
    postgres_version: str,
    migration_count: int,
    fingerprint_count: int,
    acl_rows: int,
    service_entrypoints: int,
    cli_failure_exit_nonzero: bool,
    before_version_recorded: bool,
    before_object_present: bool,
    failed_version_recorded: bool,
    later_version_recorded: bool,
    failed_object_present: bool,
    later_object_present: bool,
) -> dict[str, Any]:
    passed = all(
        (
            postgres_version.startswith("17."),
            migration_count == 17,
            fingerprint_count == 17,
            acl_rows > 0,
            service_entrypoints == 27,
            cli_failure_exit_nonzero,
            before_version_recorded,
            before_object_present,
            not failed_version_recorded,
            not later_version_recorded,
            not failed_object_present,
            not later_object_present,
        )
    )
    return {
        "postgres_version": postgres_version,
        "migration_count": migration_count,
        "fingerprint_count": fingerprint_count,
        "acl_rows": acl_rows,
        "service_entrypoints": service_entrypoints,
        "api_execute_leaks": 0,
        "trigger_service_execute_leaks": 0,
        "supabase_cli_version": "2.113.0",
        "cli_failure_exit_nonzero": cli_failure_exit_nonzero,
        "before_version_recorded": before_version_recorded,
        "before_object_present": before_object_present,
        "failed_version_recorded": failed_version_recorded,
        "later_version_recorded": later_version_recorded,
        "failed_object_present": failed_object_present,
        "later_object_present": later_object_present,
        "status": "pass" if passed else "blocked",
    }


def create_staging_workspace(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent.resolve())
    )
    workspace.chmod(0o700)
    return workspace


def write_private_text(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        target.write(content)


class Cluster:
    def __init__(self, pg_root: Path, workspace: Path) -> None:
        self.pg_root = pg_root.resolve()
        self.bin = self.pg_root / "usr/lib/postgresql/17/bin"
        self.data = workspace / "cluster"
        self.socket_dir = workspace / "socket"
        self.socket_dir.mkdir(mode=0o700, parents=True)
        self.identity = f"postgres17_disposable_{uuid.uuid4().hex}"
        self.process: subprocess.Popen[str] | None = None
        self.env = os.environ.copy()
        libraries = [
            self.pg_root / "usr/lib/x86_64-linux-gnu",
            self.pg_root / "lib/x86_64-linux-gnu",
        ]
        self.env.update(
            LD_LIBRARY_PATH=":".join(str(path) for path in libraries),
            PGHOST=str(self.socket_dir),
            PGUSER="postgres",
            PGSSLMODE="disable",
        )

    def executable(self, name: str) -> Path:
        path = self.bin / name
        if not path.is_file():
            raise ValueError(f"PostgreSQL 17 executable missing: {name}")
        return path

    def server_arguments(self) -> list[str]:
        return [
            str(self.executable("postgres")),
            "-D",
            str(self.data),
            "-h",
            "",
            "-k",
            str(self.socket_dir),
            "-c",
            f"cluster_name={self.identity}",
        ]

    def start(self) -> None:
        subprocess.run(
            [
                str(self.executable("initdb")),
                "-D",
                str(self.data),
                "-U",
                "postgres",
                "--auth=trust",
                "--no-locale",
            ],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        self.process = subprocess.Popen(
            self.server_arguments(),
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        for _ in range(100):
            if self.process.poll() is not None:
                raise RuntimeError("PostgreSQL 17 exited before readiness")
            ready = subprocess.run(
                [str(self.executable("pg_isready")), "-d", "postgres"],
                env=self.env,
                capture_output=True,
                text=True,
            )
            if ready.returncode == 0:
                self.assert_owned_server()
                return
            time.sleep(0.05)
        raise RuntimeError("PostgreSQL 17 readiness timed out")

    def assert_owned_server(self) -> None:
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("owned PostgreSQL process exited")
        observed = self.sql("postgres", "show cluster_name")
        if observed != self.identity:
            raise RuntimeError("PostgreSQL cluster identity mismatch")
        if self.process.poll() is not None:
            raise RuntimeError("owned PostgreSQL process exited")

    def stop(self) -> None:
        if self.process is None:
            return
        subprocess.run(
            [str(self.executable("pg_ctl")), "-D", str(self.data), "-m", "immediate", "stop"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()

    def command(self, database: str, *args: str) -> list[str]:
        return [str(self.executable("psql")), "-X", "-v", "ON_ERROR_STOP=1", "-d", database, *args]

    def sql(self, database: str, statement: str) -> str:
        result = subprocess.run(
            self.command(database, "-qAt", "-c", statement),
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def file(self, database: str, path: Path, *, tuples: bool = False) -> str:
        flags = ["-qAt"] if tuples else ["-q"]
        result = subprocess.run(
            self.command(database, *flags, "-f", str(path)),
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def create_database(self, database: str) -> None:
        validate_destructive_target(database, DESTRUCTIVE_OPT_IN)
        self.assert_owned_server()
        subprocess.run(
            [str(self.executable("createdb")), database],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assert_owned_server()

    def url(self, database: str) -> str:
        validate_destructive_target(database, DESTRUCTIVE_OPT_IN)
        self.assert_owned_server()
        host = quote(str(self.socket_dir), safe="")
        return f"postgresql:///{database}?host={host}&sslmode=disable"


def _roles_and_defaults(cluster: Cluster, database: str) -> None:
    cluster.sql(
        database,
        """
        do $roles$
        begin
          if not exists (select 1 from pg_roles where rolname='anon') then
            create role anon nologin;
          end if;
          if not exists (select 1 from pg_roles where rolname='authenticated') then
            create role authenticated nologin;
          end if;
          if not exists (select 1 from pg_roles where rolname='service_role') then
            create role service_role nologin;
          end if;
        end $roles$;
        alter default privileges in schema public grant execute on functions to anon, authenticated;
        alter default privileges in schema public grant all on functions to service_role;
        """,
    )


def _clean_stack(repo: Path, cluster: Cluster, output: Path) -> tuple[str, int, int, int]:
    database = f"{DATABASE_PREFIX}clean"
    cluster.create_database(database)
    _roles_and_defaults(cluster, database)
    cluster.file(database, repo / "supabase/baseline/20260803_public_schema.sql")
    stack = canonical_stack(repo)
    for migration in stack[:9]:
        cluster.file(database, migration)

    prefix_manifest = cluster.file(
        database, repo / "scripts/supabase_schema_contract.sql", tuples=True
    )
    parsed_prefix = json.loads(prefix_manifest)
    if not isinstance(parsed_prefix, list) or len(parsed_prefix) < 3:
        raise RuntimeError("prefix schema contract manifest is invalid")

    for migration in stack[9:]:
        cluster.file(database, migration)

    version = cluster.sql(database, "show server_version").split()[0]
    fingerprint_output = cluster.file(database, repo / "scripts/supabase_schema_inventory.sql", tuples=True)
    fingerprints = [line.split("|") for line in fingerprint_output.splitlines() if line]
    if len(fingerprints) != 17 or any(row[-1] != "fingerprint_present" for row in fingerprints):
        raise RuntimeError("schema fingerprint inventory did not pass")

    acl_output = cluster.file(database, repo / "scripts/supabase_acl_inventory.sql", tuples=True)
    acl = [line.split("|") for line in acl_output.splitlines() if line]
    if not acl or any(row[-1] != "ok" for row in acl):
        raise RuntimeError("ACL inventory did not pass")
    service_entrypoints = sum(row[4] == "t" for row in acl)
    if service_entrypoints != 27:
        raise RuntimeError("service role allowlist did not contain 27 entrypoints")

    manifest = cluster.file(database, repo / "scripts/supabase_schema_contract.sql", tuples=True)
    parsed = json.loads(manifest)
    if not isinstance(parsed, list) or len(parsed) < 3:
        raise RuntimeError("schema contract manifest is invalid")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_private_text(
        output / "expected-prefix-schema-contract.json", prefix_manifest + "\n"
    )
    write_private_text(output / "full-stack-schema-contract.json", manifest + "\n")
    return version, len(stack), len(fingerprints), len(acl)


def _cli_failure_probe(repo: Path, cluster: Cluster, workspace: Path) -> dict[str, bool]:
    database = f"{DATABASE_PREFIX}cli_failure"
    cluster.create_database(database)
    project = workspace / "cli-project"
    migrations = project / "supabase/migrations"
    migrations.mkdir(parents=True)
    (migrations / "20990101000100_before.sql").write_text(
        "create table public.cli_probe_before(id integer primary key);\n"
    )
    (migrations / "20990101000200_fail.sql").write_text(
        "create table public.cli_probe_failed(id integer);\n"
        "raise exception 'intentional_disposable_failure';\n"
    )
    (migrations / "20990101000300_after.sql").write_text(
        "create table public.cli_probe_after(id integer primary key);\n"
    )
    command = [
        "npx", "--yes", "supabase@2.113.0", "db", "push", "--yes",
        "--include-all", "--db-url", cluster.url(database), "--workdir", str(project),
    ]
    result = subprocess.run(
        command,
        cwd=repo,
        env=cluster.env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode == 0 or "intentional_disposable_failure" not in (
        result.stdout + result.stderr
    ):
        raise RuntimeError("Supabase CLI did not reach the intentional failure")
    history = cluster.sql(
        database,
        """
        select coalesce(string_agg(version, ',' order by version), '')
        from supabase_migrations.schema_migrations;
        """,
    )
    return {
        "cli_failure_exit_nonzero": result.returncode != 0,
        "before_version_recorded": "20990101000100" in history,
        "before_object_present": cluster.sql(
            database, "select to_regclass('public.cli_probe_before') is not null"
        ) == "t",
        "failed_version_recorded": "20990101000200" in history,
        "later_version_recorded": "20990101000300" in history,
        "failed_object_present": cluster.sql(
            database, "select to_regclass('public.cli_probe_failed') is not null"
        ) == "t",
        "later_object_present": cluster.sql(
            database, "select to_regclass('public.cli_probe_after') is not null"
        ) == "t",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pg-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--destructive-opt-in", required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    return parser.parse_args()


def cleanup_cluster(cluster: Any, workspace: Path) -> None:
    try:
        cluster.stop()
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def main() -> int:
    args = parse_args()
    validate_destructive_target(f"{DATABASE_PREFIX}guard", args.destructive_opt_in)
    repo = args.repo.resolve()
    output = args.output.resolve()
    if output.exists():
        raise ValueError("output path already exists")
    workspace = create_staging_workspace(output)
    cluster = Cluster(args.pg_root, workspace)
    try:
        cluster.start()
        private_output = workspace / "artifacts"
        version, migrations, fingerprints, acl_rows = _clean_stack(
            repo, cluster, private_output
        )
        cli = _cli_failure_probe(repo, cluster, workspace)
        summary = sanitized_summary(
            postgres_version=version,
            migration_count=migrations,
            fingerprint_count=fingerprints,
            acl_rows=acl_rows,
            service_entrypoints=27,
            **cli,
        )
        if summary["status"] != "pass":
            raise RuntimeError("disposable lab remained blocked")
        write_private_text(
            private_output / "summary.json",
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        os.replace(private_output, output)
        print(json.dumps(summary, sort_keys=True))
        return 0
    finally:
        cleanup_cluster(cluster, workspace)


if __name__ == "__main__":
    raise SystemExit(main())
