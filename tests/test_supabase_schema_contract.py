from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "compare_supabase_schema_contract.py"
CONTRACT_SQL = ROOT / "scripts" / "supabase_schema_contract.sql"
SPEC = importlib.util.spec_from_file_location("schema_contract_comparator", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def row(object_type: str, identity: str, **contract: object) -> dict[str, object]:
    complete_contract: dict[str, object] = {
        field: None for field in MODULE.CONTRACT_FIELDS.get(object_type, set())
    }
    complete_contract.update(contract)
    return {
        "object_type": object_type,
        "identity": identity,
        "contract": complete_contract,
    }


def required_rows() -> list[dict[str, object]]:
    return [
        row(
            "manifest_metadata",
            "supabase_schema_contract/v1",
            format_version=1,
            query_kind="metadata_only",
            scope="public",
        ),
        row("server", "postgresql", major_version=17),
        row("schema", "public", owner="postgres", acl=[], role_privileges={}),
    ]


def write_manifest(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(json.dumps(rows), encoding="utf-8")


def without_comments_and_literals(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", "", sql)
    return re.sub(r"'(?:''|[^'])*'", "''", sql)


def test_contract_sql_is_single_statement_and_read_only() -> None:
    executable = without_comments_and_literals(
        CONTRACT_SQL.read_text(encoding="utf-8")
    )
    forbidden = {
        "alter",
        "call",
        "comment",
        "copy",
        "create",
        "delete",
        "do",
        "drop",
        "grant",
        "insert",
        "merge",
        "reindex",
        "revoke",
        "truncate",
        "update",
    }
    observed = {token.lower() for token in re.findall(r"\b[A-Za-z_]+\b", executable)}

    assert forbidden.isdisjoint(observed)
    assert executable.strip().lower().startswith("with")
    assert executable.count(";") == 1
    for catalog in (
        "pg_attribute",
        "pg_constraint",
        "pg_index",
        "pg_trigger",
        "pg_proc",
        "pg_policy",
        "pg_extension",
        "pg_depend",
        "pg_default_acl",
        "pg_range",
        "attacl",
        "prosqlbody",
    ):
        assert catalog in executable


def test_compare_is_order_independent_and_exact() -> None:
    left = MODULE.index_manifest(
        required_rows()
        + [
            row("relation", "public.a", row_security=True),
            row("function", "public.f()", source="x"),
        ]
    )
    right = MODULE.index_manifest(
        list(reversed(required_rows()))
        + [
            row("function", "public.f()", source="x"),
            row("relation", "public.a", row_security=True),
        ]
    )

    result = MODULE.compare_manifests(left, right)

    assert result == {
        "status": "exact_match",
        "expected_count": 5,
        "observed_count": 5,
        "missing": [],
        "unexpected": [],
        "changed": [],
    }


def test_compare_reports_keys_paths_and_hashes_without_values() -> None:
    expected = MODULE.index_manifest(
        required_rows()
        + [
            row("relation", "public.expected_only", row_security=True),
            row(
                "function",
                "public.changed()",
                source="canonical-secret-like-text",
                acl={"anon": False},
            ),
        ]
    )
    observed = MODULE.index_manifest(
        required_rows()
        + [
            row("relation", "public.unexpected_only", row_security=True),
            row(
                "function",
                "public.changed()",
                source="remote-secret-like-text",
                acl={"anon": True},
            ),
        ]
    )

    result = MODULE.compare_manifests(expected, observed)
    encoded = json.dumps(result)

    assert result["status"] == "different"
    assert result["missing"][0]["object_type"] == "relation"
    assert len(result["missing"][0]["key_sha256"]) == 64
    assert result["unexpected"][0]["object_type"] == "relation"
    assert len(result["unexpected"][0]["key_sha256"]) == 64
    assert result["changed"][0]["object_type"] == "function"
    assert len(result["changed"][0]["key_sha256"]) == 64
    assert result["changed"][0]["paths"] == ["$.acl", "$.source"]
    assert len(result["changed"][0]["expected_sha256"]) == 64
    assert len(result["changed"][0]["observed_sha256"]) == 64
    assert "canonical-secret-like-text" not in encoded
    assert "remote-secret-like-text" not in encoded
    assert "public.expected_only" not in encoded
    assert "public.unexpected_only" not in encoded
    assert "public.changed" not in encoded


def test_index_manifest_rejects_duplicate_or_invalid_rows() -> None:
    duplicate = required_rows() + [
        row("relation", "public.a"),
        row("relation", "public.a"),
    ]

    with pytest.raises(ValueError, match="duplicate object key"):
        MODULE.index_manifest(duplicate)
    with pytest.raises(ValueError, match="must be a JSON array"):
        MODULE.index_manifest({"rows": []})
    with pytest.raises(ValueError, match="contract must be an object"):
        MODULE.index_manifest(
            required_rows()
            + [{"object_type": "relation", "identity": "public.a", "contract": []}]
        )


def test_manifest_validation_rejects_empty_missing_sentinels_and_extra_members() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        MODULE.index_manifest([])
    with pytest.raises(ValueError, match="required manifest sentinel"):
        MODULE.index_manifest([row("relation", "public.a")])
    with pytest.raises(ValueError, match="unexpected members"):
        MODULE.index_manifest(
            required_rows()
            + [
                {
                    "object_type": "relation",
                    "identity": "public.a",
                    "contract": {},
                    "ignored": "sensitive-value",
                }
            ]
        )
    incomplete = required_rows()
    incomplete_contract = incomplete[1]["contract"]
    assert isinstance(incomplete_contract, dict)
    del incomplete_contract["major_version"]
    with pytest.raises(ValueError, match="closed schema"):
        MODULE.index_manifest(incomplete)


def test_cli_rejects_duplicate_json_members_nan_and_unknown_contract_fields(
    tmp_path: Path,
) -> None:
    valid = tmp_path / "valid.json"
    write_manifest(valid, required_rows())
    invalid_payloads = [
        '[{"object_type":"manifest_metadata","identity":"supabase_schema_contract/v1","contract":{"format_version":1,"query_kind":"metadata_only","scope":"public","scope":"public"}}]',
        '[{"object_type":"server","identity":"postgresql","contract":{"major_version":NaN}}]',
        json.dumps(
            required_rows()
            + [row("relation", "sensitive-identity", **{"secret-field-name": True})]
        ),
    ]
    for position, payload in enumerate(invalid_payloads):
        invalid = tmp_path / f"invalid-{position}.json"
        invalid.write_text(payload, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(valid), str(invalid)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 2
        assert result.stdout == ""
        assert "sensitive" not in result.stderr
        assert "secret-field-name" not in result.stderr


def test_cli_exit_codes_are_fail_closed(tmp_path: Path) -> None:
    expected = tmp_path / "expected.json"
    observed = tmp_path / "observed.json"
    write_manifest(expected, required_rows() + [row("relation", "public.a", row_security=True)])
    write_manifest(observed, required_rows() + [row("relation", "public.a", row_security=False)])

    different = subprocess.run(
        [sys.executable, str(SCRIPT), str(expected), str(observed)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert different.returncode == 1
    assert json.loads(different.stdout)["status"] == "different"

    write_manifest(observed, required_rows() + [row("relation", "public.a", row_security=True)])
    exact = subprocess.run(
        [sys.executable, str(SCRIPT), str(expected), str(observed)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert exact.returncode == 0
    assert json.loads(exact.stdout)["status"] == "exact_match"

    observed.write_text("not-json", encoding="utf-8")
    invalid = subprocess.run(
        [sys.executable, str(SCRIPT), str(expected), str(observed)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert invalid.returncode == 2
    assert invalid.stdout == ""
    assert "comparison_error" in invalid.stderr


def test_runbook_prefix_files_exist() -> None:
    runbook = (
        ROOT / "docs" / "operations" / "lancemos-supabase-schema-contract-runbook.md"
    ).read_text(encoding="utf-8")
    paths = re.findall(r"^  (supabase/(?:baseline|migrations)/[^ ]+\.sql)$", runbook, re.M)

    assert len(paths) == 10
    assert all((ROOT / path).is_file() for path in paths)
