from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "scripts" / "supabase_schema_inventory.sql"
ACL_INVENTORY = ROOT / "scripts" / "supabase_acl_inventory.sql"
MIGRATIONS = ROOT / "supabase" / "migrations"


def _without_comments_and_literals(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", "", sql)
    return re.sub(r"'(?:''|[^'])*'", "''", sql)


def test_supabase_inventories_are_catalog_only() -> None:
    forbidden = {
        "alter",
        "call",
        "create",
        "delete",
        "do",
        "drop",
        "grant",
        "insert",
        "revoke",
        "truncate",
        "update",
    }
    for path in (INVENTORY, ACL_INVENTORY):
        executable = _without_comments_and_literals(path.read_text(encoding="utf-8"))
        observed = {
            token.lower() for token in re.findall(r"\b[A-Za-z_]+\b", executable)
        }

        assert forbidden.isdisjoint(observed), path
        assert executable.strip().lower().startswith("with"), path
        assert executable.count(";") == 1, path


def test_supabase_schema_inventory_covers_every_canonical_migration() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    documented = re.findall(r"'(\d{14}_[a-z0-9_]+\.sql)'", sql)
    canonical = [path.name for path in sorted(MIGRATIONS.glob("*.sql"))]

    assert documented == canonical
    assert len(documented) == len(set(documented))


def test_supabase_schema_inventory_reports_non_authoritative_fingerprints() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")

    assert "fingerprint_present" in sql
    assert "fingerprint_absent" in sql
    assert "fingerprint_partial" in sql
    assert "migration_applied" not in sql
    assert "select\n    version" in sql.lower()


def test_daily_feedback_fencing_fingerprint_covers_oidc_retention_boundary() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    fingerprint = sql.split("'20260911000100'", 1)[1].split(")\nselect", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", fingerprint)

    assert (
        "public.complete_daily_feedback_oidc_v1(text,text,text,text,text,text,timestamptz)"
        in compact_fingerprint
    )
    assert "p_session_expires_at>v_batch.retention_expires_at" in compact_fingerprint
    assert "invalid_notification_retry" in fingerprint
    assert "p_retry_secondsisnull" in compact_fingerprint
    assert "p_retry_secondsnotbetween1and900" in compact_fingerprint
    assert "daily_feedback_notification_envelope_lease_and_oidc_retention_fencing" in fingerprint


def test_daily_feedback_multi_reviewer_fingerprint_covers_batch_authority() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    fingerprint = sql.split("'20260911000200'", 1)[1].split(")\nselect", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", fingerprint)

    assert "daily_feedback_batch_reviewer_bindings" in fingerprint
    assert "configure_daily_feedback_scope_v2" in fingerprint
    assert "purge_expired_daily_feedback_v2" in fingerprint
    assert "accountable_reviewer_refs" in fingerprint
    assert "joinpublic.daily_feedback_batch_reviewer_bindings" in compact_fingerprint
    assert "reviewer_set_must_have_four" in fingerprint
    assert "all_reviewers_must_be_deletion_accountable" in fingerprint
    assert "(s.enabledorp_force)" in compact_fingerprint
    assert "reviewer_set_incomplete" in fingerprint
    assert "brb.oidc_subject=rb.oidc_subject" in fingerprint
    assert "relrowsecurity" in fingerprint
    assert "has_table_privilege" in fingerprint
    assert "daily_feedback_purge_tombstones_immutable" in fingerprint
    assert "daily_feedback_tombstone_immutable_guard" in fingerprint
    assert "v_authoritative_now:=clock_timestamp()" in compact_fingerprint
    assert "p_limitisnull" in compact_fingerprint
    assert "configure_daily_feedback_scope_v1" in fingerprint
    assert "brb.slack_user_id=p_slack_user_id" in fingerprint
    assert "get_daily_feedback_readiness_v1" in fingerprint
    assert "notification_state=''delivery_unknown''" in fingerprint


def test_operator_correlation_private_identity_fingerprint_covers_exact_rpc_acl() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    fingerprint = sql.split("'20260912000100'", 1)[1].split(")\nselect", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", fingerprint)

    signature = "public.get_operator_unresolved_correlation(text,text,uuid)"
    assert signature in compact_fingerprint
    assert "'''normalized_email'',identity.normalized_email" in compact_fingerprint
    assert "'''normalized_phone'',identity.normalized_phone" in compact_fingerprint
    assert "intent.tenant_ref=scope.tenant_ref" in compact_fingerprint
    assert "intent.funnel_ref=scope.funnel_ref" in compact_fingerprint
    assert "lower(intent.product_ref)=lower(scope.purchase_intent_product_ref)" in compact_fingerprint
    assert "intent.offer_ref=scope.offer_ref" in compact_fingerprint
    assert "has_function_privilege('service_role',oid,'EXECUTE')" in compact_fingerprint
    assert "nothas_function_privilege('anon',oid,'EXECUTE')" in compact_fingerprint
    assert "nothas_function_privilege('authenticated',oid,'EXECUTE')" in compact_fingerprint


def test_absolute_deadline_fingerprint_checks_semantics_and_rejects_chaining() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")

    assert "min(attempt.accepted_at)" in sql
    assert "v_next_due_at := v_sequence_started_at + v_next_delay" in sql
    assert "v_next_due_at := p_now + v_next_delay" in sql
    assert "followup_policy_step_offsets_validate" in sql


def test_hotmart_base_search_path_fingerprint_uses_exact_signatures() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    fingerprint = sql.split("'20260820000200'", 1)[1].split(")\nselect", 1)[0]

    assert re.search(
        r"to_regprocedure\(\s*'public\._admit_hotmart_purchase_approved_base\(text,jsonb\)'\s*\)",
        fingerprint,
    )
    assert re.search(
        r"to_regprocedure\(\s*'public\._admit_hotmart_cart_abandonment_base\(text,jsonb\)'\s*\)",
        fingerprint,
    )
    assert "proname in" not in fingerprint


def test_hotmart_contract_fingerprint_uses_exact_legacy_and_wrapper_signatures() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    fingerprint = sql.split("'20260820000400'", 1)[1].split(")\nselect", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", fingerprint)

    for signature in (
        "public.admit_hotmart_purchase_approved(text,jsonb)",
        "public.admit_hotmart_cart_abandonment(text,jsonb)",
        "public.admit_and_correlate_hotmart_purchase_approved(text,jsonb,text,text)",
        "public.admit_and_correlate_hotmart_cart_abandonment(text,jsonb,text,text)",
    ):
        assert f"to_regprocedure('{signature}')" in compact_fingerprint
    assert "proname" not in fingerprint


def test_precheckout_readiness_fingerprint_binds_timer_to_exact_policy() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    fingerprint = sql.split("'20260829000500'", 1)[1].split(")\nselect", 1)[0]
    compact_fingerprint = re.sub(r"\s+", " ", fingerprint)

    assert "join public.followup_policy_versions policy" in compact_fingerprint
    assert "policy.policy_key = binding.policy_key" in compact_fingerprint
    assert "policy.version = binding.policy_version" in compact_fingerprint
    assert (
        "binding.policy_key = 'johanna-precheckout-delayed-first-touch-timer'"
        in compact_fingerprint
    )
    assert "binding.policy_version = 1" in compact_fingerprint
    assert "policy.status = 'published'" in compact_fingerprint
    assert "policy.grace_period = interval '60 minutes'" in compact_fingerprint


def test_whatsapp_phone_equivalence_fingerprint_checks_the_portable_functions() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # Hasta la fila siguiente: las cuentas de abajo son de esta migracion sola.
    fingerprint = sql.split("'20261001000100'", 1)[1].split("'20261001000200'", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", fingerprint)

    for signature in (
        "public._whatsapp_phone_canonical(text)",
        "public._whatsapp_phone_variants(text)",
        "public._correlate_portable_hotmart_purchase_intent(uuid)",
        "public.admit_portable_hotmart_cart_abandonment(text,text,integer,text,jsonb,text,text)",
        "public.admit_portable_hotmart_payment_failure(text,text,integer,text,jsonb,text,text)",
        "public.admit_portable_hotmart_purchase_approved(text,text,integer,text,jsonb,text,text)",
        "public._portable_consented_intent_reason(uuid,uuid,text)",
        "public.plan_portable_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,"
        "timestamptz,bigint,bigint,text,text,integer)",
        "public._portable_chatwoot_opt_out_stop(bigint,uuid,text)",
        "public.mark_lancemos_pilot_request_started(uuid,uuid,text,bigint,timestamptz)",
        "public.mark_portable_payment_failure_request_started(uuid,uuid,text,bigint,timestamptz)",
        "public.reserve_portable_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,text,"
        "timestamptz)",
    ):
        assert f"to_regprocedure('{signature}')" in compact_fingerprint, signature
    assert "proname" not in fingerprint
    # Los cuatro helpers nuevos no son entrypoints: ni service_role los ejecuta.
    assert compact_fingerprint.count("nothas_function_privilege('service_role',") == 4
    # Los dos arranques del piloto frenan con el opt-out en las dos formas.
    assert "position('_portable_chatwoot_opt_out_stop('indefinition)>0" in compact_fingerprint
    assert "pilot_chatwoot_opt_out_stop" in fingerprint
    # La comparacion exacta del telefono no puede quedar en el correlador
    # portable ni en la compra, y carrito y pago fallido no pueden volver al
    # correlador compartido.
    assert compact_fingerprint.count("position('intent.normalized_phone=v_phone'indefinition)=0") == 2
    assert "position('public.correlate_hotmart_purchase_intent('indefinition)=0" in compact_fingerprint
    assert "consented_intent_contact_phone_mismatch" in fingerprint
    assert "consented_intent_prior_opt_out" in fingerprint
    assert "whatsapp_equivalent" in fingerprint
    # La reserva portable del enlace: un entrypoint solo de service_role, con la
    # intencion buscada por las formas y el opt-out mirado en cada una.
    assert (
        "position('intent.normalized_phone=any(public._whatsapp_phone_variants(p_external_user_id))'"
        "indefinition)>0" in compact_fingerprint
    )
    assert (
        "position('intent.normalized_phone=p_external_user_id'indefinition)=0"
        in compact_fingerprint
    )
    assert "position('asopt_out_form(user_id)'indefinition)>0" in compact_fingerprint
    # Diez chequeos: la reserva portable es el decimo.
    assert ",10,'whatsapp_phone_equivalence_portable_runtime'" in compact_fingerprint
    assert "whatsapp_phone_equivalence_portable_runtime" in fingerprint


def test_portable_precheckout_first_contact_fingerprint_checks_table_checks_and_functions() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # Hasta la fila siguiente: las cuentas de abajo son de esta migracion sola.
    fingerprint = sql.split("'20261001000200'", 1)[1].split("'20261001000300'", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", fingerprint)

    for signature in (
        "public._portable_precheckout_stop_reason(uuid,uuid)",
        "public._find_portable_precheckout_contact(uuid)",
        "public._ensure_portable_precheckout_contact(uuid,uuid)",
        "public._plan_portable_precheckout_first_contact(uuid,uuid,uuid,text,integer)",
        "public.admit_and_plan_portable_lead_precheckout(text,text,integer,text,jsonb,jsonb,text,integer)",
        "public.reevaluate_portable_precheckout_action(uuid,text,bigint,timestamptz,boolean,text,"
        "text,timestamptz,text,boolean,boolean,boolean,boolean,boolean)",
        "public.mark_portable_precheckout_request_started(uuid,uuid,text,bigint,timestamptz)",
        "public.get_portable_precheckout_pilot_runtime_status(text,integer,text,text,text)",
    ):
        assert f"to_regprocedure('{signature}')" in compact_fingerprint, signature
    assert "proname" not in fingerprint
    # La tabla del resultado de cada plan: con RLS y sin privilegios de
    # service_role. Los cuatro helpers, tampoco ejecutables por service_role.
    assert "to_regclass('public.portable_precheckout_first_contact_plans')" in compact_fingerprint
    assert "relrowsecurity" in fingerprint
    assert compact_fingerprint.count("nothas_table_privilege('service_role',oid,") == 2
    assert "nothas_function_privilege('service_role',oid,'EXECUTE')" in compact_fingerprint
    # Los tres checks con su valor nuevo.
    for constraint, value in (
        ("recovery_cases_source_check", "landing"),
        ("recovery_case_events_event_role_check", "precheckout_intent"),
        ("followup_sequences_reason_check", "precheckout_intent"),
    ):
        assert (
            f"conname='{constraint}'andposition('{value}'inpg_get_constraintdef(oid))>0"
            in compact_fingerprint
        ), constraint
    # El plan corre los triggers diferidos adentro del bloque, la reevaluacion
    # delega en la compartida y el arranque vuelve a mirar los frenos.
    assert "position('setconstraintsallimmediate'indefinition)>0" in compact_fingerprint
    assert "position('public.reevaluate_followup_action('indefinition)>0" in compact_fingerprint
    assert compact_fingerprint.count("position('_portable_precheckout_stop_reason('indefinition)>0") == 3
    assert "position('_lancemos_pilot_audience_intent('indefinition)>0" in compact_fingerprint
    assert "position('chatwoot-opt-out-user'indefinition)>0" in compact_fingerprint
    assert "portable_precheckout_first_contact" in fingerprint


def test_pilot_scope_audience_mode_read_fingerprint_checks_the_function_and_its_acl() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # Hasta la fila siguiente: las cuentas de abajo son de esta migracion sola.
    fingerprint = sql.split("'20261001000300'", 1)[1].split("'20261001000400'", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", fingerprint)

    assert "'20261001000300_pilot_scope_audience_mode_read.sql'" in fingerprint
    assert (
        compact_fingerprint.count(
            "to_regprocedure('public.get_lancemos_pilot_scope_audience_mode(text,integer)')"
        )
        == 2
    )
    assert "proname" not in fingerprint
    # Lee el modo de una version publicada, como definer y sin escribir.
    assert "andprosecdef" in compact_fingerprint
    assert "provolatile='s'" in compact_fingerprint
    assert "position('scope.audience_mode'indefinition)>0" in compact_fingerprint
    assert "position('scope.status=''published'''indefinition)>0" in compact_fingerprint
    # Es un entrypoint del bridge: solo service_role.
    assert "has_function_privilege('service_role',oid,'EXECUTE')" in compact_fingerprint
    assert "nothas_function_privilege('anon',oid,'EXECUTE')" in compact_fingerprint
    assert "nothas_function_privilege('authenticated',oid,'EXECUTE')" in compact_fingerprint
    assert compact_fingerprint.endswith(",2,'pilot_scope_audience_mode_read'unionallselect")


def test_portable_inbound_template_adoption_fingerprint_checks_the_function_and_its_acl() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # Hasta la fila siguiente: las cuentas de abajo son de esta migracion sola.
    fingerprint = sql.split("'20261001000400'", 1)[1].split("'20261001000500'", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", fingerprint)

    assert "'20261001000400_portable_inbound_adopts_template_conversation.sql'" in fingerprint
    assert (
        compact_fingerprint.count(
            "to_regprocedure('public.admit_portable_inbound_commercial_case_v1(text,integer,bigint,text)')"
        )
        == 3
    )
    assert "proname" not in fingerprint
    # Definer con search_path fijo, delega en la v2 y no toca derivaciones.
    assert "andprosecdef" in compact_fingerprint
    assert "array_to_string(proconfig,',')='search_path=pg_catalog,public,pg_temp'" in compact_fingerprint
    assert "position('public.admit_inbound_commercial_case_v2('indefinition)>0" in compact_fingerprint
    assert "position('human_handoff'indefinition)=0" in compact_fingerprint
    # La adopcion: el evento, el binding del piloto, los estados vivos, la baja
    # del contacto y las bajas de Chatwoot de ese movil.
    assert "position('inbound_adopted_template_conversation'indefinition)>0" in compact_fingerprint
    assert "position('public.pilot_recovery_case_bindings'indefinition)>0" in compact_fingerprint
    assert "position('''delivery_unknown'''indefinition)>0" in compact_fingerprint
    assert "position('''do_not_contact'''indefinition)>0" in compact_fingerprint
    assert "position('public.contact_opt_out_events'indefinition)>0" in compact_fingerprint
    # Es un entrypoint del bridge: solo service_role.
    assert "has_function_privilege('service_role',oid,'EXECUTE')" in compact_fingerprint
    assert "nothas_function_privilege('anon',oid,'EXECUTE')" in compact_fingerprint
    assert "nothas_function_privilege('authenticated',oid,'EXECUTE')" in compact_fingerprint
    assert compact_fingerprint.endswith(",3,'portable_inbound_template_adoption'unionallselect")


def test_commercial_case_lookups_by_inbound_kind_fingerprint_checks_the_four_rpcs() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # Hasta la fila siguiente: las cuentas de abajo son de esta migracion sola.
    fingerprint = sql.split("'20261001000500'", 1)[1].split("'20261005000100'", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", fingerprint)

    assert "'20261001000500_commercial_case_lookups_by_inbound_kind.sql'" in fingerprint
    assert "proname" not in fingerprint
    # Un marcador por funcion, por su firma exacta, con su guarda de ambiguedad.
    for signature, ambiguous in (
        (
            "mark_human_handoff_attended(bigint,timestamptz,timestamptz)",
            "mark_human_handoff_attended_ambiguous_case",
        ),
        (
            "claim_conversation_reactivation(bigint,text,text,text,text,bigint,integer,"
            "integer,integer,timestamptz)",
            "claim_conversation_reactivation_ambiguous_case",
        ),
        (
            "resume_paused_conversation(bigint,text,text,integer,integer,timestamptz)",
            "resume_paused_conversation_ambiguous_case",
        ),
        (
            "claim_conversation_followup_v1(bigint,bigint,bigint,text,text,text,text,text,"
            "text,text,bigint,bigint,integer,text,timestamptz)",
            "claim_conversation_followup_ambiguous_case",
        ),
    ):
        assert compact_fingerprint.count(f"to_regprocedure('public.{signature}')") == 1, signature
        assert f"position('{ambiguous}'indefinition)>0" in compact_fingerprint, ambiguous
    # Definer con search_path fijo y el filtro condicionado al evento de
    # adopcion, en las cuatro.
    for marker in (
        "andprosecdef",
        "array_to_string(proconfig,',')='search_path=pg_catalog,public,pg_temp'",
        "position('inbound_adopted_template_conversation'indefinition)>0",
        "position('''inbound_sales''ornotv_inbound_only'indefinition)>0",
        # Entrypoints del bridge: solo service_role.
        "has_function_privilege('service_role',oid,'EXECUTE')",
        "nothas_function_privilege('anon',oid,'EXECUTE')",
        "nothas_function_privilege('authenticated',oid,'EXECUTE')",
    ):
        assert compact_fingerprint.count(marker) == 4, marker
    assert compact_fingerprint.endswith(",4,'commercial_case_lookups_by_inbound_kind'unionallselect")


def test_portable_conversation_followup_claim_fingerprint_checks_the_derivation_and_its_acl() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261010000100: hasta la fila siguiente.
    fingerprint = sql.split("'20261010000100'", 1)[1].split("'20261012000100'", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", fingerprint)
    portable = (
        "public.claim_portable_conversation_followup_v1(bigint,bigint,bigint,text,text,text,"
        "text,text,text,text,bigint,bigint,integer,text,timestamptz)"
    )
    shared = (
        "public.claim_conversation_followup_v1(bigint,bigint,bigint,text,text,text,text,text,"
        "text,text,bigint,bigint,integer,text,timestamptz)"
    )

    assert "'20261010000100_portable_conversation_followup_claim.sql'" in fingerprint
    assert "proname" not in fingerprint
    # Los cuatro marcadores dependen de la portable, por su firma exacta: en la
    # base de Johanna (sin la reserva portable no se crea nada) da absent, no
    # partial.
    assert compact_fingerprint.count(f"to_regprocedure('{portable}')") == 4
    assert f"to_regprocedure('{portable}')isnotnull" in compact_fingerprint
    assert compact_fingerprint.count(f"to_regprocedure('{shared}')") == 1
    # Definer con search_path fijo, y el link sale de la reserva portable.
    assert "andprosecdef" in compact_fingerprint
    assert "array_to_string(proconfig,',')='search_path=pg_catalog,public,pg_temp'" in compact_fingerprint
    assert (
        "position('frompublic.reserve_portable_checkout_issuance_v2('indefinition)>0"
        in compact_fingerprint
    )
    assert "position('reserve_chatwoot_checkout_issuance_v2'indefinition)=0" in compact_fingerprint
    # La barrera de la conversacion adoptada, con el evento que escribe la 000400.
    for marker in (
        "position('portable_followup_template_reply:begin'indefinition)>0",
        "position('portable_followup_template_reply:end'indefinition)>0",
        "position('ifnotv_inbound_onlythen'indefinition)>0",
        "position('''blocked_not_template_reply'''indefinition)>0",
        "position('inbound_adopted_template_conversation'indefinition)>0",
    ):
        assert marker in compact_fingerprint, marker
    # Es un entrypoint del bridge: solo service_role.
    assert "has_function_privilege('service_role',oid,'EXECUTE')" in compact_fingerprint
    assert "nothas_function_privilege('anon',oid,'EXECUTE')" in compact_fingerprint
    assert "nothas_function_privilege('authenticated',oid,'EXECUTE')" in compact_fingerprint
    # La compartida queda como estaba: la reserva compartida y sin la barrera.
    for marker in (
        "position('frompublic.reserve_chatwoot_checkout_issuance_v2('inshared.definition)>0",
        "position('reserve_portable_checkout_issuance_v2'inshared.definition)=0",
        "position('blocked_not_template_reply'inshared.definition)=0",
    ):
        assert marker in compact_fingerprint, marker
    assert compact_fingerprint.endswith(",4,'portable_conversation_followup_claim'unionallselect")


def test_supabase_acl_inventory_is_exhaustive_and_allowlisted() -> None:
    sql = ACL_INVENTORY.read_text(encoding="utf-8")
    allowlisted = re.findall(r"\('public\.([a-z0-9_]+\([^']*\))'\)", sql)

    # 20261019000100 (la ficha de pago) suma sus cinco entradas del bridge y
    # 20261019000200 (B1 del pago rechazado), sus dos, y 20261019000300 el
    # freno por la compra de cualquier oferta, que llama la reevaluacion
    # (security invoker) como service_role. 20261019000700 (el seguimiento del
    # pago rechazado) suma el registro de B1 y el contexto del seguimiento.
    # 20261021000200 (las rutas del motor de flujos) suma sus dos lecturas, y
    # 20261022000100 (el camino del motor para el carrito) la admision, el
    # arranque, el contexto del paso y el apagado que llama la reevaluacion.
    # 20261022000200 (el bridge del motor) suma las rutas, las divergencias y
    # la marca de una divergencia avisada. 20261024000100 (el modo «solo
    # guardar» de Hotmart) suma su admision, y 20261024000200 (el motor
    # nativo) la suya. 20261024000500 (el bridge del motor nativo) suma sus
    # siete: los flujos nativos, la cola de la compra (anotar, leer, cerrar),
    # los rechazos de MOT-002 y su marca, y la cola del paso. 20261024000600
    # (el AE que crea flujos) suma la lectura de la simulacion de un flujo nuevo.
    assert len(allowlisted) == 166
    assert "read_flow_engine_native_trigger_counts_v1(jsonb, text, text, timestamp with time zone)" in allowlisted
    assert (
        "admit_portable_hotmart_listen_only_capture_v1"
        "(text, text, integer, text, jsonb, text, text)"
    ) in allowlisted
    assert "admit_flow_engine_event_v1(jsonb, jsonb)" in allowlisted
    for signature in (
        "read_flow_engine_routes_v1(jsonb)",
        "read_flow_engine_divergences_v1(jsonb, boolean, integer)",
        "mark_flow_engine_divergence_notified_v1(uuid, timestamp with time zone)",
        "read_flow_engine_v1(jsonb, text)",
        "read_flow_engine_history_v1(jsonb, text, timestamp with time zone)",
        "_flow_engine_run_turned_off(uuid)",
        "admit_flow_engine_hotmart_v1(uuid, uuid, text, text, text, timestamp with time zone, bigint, bigint, "
        "text, text, integer, text, integer, boolean)",
        "mark_flow_engine_request_started_v1(uuid, uuid, text, bigint, timestamp with time zone)",
        "flow_engine_execution_context_v1(uuid)",
    ):
        assert signature in allowlisted, signature
    for signature in (
        "record_payment_failure_first_touch_v1(uuid, uuid, text, bigint, text, text, "
        "timestamp with time zone)",
        "get_payment_failure_followup_context_v1(uuid, text, bigint, "
        "timestamp with time zone)",
    ):
        assert signature in allowlisted, signature
    # 20261019000600: el paso de la politica, el pedido de renovacion y su
    # lectura, que llaman funciones security invoker que corre service_role.
    assert "_recovery_followup_step(uuid)" in allowlisted
    assert "_portable_case_purchase_stop_reason(uuid)" in allowlisted
    for signature in (
        "get_portable_payment_failure_pilot_runtime_status(text, integer, text, text, text)",
        "get_portable_payment_failure_send_context_v1(uuid, text, bigint, "
        "timestamp with time zone)",
    ):
        assert signature in allowlisted, signature
    for signature in (
        "admit_portable_hotmart_payment_slip_v2(text, text, integer, text, jsonb, text, text)",
        "plan_portable_payment_slip_recovery(uuid, uuid, text, text, text, text, integer, "
        "timestamp with time zone, bigint, bigint, text, text, integer)",
        "mark_portable_payment_slip_request_started(uuid, uuid, text, bigint, "
        "timestamp with time zone)",
        "get_portable_payment_slip_pilot_runtime_status(text, integer, text, text, text)",
        "get_portable_payment_slip_send_context(uuid, text, bigint, timestamp with time zone)",
    ):
        assert signature in allowlisted, signature
    assert "get_daily_feedback_review_page_v2(text, uuid, text[])" in allowlisted
    assert "get_portable_person_activity_v1(text, text, text)" in allowlisted
    assert (
        "reserve_portable_checkout_issuance_for_funnel_v1(uuid, text, bigint, bigint, bigint, "
        "text, text, text, timestamp with time zone)"
    ) in allowlisted
    assert "close_daily_feedback_batch_v1(uuid, text, text, uuid, text)" in allowlisted
    assert "get_portable_recent_payment_failure_v1(text, text, text)" in allowlisted
    assert (
        "admit_portable_hotmart_payment_slip_event_v1(text, text, integer, text, jsonb)"
        in allowlisted
    )
    assert (
        "claim_conversation_followup_v1(bigint, bigint, bigint, text, text, text, text, text, "
        "text, text, bigint, bigint, integer, text, timestamp with time zone)"
        in allowlisted
    )
    assert (
        "claim_portable_conversation_followup_v1(bigint, bigint, bigint, text, text, text, "
        "text, text, text, text, bigint, bigint, integer, text, timestamp with time zone)"
        in allowlisted
    )
    assert (
        "settle_conversation_followup_v1(text, text, bigint, text, timestamp with time zone)"
        in allowlisted
    )
    assert "record_lead_first_name_inference_v1(text, text, text, text, text)" in allowlisted
    assert "get_lead_first_name_inference_v1(text)" in allowlisted
    assert len(allowlisted) == len(set(allowlisted))
    assert "claim_slack_handoff_notifications(text, integer, integer)" in allowlisted
    assert "complete_slack_handoff_notification(uuid, uuid, bigint, uuid)" in allowlisted
    assert "release_slack_handoff_notification(uuid, uuid, bigint, text)" in allowlisted
    assert "admit_precheckout_form_submission(text, jsonb, jsonb)" in allowlisted
    assert "get_daily_feedback_review_page_v1(text, uuid)" in allowlisted
    assert any(item.startswith("configure_daily_feedback_scope_v2(") for item in allowlisted)
    assert "purge_expired_daily_feedback_v2(timestamp with time zone, text, text, text, integer)" in allowlisted
    assert "get_daily_feedback_conversation_context_v1(text, text, bigint, bigint, bigint[])" in allowlisted
    assert not any(item.startswith("configure_daily_feedback_scope_v1(") for item in allowlisted)
    assert "admit_observed_lead_precheckout(text, jsonb, jsonb)" in allowlisted
    assert "get_lancemos_pilot_scope_audience_mode(text, integer)" in allowlisted
    assert "admit_portable_inbound_commercial_case_v1(text, integer, bigint, text)" in allowlisted
    assert (
        "admit_portable_observed_lead_precheckout"
        "(text, text, integer, text, jsonb, jsonb)" in allowlisted
    )
    assert "admit_inbound_commercial_case_v2(text, integer, bigint, text)" in allowlisted
    assert (
        "reserve_portable_checkout_issuance_v2"
        "(uuid, text, bigint, bigint, bigint, text, text, timestamp with time zone)"
        in allowlisted
    )
    assert (
        "admit_and_correlate_hotmart_checkout_issuance_v2"
        "(text, jsonb, text, timestamp with time zone)" in allowlisted
    )
    assert "admit_johanna_payment_failure(text, jsonb, text, text)" in allowlisted
    assert "read_johanna_funnel_dashboard_v1" not in "\n".join(allowlisted)
    assert (
        "admit_johanna_funnel_event_v1(text, text, text, timestamp with time zone, "
        "text, text, text, text, text, text, text, text)" in allowlisted
    )
    assert (
        "read_johanna_funnel_dashboard_v2(integer)"
        in allowlisted
    )
    assert (
        "resolve_commercial_ally_runtime_binding(text, text, integer)" in allowlisted
    )
    assert (
        "resolve_commercial_ally_discount_policy(text, text, integer, text)"
        in allowlisted
    )
    assert (
        "admit_portable_hotmart_purchase_approved"
        "(text, text, integer, text, jsonb, text, text)" in allowlisted
    )
    assert (
        "admit_portable_hotmart_payment_failure"
        "(text, text, integer, text, jsonb, text, text)" in allowlisted
    )
    assert (
        "plan_portable_payment_failure_recovery"
        "(uuid, uuid, text, text, text, text, integer, timestamp with time zone, "
        "bigint, bigint, text, text, integer)" in allowlisted
    )
    assert (
        "plan_commercial_ally_post_inbound_discount"
        "(text, text, integer, text, integer, bigint, bigint, bigint, bigint, "
        "text, timestamp with time zone)" in allowlisted
    )
    assert (
        "mark_portable_payment_failure_request_started"
        "(uuid, uuid, text, bigint, timestamp with time zone)" in allowlisted
    )
    assert (
        "prepare_johanna_payment_failure_invalid_contact_retry"
        "(text, uuid, bigint, bigint)" in allowlisted
    )
    assert "correlate_hotmart_purchase_intent(uuid)" in allowlisted
    assert (
        "begin_johanna_abandonment_one_shot(text, uuid, text, bigint, bigint, text, integer, bigint)"
        in allowlisted
    )
    assert (
        "finish_johanna_abandonment_one_shot(uuid, text, bigint, bigint, text)"
        in allowlisted
    )
    assert (
        "reconcile_johanna_abandonment_one_shot(text, bigint, bigint)"
        in allowlisted
    )
    assert (
        "begin_johanna_abandonment_hotmart_auto(text, uuid, uuid, text, bigint, bigint, text, integer, bigint)"
        in allowlisted
    )
    assert (
        "begin_johanna_abandonment_hotmart_auto_v2(text, uuid, uuid, bigint, bigint, text, integer, bigint)"
        in allowlisted
    )
    assert (
        "list_operator_unresolved_correlations(text, text, integer, uuid)"
        in allowlisted
    )
    assert (
        "get_operator_unresolved_correlation(text, text, uuid)" in allowlisted
    )
    assert (
        "prepare_operator_correlation_resolution(text, text, text, uuid, text, uuid, text, uuid)"
        in allowlisted
    )
    assert (
        "confirm_operator_correlation_resolution(text, text, text, uuid, text, uuid)"
        in allowlisted
    )
    assert (
        "list_due_hotmart_abandonment_reevaluations(timestamp with time zone, integer)"
        in allowlisted
    )
    assert (
        "reevaluate_hotmart_abandonment_timer(uuid, timestamp with time zone)"
        in allowlisted
    )
    assert (
        "list_due_hotmart_abandonment_reevaluations_v2(timestamp with time zone, integer, boolean)"
        in allowlisted
    )
    assert "get_precheckout_delayed_one_shot_command(uuid)" in allowlisted
    assert "admit_and_correlate_hotmart_purchase_approved(text, jsonb, text, text)" in allowlisted
    assert "admit_johanna_hotmart_cart_abandonment(text, jsonb, text, text)" in allowlisted
    assert (
        "admit_and_correlate_hotmart_cart_abandonment(text, jsonb, text, text)"
        not in allowlisted
    )
    assert "admit_hotmart_purchase_approved(text, jsonb)" not in allowlisted
    assert "admit_hotmart_cart_abandonment(text, jsonb)" not in allowlisted
    assert "begin_precheckout_test_first_touch(text, uuid, text, bigint, bigint)" in allowlisted
    assert "schedule_precheckout_first_touch_reevaluation(uuid, uuid)" in allowlisted
    assert "finish_precheckout_test_first_touch(uuid, text, bigint, bigint, text)" in allowlisted
    assert "has_function_privilege('anon'" in sql
    assert "has_function_privilege('authenticated'" in sql
    assert "has_function_privilege('service_role'" in sql
    assert "result_type = 'trigger'" in sql
    assert "service_role_allowlist_mismatch" in sql
    assert "security_definer_search_path_missing" in sql


def test_daily_feedback_review_filters_fingerprint_checks_the_closure_and_both_pages() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261015000100 va antes de la de 20261015000200.
    fingerprint = sql.split("'20261015000100'", 1)[1].split(
        "\n    union all\n    select\n        '20261015000200'", 1
    )[0]
    compact_fingerprint = re.sub(r"\s+", "", fingerprint)

    assert "'20261015000100_daily_feedback_review_filters_v1.sql'" in fingerprint
    for marker in (
        "to_regclass('public.daily_feedback_batch_closures')",
        "andrelrowsecurity",
        "position('''closed_unreviewed_count'''indefinition)>0",
        "position('''context'',v_item.context'indefinition)>0",
        "to_regprocedure('public.get_daily_feedback_review_page_v2(text,uuid,text[])')",
        "position('''filtered_out'''indefinition)>0",
        "position('whered.item_id=v_item.id)then'indefinition)>0",
        "position('review_closure_not_acknowledged'indefinition)>0",
        "selectcount(*)=2",
    ):
        assert marker in compact_fingerprint, marker
    assert compact_fingerprint.endswith(",6,'daily_feedback_review_filters_and_batch_closure'")


def test_automation_expert_read_fingerprint_checks_the_three_reads() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261015000200 va antes de la de 20261016000100.
    fingerprint = sql.split("'20261015000200'", 1)[1].split(
        "\n    union all\n    select\n        '20261016000100'", 1
    )[0]
    compact_fingerprint = re.sub(r"\s+", "", fingerprint)

    assert "'20261015000200_automation_expert_read_v1.sql'" in fingerprint
    for marker in (
        "to_regprocedure('public.read_automation_expert_flows_v1(jsonb)')",
        "to_regprocedure('public.find_automation_expert_lead_v1(jsonb,bigint,text,text)')",
        "to_regprocedure('public.read_automation_expert_lead_history_v1(jsonb,uuid)')",
        "andprovolatile='s'",
        "array_to_string(proconfig,',')='search_path=pg_catalog,public,pg_temp'",
        "position('contacts_matched'indefinition)>0",
        "position('reason_code_rejected'indefinition)>0",
        "andnothas_function_privilege('anon',oid,'EXECUTE')",
    ):
        assert marker in compact_fingerprint, marker
    assert compact_fingerprint.endswith(",3,'automation_expert_read'")


def test_flow_change_store_fingerprint_never_reads_the_publish_body() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261016000100 va justo antes de la de 20261016000200 (lo que
    # hizo la persona, 2b).
    fingerprint = sql.split("'20261016000100'", 1)[1].split("'20261016000200'", 1)[0]
    fingerprint = fingerprint.rsplit("union all", 1)[0]
    executable = re.sub(r"--[^\n]*", "", fingerprint)
    compact_fingerprint = re.sub(r"\s+", "", executable)

    assert "'20261016000100_flow_change_store_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",5,'flow_change_store'")
    # Sin rol propio en esta pieza: la fila no nombra el rol de las
    # herramientas del Automation Expert.
    assert "automation_expert_change" not in fingerprint
    for marker in (
        "to_regclass('public.flow_controls')",
        "to_regclass('public.flow_definitions')",
        "to_regclass('public.flow_change_proposals')",
        "to_regclass('public.flow_change_events')",
        "andrelrowsecurity",
        "andnothas_table_privilege('anon',oid,'SELECT')",
        "'flow_definitions_immutable','flow_definitions_no_truncate'",
        "'flow_change_events_append_only','flow_change_events_no_truncate'",
        "'flow_controls_record_change','flow_change_proposals_validate'",
        "andnotprosecdef",
        "proconfig@>array['search_path=pg_catalog,public,pg_temp']",
        "andnothas_function_privilege('service_role',oid,'EXECUTE')",
    ):
        assert marker in compact_fingerprint, marker
    # El motor va a reescribir publicar: ningun marcador lee su cuerpo. Su
    # firma aparece solo adentro de to_regprocedure, una vez por marcador de
    # las siete, y el unico marcador que lee un cuerpo es el de los
    # invariantes.
    publish = "operator_publish_flow_change_v1(jsonb,uuid,text,jsonb)"
    assert compact_fingerprint.count(publish) == 2
    assert compact_fingerprint.count("to_regprocedure('public." + publish + "')") == 2
    markers = [chunk for chunk in re.split(r"\)::int", executable) if "select" in chunk]
    assert len(markers) == 5
    for chunk in markers:
        reads_a_body = re.search(r"\bdefinition\b", chunk) is not None
        is_invariants = "to_regprocedure('public.validate_flow_definition_insert()')" in chunk
        assert reads_a_body == is_invariants, chunk
        if reads_a_body:
            assert "operator_publish_flow_change_v1" not in chunk
            for invariant in (
                "flow_definition_identity_mismatch",
                "flow_definition_version_not_next",
                "flow_definition_unresolved_value",
                "flow_definition_zone_must_remain_constant",
            ):
                assert f"position('{invariant}' in definition) > 0" in chunk, invariant


def test_person_activity_fingerprint_is_service_role_only() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261016000200 va justo antes de la de 20261016000300 (la
    # compra con nuestro link).
    fingerprint = sql.split("'20261016000200'", 1)[1].split("'20261016000300'", 1)[0]
    fingerprint = fingerprint.rsplit("union all", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261016000200_portable_person_activity_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",2,'portable_person_activity'")
    for marker in (
        "to_regprocedure('public.get_portable_person_activity_v1(text,text,text)')",
        "andprosecdef",
        "position('person_activity:begin'indefinition)>0",
        "position('public._whatsapp_phone_variants(p_whatsapp_phone)'indefinition)>0",
        "andhas_function_privilege('service_role',oid,'EXECUTE')",
        "andnothas_function_privilege('anon',oid,'EXECUTE')",
        "andnothas_function_privilege('authenticated',oid,'EXECUTE')",
    ):
        assert marker in compact_fingerprint, marker


def test_person_activity_link_purchase_fingerprint_reads_the_link_purchases() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261016000300 va justo antes de la de 20261016000400 (el link
    # del producto que nombra el agente).
    fingerprint = sql.split("'20261016000300'", 1)[1].split("'20261016000400'", 1)[0]
    fingerprint = fingerprint.rsplit("union all", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261016000300_portable_person_activity_link_purchase_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",1,'portable_person_activity_link_purchase'")
    for marker in (
        "to_regprocedure('public.get_portable_person_activity_v1(text,text,text)')",
        "andprosecdef",
        "position('person_activity:linkpurchase'indefinition)>0",
        "position('public.checkout_link_issuances'indefinition)>0",
        "position('purchase_matched'indefinition)>0",
    ):
        assert marker in compact_fingerprint, marker


def test_checkout_issuance_for_funnel_fingerprint_is_service_role_only() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261016000400 va justo antes de la de 20261018000100 (el
    # cupon personal del carrito).
    fingerprint = sql.split("'20261016000400'", 1)[1].split("'20261018000100'", 1)[0]
    fingerprint = fingerprint.rsplit("union all", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261016000400_portable_checkout_issuance_for_funnel_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",2,'portable_checkout_issuance_for_funnel'")
    for marker in (
        "to_regprocedure('public.reserve_portable_checkout_issuance_for_funnel_v1("
        "uuid,text,bigint,bigint,bigint,text,text,text,timestamptz)')",
        "andprosecdef",
        "position('public.commercial_ally_runtime_bindings'indefinition)>0",
        "position('''blocked_funnel'''indefinition)>0",
        "position('offer.funnel_ref=p_funnel_ref'indefinition)>0",
        "position('replay_offer.funnel_ref=p_funnel_ref'indefinition)>0",
        "has_function_privilege('service_role',oid,'EXECUTE')",
        "andnothas_function_privilege('anon',oid,'EXECUTE')",
        "andnothas_function_privilege('authenticated',oid,'EXECUTE')",
    ):
        assert marker in compact_fingerprint, marker


def test_recovery_coupons_fingerprint_is_service_role_only() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261018000100 va justo antes de la de 20261018000200 (el
    # link del primer mensaje del carrito).
    fingerprint = sql.split("'20261018000100'", 1)[1].split("'20261018000200'", 1)[0]
    fingerprint = fingerprint.rsplit("union all", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261018000100_recovery_coupons_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",4,'recovery_coupons'")
    for marker in (
        "to_regclass('public.recovery_coupons')",
        "andc.relrowsecurity",
        "andnothas_table_privilege('service_role',c.oid,'SELECT')",
        "tgname='recovery_coupon_immutable'",
        "to_regprocedure('public.claim_due_recovery_coupons_v1("
        "timestamptz,text,integer,text,interval,interval,numeric,integer)')",
        "to_regprocedure('public.settle_recovery_coupon_v1("
        "uuid,text,text,bigint,bigint,bigint,text,timestamptz,timestamptz)')",
        "to_regprocedure('public.get_recovery_coupon_for_case_v1(uuid,smallint)')",
        "andprosecdef",
        "has_function_privilege('service_role',oid,'EXECUTE')",
        "andnothas_function_privilege('anon',oid,'EXECUTE')",
    ):
        assert marker in compact_fingerprint, marker


def test_recovery_checkout_issuance_fingerprint_is_service_role_only() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261018000200 va justo antes de la de 20261019000100 (la
    # ficha de pago).
    fingerprint = sql.split("'20261018000200'", 1)[1].split("'20261019000100'", 1)[0]
    fingerprint = fingerprint.rsplit("union all", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261018000200_recovery_checkout_issuance_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",9,'recovery_checkout_issuance'")
    for marker in (
        "attnamein('recovery_case_id','delivery_attempt_id')",
        "andnotattnotnull",
        "conname='checkout_link_issuances_origin_shape'",
        "indexname='checkout_link_issuances_delivery_attempt_idx'",
        "'old.delivery_attempt_idisdistinctfromnew.delivery_attempt_id'",
        "to_regprocedure('public.reserve_recovery_checkout_issuance_v1("
        "uuid,text,bigint,uuid,text,timestamptz)')",
        "to_regprocedure('public.authorize_recovery_checkout_issuance_v1("
        "uuid,uuid,text,bigint,timestamptz)')",
        "andprosecdef",
        "has_function_privilege('service_role',oid,'EXECUTE')",
        "andnothas_function_privilege('anon',oid,'EXECUTE')",
        "position('_close_recovery_case_for_checkout_purchase_v1'indefinition)>0",
        "andnotprosecdef",
        "andnothas_function_privilege('service_role',oid,'EXECUTE')",
        "'chatwoot_conversation_idisdistinctfromp_chatwoot_conversation_id'",
    ):
        assert marker in compact_fingerprint, marker


def test_payment_slip_first_contact_fingerprint_is_service_role_only() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261019000100 (C1, la ficha de pago) va justo antes de la de
    # 20261019000200 (B1 del pago rechazado).
    fingerprint = sql.split("'20261019000100'", 1)[1].split("'20261019000200'", 1)[0]
    fingerprint = fingerprint.rsplit("union all", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261019000100_portable_payment_slip_first_contact_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",8,'portable_payment_slip_first_contact'")
    for marker in (
        "to_regclass('public.commercial_ally_payment_slip_details')",
        "andc.relrowsecurity",
        "andnothas_table_privilege('service_role',c.oid,'SELECT')",
        "tgname='commercial_ally_payment_slip_details_append_only'",
        "conname='pilot_scope_versions_source_event_type_check'",
        "conname='recovery_case_events_event_role_check'",
        "selectcount(*)=5",
        "to_regprocedure('public.admit_portable_hotmart_payment_slip_v2("
        "text,text,integer,text,jsonb,text,text)')",
        "to_regprocedure('public.get_portable_payment_slip_send_context("
        "uuid,text,bigint,timestamptz)')",
        "selectcount(*)=4",
        "andnothas_function_privilege('service_role',oid,'EXECUTE')",
        "position('payment_slip_first_contact:begin'indefinition)>0",
        "position('payment_failure'indefinition)=0",
    ):
        assert marker in compact_fingerprint, marker


def test_payment_failure_variants_fingerprint_is_service_role_only() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261019000200 (B1 del pago rechazado) va justo antes de la de
    # 20261019000300 (la compra de cualquier oferta).
    fingerprint = sql.split("'20261019000200'", 1)[1].split("'20261019000300'", 1)[0]
    fingerprint = fingerprint.rsplit("union all", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261019000200_portable_payment_failure_variants_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",6,'portable_payment_failure_variants'")
    for marker in (
        "to_regprocedure('public.get_portable_payment_failure_pilot_runtime_status("
        "text,integer,text,text,text)')",
        "to_regprocedure('public.get_portable_payment_failure_send_context_v1("
        "uuid,text,bigint,timestamptz)')",
        "andprosecdef",
        "to_regprocedure('public.payment_failure_refusal_kind(text,text)')",
        "to_regprocedure('public.payment_failure_wants_first_coupon(uuid)')",
        "andnothas_function_privilege('service_role',oid,'EXECUTE')",
        "position('payment_failure_count'indefinition)>0",
        "position('72hours'indefinition)>0",
        "position('least(due_at,now())'indefinition)=0",
        "position('payment_failure_not_a_refusal'indefinition)>0",
        "position('payment_failure_wants_first_coupon'indefinition)>0",
        "position('payment_failure_variants'indefinition)>0",
    ):
        assert marker in compact_fingerprint, marker


def test_purchase_stops_any_offer_fingerprint_is_last_and_service_role_only() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261019000300 (la compra de cualquier oferta), seguida de
    # 20261019000400 (los recordatorios de la ficha).
    fingerprint = sql.split("'20261019000300'", 1)[1].split("'20261019000400'", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261019000300_portable_purchase_stops_any_offer_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(
        ",2,'portable_purchase_stops_any_offer'unionallselect"
    )
    for marker in (
        "to_regprocedure('public._portable_case_purchase_stop_reason(uuid)')",
        "andprosecdef",
        "has_function_privilege('service_role',oid,'EXECUTE')",
        "andnothas_function_privilege('anon',oid,'EXECUTE')",
        "position('portable_purchase_stops_any_offer'indefinition)>0",
        "position('public._portable_case_purchase_stop_reason(v_case.id)'indefinition)>0",
        "position('''purchase_detected_any_offer'''indefinition)>0",
    ):
        assert marker in compact_fingerprint, marker


def test_payment_slip_reminders_fingerprint_is_service_role_only() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261019000400 (los recordatorios de la ficha) va justo antes
    # de la de 20261019000500 (el cupon de otro caso que llego).
    fingerprint = sql.split("'20261019000400'", 1)[1].split("'20261019000500'", 1)[0]
    fingerprint = fingerprint.rsplit("union all", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261019000400_portable_payment_slip_reminders_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",4,'portable_payment_slip_reminders'")
    for marker in (
        "to_regprocedure('public.mark_portable_payment_slip_request_started"
        "(uuid,uuid,text,bigint,timestamptz)')",
        "to_regprocedure('public.get_portable_payment_slip_send_context"
        "(uuid,text,bigint,timestamptz)')",
        "position('slip_sequence.reason=''payment_slip'''indefinition)>0",
        "position('action.action_type=''no_reply_review'''indefinition)>0",
        "position('v_action.action_type=''no_reply_review'''indefinition)>0",
        "to_regprocedure('public.reevaluate_followup_action(uuid,text,bigint,timestamptz,"
        "boolean,text,text,timestamptz,text,boolean,boolean,boolean,boolean,boolean)')",
        "position('portable_payment_slip_reminders(20261019000400)'indefinition)>0",
        "to_regprocedure('public.admit_portable_inbound_commercial_case_v1(text,integer,bigint,text)')",
        "position('portable_payment_slip_reminders:begin'indefinition)>0",
    ):
        assert marker in compact_fingerprint, marker


def test_payment_failure_other_coupon_delivered_fingerprint() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261019000500 (el cupon de otro caso que llego) va justo
    # antes de la de 20261019000600 (los pasos con plantilla y cupon).
    fingerprint = sql.split("'20261019000500'", 1)[1].split("'20261019000600'", 1)[0]
    fingerprint = fingerprint.rsplit("union all", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261019000500_payment_failure_other_coupon_delivered_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",3,'payment_failure_other_coupon_delivered'")
    for marker in (
        "to_regprocedure('public.recovery_coupon_was_delivered(uuid)')",
        "andprosecdef",
        "andnothas_function_privilege('service_role',oid,'EXECUTE')",
        "andnothas_function_privilege('anon',oid,'EXECUTE')",
        "andnothas_function_privilege('authenticated',oid,'EXECUTE')",
        "to_regprocedure('public.payment_failure_wants_first_coupon(uuid)')",
        "to_regprocedure('public.get_portable_payment_failure_send_context_v1"
        "(uuid,text,bigint,timestamptz)')",
        "position('public.recovery_coupon_was_delivered(coupon.id)'indefinition)>0",
    ):
        assert marker in compact_fingerprint, marker


def test_recovery_followup_steps_fingerprint_is_service_role_only() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261019000600 (los pasos con plantilla y cupon) va justo
    # antes de la de 20261019000700 (el seguimiento del pago rechazado).
    fingerprint = sql.split("'20261019000600'", 1)[1].split("'20261019000700'", 1)[0]
    fingerprint = fingerprint.rsplit("union all", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261019000600_recovery_followup_steps_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",6,'recovery_followup_steps'")
    for marker in (
        "to_regprocedure('public._recovery_followup_step(uuid)')",
        "to_regprocedure('public._recovery_renewal_requested(uuid)')",
        "to_regprocedure('public.request_recovery_renewal_v1(bigint,bigint,bigint,bigint,text,timestamptz)')",
        "andprosecdef",
        "andnothas_function_privilege('anon',oid,'EXECUTE')",
        "position('public._recovery_renewal_requested(v_action.id)'indefinition)>0",
        "position('recovery_followup_steps:begin'indefinition)>0",
        "position('public._recovery_followup_step(scheduled_actions.id)'indefinition)>0",
    ):
        assert marker in compact_fingerprint, marker


def test_payment_failure_followup_fingerprint_is_service_role_only() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261019000700 (el seguimiento del pago rechazado) va justo
    # antes de la de 20261019000800 (la ficha sobrevive a una respuesta).
    fingerprint = sql.split("'20261019000700'", 1)[1].split("'20261019000800'", 1)[0]
    fingerprint = fingerprint.rsplit("union all", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261019000700_payment_failure_followup_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",4,'payment_failure_followup'")
    for marker in (
        "to_regprocedure('public.record_payment_failure_first_touch_v1"
        "(uuid,uuid,text,bigint,text,text,timestamptz)')",
        "to_regprocedure('public.get_payment_failure_followup_context_v1"
        "(uuid,text,bigint,timestamptz)')",
        "andprosecdef",
        "andhas_function_privilege('service_role',oid,'EXECUTE')",
        "andnothas_function_privilege('anon',oid,'EXECUTE')",
        "andnothas_function_privilege('authenticated',oid,'EXECUTE')",
        "tgname='payment_failure_first_touch_record_immutable'",
        "position('payment_failure_followup(20261019000700)'indefinition)>0",
        "position('refusal_sequence.reason=''payment_failure'''indefinition)>0",
        "position('waiting.action_type=''no_reply_review'''indefinition)>0",
    ):
        assert marker in compact_fingerprint, marker


def test_payment_slip_reminder_survives_reply_fingerprint_is_service_role_only() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261019000800 (la respuesta no cancela el recordatorio de la
    # ficha) va justo antes de la de 20261020000100 (la autorizacion del link
    # acepta el intento arrancado).
    fingerprint = sql.split("'20261019000800'", 1)[1].split("'20261020000100'", 1)[0]
    fingerprint = fingerprint.rsplit("union all", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261019000800_portable_payment_slip_reminder_survives_reply_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",1,'portable_payment_slip_reminder_survives_reply'")
    for marker in (
        "to_regprocedure('public.reevaluate_followup_action(uuid,text,bigint,timestamptz,"
        "boolean,text,text,timestamptz,text,boolean,boolean,boolean,boolean,boolean)')",
        "position('portable_payment_slip_reminder_survives_reply(20261019000800)'indefinition)>0",
        "position('reply_slip_sequence.reason=''payment_slip'''indefinition)>0",
    ):
        assert marker in compact_fingerprint, marker


def test_recovery_checkout_authorize_started_attempt_fingerprint() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261020000100 (la autorizacion del link acepta el intento ya
    # arrancado) va justo antes de la del rol del Automation Expert.
    fingerprint = sql.split("'20261020000100'", 1)[1].split("'20261021000100'", 1)[0]
    fingerprint = fingerprint.rsplit("union all", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261020000100_recovery_checkout_authorize_started_attempt_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",1,'recovery_checkout_authorize_started_attempt'")
    for marker in (
        "to_regprocedure('public.authorize_recovery_checkout_issuance_v1"
        "(uuid,uuid,text,bigint,timestamptz)')",
        "position('recovery_checkout_authorize_started_attempt(20261020000100)'indefinition)>0",
        "position('v_attempt.phasenotin(''reserved'',''request_started'')'indefinition)>0",
    ):
        assert marker in compact_fingerprint, marker


def test_automation_expert_change_fingerprint_names_the_role_only_through_to_regrole() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261021000100 (el rol del Automation Expert para proponer
    # cambios de flujo) va justo antes de la de las rutas del motor.
    fingerprint = sql.split("'20261021000100'", 1)[1].split("'20261021000200'", 1)[0]
    fingerprint = fingerprint.rsplit("union all", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261021000100_automation_expert_change_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",5,'automation_expert_change'")
    for marker in (
        "to_regprocedure('public.read_flow_store_v1(jsonb)')",
        "to_regprocedure('public.save_flow_change_draft_v1(jsonb,uuid,integer,jsonb,jsonb,text,text)')",
        "to_regprocedure('public.record_flow_change_validation_v1(jsonb,uuid,integer,jsonb,jsonb,jsonb,jsonb,text)')",
        "to_regprocedure('public.get_flow_change_proposal_v1(jsonb,uuid)')",
        "position('automation_expert_change_approve_simulation(20261021000100)'indefinition)>0",
    ):
        assert marker in compact_fingerprint, marker
    # El inventario corre antes de aplicar, cuando el rol no existe: el rol
    # solo aparece adentro de to_regrole, nunca como texto suelto de un
    # has_function_privilege, que fallaria.
    code = re.sub(r"--[^\n]*", "", fingerprint)
    assert code.count("'automation_expert_change'") == code.count("to_regrole('automation_expert_change')") + 1


def test_flow_engine_routes_fingerprint_reads_are_the_only_grants() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261021000200 (las rutas, las lecturas y la guarda del
    # almacen, la T2.3a del motor de flujos), hasta la del tope de la historia.
    fingerprint = sql.split("'20261021000200'", 1)[1].split("    union all\n    select\n        '20261021000300'", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261021000200_flow_engine_routes_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",6,'flow_engine_routes'")
    for marker in (
        "to_regclass('public.flow_engine_routes')",
        "to_regclass('public.flow_engine_divergences')",
        "andc.relrowsecurity",
        "to_regprocedure('public.read_flow_engine_v1(jsonb,text)')",
        "to_regprocedure('public.read_flow_engine_history_v1(jsonb,text,timestamptz)')",
        "andhas_function_privilege('service_role',oid,'EXECUTE')",
        "andnothas_function_privilege('anon',oid,'EXECUTE')",
        "position('flow_engine_legacy_guard:begin'indefinition)>0",
        "position('flow_engine_shared_pool:begin'indefinition)>0",
        "position('flow_route_moved'inpg_get_constraintdef(con.oid))>0",
    ):
        assert marker in compact_fingerprint, marker
    # El rol de la T4 se nombra solo a traves de to_regrole: el inventario
    # corre tambien antes de aplicar.
    assert "'automation_expert_change'" not in fingerprint.replace("to_regrole('automation_expert_change')", "")


def test_flow_engine_history_cap_fingerprint() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261021000300 (el tope de read_flow_engine_history_v1 en
    # 4.000.000 bytes).
    fingerprint = sql.split("'20261021000300'", 1)[1].split("    union all\n    select\n        '20261021000400'", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261021000300_flow_engine_history_cap_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",1,'flow_engine_history_cap'")
    for marker in (
        "to_regprocedure('public.read_flow_engine_history_v1(jsonb,text,timestamptz)')",
        "position('c_max_bytesconstantinteger:=4000000'indefinition)>0",
        "position('c_max_bytesconstantinteger:=1000000'indefinition)=0",
        "andhas_function_privilege('service_role',oid,'EXECUTE')",
        "andnothas_function_privilege('anon',oid,'EXECUTE')",
    ):
        assert marker in compact_fingerprint, marker


def test_automation_expert_flows_speed_fingerprint() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261021000400 (la lectura de /flows que lee las
    # reevaluaciones una sola vez) va justo antes de la del camino del motor.
    fingerprint = sql.split("'20261021000400'", 1)[1].split("    union all\n    select\n        '20261022000100'", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261021000400_automation_expert_flows_speed_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",1,'automation_expert_flows_speed'")
    for marker in (
        "to_regprocedure('public.read_automation_expert_flows_v1(jsonb)')",
        "position('reevaluatedasmaterialized'indefinition)>0",
        "andprovolatile='s'",
        "andhas_function_privilege('service_role',oid,'EXECUTE')",
        "andnothas_function_privilege('anon',oid,'EXECUTE')",
        "andnothas_function_privilege('authenticated',oid,'EXECUTE')",
    ):
        assert marker in compact_fingerprint, marker


def test_flow_engine_motor_fingerprint_grants_four_to_service_role() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261022000100 (el camino del motor para el carrito, la T2.3b)
    # va justo antes de la del bridge del motor.
    fingerprint = sql.split("'20261022000100'", 1)[1].split("    union all\n    select\n        '20261022000200'", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261022000100_flow_engine_motor_carrito_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",5,'flow_engine_motor_carrito'")
    for marker in (
        "to_regprocedure('public.admit_flow_engine_hotmart_v1(uuid,uuid,text,text,text,timestamptz,bigint,bigint,"
        "text,text,integer,text,integer,boolean)')",
        "to_regprocedure('public.mark_flow_engine_request_started_v1(uuid,uuid,text,bigint,timestamptz)')",
        "to_regprocedure('public.flow_engine_execution_context_v1(uuid)')",
        "to_regprocedure('public._flow_engine_run_turned_off(uuid)')",
        "position('flow_engine_motor_delegation:begin'indefinition)>0",
        "position('flow_engine_motor_authorization:begin'indefinition)>0",
        "position('flow_engine_turned_off:begin'indefinition)>0",
        "position('flow_engine_route_flow_on:begin'indefinition)>0",
        "position('flow_engine_motor_guard:begin'indefinition)>0",
        "tgname='pilot_proactive_contact_caps_no_engine_route'",
    ):
        assert marker in compact_fingerprint, marker


def test_flow_engine_bridge_reads_fingerprint_grants_three_to_service_role() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261022000200 (lo que el bridge del motor lee y marca, la
    # T2.4) va justo antes de la de las lecturas que ven el motor.
    fingerprint = sql.split("'20261022000200'", 1)[1].split(
        "    union all\n    select\n        '20261022000300'", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261022000200_flow_engine_bridge_reads_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",2,'flow_engine_bridge_reads'")
    for marker in (
        "to_regprocedure('public.read_flow_engine_routes_v1(jsonb)')",
        "to_regprocedure('public.read_flow_engine_divergences_v1(jsonb,boolean,integer)')",
        "to_regprocedure('public.mark_flow_engine_divergence_notified_v1(uuid,timestamptz)')",
        "andprosecdef",
        "andnothas_function_privilege('anon',oid,'EXECUTE')",
        "position('notified_atisnull'indefinition)>0",
    ):
        assert marker in compact_fingerprint, marker


def test_flow_engine_reads_see_motor_fingerprint_comes_before_the_capture() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261022000300 (las tres lecturas que cuentan lo que
    # autoriza el motor) va justo antes de la del modo «solo guardar».
    fingerprint = sql.split("'20261022000300'", 1)[1].split(
        "    union all\n    select\n        '20261024000100'", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261022000300_flow_engine_reads_see_motor_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",2,'flow_engine_reads_see_motor'")
    for marker in (
        "to_regprocedure('public.read_flow_engine_history_v1(jsonb,text,timestamptz)')",
        "to_regprocedure('public.read_automation_expert_flows_v1(jsonb)')",
        "to_regprocedure('public.read_automation_expert_lead_history_v1(jsonb,uuid)')",
        "position('flow_engine_reads_see_motor_v1'indefinition)>0",
        "position('public.flow_engine_request_authorizations'indefinition)>0",
        "andnothas_function_privilege('anon',oid,'EXECUTE')",
        "position('flow_engine_request_start_authorized'indefinition)>0",
    ):
        assert marker in compact_fingerprint, marker


def test_portable_hotmart_listen_only_capture_fingerprint_comes_before_the_native_engine() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261024000100 (el modo «solo guardar» de Hotmart, E-057) va
    # justo antes de la del motor nativo.
    fingerprint = sql.split("'20261024000100'", 1)[1].split(
        "    union all\n    select\n        '20261024000200'", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261024000100_portable_hotmart_listen_only_capture.sql'" in fingerprint
    assert compact_fingerprint.endswith(",2,'portable_hotmart_listen_only_capture'")
    for marker in (
        "to_regprocedure('public.admit_portable_hotmart_listen_only_capture_v1"
        "(text,text,integer,text,jsonb,text,text)')",
        "andprosecdef",
        "andnothas_function_privilege('anon',oid,'EXECUTE')",
        "andnothas_function_privilege('authenticated',oid,'EXECUTE')",
        "position('listen_only_capture'indefinition)>0",
    ):
        assert marker in compact_fingerprint, marker


def test_flow_engine_native_fingerprint_comes_before_the_opted_out_reply() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261024000200 (el motor nativo, E-060) va justo antes de la
    # de F-036.
    fingerprint = sql.split("'20261024000200'", 1)[1].split(
        "    union all\n    select\n        '20261024000400'", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261024000200_flow_engine_native_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",3,'flow_engine_native'")
    for marker in (
        "to_regclass('public.flow_engine_pools')",
        "to_regclass('public.flow_cohort_members')",
        "to_regclass('public.flow_engine_native_runs')",
        "andc.relrowsecurity",
        "to_regprocedure('public.admit_flow_engine_event_v1(jsonb,jsonb)')",
        "andprosecdef",
        "andnothas_function_privilege('anon',oid,'EXECUTE')",
        "position('flow_engine_native_excluded'indefinition)>0",
    ):
        assert marker in compact_fingerprint, marker


def test_opted_out_reply_fingerprint_comes_before_the_native_bridge() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261024000400 (F-036, quien se dio de baja y escribe por su
    # cuenta recibe el link si lo pide) va justo antes de la del bridge nativo.
    fingerprint = sql.split("'20261024000400'", 1)[1].split(
        "    union all\n    select\n        '20261024000500'", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261024000400_opted_out_reply_checkout_issuance.sql'" in fingerprint
    assert compact_fingerprint.endswith(",2,'opted_out_reply_checkout_issuance'")
    for marker in (
        "'public._chatwoot_reply_follows_opt_out_v1(uuid,bigint,bigint,bigint,text,text)'",
        "andnothas_function_privilege('service_role',oid,'EXECUTE')",
        "andnothas_function_privilege('anon',oid,'EXECUTE')",
        "andnothas_function_privilege('authenticated',oid,'EXECUTE')",
        "count(*)=4",
        "position('_chatwoot_reply_follows_opt_out_v1('indefinition)>0",
    ):
        assert marker in compact_fingerprint, marker


def test_flow_engine_native_bridge_fingerprint_is_before_the_new_flow() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261024000500 (el bridge del motor nativo, E-065) va justo antes de la del AE que crea flujos.
    fingerprint = sql.split("'20261024000500'", 1)[1].split(
        "    union all\n    select\n        '20261024000600'", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261024000500_flow_engine_native_bridge_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",3,'flow_engine_native_bridge'")
    for marker in (
        "to_regclass('public.flow_engine_native_event_queue')",
        "to_regclass('public.flow_engine_rejection_notices')",
        "count(*)=7",
        "andnothas_function_privilege('anon',oid,'EXECUTE')",
        "position('flow_engine_native_adoption'indefinition)>0",
    ):
        assert marker in compact_fingerprint, marker


def test_flow_change_new_flow_fingerprint_is_last() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    # La fila de 20261024000600 (el AE que crea flujos, E-070) es la ultima.
    fingerprint = sql.split("'20261024000600'", 1)[1].split("select\n    version,", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", re.sub(r"--[^\n]*", "", fingerprint))

    assert "'20261024000600_flow_change_new_flow_v1.sql'" in fingerprint
    assert compact_fingerprint.endswith(",3,'flow_change_new_flow')")
    for marker in (
        "to_regprocedure('public.read_flow_engine_native_trigger_counts_v1(jsonb,text,text,timestamptz)')",
        "to_regprocedure('public.operator_flow_cohort_v1(jsonb,text,text,text,text,text,text)')",
        "count(*)=3",
        "andnothas_function_privilege('anon',oid,'EXECUTE')",
        "position('flow_change_new_flow:begin(registraryabrir)'indefinition)>0",
    ):
        assert marker in compact_fingerprint, marker
