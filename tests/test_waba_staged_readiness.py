import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_waba_staged_readiness.py"


def _snapshot() -> dict[str, object]:
    return {
        "channel": {
            "account_id": 101,
            "inbox_id": 303,
            "previous_inbox_id": 202,
            "portfolio_bound": True,
            "waba_connected": True,
            "phone_number_bound": True,
            "phone_number_id_bound": True,
            "official_inbox_verified": True,
            "shared_webhook_verified": True,
            "exact_scope_verified": True,
            "previous_inbox_rejected": True,
            "evolution_out_of_scope": True,
        },
        "runtime": {
            "automated_replies_enabled": False,
            "reply_splitter_enabled": False,
            "shadow_enabled": False,
            "resolution_worker_enabled": False,
            "purchase_worker_enabled": False,
            "human_pause_enabled": False,
            "durable_opt_out_enabled": False,
            "opt_out_projection_enabled": False,
            "handoff_admission_enabled": False,
            "handoff_projection_enabled": False,
            "dispatcher_enabled": False,
            "outbound_enabled": False,
            "pilot_boundary_enabled": False,
        },
        "template": {
            "contract_version": 2,
            "selection_unambiguous": True,
            "first_touch_meta_approved": True,
            "first_touch_business_approved": True,
            "followup_disabled": True,
            "first_touch_name_present": True,
            "language_present": True,
            "category_present": True,
            "category_runtime_supported": True,
            "body_placeholders_two_exact": True,
            "single_touch_runtime_compatible": True,
        },
        "controlled_template": {
            "payment_method_operational": True,
            "recipient_allowlisted": True,
            "one_send_budget": True,
            "eligible_backlog_zero": True,
            "rollback_ready": True,
            "provider_mode_compatible": True,
        },
        "supervised_pilot": {
            "durable_scope_published": True,
            "durable_scope_inactive": True,
            "remote_schema_verified": True,
            "purchase_stop_ready": True,
            "opt_out_stop_ready": True,
            "policy_approved": True,
            "conversation_release_approved": True,
            "monitoring_ready": True,
            "cohort_bounded": True,
            "pilot_budget_bounded": True,
            "kill_switch_owned": True,
            "handoff_enabled": False,
            "handoff_owner_ready": False,
        },
        "evidence": {
            "commit_digest_present": True,
            "configuration_digest_present": True,
            "observed_at_present": True,
            "zero_external_effects": True,
            "sensitive_value": "must-never-be-printed",
        },
    }


def _single_touch_snapshot() -> dict[str, object]:
    return _snapshot()


def _run(snapshot: object, *, account: str = "101", inbox: str = "303", previous: str = "202") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--expected-account-id",
            account,
            "--expected-inbox-id",
            inbox,
            "--previous-inbox-id",
            previous,
        ],
        input=json.dumps(snapshot),
        text=True,
        capture_output=True,
        check=False,
    )


def _body(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return json.loads(result.stdout)


def test_all_three_readiness_levels_can_be_ready_without_handoff() -> None:
    result = _run(_snapshot())

    assert result.returncode == 0
    body = _body(result)
    assert body["highest_ready_level"] == "ready_for_supervised_pilot"
    assert all(level["ready"] is True for level in body["levels"].values())  # type: ignore[union-attr]
    assert "101" not in result.stdout
    assert "202" not in result.stdout
    assert "303" not in result.stdout
    assert "must-never-be-printed" not in result.stdout
    assert result.stderr == ""


def test_single_touch_two_variable_contract_can_reach_all_levels() -> None:
    result = _run(_single_touch_snapshot())

    assert result.returncode == 0
    body = _body(result)
    assert body["highest_ready_level"] == "ready_for_supervised_pilot"
    assert all(level["ready"] is True for level in body["levels"].values())  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("followup_disabled", "followup_not_disabled"),
        ("first_touch_name_present", "first_touch_template_name_missing"),
        ("body_placeholders_two_exact", "template_placeholder_schema_mismatch"),
        ("single_touch_runtime_compatible", "single_touch_runtime_mismatch"),
    ],
)
def test_single_touch_contract_gates_fail_closed(field: str, reason: str) -> None:
    snapshot = _single_touch_snapshot()
    template = snapshot["template"]
    assert isinstance(template, dict)
    template[field] = False

    result = _run(snapshot)

    controlled = _body(result)["levels"]["ready_for_controlled_template"]  # type: ignore[index]
    assert controlled["ready"] is False
    assert reason in controlled["reasons"]


@pytest.mark.parametrize("invalid_version", [True, 2.0, 2e0, 3, "2"])
def test_template_contract_version_fails_closed(invalid_version: object) -> None:
    snapshot = _single_touch_snapshot()
    template = snapshot["template"]
    assert isinstance(template, dict)
    template["contract_version"] = invalid_version

    result = _run(snapshot)

    reasons = _body(result)["levels"]["ready_for_controlled_template"]["reasons"]  # type: ignore[index]
    assert "template_contract_version_unsupported" in reasons


def test_legacy_template_contract_cannot_authorize_current_runtime() -> None:
    snapshot = _snapshot()
    template = snapshot["template"]
    assert isinstance(template, dict)
    template["contract_version"] = 1

    result = _run(snapshot)

    controlled = _body(result)["levels"]["ready_for_controlled_template"]  # type: ignore[index]
    assert controlled["ready"] is False
    assert "template_contract_version_unsupported" in controlled["reasons"]


def test_observational_inbound_does_not_require_team_template_payment_or_schema() -> None:
    snapshot = _snapshot()
    snapshot["template"] = {}
    snapshot["controlled_template"] = {}
    snapshot["supervised_pilot"] = {"handoff_enabled": False}

    result = _run(snapshot)

    assert result.returncode == 0
    body = _body(result)
    assert body["highest_ready_level"] == "ready_for_observational_inbound"
    levels = body["levels"]
    assert levels["ready_for_observational_inbound"] == {"ready": True, "reasons": [], "status": "ready"}  # type: ignore[index]
    assert "payment_method_not_operational" in levels["ready_for_controlled_template"]["reasons"]  # type: ignore[index]
    assert "remote_schema_unverified" in levels["ready_for_supervised_pilot"]["reasons"]  # type: ignore[index]


def test_controlled_template_does_not_enable_supervised_pilot() -> None:
    snapshot = _snapshot()
    snapshot["supervised_pilot"] = {"handoff_enabled": False}

    result = _run(snapshot)

    body = _body(result)
    assert body["highest_ready_level"] == "ready_for_controlled_template"
    assert body["levels"]["ready_for_controlled_template"]["ready"] is True  # type: ignore[index]
    assert body["levels"]["ready_for_supervised_pilot"]["ready"] is False  # type: ignore[index]


@pytest.mark.parametrize(
    ("section", "field", "reason"),
    [
        ("channel", "previous_inbox_rejected", "previous_inbox_still_admissible"),
        ("runtime", "outbound_enabled", "outbound_not_off"),
        ("runtime", "dispatcher_enabled", "dispatcher_not_off"),
        ("evidence", "zero_external_effects", "zero_effects_unverified"),
        ("template", "selection_unambiguous", "template_selection_ambiguous"),
        ("template", "first_touch_meta_approved", "first_touch_meta_not_approved"),
        ("template", "category_runtime_supported", "template_category_runtime_unsupported"),
        ("template", "single_touch_runtime_compatible", "single_touch_runtime_mismatch"),
        ("controlled_template", "provider_mode_compatible", "provider_mode_incompatible"),
        ("controlled_template", "eligible_backlog_zero", "eligible_backlog_not_zero"),
        ("supervised_pilot", "durable_scope_inactive", "durable_scope_not_inactive"),
        ("supervised_pilot", "conversation_release_approved", "conversation_release_not_approved"),
    ],
)
@pytest.mark.parametrize("case", ["wrong", "missing", "integer"])
def test_each_gate_fails_closed_on_non_exact_boolean(section: str, field: str, reason: str, case: str) -> None:
    snapshot = _snapshot()
    target = snapshot[section]
    assert isinstance(target, dict)
    if case == "missing":
        target.pop(field)
    elif case == "integer":
        target[field] = 1 if section != "runtime" else 0
    else:
        target[field] = section == "runtime"

    result = _run(snapshot)

    levels = _body(result)["levels"]
    assert any(reason in level["reasons"] for level in levels.values())  # type: ignore[union-attr]


def test_handoff_owner_only_blocks_when_handoff_is_enabled_or_unspecified() -> None:
    snapshot = _snapshot()
    pilot = snapshot["supervised_pilot"]
    assert isinstance(pilot, dict)
    pilot["handoff_enabled"] = True
    pilot["handoff_owner_ready"] = False

    result = _run(snapshot)

    reasons = _body(result)["levels"]["ready_for_supervised_pilot"]["reasons"]  # type: ignore[index]
    assert "handoff_owner_missing" in reasons
    assert _body(result)["levels"]["ready_for_observational_inbound"]["ready"] is True  # type: ignore[index]


@pytest.mark.parametrize("invalid", [1, "enabled", None, {}, []])
def test_handoff_enabled_requires_an_exact_boolean(invalid: object) -> None:
    snapshot = _snapshot()
    pilot = snapshot["supervised_pilot"]
    assert isinstance(pilot, dict)
    pilot["handoff_enabled"] = invalid
    pilot["handoff_owner_ready"] = True

    result = _run(snapshot)

    level = _body(result)["levels"]["ready_for_supervised_pilot"]  # type: ignore[index]
    assert level["ready"] is False
    assert "handoff_enabled_invalid" in level["reasons"]


def test_missing_handoff_enabled_fails_closed_even_with_owner_ready() -> None:
    snapshot = _snapshot()
    pilot = snapshot["supervised_pilot"]
    assert isinstance(pilot, dict)
    pilot.pop("handoff_enabled")
    pilot["handoff_owner_ready"] = True

    result = _run(snapshot)

    reasons = _body(result)["levels"]["ready_for_supervised_pilot"]["reasons"]  # type: ignore[index]
    assert "handoff_enabled_invalid" in reasons


def test_boolean_ids_and_previous_inbox_reuse_fail_closed() -> None:
    snapshot = _snapshot()
    channel = snapshot["channel"]
    assert isinstance(channel, dict)
    channel["account_id"] = True
    channel["inbox_id"] = True

    result = _run(snapshot, account="1", inbox="1", previous="2")

    reasons = _body(result)["levels"]["ready_for_observational_inbound"]["reasons"]  # type: ignore[index]
    assert "active_account_mismatch" in reasons
    assert "active_inbox_mismatch" in reasons

    invalid = _run(_snapshot(), inbox="303", previous="303")
    assert invalid.returncode == 2
    assert _body(invalid) == {"error": "invalid_snapshot", "status": "error"}


@pytest.mark.parametrize(
    "arguments",
    [
        ["--expected-account-id", "secret-id", "--expected-inbox-id", "303", "--previous-inbox-id", "202"],
        ["--expected-account-id", "101", "--expected-inbox-id", "303"],
        ["--unknown", "secret-value"],
    ],
)
def test_cli_errors_are_sanitized(arguments: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        input=json.dumps(_snapshot()),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert _body(result) == {"error": "invalid_snapshot", "status": "error"}
    assert "secret" not in result.stdout
    assert "secret" not in result.stderr
    assert result.stderr == ""


def test_no_level_ready_returns_exit_one() -> None:
    snapshot = _snapshot()
    runtime = snapshot["runtime"]
    assert isinstance(runtime, dict)
    runtime["outbound_enabled"] = True

    result = _run(snapshot)

    assert result.returncode == 1
    assert _body(result)["highest_ready_level"] is None
