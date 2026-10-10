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
    # La ultima fila: hasta el cierre de la lista de huellas.
    fingerprint = sql.split("'20261010000100'", 1)[1].split(")\nselect", 1)[0]
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
    assert compact_fingerprint.endswith(",4,'portable_conversation_followup_claim'")


def test_supabase_acl_inventory_is_exhaustive_and_allowlisted() -> None:
    sql = ACL_INVENTORY.read_text(encoding="utf-8")
    allowlisted = re.findall(r"\('public\.([a-z0-9_]+\([^']*\))'\)", sql)

    assert len(allowlisted) == 120
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
