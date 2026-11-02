"""Las solicitudes de plantilla de Meta en la base (20261102000200, E-268, F-146 partes 1 y 2), leidas como texto.

Lo que hacen lo prueba ``tests/sql/followup_engine/validate_meta_template_requests.mjs``
con las RPC reales sobre la cadena entera; aca, la forma del archivo.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261102000200_meta_template_requests_v1.sql"
PACKAGE = ROOT / "tests" / "sql" / "followup_engine" / "package.json"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_it_is_one_transaction_with_lock_timeout_the_last_and_does_not_run_twice() -> None:
    sql = _sql()
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    # El barrido del seguimiento a 24 h nativo (20261102000300, E-269).
    # El reciclado de un telefono de prueba (20261102000400, E-270, F-147 PR 1).
    # Los rechazos de Meta y el unico reintento del 131049 (20261103000500, E-284).
    # El historial de la pagina de aprobacion (20261103000600, E-295).
    # Si la persona ya es cliente (20261103000700, E-297).
    assert names[-1] == "20261103000700_customer_status_v1.sql"
    assert names[-2] == "20261103000600_flow_approval_history_v1.sql"
    assert names[-3] == "20261103000500_meta_delivery_rejections_v1.sql"
    assert names[-4] == "20261102000400_identity_recycle_v1.sql"
    assert names[-5] == "20261102000300_flow_engine_native_silence_sweep_v1.sql"
    assert names[-6] == MIGRATION.name
    assert sql.count("\nbegin;\n") == 1 and sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s';" in sql
    assert "message = 'meta_template_requests_already_applied'" in sql
    assert "message = 'meta_template_requests_prerequisite_missing'" in sql


def test_it_only_adds_never_replaces_or_drops() -> None:
    sql = _sql().lower()
    assert "create or replace" not in sql
    assert "drop " not in sql
    assert "alter role" not in sql and "create role" not in sql
    assert "alter default privileges" not in sql
    created = re.findall(r"^create function public\.([a-z0-9_]+)\(", _sql(), re.M)
    assert len(created) == 19 and len(set(created)) == 19
    assert re.findall(r"^create table public\.([a-z_]+) \(", _sql(), re.M) == [
        "meta_template_requests", "meta_template_request_events"]


def test_each_role_gets_only_its_functions() -> None:
    sql = _sql()
    acl = sql[sql.index("do $acl$"):]
    agent = acl[acl.index("to_regrole('automation_expert_change')"):acl.index("to_regrole('flow_change_approver')")]
    page = acl[acl.index("to_regrole('flow_change_approver')"):acl.index("to_regrole('service_role')")]
    service = acl[acl.index("to_regrole('service_role')"):]
    assert re.findall(r"'public\.([a-z_0-9]+)\(", agent) == [
        "save_meta_template_request_draft_v1", "record_meta_template_request_validation_v1",
        "get_meta_template_request_v1", "list_meta_template_requests_v1"]
    assert re.findall(r"'public\.([a-z_0-9]+)\(", page) == [
        "approver_get_meta_template_request_v1", "approver_approve_meta_template_request_v1",
        "approver_discard_meta_template_request_v1", "approver_begin_meta_template_send_v1",
        "approver_record_meta_template_sent_v1"]
    assert re.findall(r"function public\.([a-z_0-9]+)\(", service) == ["record_meta_template_status_v1"]
    # Nada para anon ni authenticated, y las tablas para nadie de la API.
    assert "to anon" not in sql and "to authenticated" not in sql
    assert "grant select" not in sql.lower() and "grant all" not in sql.lower()


def test_nothing_in_it_calls_meta_or_reads_a_token() -> None:
    sql = _sql().lower()
    for word in ("graph.facebook.com", "http", "access_token", "provider_config", "net."):
        assert word not in sql.replace("https://", ""), word


def test_the_validator_is_the_last_of_the_npm_chain() -> None:
    chain = json.loads(PACKAGE.read_text(encoding="utf-8"))["scripts"]["test"]
    assert chain.rstrip().endswith(" && node validate_meta_template_requests.mjs --check && node validate_flow_engine_native_silence_sweep.mjs && node validate_identity_recycle.mjs && node validate_meta_delivery_rejections.mjs && node validate_flow_approval_history.mjs && node validate_customer_status.mjs")
