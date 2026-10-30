"""El btrim del nombre del producto en el pago rechazado (20261025000200, E-087), leido como texto.

El comportamiento lo prueba tests/sql/followup_engine/validate_payment_failure_product_name_btrim.mjs
sobre PGlite, con el pago rechazado capturado de ATT1 y el nombre capturado de Maribel.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261025000200_payment_failure_product_name_btrim.sql"
FIXTURE = ROOT / "tests" / "fixtures" / "hotmart_product_name_trailing_space_maribel_20261025.json"


def test_it_is_one_transaction_and_the_last() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))

    assert "\nbegin;\n" in sql and sql.rstrip().endswith("commit;")
    # Despues va la lectura del link para la nota al derivar (20261025000300, F-034 D8).
    # Y despues, las conversaciones del comprador para pausar al agente (20261025000400, E-100).
    # Y despues, la forma nativa con las variables del paso (20261026000100, E-115).
    # Y despues, la oferta del evento en el caso nativo (20261026000200, E-122).
    # Y despues, la pagina de aprobacion (20261028000100, A′ PR 2).
    # Y despues, el carrito nativo (20261028000200, E-161).
    # Despues, sumar ofertas a un scope publicado (20261028000300, E-163).
    # Despues, la compra con nuestro link cuenta siempre (20261028000400, E-169).
    # El doble caso del carrito (20261030000100, E-204): la admision nativa y el planificador del carrito.
    assert names[-1] == "20261030000100_flow_engine_native_legacy_double_case_v1.sql"
    assert names[-2] == "20261028000400_purchase_by_our_link_counts_v1.sql"
    assert names[-3] == "20261028000300_pilot_scope_offer_append_v1.sql"
    assert names[-4] == "20261028000200_flow_engine_native_cart_v1.sql"
    assert names[-5] == "20261028000100_flow_change_slack_approver_v1.sql"
    assert names[-6] == "20261026000200_flow_engine_native_offer_in_context_v1.sql"
    assert names[-7] == "20261026000100_flow_engine_native_template_shape_v1.sql"
    assert names[-8] == "20261025000400_purchase_agent_pause_v1.sql"
    assert names[-9] == "20261025000300_handoff_link_note_v1.sql"
    assert names[-10] == MIGRATION.name
    assert names[-11] == "20261025000100_automation_expert_native_reads_v1.sql"


def test_it_patches_only_the_product_name_comparison() -> None:
    code = re.sub(r"--[^\n]*", "", MIGRATION.read_text(encoding="utf-8"))

    assert re.findall(r"public\.(\w+)\(", code) == ["admit_portable_hotmart_payment_failure"]
    assert "create or replace function" not in code.lower()
    assert "grant " not in code.lower() and "revoke " not in code.lower()
    assert (
        "or p_payload #>> ''{data,product,name}'' is distinct from v_binding.product_name\\n"
        in code
    )
    assert (
        "or btrim(p_payload #>> ''{data,product,name}'') is distinct from btrim(v_binding.product_name)\\n"
        in code
    )


def test_the_replaced_line_is_in_the_live_definition() -> None:
    live = (MIGRATIONS / "20261001000100_whatsapp_phone_equivalence.sql").read_text(encoding="utf-8")
    later = [
        path.name
        for path in sorted(MIGRATIONS.glob("*.sql"))
        if "20261001000100" < path.name.split("_", 1)[0] < "20261025000200"
        and "function public.admit_portable_hotmart_payment_failure("
        in path.read_text(encoding="utf-8")
    ]

    assert later == []
    assert live.count(
        "       or p_payload #>> '{data,product,name}' is distinct from v_binding.product_name\n"
    ) == 1


def test_the_fixture_is_the_captured_name_with_the_trailing_space() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cart = [event for event in fixture["events"] if event["event"] == "PURCHASE_OUT_OF_SHOPPING_CART"]

    assert fixture["_capture"]["captured_at"].startswith("2026-10-25")
    assert cart and all(event["product"]["name"] == "El Protocolo Biohacking + " for event in cart)
    assert fixture["binding"]["product_name"] == "El Protocolo Biohacking +"


def test_the_validator_is_in_the_npm_chain() -> None:
    package = json.loads((ROOT / "tests/sql/followup_engine/package.json").read_text(encoding="utf-8"))

    assert package["scripts"]["test"].rstrip().endswith(
        "node validate_payment_failure_product_name_btrim.mjs && node validate_handoff_link_note.mjs && node validate_purchase_agent_pause.mjs"
        " && node validate_flow_engine_native_template_shape.mjs"
        " && node validate_flow_engine_native_offer_in_context.mjs --check"
        " && node validate_flow_change_slack_approver.mjs"
        " && node validate_flow_engine_native_cart.mjs --check"
        " && node validate_pilot_scope_offer_append.mjs"
        " && node validate_purchase_by_our_link_counts.mjs"
        " && node validate_flow_engine_native_legacy_double_case.mjs --check")
