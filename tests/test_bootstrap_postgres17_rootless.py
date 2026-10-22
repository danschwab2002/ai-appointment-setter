from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/bootstrap_postgres17_rootless.py"


def load_module():
    spec = importlib.util.spec_from_file_location("postgres17_bootstrap", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_required_packages_are_explicit_and_complete() -> None:
    module = load_module()

    assert module.PACKAGES == (
        "postgresql-17",
        "postgresql-client-17",
        "postgresql-common",
        "postgresql-client-common",
        "libpq5",
        "libicu76",
    )


def test_verify_prefix_rejects_missing_binary(tmp_path: Path) -> None:
    module = load_module()

    with pytest.raises(ValueError, match="missing PostgreSQL binary"):
        module.verify_prefix(tmp_path)


def test_major_parser_rejects_non_17() -> None:
    module = load_module()

    with pytest.raises(ValueError, match="major 17"):
        module.verify_version_output("postgres (PostgreSQL) 16.9")
    assert module.verify_version_output("postgres (PostgreSQL) 17.10 (Debian)") == "17.10"
