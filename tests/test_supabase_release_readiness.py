from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prepare_supabase_release_bundle.py"
SPEC = importlib.util.spec_from_file_location("release_bundle", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_bundle_covers_exact_pending_tail_and_is_deterministic(tmp_path: Path) -> None:
    first = MODULE.build(ROOT, tmp_path / "first", allow_dirty=True)
    second = MODULE.build(ROOT, tmp_path / "second", allow_dirty=True)

    expected = [
        path.name
        for path in sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
        if path.name.split("_", 1)[0] > MODULE.PREFIX_LAST
    ]
    observed = [row["filename"] for row in first["pending_tail"]]
    assert observed == expected
    assert observed[0] == "20260808000400_hotmart_purchase_safety_fences.sql"
    assert "20260812000100_supabase_function_acl_hardening.sql" in observed
    assert "20260831000100_johanna_funnel_dashboard_read.sql" in observed
    # Despues, sumar ofertas a un scope publicado (20261028000300, E-163).
    # Despues, la compra con nuestro link cuenta siempre (20261028000400, E-169).
    # El doble caso del carrito (20261030000100, E-204): la admision nativa y el planificador del carrito.
    # El pago rechazado nativo (20261030000200, E-207): el disparador pago_fallido del motor nativo.
    # La lista de las propuestas del AE (20261030000300, E-208): una lectura nueva.
    # Los frenos de la compra ven nuestro link (20261030000400, E-215): la tercera fuente.
    # El seguimiento a 24 h nativo (20261101000200, E-251): la base del silencio.
    # Los frenos del seguimiento despues de Z y con una ficha vigente (20261101000300, E-263).
    # La renovacion por respuesta y el cupon del turno (20261102000100, E-267).
    # Las solicitudes de plantilla de Meta (20261102000200, E-268, F-146 partes 1 y 2).
    # El barrido del seguimiento a 24 h nativo (20261102000300, E-269).
    # El reciclado de un telefono de prueba (20261102000400, E-270, F-147 PR 1).
    # Los rechazos de Meta y el unico reintento del 131049 (20261103000500, E-284).
    # El historial de la pagina de aprobacion (20261103000600, E-295).
    # Si la persona ya es cliente (20261103000700, E-297).
    # Las cuatro ramas del pago rechazado nativo (20261103000900, E-306).
    # El aviso de derivaciones sin atender (20261103001000, E-309).
    # La etiqueta de un segmento en un flujo nativo (20261103001300, E-316): un reemplazo contado en la forma nativa.
    # El rechazo de la cuenta espera a que la cuenta vuelva (20261107000100, E-348): dos tablas y ocho RPC de service_role.
    assert observed[-17] == "20261107000100_meta_account_hold_retry_v1.sql"
    # El almacen dice quien ejecuta cada flujo (20261108000100, E-353): una clave mas en read_flow_store_v1.
    # Las campanas del Automation Expert como dato (20261108005000, E-355): dos tablas y doce RPC.
    # El sondeo de Meta y el aviso en el hilo (20261109000100, E-361): dos columnas, una tabla y cuatro RPC.
    # La oferta de la campana en el link del agente (20261110000500, E-410): una reserva, una columna y el trigger.
    # El caso se cierra cuando su ultimo toque termina sin salir (20261111000100, E-423): una funcion privada y el envoltorio.
    # Un pago pendiente que se cancela no clasifica la intencion (20261111000200, E-423): una rama del correlador portable.
    # Los numeros del tablero (20261112200000, E-507): una lectura de service_role.
    assert observed[-9] == "20261112200000_read_results_v1.sql"
    assert observed[-10] == "20261111000200_pending_cancel_keeps_first_contact_v1.sql"
    assert observed[-11] == "20261111000100_close_recovery_case_without_pending_touch_v1.sql"
    assert observed[-12] == "20261110000500_campaign_offer_checkout_issuance_v1.sql"
    assert observed[-13] == "20261109000100_meta_template_request_notices_v1.sql"
    assert observed[-14] == "20261108005000_agent_campaigns_v1.sql"
    # El motor nativo de varios toques (20261112210000, E-483): las dos funciones de la forma nativa.
    # en_curso: cancelar en el nativo (20261112210100, E-483, C8).
    # prioridad.le_gana_a en el nativo (20261112210200, E-483, C7).
    # El pago rechazado completo en el nativo: B1 por motivo y B2 o Z (20261112210300, E-483, C3).
    # El pago rechazado sin formulario de una oferta de campana (20261112210400, E-483, pieza PF).
    # El cupon propio por persona en el nativo (20261112210500, E-483, C2).
    # El evento de un disparo por audiencia en el nativo (20261112210600, E-483, para E-484).
    # El disparo por audiencia y su lista congelada (20261112220000, E-484): dos tablas y diez funciones.
    assert observed[-1] == "20261112220000_flow_audience_triggers_v1.sql"
    assert observed[-2] == "20261112210600_e483_native_audience_source_v1.sql"
    assert observed[-3] == "20261112210500_e483_native_personal_coupon_v1.sql"
    assert observed[-4] == "20261112210400_e483_payment_failure_by_phone_v1.sql"
    assert observed[-5] == "20261112210300_e483_native_condition_after_send_v1.sql"
    assert observed[-6] == "20261112210200_e483_native_priority_wins_v1.sql"
    assert observed[-7] == "20261112210100_e483_native_cancel_in_flight_v1.sql"
    assert observed[-8] == "20261112210000_e483_native_multitouch_v1.sql"
    # La base no publica una version nativa que el motor no ejecuta (20261108004100, E-357).
    assert observed[-15] == "20261108004100_flow_change_publish_native_version_guard_v1.sql"
    assert observed[-16] == "20261108000100_flow_store_route_executor_v1.sql"
    # El cierre del caso por el cupon de una campana (20261106000100, E-339): una RPC de service_role.
    assert observed[-18] == "20261106000100_recovery_case_segment_coupon_close_v1.sql"
    # El origen de un recorrido nativo para la campana por landing (20261103001700, E-321): una lectura de service_role.
    assert observed[-19] == "20261103001700_flow_engine_native_campaign_origin_v1.sql"
    # La entrada a una campana: lo que contesto cada conversacion a la pregunta (20261103001500, E-317).
    assert observed[-20] == "20261103001500_segment_entry_answers_v1.sql"
    assert observed[-21] == "20261103001300_flow_engine_native_segment_label_v1.sql"
    assert observed[-22] == "20261103001000_handoff_wait_alerts_v1.sql"
    assert observed[-23] == "20261103000900_flow_engine_native_payment_failure_branches_v1.sql"
    assert observed[-24] == "20261103000700_customer_status_v1.sql"
    assert observed[-25] == "20261103000600_flow_approval_history_v1.sql"
    assert observed[-26] == "20261103000500_meta_delivery_rejections_v1.sql"
    assert observed[-27] == "20261102000400_identity_recycle_v1.sql"
    assert observed[-28] == "20261102000300_flow_engine_native_silence_sweep_v1.sql"
    assert observed[-29] == "20261102000200_meta_template_requests_v1.sql"
    assert observed[-30] == "20261102000100_recovery_coupon_by_reply_v1.sql"
    assert observed[-31] == "20261101000300_followup_stops_after_last_touch_and_pending_payment_v1.sql"
    # Antes, el seguimiento a 24 h nativo (20261101000200, E-251).
    assert observed[-32] == "20261101000200_flow_engine_native_silence_v1.sql"
    # Antes, el primer contacto nativo (20261101000100, E-247): el disparador intencion.
    assert observed[-33] == "20261101000100_flow_engine_native_intent_v1.sql"
    assert observed[-34] == "20261030000400_portable_purchase_stops_see_our_link_v1.sql"
    assert observed[-35] == "20261030000300_flow_change_list_v1.sql"
    assert observed[-36] == "20261030000200_flow_engine_native_payment_failure_v1.sql"
    assert observed[-37] == "20261030000100_flow_engine_native_legacy_double_case_v1.sql"
    assert observed[-38] == "20261028000400_purchase_by_our_link_counts_v1.sql"
    assert observed[-39] == "20261028000300_pilot_scope_offer_append_v1.sql"
    assert observed[-40] == "20261028000200_flow_engine_native_cart_v1.sql"
    assert observed[-41] == "20261028000100_flow_change_slack_approver_v1.sql"
    assert observed[-42] == "20261026000200_flow_engine_native_offer_in_context_v1.sql"
    assert observed[-43] == "20261026000100_flow_engine_native_template_shape_v1.sql"
    assert observed[-44] == "20261025000400_purchase_agent_pause_v1.sql"
    assert observed[-45] == "20261025000300_handoff_link_note_v1.sql"
    assert observed[-46] == "20261025000200_payment_failure_product_name_btrim.sql"
    assert observed[-47] == "20261025000100_automation_expert_native_reads_v1.sql"
    assert observed[-48] == "20261024000600_flow_change_new_flow_v1.sql"
    assert observed[-49] == "20261024000500_flow_engine_native_bridge_v1.sql"
    assert observed[-50] == "20261024000400_opted_out_reply_checkout_issuance.sql"
    assert observed[-51] == "20261024000200_flow_engine_native_v1.sql"
    assert observed[-52] == "20261024000100_portable_hotmart_listen_only_capture.sql"
    assert observed[-53] == "20261022000300_flow_engine_reads_see_motor_v1.sql"
    assert observed[-54] == "20261022000200_flow_engine_bridge_reads_v1.sql"
    assert observed[-55] == "20261022000100_flow_engine_motor_carrito_v1.sql"
    assert observed[-56] == "20261021000400_automation_expert_flows_speed_v1.sql"
    assert observed[-57] == "20261021000300_flow_engine_history_cap_v1.sql"
    assert observed[-58] == "20261021000200_flow_engine_routes_v1.sql"
    assert observed[-59] == "20261021000100_automation_expert_change_v1.sql"
    assert observed[-60] == "20261020000100_recovery_checkout_authorize_started_attempt_v1.sql"
    assert observed[-61] == "20261019000800_portable_payment_slip_reminder_survives_reply_v1.sql"
    assert observed[-62] == "20261019000700_payment_failure_followup_v1.sql"
    assert observed[-63] == "20261019000600_recovery_followup_steps_v1.sql"
    assert observed[-64] == "20261019000500_payment_failure_other_coupon_delivered_v1.sql"
    assert observed[-65] == "20261019000400_portable_payment_slip_reminders_v1.sql"
    assert observed[-66] == "20261019000300_portable_purchase_stops_any_offer_v1.sql"
    assert observed[-67] == "20261019000200_portable_payment_failure_variants_v1.sql"
    assert observed[-68] == "20261019000100_portable_payment_slip_first_contact_v1.sql"
    assert observed[-69] == "20261018000200_recovery_checkout_issuance_v1.sql"
    assert observed[-70] == "20261018000100_recovery_coupons_v1.sql"
    assert observed[-71] == "20261016000400_portable_checkout_issuance_for_funnel_v1.sql"
    assert first["bundle"]["sha256"] == second["bundle"]["sha256"]
    assert first["postflight"]["sha256"] == second["postflight"]["sha256"]
    assert first["production_authorized"] is False


def test_bundle_hashes_match_written_artifacts(tmp_path: Path) -> None:
    output = tmp_path / "bundle"
    manifest = MODULE.build(ROOT, output, allow_dirty=True)
    persisted = json.loads((output / "manifest.json").read_text(encoding="utf-8"))

    assert persisted == manifest
    assert MODULE.sha256(output / "pending-tail.sql") == manifest["bundle"]["sha256"]
    assert MODULE.sha256(output / "postflight.sql") == manifest["postflight"]["sha256"]


def test_bundle_rejects_preexisting_output_without_modifying_it(tmp_path: Path) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "manifest.json"
    sentinel.write_text("old-manifest\n")

    with pytest.raises(ValueError, match="output path already exists"):
        MODULE.build(ROOT, output, allow_dirty=True)

    assert sentinel.read_text() == "old-manifest\n"
    assert sorted(path.name for path in output.iterdir()) == ["manifest.json"]


def test_late_failure_leaves_no_output_or_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "bundle"
    real_sha256 = MODULE.sha256

    def fail_on_postflight(path: Path) -> str:
        if path.name == "postflight.sql":
            raise OSError("injected late hash failure")
        return real_sha256(path)

    monkeypatch.setattr(MODULE, "sha256", fail_on_postflight)
    with pytest.raises(OSError, match="injected late hash failure"):
        MODULE.build(ROOT, output, allow_dirty=True)

    assert not output.exists()
    assert list(tmp_path.glob(".bundle.*")) == []


def test_bundle_rejects_duplicate_versions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    migrations = repo / "supabase" / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "20260808000300_one.sql").write_text("select 1;\n")
    (migrations / "20260808000300_two.sql").write_text("select 2;\n")

    with pytest.raises(ValueError, match="duplicate migration version"):
        MODULE.migration_records(repo)


def test_cli_blocks_dirty_source_without_override(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    migrations = repo / "supabase" / "migrations"
    scripts = repo / "scripts"
    migrations.mkdir(parents=True)
    scripts.mkdir()
    (migrations / "20260808000300_prefix.sql").write_text("select 1;\n")
    (migrations / "20260808000400_tail.sql").write_text("select 2;\n")
    for relative in MODULE.REQUIRED_POSTFLIGHT:
        path = repo / relative
        path.write_text("select 1;\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git", "-c", "user.name=Test", "-c", "user.email=test@example.test",
            "commit", "-qm", "fixture",
        ],
        cwd=repo,
        check=True,
    )
    (repo / "dirty.txt").write_text("dirty\n")

    with pytest.raises(ValueError, match="repository is dirty"):
        MODULE.build(repo, tmp_path / "blocked")
