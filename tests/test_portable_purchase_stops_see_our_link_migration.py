"""Los frenos portables de la compra ven nuestro link (20261030000400, E-215), leidos como texto.

El comportamiento lo prueba el caso 14 de tests/sql/followup_engine/validate_att1_portable_chain.mjs
sobre PGlite: quien compro con nuestro link y vuelve a llenar el formulario no recibe el primer
contacto, y el carrito de quien pago el link de otro se cancela antes del toque. Sin esta migracion,
el primer contacto se planifica y el carrito se ejecuta.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261030000400_portable_purchase_stops_see_our_link_v1.sql"
VALIDATOR = ROOT / "tests" / "sql" / "followup_engine" / "validate_att1_portable_chain.mjs"
HELPER = "public._portable_checkout_link_purchase_exists(text,text,uuid,text,text)"


def _code() -> str:
    return re.sub(r"--[^\n]*", "", MIGRATION.read_text(encoding="utf-8"))


def test_it_is_one_transaction_and_the_last() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))

    assert sql.lstrip().startswith("-- Migration:")
    assert "\nbegin;\n" in sql and sql.rstrip().endswith("commit;")
    # Despues va el seguimiento a 24 h nativo (20261101000200, E-251).
    # Los frenos del seguimiento despues de Z y con una ficha vigente (20261101000300, E-263).
    assert names[-1] == "20261101000300_followup_stops_after_last_touch_and_pending_payment_v1.sql"
    # Antes, el seguimiento a 24 h nativo (20261101000200, E-251).
    assert names[-2] == "20261101000200_flow_engine_native_silence_v1.sql"
    # Antes, el primer contacto nativo (20261101000100, E-247): el disparador intencion.
    assert names[-3] == "20261101000100_flow_engine_native_intent_v1.sql"
    assert names[-4] == MIGRATION.name
    assert names[-5] == "20261030000300_flow_change_list_v1.sql"


def test_it_creates_only_the_third_source_and_patches_the_two_stops() -> None:
    code = _code()

    assert re.findall(r"create function public\.(\w+)\(", code) == [
        "_portable_checkout_link_purchase_exists"
    ]
    assert "create or replace function" not in code.lower()
    assert "grant " not in code.lower()
    assert "alter table" not in code.lower() and "insert into" not in code.lower()
    # El unico indice: las emisiones compradas (E-217, H2).
    assert re.findall(r"create index (\w+)", code) == ["checkout_link_issuances_purchase_matched_idx"]
    assert (
        "on public.checkout_link_issuances (purchase_intent_id)\n    where status = 'purchase_matched';"
        in code
    )
    assert "update public." not in code.lower() and "delete from" not in code.lower()
    for signature in (
        "public._portable_precheckout_stop_reason(uuid,uuid)",
        "public._portable_case_purchase_stop_reason(uuid)",
    ):
        assert f"'{signature}'" in code
    # Correrla dos veces aborta, y cada reemplazo es contado y con marca.
    assert "portable_checkout_link_purchase_stop_already_applied" in code
    assert "unexpected_portable_purchase_stop_definition" in code
    assert "portable_purchase_stop_not_replaced" in code


def test_the_third_source_is_the_matched_issuance_of_the_same_product_and_person() -> None:
    code = _code()
    body = code.split("create function public._portable_checkout_link_purchase_exists(", 1)[1]
    body = body.split("$function$;", 1)[0]

    assert "language sql" in body and "stable" in body and "security invoker" in body
    assert "issuance.status = 'purchase_matched'" in body
    assert "intent.tenant_ref = p_tenant_ref" in body
    assert "intent.funnel_ref = p_funnel_ref" in body
    # El comprador que informo Hotmart y la intencion de la emision.
    assert "_hotmart_purchase_intent_payload_identity(" in body
    assert "(buyer.normalized_email, buyer.normalized_phone)" in body
    assert "(intent.normalized_email, intent.normalized_phone)" in body
    # D2: el contacto de la emision frena aunque haya pagado otra persona.
    assert "issuance.contact_id = p_contact_id" in body
    # Las identidades de la persona se arman una sola vez (E-217, H2).
    assert "with me as (" in body
    assert "person.email = any(me.emails)" in body and "person.phone = any(me.phones)" in body
    assert body.count("_whatsapp_phone_variants(") == 2
    # Ningun rol de la API, ni service_role.
    assert "array['anon', 'authenticated', 'service_role']" in code
    assert f"revoke all on function {HELPER} from public" in code


def test_the_stops_keep_their_reasons() -> None:
    code = _code()

    assert code.count("        return ''purchase_by_identity'';\\n") == 2
    assert code.count("        return ''purchase_detected_any_offer'';\\n") == 1
    assert "E'\\n    return null;\\nend;\\n'" in code


def test_the_anchors_are_in_the_live_definitions_once() -> None:
    precheckout = (MIGRATIONS / "20261001000200_portable_precheckout_first_contact.sql").read_text(
        encoding="utf-8"
    )
    case_stop = (MIGRATIONS / "20261019000300_portable_purchase_stops_any_offer_v1.sql").read_text(
        encoding="utf-8"
    )
    later = [
        path.name
        for path in sorted(MIGRATIONS.glob("*.sql"))
        if "20261001000200" < path.name.split("_", 1)[0] < "20261030000400"
        and (
            "function public._portable_precheckout_stop_reason(" in path.read_text(encoding="utf-8")
            or "function public._portable_case_purchase_stop_reason(" in path.read_text(encoding="utf-8")
        )
    ]

    assert later == ["20261019000300_portable_purchase_stops_any_offer_v1.sql"]
    stop_body = precheckout.split("function public._portable_precheckout_stop_reason(", 1)[1]
    stop_body = stop_body.split("$function$;", 1)[0]
    assert stop_body.count("        return 'purchase_by_identity';\n    end if;\n") == 1
    case_body = case_stop.split("function public._portable_case_purchase_stop_reason(", 1)[1]
    case_body = case_body.split("$function$;", 1)[0]
    assert case_body.count("\n    return null;\nend;\n") == 1


def test_the_chain_validator_proves_the_two_stops() -> None:
    validator = VALIDATOR.read_text(encoding="utf-8")

    assert "20261030000400_portable_purchase_stops_see_our_link_v1.sql" in validator
    assert "refillPlan.result !== 'not_planned:purchase_by_identity'" in validator
    assert "payerRun.decision.reason_code !== 'purchase_detected_any_offer'" in validator
    assert "55000 portable_checkout_link_purchase_stop_already_applied" in validator
    # D2 escrita como asercion, aislada con la funcion sin la rama del contacto.
    assert "owner_of_link_paid_by_other: true," in validator
    assert "ownerOfLinkPaidByOther.without_the_branch !== false" in validator
    assert "buyer_in_another_tenant: false," in validator
