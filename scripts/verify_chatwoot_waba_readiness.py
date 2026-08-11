#!/usr/bin/env python3
"""Validate a sanitized WABA control-plane snapshot without echoing identifiers."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, NoReturn


def _object(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _matching_inbox(
    inboxes: object, *, expected_id: int
) -> dict[str, Any] | None:
    if not isinstance(inboxes, list):
        return None
    matches = [
        inbox
        for inbox in inboxes
        if isinstance(inbox, dict)
        and _positive_int(inbox.get("id"))
        and inbox.get("id") == expected_id
    ]
    return matches[0] if len(matches) == 1 else None


def evaluate_snapshot(
    snapshot: object,
    *,
    expected_account_id: int,
    expected_waba_inbox_id: int,
    expected_legacy_inbox_id: int,
) -> dict[str, object]:
    if (
        not _positive_int(expected_account_id)
        or not _positive_int(expected_waba_inbox_id)
        or not _positive_int(expected_legacy_inbox_id)
        or expected_waba_inbox_id == expected_legacy_inbox_id
    ):
        raise ValueError("invalid expected scope")

    root = _object(snapshot)
    if root is None:
        raise ValueError("snapshot must be an object")
    chatwoot = _object(root.get("chatwoot"))
    evolution = _object(root.get("evolution"))
    bridge = _object(root.get("bridge"))
    if chatwoot is None or evolution is None or bridge is None:
        raise ValueError("snapshot sections are missing")

    blockers: set[str] = set()
    if (
        not _positive_int(chatwoot.get("account_id"))
        or chatwoot.get("account_id") != expected_account_id
    ):
        blockers.add("chatwoot_account_mismatch")

    inboxes = chatwoot.get("inboxes")
    waba = _matching_inbox(inboxes, expected_id=expected_waba_inbox_id)
    legacy = _matching_inbox(inboxes, expected_id=expected_legacy_inbox_id)
    if waba is None:
        blockers.add("waba_inbox_missing_or_ambiguous")
    else:
        if (
            waba.get("channel_class") != "Channel::Whatsapp"
            or waba.get("provider") != "whatsapp_cloud"
        ):
            blockers.add("waba_channel_identity_mismatch")
        required_presence = (
            "phone_number_present",
            "phone_number_id_present",
            "business_account_id_present",
            "access_token_present",
        )
        if any(waba.get(key) is not True for key in required_presence):
            blockers.add("waba_provider_configuration_incomplete")
        member_count = waba.get("member_count")
        if not _positive_int(member_count):
            blockers.add("waba_inbox_member_missing")

    if legacy is None:
        blockers.add("legacy_inbox_missing_or_ambiguous")
    elif legacy.get("channel_class") != "Channel::Api":
        blockers.add("legacy_inbox_identity_mismatch")

    webhook = _object(chatwoot.get("shared_webhook"))
    if (
        webhook is None
        or webhook.get("https") is not True
        or webhook.get("bridge_path") != "/webhooks/chatwoot"
        or webhook.get("subscriptions") != ["message_created"]
    ):
        blockers.add("shared_chatwoot_webhook_mismatch")

    if not _positive_int(chatwoot.get("teams_count")):
        blockers.add("human_handoff_team_missing")

    if evolution.get("connection_state") not in {"close", "closed"}:
        blockers.add("evolution_transport_not_disconnected")
    if evolution.get("chatwoot_enabled") is not False:
        blockers.add("evolution_chatwoot_integration_enabled")

    if (
        not _positive_int(bridge.get("account_id"))
        or not _positive_int(bridge.get("inbox_id"))
        or bridge.get("account_id") != expected_account_id
        or bridge.get("inbox_id") != expected_waba_inbox_id
    ):
        blockers.add("bridge_scope_mismatch")
    if bridge.get("provider") != "waba":
        blockers.add("bridge_provider_mismatch")
    if bridge.get("automated_replies_enabled") is not False:
        blockers.add("bridge_automated_replies_not_off")
    if bridge.get("dispatcher_enabled") is not False:
        blockers.add("bridge_dispatcher_not_off")
    if bridge.get("outbound_enabled") is not False:
        blockers.add("bridge_outbound_not_off")
    if bridge.get("pilot_boundary_enabled") is not False:
        blockers.add("bridge_pilot_boundary_not_off")
    if bridge.get("shadow_enabled") is not False:
        blockers.add("bridge_shadow_not_off")
    if bridge.get("resolution_worker_enabled") is not False:
        blockers.add("bridge_resolution_worker_not_off")
    if bridge.get("hotmart_purchase_worker_enabled") is not False:
        blockers.add("bridge_hotmart_purchase_worker_not_off")
    if bridge.get("reply_splitter_enabled") is not False:
        blockers.add("bridge_reply_splitter_not_off")
    if bridge.get("durable_opt_out_enabled") is not False:
        blockers.add("bridge_durable_opt_out_not_off")
    if bridge.get("opt_out_projection_enabled") is not False:
        blockers.add("bridge_opt_out_projection_not_off")
    if bridge.get("human_handoff_admission_enabled") is not False:
        blockers.add("bridge_handoff_admission_not_off")
    if bridge.get("human_handoff_projection_enabled") is not False:
        blockers.add("bridge_handoff_projection_not_off")
    if bridge.get("opt_out_projection_backlog_zero") is not True:
        blockers.add("opt_out_projection_backlog_not_zero")
    if bridge.get("human_handoff_projection_backlog_zero") is not True:
        blockers.add("handoff_projection_backlog_not_zero")

    ordered = sorted(blockers)
    ready = not ordered
    return {
        "blockers": ordered,
        "safe_for_controlled_inbound": ready,
        "status": "ready" if ready else "blocked",
    }


class _SanitizedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError("invalid arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _SanitizedArgumentParser(
        description="Validate a sanitized WABA readiness snapshot from stdin."
    )
    parser.add_argument("--expected-account-id", type=int, required=True)
    parser.add_argument("--expected-waba-inbox-id", type=int, required=True)
    parser.add_argument("--expected-legacy-inbox-id", type=int, required=True)
    return parser


def main() -> int:
    try:
        args = _parser().parse_args()
        snapshot = json.load(sys.stdin)
        result = evaluate_snapshot(
            snapshot,
            expected_account_id=args.expected_account_id,
            expected_waba_inbox_id=args.expected_waba_inbox_id,
            expected_legacy_inbox_id=args.expected_legacy_inbox_id,
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        print(json.dumps({"error": "invalid_snapshot", "status": "error"}))
        return 2

    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
