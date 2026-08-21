from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prepare_supabase_release_bundle.py"
SPEC = importlib.util.spec_from_file_location("release_bundle", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_bundle_covers_exact_pending_tail_and_is_deterministic(tmp_path: Path) -> None:
    first = MODULE.build(ROOT, tmp_path / "first", allow_dirty=True)
    second = MODULE.build(ROOT, tmp_path / "second", allow_dirty=True)

    expected = [
        path.name
        for path in sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
        if path.name.split("_", 1)[0] > MODULE.PREFIX_LAST
    ]
    observed = [row["filename"] for row in first["pending_tail"]]
    assert observed == expected
    assert observed[0] == "20260808000400_hotmart_purchase_safety_fences.sql"
    assert "20260812000100_supabase_function_acl_hardening.sql" in observed
    assert observed[-1] == "20260821000200_lead_whatsapp_consent_authorization.sql"
    assert first["bundle"]["sha256"] == second["bundle"]["sha256"]
    assert first["postflight"]["sha256"] == second["postflight"]["sha256"]
    assert first["production_authorized"] is False


def test_bundle_hashes_match_written_artifacts(tmp_path: Path) -> None:
    output = tmp_path / "bundle"
    manifest = MODULE.build(ROOT, output, allow_dirty=True)
    persisted = json.loads((output / "manifest.json").read_text(encoding="utf-8"))

    assert persisted == manifest
    assert MODULE.sha256(output / "pending-tail.sql") == manifest["bundle"]["sha256"]
    assert MODULE.sha256(output / "postflight.sql") == manifest["postflight"]["sha256"]


def test_bundle_rejects_preexisting_output_without_modifying_it(tmp_path: Path) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "manifest.json"
    sentinel.write_text("old-manifest\n")

    with pytest.raises(ValueError, match="output path already exists"):
        MODULE.build(ROOT, output, allow_dirty=True)

    assert sentinel.read_text() == "old-manifest\n"
    assert sorted(path.name for path in output.iterdir()) == ["manifest.json"]


def test_late_failure_leaves_no_output_or_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "bundle"
    real_sha256 = MODULE.sha256

    def fail_on_postflight(path: Path) -> str:
        if path.name == "postflight.sql":
            raise OSError("injected late hash failure")
        return real_sha256(path)

    monkeypatch.setattr(MODULE, "sha256", fail_on_postflight)
    with pytest.raises(OSError, match="injected late hash failure"):
        MODULE.build(ROOT, output, allow_dirty=True)

    assert not output.exists()
    assert list(tmp_path.glob(".bundle.*")) == []


def test_bundle_rejects_duplicate_versions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    migrations = repo / "supabase" / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "20260808000300_one.sql").write_text("select 1;\n")
    (migrations / "20260808000300_two.sql").write_text("select 2;\n")

    with pytest.raises(ValueError, match="duplicate migration version"):
        MODULE.migration_records(repo)


def test_cli_blocks_dirty_source_without_override(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    migrations = repo / "supabase" / "migrations"
    scripts = repo / "scripts"
    migrations.mkdir(parents=True)
    scripts.mkdir()
    (migrations / "20260808000300_prefix.sql").write_text("select 1;\n")
    (migrations / "20260808000400_tail.sql").write_text("select 2;\n")
    for relative in MODULE.REQUIRED_POSTFLIGHT:
        path = repo / relative
        path.write_text("select 1;\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git", "-c", "user.name=Test", "-c", "user.email=test@example.test",
            "commit", "-qm", "fixture",
        ],
        cwd=repo,
        check=True,
    )
    (repo / "dirty.txt").write_text("dirty\n")

    with pytest.raises(ValueError, match="repository is dirty"):
        MODULE.build(repo, tmp_path / "blocked")
