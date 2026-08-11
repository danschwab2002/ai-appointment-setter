#!/usr/bin/env python3
"""Compare two sanitized Supabase schema-contract manifests exactly."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


ManifestIndex = dict[str, dict[str, Any]]

ROW_FIELDS = {"object_type", "identity", "contract"}
REQUIRED_SENTINELS = {
    "manifest_metadata:supabase_schema_contract/v1",
    "server:postgresql",
    "schema:public",
}
CONTRACT_FIELDS = {
    "manifest_metadata": {"format_version", "query_kind", "scope"},
    "server": {"major_version"},
    "schema": {"owner", "acl", "role_privileges"},
    "relation": {
        "kind", "persistence", "owner", "row_security", "force_row_security",
        "replica_identity", "partition_key", "partition_bound", "parents",
        "options", "acl", "role_privileges", "access_method", "tablespace",
        "populated",
    },
    "column": {
        "position", "type", "not_null", "identity", "generated", "default",
        "collation", "compression", "storage", "acl", "role_privileges",
    },
    "composite_attribute": {
        "position", "type", "not_null", "collation", "storage",
    },
    "constraint": {
        "type", "definition", "deferrable", "initially_deferred", "validated",
        "no_inherit",
    },
    "domain_constraint": {
        "definition", "validated", "deferrable", "initially_deferred",
    },
    "index": {
        "definition", "unique", "primary", "exclusion", "immediate", "valid",
        "ready", "live", "replica_identity", "clustered", "access_method",
        "tablespace",
    },
    "trigger": {"enabled", "definition", "function"},
    "function": {
        "owner", "language", "kind", "arguments", "identity_arguments", "result",
        "security_definer", "leakproof", "strict", "volatility", "parallel",
        "estimated_cost", "estimated_rows", "config", "source", "binary",
        "definition", "sql_body", "support", "role_execute", "public_execute",
        "acl",
    },
    "policy": {"command", "permissive", "roles", "using", "with_check"},
    "sequence": {
        "owner", "persistence", "data_type", "start", "increment", "minimum",
        "maximum", "cache", "cycle", "owned_by", "acl", "role_privileges",
    },
    "view_definition": {"definition"},
    "type": {
        "kind", "category", "owner", "not_null", "default", "base_type",
        "enum_labels", "acl", "role_privileges", "range_subtype",
        "range_multirange", "range_opclass", "range_collation",
        "range_canonical", "range_subdiff",
    },
    "extension": {"version", "schema"},
    "foreign_table": {"server", "table_options", "server_type", "server_version", "fdw"},
    "default_acl": {"owner", "schema", "object_kind", "acl"},
}


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def index_manifest(rows: object) -> ManifestIndex:
    if not isinstance(rows, list):
        raise ValueError("manifest must be a JSON array")
    if not rows:
        raise ValueError("manifest must not be empty")

    indexed: ManifestIndex = {}
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"row {position} must be an object")
        if set(row) != ROW_FIELDS:
            raise ValueError(f"row {position} has unexpected members")
        object_type = row.get("object_type")
        identity = row.get("identity")
        contract = row.get("contract")
        if not isinstance(object_type, str) or not object_type:
            raise ValueError(f"row {position} object_type must be a non-empty string")
        if not isinstance(identity, str) or not identity:
            raise ValueError(f"row {position} identity must be a non-empty string")
        if not isinstance(contract, dict):
            raise ValueError(f"row {position} contract must be an object")
        allowed_fields = CONTRACT_FIELDS.get(object_type)
        if allowed_fields is None:
            raise ValueError(f"row {position} has an unsupported object type")
        if set(contract) != allowed_fields:
            raise ValueError(f"row {position} contract does not match its closed schema")
        key = f"{object_type}:{identity}"
        if key in indexed:
            raise ValueError(f"duplicate object key at row {position}")
        indexed[key] = contract
    if not REQUIRED_SENTINELS.issubset(indexed):
        raise ValueError("required manifest sentinel is missing")
    return indexed


def _different_paths(expected: object, observed: object, path: str = "$") -> list[str]:
    if not isinstance(expected, dict) or not isinstance(observed, dict):
        return [path] if expected != observed else []
    return [
        f"{path}.{key}"
        for key in sorted(set(expected) | set(observed))
        if expected.get(key) != observed.get(key) or (key in expected) != (key in observed)
    ]


def _key_summary(key: str) -> dict[str, str]:
    object_type, _, _identity = key.partition(":")
    return {"object_type": object_type, "key_sha256": _sha256(key)}


def compare_manifests(expected: ManifestIndex, observed: ManifestIndex) -> dict[str, Any]:
    expected_keys = set(expected)
    observed_keys = set(observed)
    changed = []
    for key in sorted(expected_keys & observed_keys):
        if expected[key] == observed[key]:
            continue
        changed.append(
            {
                **_key_summary(key),
                "paths": _different_paths(expected[key], observed[key]),
                "expected_sha256": _sha256(expected[key]),
                "observed_sha256": _sha256(observed[key]),
            }
        )

    missing = [_key_summary(key) for key in sorted(expected_keys - observed_keys)]
    unexpected = [_key_summary(key) for key in sorted(observed_keys - expected_keys)]
    return {
        "status": "exact_match" if not missing and not unexpected and not changed else "different",
        "expected_count": len(expected),
        "observed_count": len(observed),
        "missing": missing,
        "unexpected": unexpected,
        "changed": changed,
    }


def _load(path: Path) -> ManifestIndex:
    with path.open(encoding="utf-8") as handle:
        return index_manifest(
            json.load(
                handle,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
        )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare expected and observed Supabase schema contracts exactly."
    )
    parser.add_argument("expected", type=Path)
    parser.add_argument("observed", type=Path)
    args = parser.parse_args(argv)

    try:
        result = compare_manifests(_load(args.expected), _load(args.observed))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(
            json.dumps({"comparison_error": str(error)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "exact_match" else 1


if __name__ == "__main__":
    raise SystemExit(main())
