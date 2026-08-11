import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_chatwoot_waba_readiness.py"


def _snapshot() -> dict[str, object]:
    return {
        "chatwoot": {
            "account_id": 101,
            "inboxes": [
                {
                    "id": 202,
                    "channel_class": "Channel::Api",
                    "provider": None,
                },
                {
                    "id": 303,
                    "channel_class": "Channel::Whatsapp",
                    "provider": "whatsapp_cloud",
                    "phone_number_present": True,
                    "phone_number_id_present": True,
                    "business_account_id_present": True,
                    "access_token_present": True,
                    "member_count": 2,
                },
            ],
            "teams_count": 1,
            "shared_webhook": {
                "https": True,
                "bridge_path": "/webhooks/chatwoot",
                "subscriptions": ["message_created"],
            },
        },
        "evolution": {
            "connection_state": "close",
            "chatwoot_enabled": False,
        },
        "bridge": {
            "account_id": 101,
            "inbox_id": 303,
            "provider": "waba",
            "automated_replies_enabled": False,
            "dispatcher_enabled": False,
            "outbound_enabled": False,
            "pilot_boundary_enabled": False,
            "shadow_enabled": False,
            "resolution_worker_enabled": False,
            "hotmart_purchase_worker_enabled": False,
            "reply_splitter_enabled": False,
            "durable_opt_out_enabled": False,
            "opt_out_projection_enabled": False,
            "human_handoff_admission_enabled": False,
            "human_handoff_projection_enabled": False,
            "opt_out_projection_backlog_zero": True,
            "human_handoff_projection_backlog_zero": True,
            "sensitive_probe_value": "must-never-be-printed",
        },
    }


def _run(snapshot: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--expected-account-id",
            "101",
            "--expected-waba-inbox-id",
            "303",
            "--expected-legacy-inbox-id",
            "202",
        ],
        input=json.dumps(snapshot),
        text=True,
        capture_output=True,
        check=False,
    )


def test_readiness_probe_accepts_an_isolated_default_off_waba_scope() -> None:
    result = _run(_snapshot())

    assert result.returncode == 0
    body = json.loads(result.stdout)
    assert body == {
        "blockers": [],
        "safe_for_controlled_inbound": True,
        "status": "ready",
    }
    assert "101" not in result.stdout
    assert "202" not in result.stdout
    assert "303" not in result.stdout
    assert "must-never-be-printed" not in result.stdout
    assert result.stderr == ""


def test_readiness_probe_reports_sanitized_fail_closed_blockers() -> None:
    snapshot = _snapshot()
    snapshot["chatwoot"]["teams_count"] = 0  # type: ignore[index]
    snapshot["evolution"]["chatwoot_enabled"] = True  # type: ignore[index]
    snapshot["bridge"].update(  # type: ignore[union-attr]
        {
            "inbox_id": 202,
            "provider": "evolution",
            "automated_replies_enabled": True,
            "dispatcher_enabled": True,
            "outbound_enabled": True,
            "shadow_enabled": True,
            "resolution_worker_enabled": True,
        }
    )

    result = _run(snapshot)

    assert result.returncode == 1
    body = json.loads(result.stdout)
    assert body["status"] == "blocked"
    assert body["safe_for_controlled_inbound"] is False
    assert body["blockers"] == [
        "bridge_automated_replies_not_off",
        "bridge_dispatcher_not_off",
        "bridge_outbound_not_off",
        "bridge_provider_mismatch",
        "bridge_resolution_worker_not_off",
        "bridge_scope_mismatch",
        "bridge_shadow_not_off",
        "evolution_chatwoot_integration_enabled",
        "human_handoff_team_missing",
    ]
    assert "must-never-be-printed" not in result.stdout
    assert result.stderr == ""


def test_readiness_probe_rejects_malformed_input_without_echoing_it() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--expected-account-id",
            "101",
            "--expected-waba-inbox-id",
            "303",
            "--expected-legacy-inbox-id",
            "202",
        ],
        input='{"secret":"must-never-be-printed"}',
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout) == {
        "error": "invalid_snapshot",
        "status": "error",
    }
    assert "must-never-be-printed" not in result.stdout
    assert result.stderr == ""


def test_readiness_probe_rejects_boolean_ids_that_compare_equal_to_one() -> None:
    snapshot = _snapshot()
    chatwoot = snapshot["chatwoot"]
    bridge = snapshot["bridge"]
    assert isinstance(chatwoot, dict)
    assert isinstance(bridge, dict)
    inboxes = chatwoot["inboxes"]
    assert isinstance(inboxes, list)
    assert isinstance(inboxes[0], dict)
    assert isinstance(inboxes[1], dict)
    chatwoot["account_id"] = True
    inboxes[0]["id"] = 2
    inboxes[1]["id"] = True
    bridge["account_id"] = True
    bridge["inbox_id"] = True

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--expected-account-id",
            "1",
            "--expected-waba-inbox-id",
            "1",
            "--expected-legacy-inbox-id",
            "2",
        ],
        input=json.dumps(snapshot),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    blockers = json.loads(result.stdout)["blockers"]
    assert "chatwoot_account_mismatch" in blockers
    assert "waba_inbox_missing_or_ambiguous" in blockers
    assert "bridge_scope_mismatch" in blockers
    assert result.stderr == ""


@pytest.mark.parametrize(
    "arguments",
    [
        [
            "--expected-account-id",
            "sensitive-account",
            "--expected-waba-inbox-id",
            "303",
            "--expected-legacy-inbox-id",
            "202",
        ],
        [
            "--expected-account-id",
            "101",
            "--expected-waba-inbox-id",
            "303",
        ],
    ],
)
def test_readiness_probe_sanitizes_cli_argument_errors(
    arguments: list[str],
) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        input=json.dumps(_snapshot()),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout) == {
        "error": "invalid_snapshot",
        "status": "error",
    }
    assert "sensitive-account" not in result.stdout
    assert "sensitive-account" not in result.stderr
    assert result.stderr == ""


_OFF_STATE_GATES = [
    ("automated_replies_enabled", "bridge_automated_replies_not_off"),
    ("dispatcher_enabled", "bridge_dispatcher_not_off"),
    ("outbound_enabled", "bridge_outbound_not_off"),
    ("pilot_boundary_enabled", "bridge_pilot_boundary_not_off"),
    ("shadow_enabled", "bridge_shadow_not_off"),
    ("resolution_worker_enabled", "bridge_resolution_worker_not_off"),
    ("hotmart_purchase_worker_enabled", "bridge_hotmart_purchase_worker_not_off"),
    ("reply_splitter_enabled", "bridge_reply_splitter_not_off"),
    ("durable_opt_out_enabled", "bridge_durable_opt_out_not_off"),
    ("opt_out_projection_enabled", "bridge_opt_out_projection_not_off"),
    ("human_handoff_admission_enabled", "bridge_handoff_admission_not_off"),
    ("human_handoff_projection_enabled", "bridge_handoff_projection_not_off"),
]


@pytest.mark.parametrize(("field", "blocker"), _OFF_STATE_GATES)
@pytest.mark.parametrize("case", ["enabled", "missing"])
def test_readiness_probe_independently_blocks_each_effect_switch(
    field: str,
    blocker: str,
    case: str,
) -> None:
    snapshot = _snapshot()
    bridge = snapshot["bridge"]
    assert isinstance(bridge, dict)
    if case == "missing":
        bridge.pop(field)
    else:
        bridge[field] = True

    result = _run(snapshot)

    assert result.returncode == 1
    assert blocker in json.loads(result.stdout)["blockers"]
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("field", "blocker"),
    [
        ("opt_out_projection_backlog_zero", "opt_out_projection_backlog_not_zero"),
        (
            "human_handoff_projection_backlog_zero",
            "handoff_projection_backlog_not_zero",
        ),
    ],
)
@pytest.mark.parametrize("case", ["nonzero", "missing"])
def test_readiness_probe_independently_blocks_each_projection_backlog(
    field: str,
    blocker: str,
    case: str,
) -> None:
    snapshot = _snapshot()
    bridge = snapshot["bridge"]
    assert isinstance(bridge, dict)
    if case == "missing":
        bridge.pop(field)
    else:
        bridge[field] = False

    result = _run(snapshot)

    assert result.returncode == 1
    assert blocker in json.loads(result.stdout)["blockers"]
    assert result.stderr == ""
