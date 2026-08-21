"""Focused PostgreSQL 17 behavior probe for the configurable Hotmart timer."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "tests/sql/followup_engine/real_postgres_hotmart_intent_correlation.py"
spec = importlib.util.spec_from_file_location("timer_correlation_helpers", SOURCE)
if spec is None or spec.loader is None:
    raise RuntimeError("correlation helper import failed")
helpers = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helpers
spec.loader.exec_module(helpers)

if os.environ.get("PRIVATE_PGHOST"):
    _original_pg_env = getattr(helpers, "pg_env")

    def _private_pg_env() -> dict[str, str]:
        env = _original_pg_env()
        env["PGHOST"] = os.environ["PRIVATE_PGHOST"]
        return env

    setattr(helpers, "pg_env", _private_pg_env)


def q(sql: str, *, expect_failure: bool = False) -> str:
    return helpers.query(sql, expect_failure=expect_failure)


def require(condition: bool, message: str) -> None:
    helpers.require(condition, message)


def policy(key: str, version: int, delay: str) -> None:
    q(
        "insert into public.followup_policy_versions ("
        "policy_key,version,status,purpose,timezone,business_windows,"
        "grace_period,expires_after,max_automatic_messages,steps,"
        "approved_by,approved_at,published_at) values ("
        f"{key!r},{version},'published','cart_recovery','UTC','{{}}'::jsonb,"
        f"interval {delay!r},interval '7 days',1,'[]'::jsonb,"
        "'timer-probe',clock_timestamp(),clock_timestamp())"
    )


def insert_binding(
    *, policy_key: str, product_ref: str | None, offer_ref: str | None, enabled: bool
) -> str:
    product = "null" if product_ref is None else repr(product_ref)
    offer = "null" if offer_ref is None else repr(offer_ref)
    return q(
        "insert into public.hotmart_abandonment_timer_policy_bindings ("
        "tenant_ref,funnel_ref,product_ref,offer_ref,enabled,policy_key,policy_version"
        ") values ("
        f"'lancemos','psicologajohanna',{product},{offer},{str(enabled).lower()},"
        f"{policy_key!r},1) returning id"
    ).splitlines()[-1]


def timer_for(event_id: str) -> str:
    return q(
        "select id||'|'||delay_seconds_snapshot||'|'||"
        "extract(epoch from (due_at-observed_at))::int||'|'||status||'|'||"
        "coalesce(outcome,'')||'|'||policy_binding_generation "
        "from public.hotmart_abandonment_reevaluations "
        f"where source_webhook_event_id={event_id!r}::uuid"
    )


def admit_cart(tag: str, email: str, phone: str) -> tuple[str, str]:
    intent = helpers.insert_intent(email, phone)
    payload = helpers.cart_payload(tag, email=email, phone=f"+{phone}")
    event = helpers.insert_event(payload)
    return intent, event


def admit_purchase(tag: str, transaction: str, email: str, phone: str) -> str:
    payload = helpers.purchase_payload(
        tag, transaction, email=email, phone=f"+{phone}"
    )
    return helpers.insert_event(payload)


def main() -> None:
    require(
        os.environ.get("ALLOW_HOTMART_TIMER_PROBE") == "hotmart-abandonment-timer",
        "timer probe confirmation required",
    )
    require(
        q("select current_database()").startswith("hotmart_intent_correlation"),
        "unexpected database",
    )

    baseline_effects = q(
        "select (select count(*) from public.scheduled_actions)||'|'||"
        "(select count(*) from public.followup_delivery_attempts)"
    )

    policy("timer-default-10m", 1, "10 minutes")
    policy("timer-override-20m", 1, "20 minutes")
    policy("timer-invalid-30s", 1, "30 seconds")

    q(
        "insert into public.hotmart_abandonment_timer_policy_bindings ("
        "tenant_ref,funnel_ref,enabled,policy_key,policy_version) values ("
        "'invalid','producer',true,'timer-invalid-30s',1)",
        expect_failure=True,
    )
    require(
        q(
            "select count(*) from public.hotmart_abandonment_timer_policy_bindings "
            "where tenant_ref='invalid' and funnel_ref='producer'"
        )
        == "0",
        "invalid delay binding left durable residue",
    )

    default_binding = insert_binding(
        policy_key="timer-default-10m",
        product_ref=None,
        offer_ref=None,
        enabled=True,
    )

    first_email = "timer-first@example.test"
    first_phone = "573001211001"
    first_intent, first_event = admit_cart(
        "timer-cart-first-001", first_email, first_phone
    )
    first_timer = timer_for(first_event)
    first_parts = first_timer.split("|")
    require(first_parts[1:4] == ["600", "600", "scheduled"], first_timer)
    first_timer_id = first_parts[0]
    first_due = q(
        "select due_at from public.hotmart_abandonment_reevaluations "
        f"where id={first_timer_id!r}::uuid"
    )

    replay_payload = helpers.cart_payload(
        "timer-cart-first-001", email=first_email, phone=f"+{first_phone}"
    )
    helpers.insert_event(replay_payload)
    require(
        q(
            "select count(*)||'|'||min(id::text)||'|'||max(id::text) from "
            "public.hotmart_abandonment_reevaluations where purchase_intent_id="
            f"{first_intent!r}::uuid"
        )
        == f"1|{first_timer_id}|{first_timer_id}",
        "cart replay duplicated timer",
    )

    exact_binding = insert_binding(
        policy_key="timer-override-20m",
        product_ref="F106691755G",
        offer_ref="bxjge6zq",
        enabled=True,
    )
    second_email = "timer-second@example.test"
    second_phone = "573001211002"
    second_intent, second_event = admit_cart(
        "timer-cart-second-001", second_email, second_phone
    )
    second_timer = timer_for(second_event)
    second_parts = second_timer.split("|")
    require(second_parts[1:4] == ["1200", "1200", "scheduled"], second_timer)

    q(
        "update public.hotmart_abandonment_timer_policy_bindings set "
        "enabled=false,generation=generation+1 "
        f"where id={exact_binding!r}::uuid"
    )
    disabled_email = "timer-disabled@example.test"
    disabled_phone = "573001211003"
    disabled_intent, disabled_event = admit_cart(
        "timer-cart-disabled-001", disabled_email, disabled_phone
    )
    require(
        q(
            "select count(*) from public.hotmart_abandonment_reevaluations "
            f"where purchase_intent_id={disabled_intent!r}::uuid"
        )
        == "0",
        "disabled specific override fell back to producer default",
    )

    q(
        "set role service_role; select count(*) from "
        "public.hotmart_abandonment_reevaluations; reset role",
        expect_failure=True,
    )
    due_before = q(
        "set role service_role; select count(*) from "
        "public.list_due_hotmart_abandonment_reevaluations("
        f"({first_due!r}::timestamptz - interval '1 second'),100); reset role"
    ).splitlines()[-1]
    require(due_before == "0", "timer appeared before due_at")
    due_at = q(
        "set role service_role; select count(*) from "
        "public.list_due_hotmart_abandonment_reevaluations("
        f"{first_due!r}::timestamptz,100) where reevaluation_id="
        f"{first_timer_id!r}::uuid; reset role"
    ).splitlines()[-1]
    require(due_at == "1", "timer missing at due_at")

    blocked = q(
        "set role service_role; select reevaluation_status||'|'||"
        "reevaluation_outcome||'|'||replayed from "
        "public.reevaluate_hotmart_abandonment_timer("
        f"{first_timer_id!r}::uuid,{first_due!r}::timestamptz); reset role"
    ).splitlines()[-1]
    require(blocked == "completed|blocked_not_authorized|false", blocked)
    replayed = q(
        "set role service_role; select reevaluation_status||'|'||"
        "reevaluation_outcome||'|'||replayed from "
        "public.reevaluate_hotmart_abandonment_timer("
        f"{first_timer_id!r}::uuid,{first_due!r}::timestamptz); reset role"
    ).splitlines()[-1]
    require(replayed == "completed|blocked_not_authorized|true", replayed)

    admit_purchase(
        "timer-purchase-first-001",
        "HPTIMERFIRST001",
        first_email,
        first_phone,
    )
    require(
        q(
            "select lifecycle_state from public.purchase_intents where id="
            f"{first_intent!r}::uuid"
        )
        == "purchased",
        "purchase did not supersede intent",
    )
    require(
        q(
            "select outcome from public.hotmart_abandonment_reevaluations "
            f"where id={first_timer_id!r}::uuid"
        )
        == "cancelled_purchased",
        "purchase did not supersede blocked timer",
    )

    admit_purchase(
        "timer-purchase-second-001",
        "HPTIMERSECOND01",
        second_email,
        second_phone,
    )
    require(
        q(
            "select status||'|'||outcome from public.hotmart_abandonment_reevaluations "
            f"where source_webhook_event_id={second_event!r}::uuid"
        )
        == "completed|cancelled_purchased",
        "purchase before due did not cancel timer",
    )

    q(
        "update public.hotmart_abandonment_timer_policy_bindings set "
        "enabled=true,generation=generation+1 "
        f"where id={exact_binding!r}::uuid"
    )
    authorized_email = "timer-authorized@example.test"
    authorized_phone = "573001211004"
    authorized_intent, authorized_event = admit_cart(
        "timer-cart-authorized-001", authorized_email, authorized_phone
    )
    authorized_timer = timer_for(authorized_event).split("|")[0]
    authorized_due = q(
        "select due_at from public.hotmart_abandonment_reevaluations where id="
        f"{authorized_timer!r}::uuid"
    )
    q(
        "update public.purchase_intents set activation_authorized=true,"
        "whatsapp_contact_authorized=true where id="
        f"{authorized_intent!r}::uuid"
    )
    missing_binding = q(
        "set role service_role; select reevaluation_outcome from "
        "public.reevaluate_hotmart_abandonment_timer("
        f"{authorized_timer!r}::uuid,{authorized_due!r}::timestamptz); reset role"
    ).splitlines()[-1]
    require(
        missing_binding == "blocked_contact_binding_missing",
        "authorized flags bypassed canonical contact binding",
    )

    require(
        q(
            "select delay_seconds_snapshot from public.hotmart_abandonment_reevaluations "
            f"where id={first_timer_id!r}::uuid"
        )
        == "600",
        "existing timer snapshot changed after policy override",
    )
    require(
        q(
            "select count(*) from public.hotmart_abandonment_reevaluation_events "
            f"where reevaluation_id={first_timer_id!r}::uuid"
        )
        == "3",
        "timer transition audit is incomplete",
    )
    q(
        "update public.hotmart_abandonment_reevaluation_events set "
        "reason_code='tampered' where reevaluation_id="
        f"{first_timer_id!r}::uuid",
        expect_failure=True,
    )
    require(
        q(
            "select count(*) from public.hotmart_abandonment_reevaluation_events "
            f"where reevaluation_id={first_timer_id!r}::uuid "
            "and reason_code='tampered'"
        )
        == "0",
        "timer transition audit was mutable",
    )

    final_effects = q(
        "select (select count(*) from public.scheduled_actions)||'|'||"
        "(select count(*) from public.followup_delivery_attempts)"
    )
    require(final_effects == baseline_effects, "timer created commercial effects")
    require(
        q(
            "select generation||'|'||enabled from "
            "public.hotmart_abandonment_timer_policy_bindings where id="
            f"{default_binding!r}::uuid"
        )
        == "1|true",
        "producer default binding changed unexpectedly",
    )

    print("HOTMART_ABANDONMENT_TIMER_REAL_POSTGRES_OK")


if __name__ == "__main__":
    main()
