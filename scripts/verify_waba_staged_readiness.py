#!/usr/bin/env python3
"""Evaluate sanitized WABA readiness as three independent, fail-closed levels."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, NoReturn

LEVELS = (
    "ready_for_observational_inbound",
    "ready_for_controlled_template",
    "ready_for_supervised_pilot",
)


def _object(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _require_true(section: dict[str, Any] | None, field: str, reason: str, reasons: set[str]) -> None:
    if section is None or section.get(field) is not True:
        reasons.add(reason)


def _require_false(section: dict[str, Any] | None, field: str, reason: str, reasons: set[str]) -> None:
    if section is None or section.get(field) is not False:
        reasons.add(reason)


def evaluate_snapshot(
    snapshot: object,
    *,
    expected_account_id: int,
    expected_inbox_id: int,
    previous_inbox_id: int,
) -> dict[str, object]:
    if (
        not _positive_int(expected_account_id)
        or not _positive_int(expected_inbox_id)
        or not _positive_int(previous_inbox_id)
        or expected_inbox_id == previous_inbox_id
    ):
        raise ValueError("invalid expected scope")
    root = _object(snapshot)
    if root is None:
        raise ValueError("snapshot must be an object")

    channel = _object(root.get("channel"))
    runtime = _object(root.get("runtime"))
    template = _object(root.get("template"))
    control = _object(root.get("controlled_template"))
    pilot = _object(root.get("supervised_pilot"))
    evidence = _object(root.get("evidence"))

    inbound: set[str] = set()
    if channel is None:
        inbound.add("channel_snapshot_missing")
    else:
        if not _positive_int(channel.get("account_id")) or channel.get("account_id") != expected_account_id:
            inbound.add("active_account_mismatch")
        if not _positive_int(channel.get("inbox_id")) or channel.get("inbox_id") != expected_inbox_id:
            inbound.add("active_inbox_mismatch")
        if not _positive_int(channel.get("previous_inbox_id")) or channel.get("previous_inbox_id") != previous_inbox_id:
            inbound.add("previous_inbox_mismatch")
    for field, reason in (
        ("portfolio_bound", "portfolio_binding_unverified"),
        ("waba_connected", "waba_connection_unverified"),
        ("phone_number_bound", "phone_number_binding_unverified"),
        ("phone_number_id_bound", "phone_number_id_binding_unverified"),
        ("official_inbox_verified", "official_inbox_unverified"),
        ("shared_webhook_verified", "shared_webhook_unverified"),
        ("exact_scope_verified", "exact_scope_unverified"),
        ("previous_inbox_rejected", "previous_inbox_still_admissible"),
        ("evolution_out_of_scope", "evolution_scope_not_isolated"),
    ):
        _require_true(channel, field, reason, inbound)
    for field, reason in (
        ("automated_replies_enabled", "automated_replies_not_off"),
        ("reply_splitter_enabled", "reply_splitter_not_off"),
        ("shadow_enabled", "shadow_not_off"),
        ("resolution_worker_enabled", "resolution_worker_not_off"),
        ("purchase_worker_enabled", "purchase_worker_not_off"),
        ("human_pause_enabled", "human_pause_not_off"),
        ("durable_opt_out_enabled", "durable_opt_out_not_off"),
        ("opt_out_projection_enabled", "opt_out_projection_not_off"),
        ("handoff_admission_enabled", "handoff_admission_not_off"),
        ("handoff_projection_enabled", "handoff_projection_not_off"),
        ("dispatcher_enabled", "dispatcher_not_off"),
        ("outbound_enabled", "outbound_not_off"),
        ("pilot_boundary_enabled", "pilot_boundary_not_off"),
    ):
        _require_false(runtime, field, reason, inbound)
    for field, reason in (
        ("commit_digest_present", "commit_digest_missing"),
        ("configuration_digest_present", "configuration_digest_missing"),
        ("observed_at_present", "observation_timestamp_missing"),
        ("zero_external_effects", "zero_effects_unverified"),
    ):
        _require_true(evidence, field, reason, inbound)

    controlled = set(inbound)
    for field, reason in (
        ("payment_method_operational", "payment_method_not_operational"),
        ("recipient_allowlisted", "recipient_not_allowlisted"),
        ("one_send_budget", "one_send_budget_unverified"),
        ("eligible_backlog_zero", "eligible_backlog_not_zero"),
        ("rollback_ready", "controlled_template_rollback_missing"),
        ("provider_mode_compatible", "provider_mode_incompatible"),
    ):
        _require_true(control, field, reason, controlled)
    for field, reason in (
        ("selection_unambiguous", "template_selection_ambiguous"),
        ("first_touch_meta_approved", "first_touch_meta_not_approved"),
        ("first_touch_business_approved", "first_touch_business_not_approved"),
        ("followup_meta_approved", "followup_meta_not_approved"),
        ("followup_business_approved", "followup_business_not_approved"),
        ("names_present", "template_names_missing"),
        ("language_present", "template_language_missing"),
        ("category_present", "template_category_missing"),
        ("category_runtime_supported", "template_category_runtime_unsupported"),
        ("body_placeholder_one_exact", "template_placeholder_schema_mismatch"),
        ("pair_runtime_compatible", "template_pair_runtime_mismatch"),
    ):
        _require_true(template, field, reason, controlled)

    supervised = set(controlled)
    for field, reason in (
        ("durable_scope_published", "durable_scope_not_published"),
        ("durable_scope_inactive", "durable_scope_not_inactive"),
        ("remote_schema_verified", "remote_schema_unverified"),
        ("purchase_stop_ready", "purchase_stop_not_ready"),
        ("opt_out_stop_ready", "opt_out_stop_not_ready"),
        ("policy_approved", "policy_not_approved"),
        ("conversation_release_approved", "conversation_release_not_approved"),
        ("monitoring_ready", "monitoring_not_ready"),
        ("cohort_bounded", "cohort_not_bounded"),
        ("pilot_budget_bounded", "pilot_budget_not_bounded"),
        ("kill_switch_owned", "kill_switch_owner_missing"),
    ):
        _require_true(pilot, field, reason, supervised)
    handoff_enabled = pilot.get("handoff_enabled") if pilot is not None else None
    if handoff_enabled is not True and handoff_enabled is not False:
        supervised.add("handoff_enabled_invalid")
    elif handoff_enabled is True:
        _require_true(pilot, "handoff_owner_ready", "handoff_owner_missing", supervised)

    reason_sets = (inbound, controlled, supervised)
    levels: dict[str, object] = {}
    highest_ready: str | None = None
    for name, reasons in zip(LEVELS, reason_sets, strict=True):
        ordered = sorted(reasons)
        ready = not ordered
        levels[name] = {
            "ready": ready,
            "reasons": ordered,
            "status": "ready" if ready else "blocked",
        }
        if ready:
            highest_ready = name

    return {"highest_ready_level": highest_ready, "levels": levels, "status": "ready" if highest_ready else "blocked"}


class _SanitizedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError("invalid arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _SanitizedArgumentParser(description="Evaluate a sanitized staged WABA readiness snapshot from stdin.")
    parser.add_argument("--expected-account-id", type=int, required=True)
    parser.add_argument("--expected-inbox-id", type=int, required=True)
    parser.add_argument("--previous-inbox-id", type=int, required=True)
    return parser


def main() -> int:
    try:
        args = _parser().parse_args()
        result = evaluate_snapshot(
            json.load(sys.stdin),
            expected_account_id=args.expected_account_id,
            expected_inbox_id=args.expected_inbox_id,
            previous_inbox_id=args.previous_inbox_id,
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        print(json.dumps({"error": "invalid_snapshot", "status": "error"}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["highest_ready_level"] is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
