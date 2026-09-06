from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles" / "att1" / "agente-comercial"
BUNDLE = ROOT / "profiles" / "att1" / "att1-product-bundle-v1.json"
INSTALLER = ROOT / "scripts" / "install_att1_product_profiles.py"


def _load_installer_module():
    spec = importlib.util.spec_from_file_location("att1_profile_installer", INSTALLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_att1_commercial_agent_candidate_is_inert_and_unapproved() -> None:
    required_files = [
        PROFILE / "SOUL.md",
        PROFILE / "config.yaml",
        PROFILE / "distribution.yaml",
        PROFILE / "manifest.json",
        PROFILE / "release" / "conversation-release-v1.yaml",
        PROFILE / "release" / "commercial-knowledge-v1.yaml",
        PROFILE / "release" / "conversation-policy-v1.md",
        PROFILE / "release" / "output-contract-v1.json",
        BUNDLE,
    ]
    missing = [str(path.relative_to(ROOT)) for path in required_files if not path.is_file()]
    assert missing == []

    manifest = json.loads((PROFILE / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["profile_name"] == "agente-comercial"
    assert manifest["package_status"] == "candidate"
    assert manifest["conversation_release_status"] == "draft"
    assert manifest["activation_capability"] is False
    assert manifest["provider_effect_capability"] is False
    assert manifest["runtime_binding_included"] is False
    assert manifest["permitted_runtime_surfaces"] == []
    assert manifest["verified_effective_toolsets"] == {}

    release = yaml.safe_load(
        (PROFILE / "release" / "conversation-release-v1.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert release["release_status"] == "draft"
    assert release["completeness"] == "draft_incomplete"
    assert release["activation"] == "prohibited"
    assert release["provider_effects"] == "prohibited"
    assert release["runtime_binding"] == "absent"
    assert {
        "brand_voice_approval",
        "conversation_release_approval",
    } <= set(release["missing_external_gates"])

    knowledge = yaml.safe_load(
        (PROFILE / "release" / "commercial-knowledge-v1.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert knowledge["status"] == "fallback_only"
    assert knowledge["disclosable"] == {"assistant_identity": "virtual assistant"}
    assert {
        "brand_or_personal_identity",
        "offer_name",
        "price_or_currency",
        "checkout_or_other_links",
        "discounts_or_coupon_reference",
    } <= set(knowledge["prohibited_without_new_release"])

    assert "No ejecutes herramientas ni acciones externas" in (
        PROFILE / "SOUL.md"
    ).read_text(encoding="utf-8")

    config = yaml.safe_load((PROFILE / "config.yaml").read_text(encoding="utf-8"))
    assert config["toolsets"] == []
    assert all(toolsets == [] for toolsets in config["platform_toolsets"].values())
    assert all(platform["enabled"] is False for platform in config["platforms"].values())
    assert config["plugins"]["enabled"] == []

    bundle = json.loads(BUNDLE.read_text(encoding="utf-8"))
    assert bundle["profiles"] == ["agente-comercial"]
    assert bundle["files"]
    assert all(entry["path"].startswith("agente-comercial/") for entry in bundle["files"])


def test_installer_creates_verified_private_home_without_overwrite(tmp_path: Path) -> None:
    assert INSTALLER.is_file()
    target = tmp_path / "att1-agente-comercial"

    first = subprocess.run(
        [
            sys.executable,
            str(INSTALLER),
            "--profile",
            "agente-comercial",
            "--target-home",
            str(target),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr

    receipt = json.loads((target / "profile-package-installation.json").read_text())
    installed_files = {
        path.relative_to(target).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in target.rglob("*")
        if path.is_file() and path.name != "profile-package-installation.json"
    }
    assert receipt["profile_name"] == "agente-comercial"
    assert receipt["sha256"] == installed_files
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in target.rglob("*") if path.is_file())

    before = {
        path.relative_to(target).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in target.rglob("*")
        if path.is_file()
    }
    second = subprocess.run(
        [
            sys.executable,
            str(INSTALLER),
            "--profile",
            "agente-comercial",
            "--target-home",
            str(target),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    after = {
        path.relative_to(target).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in target.rglob("*")
        if path.is_file()
    }
    assert second.returncode == 2
    assert "target profile already exists" in second.stderr
    assert after == before


def test_bundle_inventory_exactly_matches_profile_source() -> None:
    bundle = json.loads(BUNDLE.read_text(encoding="utf-8"))
    bundled = {entry["path"] for entry in bundle["files"]}
    source = {
        path.relative_to(PROFILE.parent).as_posix()
        for path in PROFILE.rglob("*")
        if path.is_file()
    }
    assert bundled == source
    assert all(Path(path).name not in {".env", "auth.json"} for path in bundled)
    assert all("credential" not in Path(path).name.lower() for path in bundled)


def test_installer_rejects_tampered_profile_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = _load_installer_module()
    profiles = tmp_path / "profiles"
    shutil.copytree(PROFILE.parent, profiles)
    bundle = profiles / BUNDLE.name
    monkeypatch.setattr(installer, "PROFILES", profiles)
    monkeypatch.setattr(installer, "BUNDLE", bundle)
    monkeypatch.setattr(installer, "EXPECTED_BUNDLE_SHA256", hashlib.sha256(bundle.read_bytes()).hexdigest())
    (profiles / "agente-comercial" / "SOUL.md").write_text("tampered", encoding="utf-8")

    with pytest.raises(ValueError, match="source package integrity mismatch"):
        installer.install("agente-comercial", tmp_path / "target")


def test_installer_rejects_tampered_bundle_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = _load_installer_module()
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    bundle = profiles / BUNDLE.name
    shutil.copy2(BUNDLE, bundle)
    manifest = json.loads(bundle.read_text(encoding="utf-8"))
    manifest["bundle_version"] = "tampered"
    bundle.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(installer, "PROFILES", profiles)
    monkeypatch.setattr(installer, "BUNDLE", bundle)

    with pytest.raises(ValueError, match="source package integrity mismatch"):
        installer._load_bundle()


@pytest.mark.parametrize(
    "unsafe",
    ["/absolute", "agente-comercial/../escape", "agente-comercial/profile-package-installation.json"],
)
def test_installer_rejects_unsafe_bundle_paths(unsafe: str) -> None:
    installer = _load_installer_module()
    with pytest.raises(ValueError, match="bundle file path is unsafe"):
        installer._relative_path(unsafe, profile_name="agente-comercial")


def test_installer_rejects_symlinked_target_parent(tmp_path: Path) -> None:
    installer = _load_installer_module()
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    symlink_parent = tmp_path / "linked"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(FileExistsError, match="target parent path contains a symlink"):
        installer.install("agente-comercial", symlink_parent / "target")
    assert not (real_parent / "target").exists()


def test_failure_before_publication_leaves_no_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = _load_installer_module()
    target = tmp_path / "target"

    def fail_before_publish(_parent_fd: int, _parent_path: Path) -> None:
        raise RuntimeError("injected pre-publication failure")

    monkeypatch.setattr(installer, "_before_publish", fail_before_publish)
    with pytest.raises(RuntimeError, match="injected pre-publication failure"):
        installer.install("agente-comercial", target)
    assert not target.exists()
