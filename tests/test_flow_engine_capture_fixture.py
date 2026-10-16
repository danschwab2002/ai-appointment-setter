"""El constructor del fixture de la captura de ATT1 (§6.4), y lo que el fixture del repo no perdio.

Diseno del motor de flujos (2026-10-16), §5.3, §6.4 y §3.2.

La primera parte corre el constructor sobre una captura chica escrita a mano,
con la forma de la salida de ``att1_recovery_capture_query_v2.sql`` y datos
de juguete (uuids y claves de persona inventados, ningun payload de Hotmart):
recorta la siembra, saca las constantes (y ``restore_fixture`` las devuelve
iguales), saca los nulos, pasa las horas a Z, ordena las claves, rechaza una
clave fuera de la lista y rechaza pasar de 1 MB. Lo que se espera se escribe
sin el constructor: el recorte a mano y las horas con su propia regex.

La segunda parte mira el fixture del repo
(``tests/fixtures/flow_engine/att1_recovery_capture_20261016.json``): los
conteos de la consulta, la conciliacion de los 252 disparos desde la v3 y el
origen (el sha256 de la captura de los anexos y el de la consulta).
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from bridge import flow_capture as fc
from bridge.flow_definition import loads_strict

ROOT = Path(__file__).resolve().parents[1]
FLOW_ENGINE = ROOT / "tests" / "fixtures" / "flow_engine"
SCRIPT = ROOT / "scripts" / "flow_engine_capture_fixture.py"
SPEC = importlib.util.spec_from_file_location("flow_engine_capture_fixture", SCRIPT)
assert SPEC and SPEC.loader
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)

SHA = "c0ffee" * 10 + "c0de"
PHONE = "+5215512345678"
PHONE_DIGITS = "5512345678"
DESDE = "2026-10-08T22:42:19.5+00:00"


def _id(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-0000000ca97e"


def _person(n: int) -> str:
    return f"ab{n:014x}"


# Los uuid de la captura chica, por nombre.
E_SEED, E_CART, E_CANCEL, E_BUY = _id(1), _id(2), _id(3), _id(4)
C_SEED, C_CART = _id(11), _id(12)
A_SEED, A_CART = _id(21), _id(22)
I_SEED_EVENT, I_CART, I_CANCEL, I_SEED_PLAN, I_PLAN, I_NOT_PLANNED, I_UNTOUCHED = (_id(n) for n in range(31, 38))
K_ONE, K_TWO = _id(41), _id(42)


def _attempt(number: int, phase: str, *, sent: bool) -> dict[str, Any]:
    return {
        "attempt_number": number, "mode": "approved_template", "phase": phase,
        "outcome": "accepted_by_chatwoot" if sent else None,
        "started_at": "2026-10-09T15:00:01.25+00:00",
        "request_started_at": "2026-10-09T15:00:02+00:00" if sent else None,
        "accepted_at": "2026-10-09T15:00:03.123456+00:00" if sent else None,
        "reason_code": "accepted_by_chatwoot" if sent else None,
        "finalized_next_attempt_at": None, "reconciliation_resolution": None, "reconciled_at": None,
    }


def _event(event_id: str, event_type: str, received_at: str, person: int, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": event_id, "event_type": event_type, "received_at": received_at,
        "processed_at": received_at, "processing_status": "processed", "processing_error": None,
        "creation_date_ms": 1_760_000_000_000 + person, "approved_date_ms": None,
        "purchase_shape_ok": None, "product_id": "1234567", "offer_code": "abc12345",
        "purchase_status": None, "person_key": _person(person), "digits_key": None,
        "email_key": None, "funnel_ref": "att1",
        "cart_correlation": None, "payment_failure_correlation": None,
    }
    row.update(extra)
    return row


def _intent(intent_id: str, submitted_at: str, offer: str, person: int,
            state: str = "waiting_for_purchase") -> dict[str, Any]:
    return {
        "id": intent_id, "funnel_ref": "att1", "product_ref": "X1234567Z", "offer_ref": offer,
        "submitted_at": submitted_at, "lifecycle_state": state,
        "current_classification": "confirmed_abandonment" if state == "cancelled" else None,
        "whatsapp_contact_authorized": True, "provisional": False,
        "provider_observed": True, "activation_authorized": True,
        "updated_at": "2026-10-12T00:00:00+00:00", "person_key": _person(person),
    }


def _plan(intent_id: str, created_at: str, *, planned: bool) -> dict[str, Any]:
    return {
        "purchase_intent_id": intent_id, "contact_id": K_TWO if planned else None,
        "recovery_case_id": _id(51) if planned else None, "scope_key": "att1-primer-contacto",
        "scope_version": 2, "outcome": "planned" if planned else "not_planned",
        "reason_code": "first_contact_scheduled" if planned else "superseded_by_provider_event",
        "created_at": created_at, "caso_status": "sequence_exhausted" if planned else None,
        "accion_status": "accepted_by_chatwoot" if planned else None,
        "accion_terminal_reason": "accepted_by_chatwoot" if planned else None,
        "accion_due_at": "2026-10-09T17:00:00+00:00" if planned else None,
        "accion_expires_at": "2026-10-10T16:00:00+00:00" if planned else None,
        "accion_cerrada_at": "2026-10-09T17:00:05.5+00:00" if planned else None,
        "request_started_at": "2026-10-09T17:00:04+00:00" if planned else None,
    }


def _capture() -> dict[str, Any]:
    """Una captura chica con la forma de la consulta v2: dos disparos desde «desde» y la siembra."""
    return {
        "_captured": {
            "origin": "captura chica escrita a mano para la prueba del constructor",
            "query": fc.QUERY_NAME, "captured_at": "2026-10-16T21:03:26.141035+00:00",
            "desde": DESDE, "desde_origen": "pilot_scope_version_activated:att1-recuperacion:v>=3",
            "siembra": "2026-10-01T22:42:19.5+00:00",
            "redactions": ["personas: sha256 truncado a 16 hex con una sal por corrida"],
            "conteos": {
                "eventos": 4, "disparos_desde": 2, "casos": 2, "acciones": 2, "intentos": 4,
                "intentos_tramos": 2, "envios": 1, "reevaluaciones": 4, "reevaluaciones_tramos": 3,
                "autorizaciones": 3, "primer_contacto": 3, "intenciones": 7, "contactos": 2,
            },
            "censo_webhook_events": [{
                "source": "hotmart", "event_type": "PURCHASE_OUT_OF_SHOPPING_CART", "n": 2,
                "primero": "2026-10-05T10:00:00+00:00", "ultimo": "2026-10-09T15:00:00+00:00",
            }],
        },
        "config": {
            "pilot_scope_versions": [{
                "scope_key": "att1-recuperacion", "version": 3, "status": "published",
                "tenant_key": "lancemos", "source": "hotmart",
                "source_event_type": "PURCHASE_OUT_OF_SHOPPING_CART",
                "additional_source_event_types": ["PURCHASE_CANCELED"], "purpose": "cart_recovery",
                "timezone": "America/Mexico_City", "external_product_id": "1234567",
                "offer_code": "abc12345", "additional_offer_codes": ["def67890"],
                "policy_key": "att1-recuperacion-un-toque", "policy_version": 1,
                "audience_mode": "consented_intent", "max_outbound_request_starts_per_day": 80,
                "max_outbound_request_starts_total": 1000, "published_at": "2026-10-08T22:40:00+00:00",
            }],
            "followup_policy_versions": [{
                "policy_key": "att1-recuperacion-un-toque", "version": 1, "status": "published",
                "purpose": "cart_recovery", "timezone": "America/Mexico_City",
                "business_windows": [{"days": [1, 2, 3, 4, 5, 6, 7], "start": "09:00", "end": "21:00"}],
                "grace_period": "00:00:00", "expires_after": "1 day", "max_automatic_messages": 1,
                "steps": [{"step_key": "first_contact", "mode": "freeform"}],
                "published_at": "2026-10-01T00:00:00+00:00",
            }],
            "pilot_runtime_controls": [{
                "scope_key": "att1-recuperacion", "scope_version": 3, "runtime_state": "armed",
                "generation": 7, "changed_at": "2026-10-08T22:43:00+00:00",
            }],
            "pilot_proactive_contact_caps": [],
            "pilot_control_events_control_level": [
                {"scope_key": "att1-recuperacion", "scope_version": 3,
                 "event_type": "pilot_scope_version_activated", "runtime_generation": 6,
                 "reason_code": "pilot_scope_version_activated", "to_state": None, "created_at": DESDE},
                {"scope_key": "att1-recuperacion", "scope_version": 3,
                 "event_type": "pilot_runtime_state_changed", "runtime_generation": 7,
                 "reason_code": "pilot_runtime_state_changed", "to_state": "armed",
                 "created_at": "2026-10-08T22:43:00+00:00"},
            ],
        },
        "eventos": [
            # Un pago fallido de la siembra: estado, no disparo.
            _event(E_SEED, "PURCHASE_CANCELED", "2026-10-05T10:00:00+00:00", 6, purchase_status="CANCELED",
                   cart_correlation={"outcome": "resolved", "purchase_intent_id": I_SEED_EVENT,
                                     "reason_code": "exact_email", "observed_at": "2026-10-05T10:00:01+00:00"},
                   payment_failure_correlation={"outcome": "resolved", "purchase_intent_id": I_SEED_EVENT}),
            # Un carrito desde «desde», con su caso.
            _event(E_CART, "PURCHASE_OUT_OF_SHOPPING_CART", "2026-10-09T15:00:00+00:00", 1,
                   cart_correlation={"outcome": "resolved", "purchase_intent_id": I_CART,
                                     "reason_code": "exact_email", "observed_at": "2026-10-09T15:00:00.75+00:00"}),
            # Un pago fallido desde «desde» que no llego al flujo.
            _event(E_CANCEL, "PURCHASE_CANCELED", "2026-10-10T03:00:00.25+00:00", 7,
                   processing_status="failed", processing_error="payment_failure_correlation_unresolved",
                   purchase_status="CANCELED", offer_code="def67890",
                   cart_correlation={"outcome": "resolved", "purchase_intent_id": I_CANCEL,
                                     "reason_code": "exact_email_and_phone",
                                     "observed_at": "2026-10-10T03:00:01+00:00"},
                   payment_failure_correlation={"outcome": "unmatched", "purchase_intent_id": None}),
            # Una compra: no es un disparo.
            _event(E_BUY, "PURCHASE_APPROVED", "2026-10-11T12:00:00+00:00", 1,
                   approved_date_ms=1_760_184_000_000, purchase_shape_ok=True, purchase_status="APPROVED",
                   digits_key=_person(3), email_key=_person(4), funnel_ref=None),
        ],
        "conflictos_de_compra": [],
        "intenciones": [
            _intent(I_SEED_EVENT, "2026-10-05T09:00:00+00:00", "abc12345", 6),
            _intent(I_CART, "2026-10-09T14:00:00+00:00", "abc12345", 1, "cancelled"),
            _intent(I_CANCEL, "2026-10-10T02:00:00+00:00", "def67890", 7),
            _intent(I_SEED_PLAN, "2026-10-03T09:00:00+00:00", "abc12345", 8),
            _intent(I_PLAN, "2026-10-09T16:00:00+00:00", "abc12345", 2, "purchased"),
            _intent(I_NOT_PLANNED, "2026-10-12T09:00:00+00:00", "def67890", 9),
            _intent(I_UNTOUCHED, "2026-10-13T09:00:00+00:00", "abc12345", 10),
        ],
        "contactos": [
            {"id": K_ONE, "person_keys": [_person(1)], "digits_keys": [_person(3)], "email_keys": [_person(4)]},
            {"id": K_TWO, "person_keys": [_person(2), _person(5)], "digits_keys": [], "email_keys": []},
        ],
        "casos": [
            {"id": C_SEED, "contact_id": K_ONE, "abandonment_event_id": E_SEED, "purchase_event_id": None,
             "external_product_id": "1234567", "offer_code": "abc12345", "status": "expired",
             "grace_expires_at": "2026-10-05T10:00:00+00:00", "created_at": "2026-10-05T10:00:00.5+00:00",
             "closed_at": "2026-10-06T10:00:00+00:00", "won_at": None,
             "policy_key": "att1-recuperacion-un-toque", "policy_version": 1, "trigger_kind": "payment_failure",
             "purchase_correlation": None, "has_conversation": False,
             "binding": {"scope_key": "att1-recuperacion", "scope_version": 2, "source_event_id": E_SEED,
                         "audience_mode": "consented_intent_in_cohort",
                         "audience_purchase_intent_id": I_SEED_EVENT,
                         "bound_at": "2026-10-05T10:00:00.5+00:00"},
             "eventos": [{"webhook_event_id": E_SEED, "event_role": "payment_failure",
                          "observed_at": "2026-10-05T10:00:00+00:00"}]},
            {"id": C_CART, "contact_id": K_TWO, "abandonment_event_id": E_CART, "purchase_event_id": None,
             "external_product_id": "1234567", "offer_code": "def67890", "status": "sequence_exhausted",
             "grace_expires_at": "2026-10-09T15:00:00+00:00", "created_at": "2026-10-09T15:00:00.5+00:00",
             "closed_at": "2026-10-09T15:00:04+00:00", "won_at": None,
             "policy_key": "att1-recuperacion-un-toque", "policy_version": 1, "trigger_kind": None,
             "purchase_correlation": None, "has_conversation": True,
             "binding": {"scope_key": "att1-recuperacion", "scope_version": 3, "source_event_id": E_CART,
                         "audience_mode": "consented_intent", "audience_purchase_intent_id": I_CART,
                         "bound_at": "2026-10-09T15:00:00.5+00:00"},
             "eventos": [{"webhook_event_id": E_CART, "event_role": "cart_abandonment",
                          "observed_at": "2026-10-09T15:00:00+00:00"}]},
        ],
        "acciones": [
            {"id": A_SEED, "recovery_case_id": C_SEED, "action_type": "first_contact_review",
             "status": "expired", "due_at": "2026-10-05T10:00:00+00:00",
             "expires_at": "2026-10-06T10:00:00+00:00", "step_key": "payment_failure_first_contact",
             "anchor_type": "payment_failure", "anchor_observed_at": "2026-10-05T10:00:00+00:00",
             "next_attempt_at": None, "execution_attempt_count": 3,
             "terminal_reason": "expired_after_worker_crash", "executed_at": None,
             "created_at": "2026-10-05T10:00:00.5+00:00", "updated_at": "2026-10-06T10:00:00+00:00",
             "intentos": [{"n": 3, "primero": _attempt(1, "reserved", sent=False),
                           "ultimo": _attempt(3, "reserved", sent=False)}]},
            {"id": A_CART, "recovery_case_id": C_CART, "action_type": "first_contact_review",
             "status": "accepted_by_chatwoot", "due_at": "2026-10-09T15:00:00+00:00",
             "expires_at": "2026-10-10T15:00:00+00:00", "step_key": "first_contact",
             "anchor_type": "cart_abandonment", "anchor_observed_at": "2026-10-09T15:00:00+00:00",
             "next_attempt_at": None, "execution_attempt_count": 1,
             "terminal_reason": "accepted_by_chatwoot", "executed_at": "2026-10-09T15:00:03.123456+00:00",
             "created_at": "2026-10-09T15:00:00.5+00:00", "updated_at": "2026-10-09T15:00:04+00:00",
             "intentos": [{"n": 1, "primero": _attempt(1, "completed", sent=True), "ultimo": None}]},
        ],
        "autorizaciones": [
            {"scope_key": "att1-recuperacion", "scope_version": 2, "action_id": A_SEED, "contact_id": K_ONE,
             "local_budget_date": "2026-10-05", "runtime_generation": 5,
             "authorized_at": "2026-10-05T10:00:01+00:00"},
            {"scope_key": "att1-recuperacion", "scope_version": 3, "action_id": A_CART, "contact_id": K_TWO,
             "local_budget_date": "2026-10-09", "runtime_generation": 7,
             "authorized_at": "2026-10-09T15:00:01+00:00"},
            {"scope_key": "att1-primer-contacto", "scope_version": 2, "action_id": _id(52), "contact_id": K_TWO,
             "local_budget_date": "2026-10-09", "runtime_generation": 3,
             "authorized_at": "2026-10-09T17:00:03+00:00"},
        ],
        "reevaluaciones": [
            {"recovery_case_id": C_SEED, "related_action_id": A_SEED,
             "event_type": "followup_action_expired", "actor_type": "integration",
             "data": {"reason_code": "expired_after_worker_crash"},
             "n": 1, "primero_at": "2026-10-06T10:00:00+00:00", "ultimo_at": "2026-10-06T10:00:00+00:00"},
            {"recovery_case_id": C_CART, "related_action_id": A_CART,
             "event_type": "followup_action_reevaluated", "actor_type": "system",
             "data": {"decision": "defer", "reason_code": "business_window_closed",
                      "policy_key": "att1-recuperacion-un-toque", "policy_version": 1, "chatwoot_checked": True},
             "n": 2, "primero_at": "2026-10-09T15:00:00.6+00:00", "ultimo_at": "2026-10-09T15:00:00.7+00:00"},
            {"recovery_case_id": C_CART, "related_action_id": A_CART,
             "event_type": "followup_action_reevaluated", "actor_type": "system",
             "data": {"decision": "execute", "reason_code": "eligible_for_execution",
                      "policy_key": "att1-recuperacion-un-toque", "policy_version": 1, "chatwoot_checked": True},
             "n": 1, "primero_at": "2026-10-09T15:00:01+00:00", "ultimo_at": "2026-10-09T15:00:01+00:00"},
        ],
        "primer_contacto": [
            _plan(I_SEED_PLAN, "2026-10-03T09:00:00+00:00", planned=True),
            _plan(I_PLAN, "2026-10-09T16:00:00+00:00", planned=True),
            _plan(I_NOT_PLANNED, "2026-10-12T09:00:00+00:00", planned=False),
        ],
    }


# Lo que el fixture tiene que devolver, escrito sin el constructor: las horas a
# Z con seis cifras, sin nulos y con el recorte a mano.
_HOUR = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,6}))?\+00:00")
_Z_HOUR = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")


def _by_hand(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _by_hand(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_by_hand(item) for item in value]
    if isinstance(value, str) and (match := _HOUR.fullmatch(value)):
        return f"{match.group(1)}.{(match.group(2) or '').ljust(6, '0')}Z"
    return value


def _expected() -> dict[str, Any]:
    capture = _capture()
    capture["primer_contacto"] = [
        {key: plan[key] for key in fc.FIRST_CONTACT_KEYS} for plan in capture["primer_contacto"][1:]]
    # El carrito y el pago fallido desde «desde» (por su correlacion) y los dos
    # planes desde «desde». Afuera: la del pago fallido de la siembra, la del
    # plan de la siembra y la que nadie toca.
    kept = {I_CART, I_CANCEL, I_PLAN, I_NOT_PLANNED}
    capture["intenciones"] = [
        {key: intent[key] for key in fc.INTENT_KEYS} for intent in capture["intenciones"] if intent["id"] in kept]
    return _by_hand(capture)


def _build(capture: dict[str, Any] | None = None) -> dict[str, Any]:
    return fc.build_fixture(_capture() if capture is None else capture, source_sha256=SHA, source_bytes=12345)


def _leaves(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _leaves(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _leaves(item)]
    return [value]


# ---------------------------------------------------------------------------
# 1. El constructor, sobre la captura chica.
# ---------------------------------------------------------------------------

def test_the_hand_written_capture_has_the_shape_of_the_query() -> None:
    assert fc.shape_problems(_capture()) == []
    assert fc.count_sections(_capture()) == _capture()["_captured"]["conteos"]


def test_the_builder_trims_the_seed() -> None:
    fixture = _build()
    plans = fixture["primer_contacto"]
    assert [plan["purchase_intent_id"] for plan in plans] == [I_PLAN, I_NOT_PLANNED]
    assert all(set(plan) <= set(fc.FIRST_CONTACT_KEYS) for plan in plans)
    assert "scope_key" not in json.dumps(plans) and "caso_status" not in json.dumps(plans)
    assert [intent["id"] for intent in fixture["intenciones"]] == [I_CART, I_CANCEL, I_PLAN, I_NOT_PLANNED]
    assert all(set(intent) <= set(fc.INTENT_KEYS) for intent in fixture["intenciones"])
    assert fixture["_captured"]["fixture"]["recortes"] == {
        "primer_contacto": {"captura": 3, "fixture": 2},
        "intenciones": {"captura": 7, "fixture": 4},
    }
    # Lo demas de la siembra queda: el pago fallido, su caso, su accion y su autorizacion son estado.
    assert [event["id"] for event in fixture["eventos"]] == [E_SEED, E_CART, E_CANCEL, E_BUY]
    assert [case["id"] for case in fixture["casos"]] == [C_SEED, C_CART]


def test_the_builder_takes_out_the_constants_and_restore_brings_them_back_equal() -> None:
    fixture = _build()
    constants = fixture["_captured"]["fixture"]["constantes"]
    # Las mismas que en la v2 (§6.4): el producto de los eventos, de las
    # intenciones y de los casos, la politica de los casos y el tipo de las acciones.
    assert constants == {
        "eventos": {"product_id": "1234567"},
        "intenciones": {"product_ref": "X1234567Z"},
        "casos": {"external_product_id": "1234567", "policy_key": "att1-recuperacion-un-toque",
                  "policy_version": 1},
        "acciones": {"action_type": "first_contact_review"},
    }
    for section, values in constants.items():
        assert all(key not in row for row in fixture[section] for key in values), section
    restored = fc.restore_fixture(fixture)
    del restored["_captured"]["fixture"]
    assert restored == _expected()


def test_a_constant_is_a_leaf_present_and_equal_in_every_row() -> None:
    capture = _capture()
    # Un objeto que se repite no es constante; una columna nula en una fila tampoco.
    for case in capture["casos"]:
        case["binding"]["audience_mode"] = "consented_intent"
    capture["acciones"][0]["executed_at"] = capture["acciones"][1]["executed_at"]
    # Un booleano igual en todas las filas si es constante.
    capture["casos"][0]["has_conversation"] = True
    constants = _build(capture)["_captured"]["fixture"]["constantes"]
    assert "binding" not in constants["casos"] and constants["casos"]["has_conversation"] is True
    assert constants["acciones"] == {"action_type": "first_contact_review",
                                     "executed_at": "2026-10-09T15:00:03.123456Z"}
    capture["acciones"][0]["executed_at"] = None
    assert "executed_at" not in _build(capture)["_captured"]["fixture"]["constantes"]["acciones"]


def test_the_builder_drops_the_nulls() -> None:
    fixture = _build()
    assert None not in _leaves(fixture)
    assert "won_at" not in fixture["casos"][0] and "won_at" not in fixture["casos"][1]
    assert "ultimo" not in fixture["acciones"][1]["intentos"][0]
    assert fixture["eventos"][2]["payment_failure_correlation"] == {"outcome": "unmatched"}
    # El control de la activacion no trae to_state: ausente, y se lee nulo.
    assert "to_state" not in fixture["config"]["pilot_control_events_control_level"][0]
    assert fixture["config"]["pilot_proactive_contact_caps"] == []


def test_the_builder_writes_every_hour_in_z_with_six_decimals() -> None:
    fixture = _build()
    text = fc.serialize(fixture).decode("utf-8")
    assert "+00:00" not in text
    hours = [leaf for leaf in _leaves(fixture) if isinstance(leaf, str) and "T" in leaf and ":" in leaf]
    assert hours and all(_Z_HOUR.fullmatch(hour) for hour in hours)
    assert fixture["_captured"]["desde"] == "2026-10-08T22:42:19.500000Z"
    assert fixture["eventos"][1]["received_at"] == "2026-10-09T15:00:00.000000Z"
    assert fixture["eventos"][2]["received_at"] == "2026-10-10T03:00:00.250000Z"
    assert fixture["acciones"][1]["intentos"][0]["primero"]["accepted_at"] == "2026-10-09T15:00:03.123456Z"
    # Lo que no es una hora no se toca: la fecha local, el intervalo y la hora del dia.
    assert fixture["autorizaciones"][1]["local_budget_date"] == "2026-10-09"
    assert fixture["config"]["followup_policy_versions"][0]["grace_period"] == "00:00:00"
    assert fixture["config"]["followup_policy_versions"][0]["business_windows"][0]["start"] == "09:00"


def _reversed(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _reversed(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [_reversed(item) for item in value]
    return value


def test_the_builder_sorts_the_keys_compact_and_deterministic() -> None:
    body = fc.serialize(_build())
    text = body.decode("utf-8")
    assert text == json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    assert text.count("\n") == 1
    assert fc.serialize(_build(_reversed(_capture()))) == body
    assert fc.serialize(_build()) == body


def test_the_builder_writes_where_it_comes_from_and_how_much_it_weighs() -> None:
    fixture = _build()
    meta = fixture["_captured"]["fixture"]
    assert meta["source_sha256"] == SHA and meta["source_bytes"] == 12345
    assert meta["max_bytes"] == fc.FIXTURE_MAX_BYTES == 1_000_000
    assert meta["transformaciones"] == list(fc.TRANSFORMATIONS)
    assert meta["fixture_bytes"] == len(fc.serialize(fixture))
    # Los conteos quedan como los midio la consulta; el recorte se lee aparte.
    assert fixture["_captured"]["conteos"] == _capture()["_captured"]["conteos"]
    counted = fc.count_sections(fc.restore_fixture(fixture))
    for name, value in fixture["_captured"]["conteos"].items():
        trim = meta["recortes"].get(name)
        assert counted[name] == (trim["fixture"] if trim else value), name


@pytest.mark.parametrize(("where", "path"), [
    (("casos", 1, "phone_raw"), "casos[1].phone_raw"),
    (("eventos", 1, "cart_correlation", "payload"), "eventos[1].cart_correlation.payload"),
    (("acciones", 1, "intentos", 0, "primero", "refusal_reason"), "acciones[1].intentos[0].primero.refusal_reason"),
    (("config", "followup_policy_versions", 0, "approved_by"), "config.followup_policy_versions[0].approved_by"),
    (("secuencias",), "secuencias"),
])
def test_the_builder_rejects_a_key_outside_the_list(where: tuple[Any, ...], path: str) -> None:
    capture = _capture()
    target: Any = capture
    for step in where[:-1]:
        target = target[step]
    target[where[-1]] = "Juana, 55 1234 5678"
    with pytest.raises(fc.CaptureShapeError) as caught:
        _build(capture)
    assert caught.value.path == path
    assert "clave fuera de la lista" in str(caught.value)
    assert "Juana" not in str(caught.value) and "1234" not in str(caught.value)


def test_the_builder_rejects_a_phone_in_a_row() -> None:
    capture = _capture()
    capture["eventos"][1]["person_key"] = PHONE
    with pytest.raises(fc.CaptureShapeError) as caught:
        _build(capture)
    assert str(caught.value) == "eventos[1].person_key: no tiene la forma clave_de_persona"


def test_the_builder_rejects_more_than_one_megabyte() -> None:
    capture = _capture()
    capture["contactos"] = [
        {"id": _id(100_000 + n), "person_keys": [_person(3 * n)], "digits_keys": [_person(3 * n + 1)],
         "email_keys": [_person(3 * n + 2)]}
        for n in range(8000)
    ]
    with pytest.raises(fc.FixtureTooLargeError) as caught:
        _build(capture)
    error = caught.value
    assert error.size > error.limit == 1_000_000
    assert max(error.sections, key=error.sections.__getitem__) == "contactos"
    assert set(error.sections) == set(fc.TOP_SECTIONS)
    assert str(error).startswith(f"fixture_too_large: {error.size} bytes, el limite es 1000000 (")


def test_a_fixture_is_not_built_again_and_a_capture_restores_as_a_copy() -> None:
    fixture = _build()
    with pytest.raises(fc.CaptureFixtureError, match="ya es un fixture"):
        fc.build_fixture(fixture, source_sha256=SHA)
    capture = _capture()
    restored = fc.restore_fixture(capture)
    assert restored == capture and restored is not capture
    with pytest.raises(fc.CaptureFixtureError, match="source_sha256"):
        fc.build_fixture(capture, source_sha256="no-es-un-sha")


def test_what_is_recounted_asks_for_its_keys_with_the_path() -> None:
    capture = _capture()
    capture["eventos"][1]["received_at"] = None
    with pytest.raises(fc.CaptureFixtureError, match=r"^eventos\[1\]\.received_at: falta$"):
        _build(capture)
    capture = _capture()
    del capture["acciones"][0]["intentos"][0]["n"]
    with pytest.raises(fc.CaptureFixtureError, match=r"^acciones\[0\]\.intentos\[0\]\.n: falta$"):
        fc.count_sections(capture)


def test_the_cli_builds_the_same_fixture_and_rejects_a_phone_without_printing_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    capture_path = tmp_path / "captura.json"
    raw = json.dumps(_capture()).encode("utf-8")
    capture_path.write_bytes(raw)
    out_path = tmp_path / "fixture.json"
    assert CLI.main(["--capture", str(capture_path), "--out", str(out_path)]) == 0
    expected = fc.build_fixture(_capture(), source_sha256=hashlib.sha256(raw).hexdigest(), source_bytes=len(raw))
    assert out_path.read_bytes() == fc.serialize(expected)
    assert CLI.main(["--check-privacy", str(out_path)]) == 0
    capsys.readouterr()
    capture = _capture()
    capture["eventos"][1]["person_key"] = PHONE
    capture_path.write_text(json.dumps(capture), encoding="utf-8")
    assert CLI.main(["--check-privacy", str(capture_path)]) == 1
    assert CLI.main(["--capture", str(capture_path), "--out", str(tmp_path / "otro.json")]) == 1
    out = capsys.readouterr().out
    assert "eventos[1].person_key: no tiene la forma clave_de_persona" in out
    assert PHONE_DIGITS not in out
    assert not (tmp_path / "otro.json").exists()


# ---------------------------------------------------------------------------
# 2. El fixture del repo no perdio lo que T1 lee.
# ---------------------------------------------------------------------------

REPO_RAW = (FLOW_ENGINE / "att1_recovery_capture_20261016.json").read_bytes()
REPO_FIXTURE: dict[str, Any] = loads_strict(REPO_RAW)
REPO = fc.restore_fixture(REPO_FIXTURE)


def test_the_repo_fixture_comes_from_the_annexed_capture_and_query() -> None:
    meta = REPO_FIXTURE["_captured"]["fixture"]
    # La captura v2 de los anexos del diseno (att1-captura-motor-v2-20261016.json).
    assert meta["source_sha256"] == "df6addecfc2ef92d1c554fbad77bc91f42613a0d49e4a1ff6695ef6ac243e015"
    assert meta["source_bytes"] == 1_400_924
    query = (FLOW_ENGINE / "att1_recovery_capture_query_v2.sql").read_bytes()
    assert hashlib.sha256(query).hexdigest() == "0a63678a135cd1038485b1058db60963af8dd1016094f1c00be2192f3013db51"
    assert REPO_FIXTURE["_captured"]["query"] == fc.QUERY_NAME
    assert REPO_FIXTURE["_captured"]["desde"] == "2026-10-08T22:42:19.893671Z"
    assert REPO_FIXTURE["_captured"]["captured_at"] == "2026-10-16T21:03:26.141035Z"


def test_the_repo_fixture_keeps_the_counts_of_the_capture() -> None:
    # §6.1: los conteos de la corrida del 16/10 a las 21:03:26Z.
    counts = {
        "eventos": 390, "disparos_desde": 252, "casos": 215, "acciones": 215, "intentos": 930,
        "intentos_tramos": 215, "envios": 210, "reevaluaciones": 1962, "reevaluaciones_tramos": 321,
        "autorizaciones": 229, "primer_contacto": 313, "intenciones": 346, "contactos": 278,
    }
    assert REPO_FIXTURE["_captured"]["conteos"] == counts
    trims = REPO_FIXTURE["_captured"]["fixture"]["recortes"]
    assert trims == {"primer_contacto": {"captura": 313, "fixture": 297},
                     "intenciones": {"captura": 346, "fixture": 286}}
    counted = fc.count_sections(REPO)
    for name, value in counts.items():
        assert counted[name] == (trims[name]["fixture"] if name in trims else value), name


def test_the_seed_trim_keeps_every_intent_that_t1_reads() -> None:
    kept = {intent["id"] for intent in REPO["intenciones"]}
    wanted = {plan["purchase_intent_id"] for plan in REPO["primer_contacto"]}
    for event in fc.firings(REPO):
        for name in ("cart_correlation", "payment_failure_correlation"):
            if (event.get(name) or {}).get("purchase_intent_id"):
                wanted.add(event[name]["purchase_intent_id"])
    assert wanted == kept
    desde = fc.parse_instant(REPO["_captured"]["desde"])
    assert all(fc.parse_instant(plan["created_at"]) >= desde for plan in REPO["primer_contacto"])
    # Las constantes de la v2 que nombra §6.4.
    assert {section: sorted(values) for section, values in REPO_FIXTURE["_captured"]["fixture"]["constantes"].items()} == {
        "eventos": ["product_id"], "intenciones": ["product_ref"],
        "casos": ["external_product_id", "policy_key", "policy_version"], "acciones": ["action_type"],
    }


def test_the_252_firings_since_v3_reconcile_from_the_restored_fixture() -> None:
    # §3.2: 239 carritos y 13 pagos fallidos desde la activacion de la v3.
    fired = list(fc.firings(REPO))
    assert Counter(event["event_type"] for event in fired) == {
        "PURCHASE_OUT_OF_SHOPPING_CART": 239, "PURCHASE_CANCELED": 13}
    case_of = {case["abandonment_event_id"]: case for case in REPO["casos"]}
    evidence = {link["webhook_event_id"] for case in REPO["casos"] for link in case.get("eventos", [])
                if link["webhook_event_id"] != case["abandonment_event_id"]}
    [action_of] = [{action["recovery_case_id"]: action for action in REPO["acciones"]}]
    assert len(action_of) == len(REPO["acciones"]) == len(REPO["casos"])
    outcome: Counter[Any] = Counter()
    for event in fired:
        kind = "carrito" if event["event_type"] == "PURCHASE_OUT_OF_SHOPPING_CART" else "pago_fallido"
        if event.get("processing_error"):
            outcome[event["processing_error"]] += 1
        elif event["id"] in case_of:
            action = action_of[case_of[event["id"]]["id"]]
            sent = any(tramo["primero"].get("request_started_at") for tramo in action["intentos"])
            outcome[(kind, "enviado" if sent else action["terminal_reason"])] += 1
        elif event["id"] in evidence:
            outcome[(kind, "evidencia")] += 1
        else:
            outcome[(kind, "sin_caso")] += 1
    assert outcome == {
        # 208 carritos planificados: 203 enviados y 5 vencidos.
        ("carrito", "enviado"): 203,
        ("carrito", "expired_after_worker_crash"): 5,
        # 6 pagos fallidos procesados: 5 casos enviados y 1 sumado como evidencia.
        ("pago_fallido", "enviado"): 5,
        ("pago_fallido", "evidencia"): 1,
        # Los que no llegaron al flujo.
        "pilot_scope_rejected:pilot_audience_intent_unresolved": 26,
        "payment_failure_correlation_unresolved": 5,
        "pilot_scope_rejected:pilot_audience_consented_intent_prior_opt_out": 3,
        "create_recovery_case_failed": 4,
    }
    assert sum(outcome.values()) == 252
    # Desde la v3 no hay ningun pilot_runtime_not_armed.
    assert not any("pilot_runtime_not_armed" in str(key) for key in outcome)
    # 208 envios de disparos de la ventana; la captura trae 210, porque 2 son de
    # casos de la v2 que siguen como estado.
    assert outcome[("carrito", "enviado")] + outcome[("pago_fallido", "enviado")] == 208
    assert fc.count_sections(REPO)["envios"] == 210
