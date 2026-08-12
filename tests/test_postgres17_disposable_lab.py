from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_postgres17_disposable_lab.py"


def load_module():
    spec = importlib.util.spec_from_file_location("postgres17_lab", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_disposable_database_guard_requires_opt_in_and_prefix() -> None:
    module = load_module()

    with pytest.raises(ValueError, match="explicit opt-in"):
        module.validate_destructive_target("postgres17_lab_test", "")
    with pytest.raises(ValueError, match="disposable prefix"):
        module.validate_destructive_target("postgres", module.DESTRUCTIVE_OPT_IN)

    module.validate_destructive_target(
        "postgres17_lab_test", module.DESTRUCTIVE_OPT_IN
    )


def test_canonical_stack_has_exact_ordered_prefix_and_tail() -> None:
    module = load_module()
    stack = module.canonical_stack(ROOT)

    assert len(stack) == 17
    assert [path.name for path in stack[:9]] == [
        "20260803000100_followup_engine_v1.sql",
        "20260804000100_followup_engine_permissions_hotfix.sql",
        "20260804000200_followup_identity_binding.sql",
        "20260805000100_followup_identity_audit.sql",
        "20260805000200_followup_contact_authorization_grant.sql",
        "20260805000300_per_case_conversation_anchor.sql",
        "20260808000100_hotmart_purchase_approved.sql",
        "20260808000200_hotmart_purchase_ordering_guard.sql",
        "20260808000300_hotmart_purchase_ordering_guard_privileges.sql",
    ]
    assert stack[-1].name == "20260812000100_supabase_function_acl_hardening.sql"


def test_sanitized_summary_rejects_secret_bearing_fields() -> None:
    module = load_module()

    summary = module.sanitized_summary(
        postgres_version="17.10",
        migration_count=17,
        fingerprint_count=17,
        acl_rows=65,
        service_entrypoints=27,
        cli_failure_exit_nonzero=True,
        before_version_recorded=True,
        before_object_present=True,
        failed_version_recorded=False,
        later_version_recorded=False,
        failed_object_present=False,
        later_object_present=False,
    )

    assert summary == {
        "postgres_version": "17.10",
        "migration_count": 17,
        "fingerprint_count": 17,
        "acl_rows": 65,
        "service_entrypoints": 27,
        "api_execute_leaks": 0,
        "trigger_service_execute_leaks": 0,
        "supabase_cli_version": "2.113.0",
        "cli_failure_exit_nonzero": True,
        "before_version_recorded": True,
        "before_object_present": True,
        "failed_version_recorded": False,
        "later_version_recorded": False,
        "failed_object_present": False,
        "later_object_present": False,
        "status": "pass",
    }
    assert not any(
        token in str(summary).lower()
        for token in ("password", "host", "port", "database_url", "project")
    )


def test_rootless_server_uses_disposable_socket_directory(tmp_path: Path) -> None:
    module = load_module()
    pg_root = tmp_path / "pg-root"
    postgres = pg_root / "usr/lib/postgresql/17/bin/postgres"
    postgres.parent.mkdir(parents=True)
    postgres.touch()
    cluster = module.Cluster(pg_root, tmp_path / "workspace")

    arguments = cluster.server_arguments()

    assert arguments[arguments.index("-k") + 1] == str(cluster.socket_dir)
    assert "/var/run/postgresql" not in arguments
    assert arguments[arguments.index("-h") + 1] == ""


def test_staging_is_created_next_to_atomic_output(tmp_path: Path) -> None:
    module = load_module()
    output = tmp_path / "evidence"

    staging = module.create_staging_workspace(output)
    try:
        assert staging.parent == output.parent
        assert staging.stat().st_mode & 0o777 == 0o700
    finally:
        staging.rmdir()


def test_private_artifact_contract_includes_prefix_and_full_manifests() -> None:
    module = load_module()

    assert module.PRIVATE_ARTIFACTS == (
        "expected-prefix-schema-contract.json",
        "full-stack-schema-contract.json",
        "summary.json",
    )


def test_private_artifact_is_owner_only_under_permissive_umask(tmp_path: Path) -> None:
    module = load_module()
    artifact = tmp_path / "manifest.json"
    previous = os.umask(0)
    try:
        module.write_private_text(artifact, "private\n")
    finally:
        os.umask(previous)

    assert artifact.stat().st_mode & 0o777 == 0o600


def test_server_identity_rejects_exited_process_even_when_probe_matches(
    tmp_path: Path,
) -> None:
    module = load_module()
    cluster = module.Cluster(tmp_path / "pg-root", tmp_path / "workspace")

    class ExitedProcess:
        def poll(self) -> int:
            return 1

    cluster.process = ExitedProcess()
    cluster.sql = lambda database, statement: cluster.identity

    with pytest.raises(RuntimeError, match="owned PostgreSQL process exited"):
        cluster.assert_owned_server()


def test_server_identity_rejects_foreign_cluster(tmp_path: Path) -> None:
    module = load_module()
    cluster = module.Cluster(tmp_path / "pg-root", tmp_path / "workspace")

    class RunningProcess:
        def poll(self):
            return None

    cluster.process = RunningProcess()
    cluster.sql = lambda database, statement: "foreign-cluster"

    with pytest.raises(RuntimeError, match="identity mismatch"):
        cluster.assert_owned_server()


def test_cli_url_uses_owned_unix_socket_not_tcp(tmp_path: Path) -> None:
    module = load_module()
    cluster = module.Cluster(tmp_path / "pg-root", tmp_path / "workspace")

    class RunningProcess:
        def poll(self):
            return None

    cluster.process = RunningProcess()
    cluster.sql = lambda database, statement: cluster.identity

    parsed = urlsplit(cluster.url("postgres17_lab_probe"))

    assert parsed.hostname is None
    assert parsed.port is None
    assert parse_qs(parsed.query)["host"] == [str(cluster.socket_dir)]


def test_private_output_tree_is_owner_only(tmp_path: Path) -> None:
    module = load_module()
    output = tmp_path / "artifacts"
    output.mkdir(mode=0o700)

    for name in module.PRIVATE_ARTIFACTS:
        module.write_private_text(output / name, "private\n")

    assert output.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.iterdir())


def test_cleanup_workspace_survives_stop_failure(tmp_path: Path) -> None:
    module = load_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class BrokenCluster:
        def stop(self) -> None:
            raise RuntimeError("stop failed")

    with pytest.raises(RuntimeError, match="stop failed"):
        module.cleanup_cluster(BrokenCluster(), workspace)

    assert not workspace.exists()
