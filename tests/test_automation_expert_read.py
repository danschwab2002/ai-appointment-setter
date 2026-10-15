"""El modulo puro de automation-expert-read-v1: la busqueda, el nombre, los
motivos y el cursor (docs/contracts/automation-expert-read-v1.md)."""

from __future__ import annotations

import pytest

from bridge import automation_expert_read as reader
from bridge.automation_expert_read import LookupRejected


@pytest.mark.parametrize(
    "value",
    [
        "+52 1 55 1234 5678",
        "(55) 1234-5678",
        "55.1234.5678",
        "5215512345678",
        "ana 1234567",
        "ana.canary@example.test",
        "escribile a ana@x.y",
    ],
)
def test_a_phone_or_mail_shape_is_not_an_allowed_key(value: str) -> None:
    # Prueba 7: con espacios, guiones, puntos o parentesis, siete digitos o mas
    # es un telefono.
    assert reader.looks_like_identifier(value)
    with pytest.raises(LookupRejected) as caught:
        reader.parse_lookup({"name": value})
    assert caught.value.code == "identifier_not_allowed"


def test_a_conversation_number_with_seven_digits_counts_as_a_phone() -> None:
    with pytest.raises(LookupRejected) as caught:
        reader.parse_lookup({"conversation": 5512345})
    assert caught.value.code == "identifier_not_allowed"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"conversation": 501, "name": "ana"},
        {"conversation": 0},
        {"conversation": True},
        {"issuance": "https://pay.hotmart.com/x?sck=hermes~v1~01JABCDEFGHJKMNPQRSTVWXYZ0"},
        {"issuance": "01JABCDEFGHJKMNPQRSTVWXYZ"},
        {"name": "an"},
        {"name": "an%"},
        {"phone": "ana"},
        [],
        None,
    ],
)
def test_the_lookup_takes_exactly_one_of_three_keys(body: object) -> None:
    with pytest.raises(LookupRejected) as caught:
        reader.parse_lookup(body)
    assert caught.value.code == "invalid_lookup"


def test_the_lookup_accepts_each_key() -> None:
    assert reader.parse_lookup({"conversation": 501}).conversation == 501
    assert reader.parse_lookup({"conversation": "#501"}).conversation == 501
    assert (
        reader.parse_lookup({"issuance": "01JABCDEFGHJKMNPQRSTVWXYZ0"}).issuance
        == "01JABCDEFGHJKMNPQRSTVWXYZ0"
    )
    assert reader.parse_lookup({"name": "  María   José "}).name == "María José"


def test_the_first_name_passes_the_name_word_guard() -> None:
    assert reader.first_name("Ana", "Ana") == "Ana"
    assert reader.first_name(None, "O'Brien") == "O'Brien"
    assert reader.first_name("María del Carmen", "x") == "María del Carmen"
    # Un emoji o un caracter TAG no pasan: va el siguiente nivel o nada.
    assert reader.first_name("Ana\U000e0041", "Beto") == "Beto"
    assert reader.first_name("🙂", "") is None
    assert reader.first_name(None, "5512345678") is None


def test_the_label_never_falls_back_to_the_full_name() -> None:
    assert reader.lead_label("Ana", [17, 501]) == "Ana · conversación #501"
    assert reader.lead_label(None, [501]) == "conversación #501"


# La traduccion del contrato («Asi se traducen los codigos de hoy»), escrita a
# mano: la prueba no puede recorrer el propio catalogo que prueba.
CONTRACT_TRANSLATIONS = {
    "business_window_closed": "fuera_de_horario",
    "pilot_audience_intent_unresolved": "no_entra:permiso",
    "precheckout_intent_not_live": "ya_no_entra",
    "precheckout_authorization_lost": "ya_no_entra",
    "intent_purchased": "compro",
    "purchase_by_identity": "compro",
    "intent_purchase_ambiguous": "compro",
    "contact_opted_out": "baja",
    "pilot_audience_consented_intent_prior_opt_out": "baja",
    "precheckout_contact_already_planned": "ya_en_curso",
    "precheckout_conversation_handoff": "tomada_por_el_equipo",
    "human_takeover_active": "tomada_por_el_equipo",
    "expired": "vencio",
    "expired_after_worker_crash": "vencio",
    "accepted_by_chatwoot": "completo",
    "policy_exhausted": "completo",
}
# Los codigos de ATT1 que salen sin motivo, con su frase (medido el 2026-10-15).
CODES_WITHOUT_REASON = (
    "pilot_contact_not_in_cohort", "precheckout_contact_ambiguous",
    "payment_failure_correlation_unresolved", "pilot_scope_version_mismatch",
    "first_contact_scheduled", "eligible_for_execution", "pilot_request_start_authorized",
    "create_recovery_case_failed", "purchase_correlation_case_not_found", "repeated_payment_failure",
)


@pytest.mark.parametrize("code,reason", sorted(CONTRACT_TRANSLATIONS.items()))
def test_every_contract_code_has_its_reason_and_sentence(code: str, reason: str) -> None:
    # Prueba 10.
    assert reader._reason(code, "stopped", {}) == reason
    assert reader.EXPLANATIONS[code]


@pytest.mark.parametrize("code", CODES_WITHOUT_REASON)
def test_the_codes_without_a_reason_still_have_their_sentence(code: str) -> None:
    assert reader._reason(code, "stopped", {}) is None
    assert reader.EXPLANATIONS[code]


def test_an_unknown_but_well_formed_code_leaves_its_gap() -> None:
    gaps: list[dict[str, object]] = []
    moment = reader._moment_from_database({
        "at": "2026-10-15T12:00:00+00:00", "kind": "stopped", "flow_key": "carrito",
        "flow_version": None, "reason_code": "algo_nuevo_del_motor", "reason_code_rejected": False,
        "decision": "cancel", "certainty": "stored", "attached_by": "case",
        "evidence": {"table": "conversation_events", "id": "11111111-1111-1111-1111-111111111111"},
        "extra": {},
    }, zone=reader.ZoneInfo("America/Mexico_City"), gaps=gaps)
    assert moment.data["reason"] is None and moment.data["explanation"] is None
    assert moment.data["reason_code"] == "algo_nuevo_del_motor"
    assert [gap["code"] for gap in gaps] == ["unknown_reason_code"]


def test_the_reasons_that_depend_on_the_moment() -> None:
    assert reader._reason("superseded_by_provider_event", "stopped", {"winner_flow_key": "carrito"}) == (
        "le_gano:carrito"
    )
    assert reader._reason("superseded_by_provider_event", "stopped", {}) is None
    assert reader._reason("pilot_runtime_not_armed", "rejected_on_entry", {"control_state": "paused"}) == (
        "flujo_pausado"
    )
    # inactive o closed no son una pausa (contrato, «Tres elecciones tecnicas»).
    assert reader._reason("pilot_runtime_not_armed", "rejected_on_entry", {"control_state": "inactive"}) is None
    assert reader._reason("pilot_contact_not_in_cohort", "rejected_on_entry", {}) is None


def test_the_cursor_round_trips_and_rejects_garbage() -> None:
    key = ("2026-10-12T20:26:08.763000+00:00", 1, "chatwoot.messages", "2820", "meta_delivery")
    assert reader.decode_cursor(reader.encode_cursor(key)) == key
    for garbage in ("x", "not a cursor!", reader.encode_cursor(key)[:-3] + "AAA"):
        with pytest.raises(LookupRejected) as caught:
            reader.decode_cursor(garbage)
        assert caught.value.code == "invalid_cursor"


def test_only_the_digits_of_the_meta_error_leave() -> None:
    message = {"content_attributes": {"external_error": "131049: This message was not delivered"}}
    assert reader._meta_error_code(message) == "131049"
    assert reader._meta_error_code({"content_attributes": {"external_error": "Template not found"}}) is None
    assert reader._meta_error_code({"content_attributes": "texto"}) is None


def test_limit_and_flow_key_are_validated_by_hand() -> None:
    assert reader.parse_limit(None) == 50
    assert reader.parse_limit("100") == 100
    for value in ("0", "101", "x"):
        with pytest.raises(LookupRejected) as caught:
            reader.parse_limit(value)
        assert caught.value.code == "limit_out_of_range"
    with pytest.raises(LookupRejected) as caught:
        reader.parse_flow_key("Bad-Key")
    assert caught.value.code == "invalid_flow_key"


def _fixture_conversation(number: str) -> list[dict[str, object]]:
    import json
    from pathlib import Path

    path = (Path(__file__).parent / "fixtures" / "automation_expert"
            / "chatwoot_messages_inbox_11_convs_50_17_52_20261015.json")
    return json.loads(path.read_text())["conversations"][number]["payload"]


def test_the_coupon_view_follows_the_sweep_with_real_messages() -> None:
    from datetime import datetime, timedelta, timezone

    day = 86_400
    # La 52: solo la plantilla del motor, que fallo. Nunca contesto.
    code, _ = reader._coupon_view(_fixture_conversation("52"), now=datetime.now(timezone.utc),
                                  min_age=day, max_age=3 * day)
    assert code == "never_replied"
    # La 50: lo ultimo es de una persona del equipo.
    code, _ = reader._coupon_view(_fixture_conversation("50"), now=datetime.now(timezone.utc),
                                  min_age=day, max_age=3 * day)
    assert code == "last_message_not_from_agent"
    # La 17 hasta la respuesta del agente (2769): lo ultimo es del agente, y la
    # ventana se cuenta desde el ultimo mensaje de la persona (2767).
    until_agent = [message for message in _fixture_conversation("17") if message["id"] <= 2769]
    inbound = next(message for message in until_agent if message["id"] == 2767)
    since = datetime.fromtimestamp(inbound["created_at"], tz=timezone.utc)
    for hours, expected in ((10, "inbound_too_recent"), (25, "eligible_for_execution"), (80, "inbound_too_old")):
        code, measured = reader._coupon_view(until_agent, now=since + timedelta(hours=hours),
                                             min_age=day, max_age=3 * day)
        assert code == expected
        assert measured == since
    # Una nota privada despues del ultimo mensaje de la persona no cambia nada.
    note = dict(next(message for message in _fixture_conversation("17") if message["id"] == 2773))
    code, _ = reader._coupon_view(until_agent + [note], now=since + timedelta(hours=25),
                                  min_age=day, max_age=3 * day)
    assert code == "eligible_for_execution"


def test_the_fallback_never_becomes_the_full_name() -> None:
    # Revision del 2026-10-15: un espacio duro que la base no partio no puede
    # convertir el respaldo en el nombre completo.
    assert reader.first_name(None, "Juan Pérez López") == "Juan"
    assert reader.first_name(None, "Juan Pérez López") == "Juan"
    # Un superindice o un numero en circulo no es una letra.
    assert reader.first_name(None, "⁵⁵¹²³⁴⁵⁶⁷⁸") is None
    assert reader.first_name("①②③", None) is None


def test_unicode_digits_count_as_a_phone() -> None:
    assert reader.looks_like_identifier("⁵⁵¹²³⁴⁵⁶⁷⁸")
    assert reader.looks_like_identifier("ana ٥٥١٢٣٤٥٦٧٨")
    with pytest.raises(LookupRejected) as caught:
        reader.parse_lookup({"conversation": "⁵⁵¹²³⁴"})
    assert caught.value.code == "invalid_lookup"
    with pytest.raises(LookupRejected) as caught:
        reader.parse_lookup({"name": "ana²"})
    assert caught.value.code == "invalid_lookup"


def test_a_code_with_seven_digits_never_leaves() -> None:
    assert reader.code_ok("pilot_runtime_not_armed")
    assert not reader.code_ok("algo:5215512345678")
    assert not reader.code_ok("Error con 5215512345678")


def test_the_chatwoot_links_need_a_clean_https_url() -> None:
    assert reader.conversation_links([7], chatwoot_public_url="https://chat.example.org/", account_id=2) == [
        {"number": 7, "url": "https://chat.example.org/app/accounts/2/conversations/7"}]
    for url in ("http://chat.example.org", "https://user:pass@chat.example.org",
                "https://chat.example.org/?token=x", None):
        assert reader.conversation_links([7], chatwoot_public_url=url, account_id=2)[0]["url"] is None


def test_who_wrote_each_message() -> None:
    # Contrato, «Como reconoce a cada autor».
    actor = reader._actor
    assert actor({"message_type": 0, "sender": {"type": "contact"}}, agent_bot_id=2) == "prospect"
    assert actor({"message_type": 0, "sender": {"type": "user"}}, agent_bot_id=2) == "other"
    assert actor({"message_type": 1, "sender": {"type": "agent_bot", "id": 2}}, agent_bot_id=2) == "ai_agent"
    assert actor({"message_type": 1, "sender": {"type": "agent_bot", "id": 9}}, agent_bot_id=2) == "other"
    assert actor({"message_type": 3, "sender": {"type": "user", "id": 4}}, agent_bot_id=2) == "human_agent"
    handoff = {"message_type": 1, "private": True, "content": "x [supportmagician-handoff:y]"}
    assert actor({**handoff, "sender": {"type": "user", "id": 1}}, agent_bot_id=2) == "system"
    # La marca sola no alcanza: sin el usuario que publica el bridge, es del equipo.
    assert actor({**handoff, "sender": {"type": "agent_bot", "id": 2}}, agent_bot_id=2) == "human_agent"
    assert actor({"message_type": 1, "private": True, "content": "nota", "sender": None}, agent_bot_id=2) == (
        "human_agent")


def _settings(**overrides: object) -> reader.FlowSettings:
    values: dict[str, object] = dict(
        manifest_flows={"descuento": True}, templates={}, time_zone="America/Mexico_City",
        precheckout_enabled=False, pilot_enabled=True, recovery_enabled=True, payment_failure_enabled=True,
        coupon_enabled=True, coupon_template=("att1_seguimiento_descuento_01", "es_MX"), coupon_code="TIROIDES10",
        coupon_product_name="x", coupon_min_age_seconds=86_400, coupon_max_age_seconds=259_200,
        coupon_send_hours=(9, 21), coupon_test_phone_set=False, reactivation_enabled=False,
        reactivation_template=None, reactivation_min_age_seconds=86_400, reactivation_max_age_seconds=2_592_000,
        stalled_monitor_enabled=False, resume_enabled=False, precheckout_test_phone_set=False,
        allowed_jid_set=False,
    )
    values.update(overrides)
    return reader.FlowSettings(**values)  # type: ignore[arg-type]


_INSTANCE = reader.InstanceMark(
    tenant_ref="lancemos", ally_ref="att1", funnel_ref="att1", chatwoot_account_id=2, chatwoot_inbox_id=11,
    hotmart_product_id="5071808", inbound_scope_key="att1-inbound", agent_scope_ref="att1-agent-bot",
    time_zone="America/Mexico_City", product_version="1.8.0", recovery_scope=None, precheckout_scope=None,
)


def _history_raw(adopted: list[int], **now: object) -> dict[str, object]:
    return {
        "database_at": "2026-10-15T12:00:00+00:00",
        "lead": {"lead_ref": "11111111-1111-1111-1111-111111111111", "first_name_inferred": "Ana",
                 "first_name_fallback": "Ana", "conversations": sorted(adopted),
                 "identity": {"has_phone": True, "has_email": True, "whatsapp_authorized": True, "opted_out": False}},
        "moments": [], "upcoming": [],
        "now": {"purchased": False, "opted_out": False, "pending_handoff": False, "human_takeover": False,
                "live_actions": 0, **now},
        "adopted_conversations": [
            {"conversation": number, "coupon_sent": False, "last_agent_turn_at": "2026-10-13T12:00:00+00:00"}
            for number in adopted
        ],
        "gap_counts": {},
    }


def test_the_coupon_end_to_end_with_real_messages() -> None:
    from datetime import datetime, timedelta, timezone

    until_agent = [message for message in _fixture_conversation("17") if message["id"] <= 2769]
    inbound = next(message for message in until_agent if message["id"] == 2767)
    since = datetime.fromtimestamp(inbound["created_at"], tz=timezone.utc)
    reads = [reader.ChatwootRead(17, until_agent), reader.ChatwootRead(50, _fixture_conversation("50"))]
    data, gaps, _ = reader.build_history(
        _history_raw([17, 50]), reads=reads, instance=_INSTANCE, settings=_settings(), agent_bot_id=2,
        chatwoot_public_url=None, now=since + timedelta(hours=10), limit=100, cursor=None)
    # La 17 espera la ventana; la 50 no la tiene porque lo ultimo es del equipo.
    window = next(item for item in data["upcoming"] if item["kind"] == "coupon_window")
    assert window["conversation"] == 17 and window["expected"]["decision"] == "execute"
    assert reader._parse_time(window["due_at"]) >= since + timedelta(hours=24)
    assert reader.ZoneInfo("America/Mexico_City") and 9 <= reader._parse_time(window["due_at"]).astimezone(
        reader.ZoneInfo("America/Mexico_City")).hour < 21
    missed = [moment for moment in data["past"] if moment["kind"] == "coupon_not_sent"]
    assert [(m["conversation"], m["reason"]) for m in missed] == [(50, "tomada_por_el_equipo")]
    # Una compra frena el cupon aunque la ventana este abierta.
    data, _, _ = reader.build_history(
        _history_raw([17], purchased=True), reads=reads[:1], instance=_INSTANCE, settings=_settings(),
        agent_bot_id=2, chatwoot_public_url=None, now=since + timedelta(hours=30), limit=100, cursor=None)
    assert data["upcoming"][0]["expected"]["decision"] == "cancel"
    assert data["upcoming"][0]["expected"]["reason"] == "compro"
    # Sin Chatwoot, la ventana sale de los turnos del agente y queda el hueco.
    data, gaps, _ = reader.build_history(
        _history_raw([17]), reads=[reader.ChatwootRead(17, None, "caido")], instance=_INSTANCE,
        settings=_settings(), agent_bot_id=2, chatwoot_public_url=None,
        now=datetime(2026, 10, 13, 20, tzinfo=timezone.utc), limit=100, cursor=None)
    assert data["upcoming"][0]["expected"]["decision"] == "unknown"
    assert {"chatwoot_not_read", "followup_skip_not_persisted"} <= {gap["code"] for gap in gaps}


def test_the_coupon_moment_keeps_its_place_across_pages() -> None:
    from datetime import datetime, timedelta, timezone

    reads = [reader.ChatwootRead(50, _fixture_conversation("50")), reader.ChatwootRead(52, _fixture_conversation("52"))]
    pages = []
    for minutes in (0, 5):
        now = datetime(2026, 10, 15, 20, tzinfo=timezone.utc) + timedelta(minutes=minutes)
        data, _, cursor = reader.build_history(
            _history_raw([50, 52]), reads=reads, instance=_INSTANCE, settings=_settings(), agent_bot_id=2,
            chatwoot_public_url=None, now=now, limit=100, cursor=None)
        pages.append([(m["kind"], m["evidence"]["id"], m["at"]) for m in data["past"]])
    assert pages[0] == pages[1]


def test_flows_follow_the_runtime_flag_of_each_flow() -> None:
    import json
    from pathlib import Path

    raw = json.loads((Path(__file__).parent / "fixtures" / "automation_expert" / "rpc_flows_att1.json")
                     .read_text())["result"]
    settings = _settings(manifest_flows={"carrito": True, "pago_fallido": True, "precheckout": True},
                         payment_failure_enabled=False, templates={})
    instance = reader.InstanceMark(**{**_INSTANCE.__dict__})
    data, _ = reader.build_flows(raw, instance=instance, settings=settings)
    flows = {flow["flow_key"]: flow for flow in data["flows"]}
    assert flows["carrito"]["status"]["state"] == "on"
    assert flows["pago_fallido"]["status"] == {"state": "off", "detail": "flag_off", "since": None}
    # Un flujo apagado no le gana a nadie.
    assert flows["precheckout"]["priority"]["le_ganan"] == ["carrito"]
    assert flows["precheckout"]["definition"]["pasos"] == [{"esperar": "60m"}]
    assert flows["descuento"]["definition"]["dispara"]["silencio"]["desde"] == "24h"
    assert flows["descuento"]["definition"]["dispara"]["silencio"]["hasta"] == "72h"
