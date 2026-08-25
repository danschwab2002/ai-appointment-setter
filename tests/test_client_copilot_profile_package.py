import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles" / "client-copilot"
PLUGIN = PROFILE / "plugins" / "operator-correlation-review"
INSTALLER = ROOT / "scripts" / "install_client_copilot_profile.py"


def _load_plugin_tools() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "operator_correlation_review_tools", PLUGIN / "tools.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_installer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("client_copilot_installer", INSTALLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_client_copilot_package_exposes_only_correlation_review_toolset() -> None:
    manifest = json.loads((PROFILE / "manifest.json").read_text(encoding="utf-8"))
    config = yaml.safe_load((PROFILE / "config.yaml").read_text(encoding="utf-8"))
    plugin = yaml.safe_load((PLUGIN / "plugin.yaml").read_text(encoding="utf-8"))

    assert manifest["profile_name"] == "client-copilot"
    assert manifest["package_status"] == "candidate"
    assert manifest["activation_capability"] is False
    assert manifest["session_launch"]["toolsets"] == [
        "operator_correlation_review"
    ]
    assert config["platform_toolsets"] == {
        "cli": ["operator_correlation_review"],
        "api_server": ["operator_correlation_review"],
    }
    assert "terminal" in config["agent"]["disabled_toolsets"]
    assert "web" in config["agent"]["disabled_toolsets"]
    assert plugin["provides_tools"] == [
        "list_unresolved_correlations",
        "get_unresolved_correlation",
    ]
    assert plugin["requires_env"] == [
        "OPERATOR_CORRELATION_API_URL",
        "OPERATOR_CORRELATION_API_TOKEN",
    ]


def test_plugin_list_handler_calls_only_bounded_read_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _load_plugin_tools()
    monkeypatch.setenv(
        "OPERATOR_CORRELATION_API_URL", "https://bridge.example.test"
    )
    monkeypatch.setenv("OPERATOR_CORRELATION_API_TOKEN", "t" * 32)
    seen: dict[str, Any] = {}

    def fake_read(path: str) -> dict[str, object]:
        seen["path"] = path
        return {"count": 0, "cases": []}

    monkeypatch.setattr(tools, "_read_json", fake_read)

    result = json.loads(tools.list_unresolved_correlations({"limit": 12}))

    assert result == {"count": 0, "cases": []}
    assert seen["path"] == "/internal/operator/correlations/unresolved?limit=12"


def test_plugin_reads_settings_from_multiplexed_profile_secret_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPERATOR_CORRELATION_API_URL", raising=False)
    monkeypatch.delenv("OPERATOR_CORRELATION_API_TOKEN", raising=False)
    scoped_values = {
        "OPERATOR_CORRELATION_API_URL": "https://bridge.example.test",
        "OPERATOR_CORRELATION_API_TOKEN": "t" * 32,
    }
    agent = ModuleType("agent")
    secret_scope = ModuleType("agent.secret_scope")
    setattr(
        secret_scope,
        "get_secret",
        lambda name, default="": scoped_values.get(name, default),
    )
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", secret_scope)

    tools = _load_plugin_tools()

    assert tools._api_settings() == (
        "https://bridge.example.test",
        "t" * 32,
    )


def test_plugin_detail_handler_rejects_non_uuid_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _load_plugin_tools()
    called = False

    def fake_read(path: str) -> dict[str, object]:
        nonlocal called
        called = True
        return {"path": path}

    monkeypatch.setattr(tools, "_read_json", fake_read)

    result = json.loads(tools.get_unresolved_correlation({"case_id": "not-an-id"}))

    assert result == {"error": "invalid_case_id"}
    assert called is False


def test_plugin_refuses_redirects_before_reusing_authorization() -> None:
    tools = _load_plugin_tools()
    request = tools.Request(
        "https://bridge.example.test/internal/operator/correlations/unresolved",
        headers={"Authorization": "Bearer sentinel"},
    )

    redirected = tools._NoRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        {"Location": "https://attacker.example/collect"},
        "https://attacker.example/collect",
    )

    assert redirected is None


def test_profile_installer_uses_private_permissions_and_preserves_env(
    tmp_path: Path,
) -> None:
    installer = _load_installer()
    target = tmp_path / "client-copilot"

    receipt = installer.install(target)

    assert receipt["profile_name"] == "client-copilot"
    for relative in receipt["sha256"]:
        assert (target / relative).stat().st_mode & 0o777 == 0o600
    assert (target / "profile-package-installation.json").stat().st_mode & 0o777 == 0o600
    assert target.stat().st_mode & 0o777 == 0o700

    env_file = target / ".env"
    env_file.write_text("PRIVATE_VALUE=preserve-me\n", encoding="utf-8")
    env_file.chmod(0o600)
    installer.install(target, update_existing=True)

    assert env_file.read_text(encoding="utf-8") == "PRIVATE_VALUE=preserve-me\n"
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_profile_update_recovers_after_staging_copy_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = _load_installer()
    target = tmp_path / "client-copilot"
    original = installer.install(target)
    env_file = target / ".env"
    env_file.write_text("PRIVATE_VALUE=preserve-me\n", encoding="utf-8")
    env_file.chmod(0o600)
    real_copyfile = installer.shutil.copyfile
    calls = 0

    def fail_during_staging(
        source: Path, destination: Path, **kwargs: object
    ) -> None:
        del source, kwargs
        nonlocal calls
        calls += 1
        if calls == 1:
            Path(destination).write_text("partial\n", encoding="utf-8")
            return
        raise OSError("injected staging failure")

    monkeypatch.setattr(installer.shutil, "copyfile", fail_during_staging)
    with pytest.raises(OSError, match="injected staging failure"):
        installer.install(target, update_existing=True)

    for relative, expected_hash in original["sha256"].items():
        assert installer._sha256(target / relative) == expected_hash
    monkeypatch.setattr(installer.shutil, "copyfile", real_copyfile)

    installer.install(target, update_existing=True)
    assert env_file.read_text(encoding="utf-8") == "PRIVATE_VALUE=preserve-me\n"


def test_profile_update_keeps_active_target_when_atomic_exchange_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = _load_installer()
    target = tmp_path / "client-copilot"
    original = installer.install(target)

    def fail_exchange(staging: Path, active: Path) -> None:
        assert staging.exists()
        assert active == target
        assert target.exists()
        raise OSError("injected exchange failure")

    monkeypatch.setattr(installer, "_exchange_directories", fail_exchange)
    with pytest.raises(OSError, match="injected exchange failure"):
        installer.install(target, update_existing=True)

    assert target.exists()
    for relative, expected_hash in original["sha256"].items():
        assert installer._sha256(target / relative) == expected_hash
