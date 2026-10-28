"""Las tres lecturas que cuentan tambien lo que autoriza el motor (20261022000300).

La migracion parcha por ancla, sobre pg_get_functiondef, la historia de la
simulacion (read_flow_engine_history_v1) y las dos lecturas del AE
(read_automation_expert_flows_v1 y read_automation_expert_lead_history_v1),
con el criterio del tope: una autorizacion del motor cuenta para el scope del
legado cuando su pool es ese scope_key. Estas pruebas fijan la forma; el
comportamiento (sin filas del motor da lo mismo que antes, con una de cada lado
cuenta las dos, y la version de antes no ve la del motor) lo verifica
validate_flow_engine_reads_see_motor_v1.mjs en PGlite, sobre la cadena entera.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from bridge import flow_capture

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261022000300_flow_engine_reads_see_motor_v1.sql"
FIXTURE = ROOT / "tests" / "fixtures" / "flow_engine" / "motor_authorization_reads.json"
PACKAGE = ROOT / "tests" / "sql" / "followup_engine" / "package.json"

# Donde vive la definicion vigente de cada funcion antes de esta migracion.
SOURCES = {
    "public.read_flow_engine_history_v1(jsonb,text,timestamp with time zone)":
        MIGRATIONS / "20261021000200_flow_engine_routes_v1.sql",
    "public.read_automation_expert_flows_v1(jsonb)":
        MIGRATIONS / "20261021000400_automation_expert_flows_speed_v1.sql",
    "public.read_automation_expert_lead_history_v1(jsonb,uuid)":
        MIGRATIONS / "20261015000200_automation_expert_read_v1.sql",
}
PAIR = re.compile(
    r"v_signatures := v_signatures \|\| '([^']+)'::text;\s*"
    r"v_anchors := v_anchors \|\| \$a\$(.*?)\$a\$::text;\s*"
    r"v_replacements := v_replacements \|\| \$r\$(.*?)\$r\$::text;",
    re.S,
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _pairs() -> list[tuple[str, str, str]]:
    return PAIR.findall(_sql())


def test_it_is_one_transaction_and_is_the_last() -> None:
    sql = _sql()
    assert "\nbegin;\n" in sql and sql.rstrip().endswith("commit;")
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    # Despues van el modo «solo guardar» de Hotmart (20261024000100), que crea
    # una funcion nueva y no redefine ninguna de las que esta parcha, y el motor
    # nativo (20261024000200), que parcha la historia por ancla sobre esta
    # version (conserva sus bloques) y no toca las dos lecturas del AE. F-036
    # (20261024000400) solo parcha el link del agente.
    assert names[names.index(MIGRATION.name) + 1:] == [
        "20261024000100_portable_hotmart_listen_only_capture.sql",
        "20261024000200_flow_engine_native_v1.sql",
        "20261024000400_opted_out_reply_checkout_issuance.sql",
        # El bridge del motor nativo (20261024000500) suma lecturas y parcha la
        # adopcion de la respuesta; no redefine el motor del carrito.
        "20261024000500_flow_engine_native_bridge_v1.sql",
        # El AE que crea flujos (20261024000600) parcha el ciclo del almacen (el item
        # nuevo) y read_flow_store_v1; no redefine el motor del carrito.
        "20261024000600_flow_change_new_flow_v1.sql",
        # Las lecturas nativas del AE (20261025000100) suman lecturas y parchan el anclaje del AE y prender.
        "20261025000100_automation_expert_native_reads_v1.sql",
        # El btrim del nombre del producto en el pago rechazado (20261025000200, E-087) solo parcha
        # admit_portable_hotmart_payment_failure.
        "20261025000200_payment_failure_product_name_btrim.sql",
        # La lectura del link para la nota privada al derivar (20261025000300, F-034 D8):
        # una funcion nueva, no toca ninguna existente.
        "20261025000300_handoff_link_note_v1.sql",
        # Las conversaciones del comprador para pausar al agente (20261025000400,
        # E-100): una funcion nueva, no toca ninguna existente.
        "20261025000400_purchase_agent_pause_v1.sql",
        # La forma nativa con las variables del paso (20261026000100, E-115): parcha la
        # forma nativa y el compilador, y no toca las funciones de esta.
        "20261026000100_flow_engine_native_template_shape_v1.sql",
        # La oferta del evento en el caso nativo (20261026000200, E-122): parcha la
        # admision nativa y get_followup_execution_context, y no toca las funciones de esta.
        "20261026000200_flow_engine_native_offer_in_context_v1.sql",
        # La pagina de aprobacion (20261028000100, A′ PR 2): suma su rol, sus
        # sesiones y sus envoltorios, y no toca las funciones de esta.
        "20261028000100_flow_change_slack_approver_v1.sql",
    ]
    native = (MIGRATIONS / "20261024000200_flow_engine_native_v1.sql").read_text(encoding="utf-8")
    assert "read_automation_expert_flows_v1" not in native
    assert "read_automation_expert_lead_history_v1" not in native
    assert names[names.index(MIGRATION.name) - 1] == (
        "20261022000200_flow_engine_bridge_reads_v1.sql"
    )


def test_it_patches_by_anchor_and_keeps_owner_and_grants() -> None:
    code = re.sub(r"--[^\n]*", "", _sql()).lower()
    assert "create function" not in code and "create or replace function" not in code
    assert "grant " not in code and "revoke " not in code and "drop " not in code
    assert "pg_get_functiondef" in code
    for reason in ("flow_engine_reads_owner_mismatch", "flow_engine_reads_acl_changed",
                   "unexpected_flow_engine_reads_definition", "flow_engine_reads_not_replaced"):
        assert reason in _sql()


def test_every_anchor_is_in_the_current_definition_exactly_once() -> None:
    pairs = _pairs()
    assert len(pairs) == 9
    assert {signature for signature, _, _ in pairs} == set(SOURCES)
    for signature, anchor, replacement in pairs:
        source = SOURCES[signature].read_text(encoding="utf-8")
        # La definicion vigente es la de su archivo: ninguna migracion entre
        # ese archivo y esta redefine la funcion.
        assert source.count(anchor) == 1, (signature, anchor[:80])
        assert "flow_engine_reads_see_motor_v1" in replacement
        assert "public.flow_engine_request_authorizations" in replacement
        # El reemplazo conserva lo del legado: solo suma.
        legacy = [line for line in anchor.splitlines() if "pilot_outbound_request_authorizations" in line]
        assert legacy, anchor[:80]


def test_nothing_between_the_sources_and_this_one_redefines_them() -> None:
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    for signature, source in SOURCES.items():
        function = signature.split("(", 1)[0]
        for later in names[names.index(source.name) + 1:names.index(MIGRATION.name)]:
            text = (MIGRATIONS / later).read_text(encoding="utf-8")
            assert f"function {function}(" not in text, (function, later)


def test_it_copies_the_cap_criterion_pool_equals_scope_key() -> None:
    # El tope del legado (20261021000200, flow_engine_shared_pool) y el del
    # motor (20261022000100) suman las del motor con pool = scope_key. Las
    # lecturas usan ese criterio, o la accion del caso.
    for signature, _, replacement in _pairs():
        motor = replacement.split("flow_engine_reads_see_motor_v1", 1)[1]
        assert re.search(r"\.pool = |engine_row\.action_id|engine_row\.attempt_id|m\.pool, ", motor), signature


def test_the_history_row_of_the_motor_keeps_the_capture_shape() -> None:
    # La fila del motor, como la da la cadena real (PGlite), pasa por la forma
    # cerrada de la captura v3: misma lista de claves, con scope_version y
    # runtime_generation en null.
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    row = data["historia_autorizacion_del_motor"]
    problems: list[flow_capture.ShapeProblem] = []
    flow_capture._walk(row, flow_capture.ROWS_V3["autorizaciones"], "autorizaciones[0]", problems)
    assert problems == []
    assert row["scope_version"] is None and row["runtime_generation"] is None
    assert row["scope_key"] == "att1-recuperacion"


def test_the_validator_runs_in_ci() -> None:
    package = json.loads(PACKAGE.read_text(encoding="utf-8"))
    script = package["scripts"]["test"]
    assert "node validate_flow_engine_reads_see_motor_v1.mjs --emit-fixture --check" in script
