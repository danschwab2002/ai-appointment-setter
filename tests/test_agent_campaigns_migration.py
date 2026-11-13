"""La forma de 20261108005000 (agent-campaigns-v1, E-355). Lo que hace, lo prueba validate_agent_campaigns.mjs."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261108005000_agent_campaigns_v1.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_it_is_one_transaction_and_is_the_last() -> None:
    sql = _sql()
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    # El sondeo de Meta y el aviso en el hilo (20261109000100, E-361): dos columnas, una tabla y cuatro RPC.
    # La oferta de la campana en el link del agente (20261110000500, E-410): una reserva, una columna y el trigger.
    # El caso se cierra cuando su ultimo toque termina sin salir (20261111000100, E-423): una funcion privada y el envoltorio.
    # Un pago pendiente que se cancela no clasifica la intencion (20261111000200, E-423): una rama del correlador portable.
    assert names[-12] == "20261111000200_pending_cancel_keeps_first_contact_v1.sql"
    # Los numeros del tablero (20261112200000, E-507): una lectura nueva.
    assert names[-11] == "20261112200000_read_results_v1.sql"
    assert names[-13] == "20261111000100_close_recovery_case_without_pending_touch_v1.sql"
    assert names[-14] == "20261110000500_campaign_offer_checkout_issuance_v1.sql"
    assert names[-15] == "20261109000100_meta_template_request_notices_v1.sql"
    assert names[-16] == MIGRATION.name
    # El motor nativo de varios toques (20261112210000, E-483): las dos funciones de la forma nativa.
    # en_curso: cancelar en el nativo (20261112210100, E-483, C8).
    # prioridad.le_gana_a en el nativo (20261112210200, E-483, C7).
    # El pago rechazado completo en el nativo: B1 por motivo y B2 o Z (20261112210300, E-483, C3).
    # El pago rechazado sin formulario de una oferta de campana (20261112210400, E-483, pieza PF).
    # El evento de un disparo por audiencia en el nativo (20261112210600, E-483, para E-484).
    assert names[-1] == "20261113200000_e528_native_payment_slip_v1.sql"
    assert names[-2] == "20261112230000_e483_native_person_cap_v1.sql"
    assert names[-3] == "20261112220000_flow_audience_triggers_v1.sql"
    assert names[-4] == "20261112210600_e483_native_audience_source_v1.sql"
    assert names[-5] == "20261112210500_e483_native_personal_coupon_v1.sql"
    assert names[-6] == "20261112210400_e483_payment_failure_by_phone_v1.sql"
    assert names[-7] == "20261112210300_e483_native_condition_after_send_v1.sql"
    assert names[-8] == "20261112210200_e483_native_priority_wins_v1.sql"
    assert names[-9] == "20261112210100_e483_native_cancel_in_flight_v1.sql"
    assert names[-10] == "20261112210000_e483_native_multitouch_v1.sql"
    assert sql.count("\nbegin;\n") == 1 and sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s';" in sql
    assert "agent_campaigns_already_applied" in sql and "agent_campaigns_prerequisite_missing" in sql


def test_it_only_adds() -> None:
    lowered = _sql().lower()
    assert "create or replace" not in lowered and "drop " not in lowered


def test_one_live_is_enforced_by_a_unique_index() -> None:
    assert "agent_campaigns_one_live" in _sql()
    assert "where status = 'publicada'" in _sql()


def test_publishing_is_only_for_the_approver_role() -> None:
    sql = _sql()
    assert "grant execute on function %s to flow_change_approver" in sql
    grants_ae = sql.split("to_regrole('automation_expert_change')", 1)[1].split("end if;", 1)[0]
    assert "approver_publish" not in grants_ae and "approver_pause" not in grants_ae


def test_the_validator_is_in_the_npm_chain() -> None:
    chain = json.loads((ROOT / "tests/sql/followup_engine/package.json").read_text(encoding="utf-8"))["scripts"]["test"]
    assert chain.endswith("node validate_agent_campaigns.mjs && node validate_meta_template_request_notices.mjs && node validate_recovery_case_no_touch_left_close.mjs && node validate_e483_native_multitouch.mjs && node validate_e483_native_cancel_in_flight.mjs && node validate_e483_native_priority_wins.mjs && node validate_e483_native_condition_after_send.mjs --check && node validate_e483_payment_failure_by_phone.mjs && node validate_e483_native_personal_coupon.mjs --check"
        " && node validate_e483_native_audience_source.mjs && node validate_flow_audience_triggers.mjs && node validate_flow_audience_native_e2e.mjs"
        " && node validate_e483_native_legacy_double_case_touches.mjs --check && node validate_e483_native_person_cap.mjs"
        " && node validate_e528_native_payment_slip.mjs")
