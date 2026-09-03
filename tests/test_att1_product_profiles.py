from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from bridge.hermes import _is_valid_proposal

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "profiles" / "att1"
BUNDLE = PROFILES / "att1-product-bundle-v1.json"
INSTALLER = ROOT / "scripts" / "install_att1_product_profiles.py"
PROFILE_NAMES = {"agente-comercial", "automation-expert", "client-copilot"}
DANGEROUS_TOOLSETS = {
    "terminal",
    "file",
    "code_execution",
    "coding",
    "web",
    "browser",
    "memory",
    "session_search",
    "skills",
    "delegation",
    "cronjob",
    "project",
}


def _load_installer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("att1_product_profile_installer", INSTALLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(profile_name: str) -> dict[str, object]:
    return yaml.safe_load((PROFILES / profile_name / "config.yaml").read_text(encoding="utf-8"))


def _manifest(profile_name: str) -> dict[str, object]:
    return json.loads((PROFILES / profile_name / "manifest.json").read_text(encoding="utf-8"))


def test_bundle_contains_exactly_the_three_product_profiles() -> None:
    bundle = json.loads(BUNDLE.read_text(encoding="utf-8"))

    assert bundle["bundle_name"] == "att1-product-profiles"
    assert bundle["package_status"] == "candidate"
    assert bundle["activation_capability"] is False
    assert bundle["production_credentials"] is False
    assert set(bundle["profiles"]) == PROFILE_NAMES
    assert bundle["default_profile_exposed"] is False


@pytest.mark.parametrize("profile_name", sorted(PROFILE_NAMES))
def test_every_profile_has_private_fail_closed_defaults(profile_name: str) -> None:
    manifest = _manifest(profile_name)
    config = _config(profile_name)

    assert manifest["profile_name"] == profile_name
    assert manifest["package_status"] == "candidate"
    assert manifest["production_credentials"] is False
    assert manifest["activation_capability"] is False
    assert config["memory"] == {
        "memory_enabled": False,
        "user_profile_enabled": False,
    }
    assert DANGEROUS_TOOLSETS <= set(config["agent"]["disabled_toolsets"])
    assert config["security"] == {
        "redact_secrets": True,
        "tirith_enabled": True,
        "tirith_fail_open": False,
    }
    assert config["approvals"]["mode"] == "manual"
    assert config["approvals"]["cron_mode"] == "deny"
    assert config["terminal"]["env_passthrough"] == []


def test_commercial_profile_is_draft_toolless_and_bridge_compatible() -> None:
    manifest = _manifest("agente-comercial")
    config = _config("agente-comercial")
    contract = json.loads(
        (PROFILES / "agente-comercial/release/output-contract-v1.json").read_text(
            encoding="utf-8"
        )
    )

    assert manifest["conversation_release_status"] == "draft"
    assert manifest["provider_effect_capability"] is False
    assert manifest["runtime_binding_included"] is False
    assert manifest["permitted_runtime_surfaces"] == ["api_server"]
    assert manifest["verified_effective_toolsets"] == {"api_server": []}
    assert config["toolsets"] == []
    assert all(value == [] for value in config["platform_toolsets"].values())
    assert config["platforms"]["api_server"]["enabled"] is True
    proposal = contract["fallback_proposal"]
    assert proposal["decision"] == "handoff"
    assert proposal["qualification_status"] == "needs_human"
    assert proposal["reason_code"] == "att1_release_incomplete"
    assert all(value is None for value in proposal["captured_fields"].values())
    assert set(proposal["missing_fields"]) == set(proposal["captured_fields"])
    assert _is_valid_proposal(proposal) is True


def test_automation_expert_is_installed_but_has_no_execution_authority() -> None:
    manifest = _manifest("automation-expert")
    config = _config("automation-expert")
    contract = json.loads(
        (PROFILES / "automation-expert/release/output-contract-v1.json").read_text(
            encoding="utf-8"
        )
    )

    assert manifest["functional_release_status"] == "not_released"
    assert manifest["scheduler_effect_capability"] is False
    assert manifest["provider_effect_capability"] is False
    assert manifest["permitted_runtime_surfaces"] == ["api_server"]
    assert manifest["verified_effective_toolsets"] == {"api_server": []}
    assert config["toolsets"] == []
    assert all(value == [] for value in config["platform_toolsets"].values())
    assert config["platforms"]["api_server"]["enabled"] is True
    assert contract["fallback_proposal"] == {
        "status": "unavailable",
        "reason_code": "automation_expert_not_released",
        "proposal": None,
    }


def test_client_copilot_keeps_only_its_existing_bounded_capability() -> None:
    manifest = _manifest("client-copilot")
    config = _config("client-copilot")

    assert manifest["activation_capability"] is False
    assert config["_config_version"] == 40
    assert manifest["session_launch"]["toolsets"] == [
        "operator_correlation_review"
    ]
    assert config["platform_toolsets"]["cli"] == ["operator_correlation_review"]
    assert config["platform_toolsets"]["api_server"] == [
        "operator_correlation_review"
    ]
    assert all(
        value == []
        for name, value in config["platform_toolsets"].items()
        if name not in {"cli", "api_server"}
    )
    assert config["platforms"]["api_server"]["enabled"] is True
    assert config["plugins"]["enabled"] == ["operator-correlation-review"]


def test_bundle_manifest_binds_every_regular_profile_file() -> None:
    bundle = json.loads(BUNDLE.read_text(encoding="utf-8"))
    entries = bundle["files"]
    assert all(set(entry) == {"path", "size", "sha256"} for entry in entries)
    assert len({entry["path"] for entry in entries}) == len(entries)

    actual = {
        path.relative_to(PROFILES).as_posix()
        for profile_name in PROFILE_NAMES
        for path in (PROFILES / profile_name).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    assert {entry["path"] for entry in entries} == actual
    for entry in entries:
        payload = (PROFILES / entry["path"]).read_bytes()
        assert entry["size"] == len(payload)
        assert entry["sha256"] == hashlib.sha256(payload).hexdigest()


def test_att1_namespace_contains_no_other_ally_content() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for profile_name in PROFILE_NAMES
        for path in (PROFILES / profile_name).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )

    for forbidden in (
        "Johanna",
        "Libre de Ansiedad",
        "Nina Garza",
        "Hashimoto",
        "hipotiroidismo",
    ):
        assert forbidden not in combined


@pytest.mark.parametrize("profile_name", sorted(PROFILE_NAMES))
def test_create_only_installer_is_private_and_verifies_every_byte(
    profile_name: str, tmp_path: Path
) -> None:
    installer = _load_installer()
    assert "update_existing" not in inspect.signature(installer.install).parameters
    target = tmp_path / profile_name

    receipt = installer.install(profile_name, target)

    assert receipt["profile_name"] == profile_name
    assert target.stat().st_mode & 0o777 == 0o700
    assert (target / "profile-package-installation.json").stat().st_mode & 0o777 == 0o600
    for relative, expected_hash in receipt["sha256"].items():
        installed = target / relative
        assert installed.stat().st_mode & 0o777 == 0o600
        assert hashlib.sha256(installed.read_bytes()).hexdigest() == expected_hash
    with pytest.raises(FileExistsError, match="already exists"):
        installer.install(profile_name, target)


def test_installer_rejects_a_mutated_source_package(tmp_path: Path) -> None:
    installer = _load_installer()
    soul = PROFILES / "automation-expert/SOUL.md"
    original = soul.read_bytes()
    try:
        soul.write_bytes(original + b"\nmutated\n")
        with pytest.raises(ValueError, match="source package integrity mismatch"):
            installer.install("automation-expert", tmp_path / "automation-expert")
    finally:
        soul.write_bytes(original)


def test_installer_atomic_noreplace_loses_safely_to_concurrent_creator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = _load_installer()
    target = tmp_path / "automation-expert"
    real_rename = installer._rename_noreplace

    def create_target_then_rename(
        parent_fd: int, staging_name: str, target_name: str
    ) -> None:
        os.mkdir(target_name, 0o700, dir_fd=parent_fd)
        target_fd = os.open(
            target_name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent_fd
        )
        try:
            marker_fd = os.open(
                "concurrent-owner",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=target_fd,
            )
            os.close(marker_fd)
        finally:
            os.close(target_fd)
        real_rename(parent_fd, staging_name, target_name)

    monkeypatch.setattr(installer, "_rename_noreplace", create_target_then_rename)

    with pytest.raises(FileExistsError, match="already exists"):
        installer.install("automation-expert", target)
    assert (target / "concurrent-owner").is_file()
    retained = [
        path
        for path in tmp_path.iterdir()
        if path.name.startswith(".automation-expert.staging-")
    ]
    assert len(retained) == 1
    assert retained[0].stat().st_mode & 0o777 == 0o700
    assert (retained[0] / "manifest.json").is_file()


def test_installer_never_follows_parent_swapped_to_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = _load_installer()
    parent = tmp_path / "profiles"
    parent.mkdir()
    moved_parent = tmp_path / "profiles-original"
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    target = parent / "agente-comercial"

    def swap_parent(_parent_fd: int, parent_path: Path) -> None:
        assert parent_path == parent
        parent.rename(moved_parent)
        parent.symlink_to(attacker, target_is_directory=True)

    monkeypatch.setattr(installer, "_before_publish", swap_parent)

    with pytest.raises((FileExistsError, RuntimeError), match="parent|symlink"):
        installer.install("agente-comercial", target)
    assert not (attacker / "agente-comercial").exists()
    assert not (moved_parent / "agente-comercial").exists()
    retained = [
        path
        for path in moved_parent.iterdir()
        if path.name.startswith(".agente-comercial.staging-")
    ]
    assert len(retained) == 1
    assert retained[0].stat().st_mode & 0o777 == 0o700
    assert (retained[0] / "manifest.json").is_file()


def test_installer_rejects_symlink_in_source_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = _load_installer()
    linked_profiles = tmp_path / "linked-profiles"
    linked_profiles.symlink_to(PROFILES, target_is_directory=True)
    monkeypatch.setattr(installer, "PROFILES", linked_profiles)
    monkeypatch.setattr(
        installer, "BUNDLE", linked_profiles / "att1-product-bundle-v1.json"
    )
    target = tmp_path / "installed"

    with pytest.raises(ValueError, match="source package integrity mismatch"):
        installer.install("agente-comercial", target)
    assert not target.exists()


def test_cleanup_preserves_a_concurrent_staging_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = _load_installer()
    target = tmp_path / "automation-expert"
    moved = tmp_path / "original-staging"

    def replace_staging(parent_fd: int, _parent_path: Path) -> None:
        staging_name = next(
            name for name in os.listdir(parent_fd) if name.startswith(".automation-expert.staging-")
        )
        os.rename(staging_name, moved.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.mkdir(staging_name, 0o700, dir_fd=parent_fd)
        replacement_fd = os.open(
            staging_name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent_fd
        )
        try:
            marker_fd = os.open(
                "concurrent-owner",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=replacement_fd,
            )
            os.close(marker_fd)
        finally:
            os.close(replacement_fd)
        raise RuntimeError("injected pre-publication failure")

    monkeypatch.setattr(installer, "_before_publish", replace_staging)

    with pytest.raises(RuntimeError, match="injected pre-publication failure"):
        installer.install("automation-expert", target)
    replacement = next(
        path for path in tmp_path.iterdir() if path.name.startswith(".automation-expert.staging-")
    )
    assert (replacement / "concurrent-owner").is_file()
    assert (moved / "manifest.json").is_file()


def test_post_publish_cleanup_preserves_concurrent_target_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = _load_installer()
    target = tmp_path / "agente-comercial"
    published_copy = tmp_path / "published-copy"
    real_assert = installer._assert_same_parent
    calls = 0

    def replace_target_on_postcheck(parent_path: Path, parent_fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            real_assert(parent_path, parent_fd)
            return
        os.rename(target.name, published_copy.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.mkdir(target.name, 0o700, dir_fd=parent_fd)
        target_fd = os.open(target.name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent_fd)
        try:
            marker_fd = os.open(
                "concurrent-owner",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=target_fd,
            )
            os.close(marker_fd)
        finally:
            os.close(target_fd)
        raise RuntimeError("injected post-publication replacement")

    monkeypatch.setattr(installer, "_assert_same_parent", replace_target_on_postcheck)

    with pytest.raises(RuntimeError, match="injected post-publication replacement"):
        installer.install("agente-comercial", target)
    assert (target / "concurrent-owner").is_file()
    assert (published_copy / "manifest.json").is_file()


def test_installer_rejects_unknown_profile_without_writing(tmp_path: Path) -> None:
    installer = _load_installer()
    target = tmp_path / "default"

    with pytest.raises(ValueError, match="unknown profile"):
        installer.install("default", target)
    assert not target.exists()
