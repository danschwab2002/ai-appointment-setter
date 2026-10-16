"""La paridad del motor sobre lineas de tiempo chicas, escritas a mano (T1).

Diseno del motor de flujos, §3.1 y §5.3. Cada prueba arma una captura minima
con la forma de ``captura-paridad-recuperacion-v2`` (la misma que valida
``Capture.from_json``) y mira una cosa:

* ``eligible_at``: un carrito de dia, uno de noche, el control pausado, el
  tope del dia agotado (con las autorizaciones observadas antes del instante),
  el tope total, y la version del recorrido (lo en curso sigue con la suya);
* P6: un primer contacto que cede por la intencion desde otro telefono, uno
  que cede por la persona con su caso en curso, uno sin ganador, y un ganador
  sin cancelacion;
* el reporte: nunca antes, la tolerancia y lo que la explica, y las
  diferencias con nombre.

Los datos son inventados y no copian a nadie: uuid de la forma
``00000000-0000-4000-8000-<n>``, claves de persona que son el numero en hex y
la configuracion de ATT1 (el scope ``att1-recuperacion``, la politica
``att1-recuperacion-un-toque``, de 09:00 a 21:00 en America/Mexico_City, que es
UTC-6 todo el anio).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from bridge import flow_parity
from bridge.flow_capture import QUERY_NAME, count_sections
from bridge.flow_parity import (
    Capture,
    CaptureError,
    control_state_at,
    eligible_at,
    parity_report,
    send_timings,
    supersessions,
    winners_without_cancel,
)

SCOPE = "att1-recuperacion"
POLICY = "att1-recuperacion-un-toque"
ZONE = "America/Mexico_City"
PRODUCT = "5071808"
OFFER = "gopi6lh7"
DESDE = "2026-10-08T22:42:19.000000Z"
CAPTURED_AT = "2026-10-16T21:00:00.000000Z"


def uid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def person(number: int) -> str:
    return f"{number:016x}"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def z(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def shifted(text: str, **delta: float) -> str:
    return z(at(text) + timedelta(**delta))


def scope_row(version: int, *, per_day: int = 40, total: int = 1000) -> dict[str, Any]:
    return {
        "scope_key": SCOPE, "version": version, "status": "published", "tenant_key": "lancemos",
        "source": "hotmart", "source_event_type": "PURCHASE_OUT_OF_SHOPPING_CART",
        "additional_source_event_types": ["PURCHASE_CANCELED"], "purpose": "cart_recovery",
        "timezone": ZONE, "external_product_id": PRODUCT, "offer_code": OFFER,
        "additional_offer_codes": [], "policy_key": POLICY, "policy_version": 1,
        "audience_mode": "consented_intent", "max_outbound_request_starts_per_day": per_day,
        "max_outbound_request_starts_total": total, "published_at": "2026-10-01T00:00:00.000000Z",
    }


PRECHECKOUT_SCOPE = {
    **scope_row(2, per_day=10, total=150), "scope_key": "att1-primer-contacto", "source": "landing",
    "source_event_type": "PRECHECKOUT_FORM_SUBMITTED", "additional_source_event_types": [],
    "policy_key": "att1-primer-contacto-formulario",
}

POLICY_ROW = {
    "policy_key": POLICY, "version": 1, "status": "published", "purpose": "cart_recovery",
    "timezone": ZONE, "business_windows": [{"days": [1, 2, 3, 4, 5, 6, 7], "start": "09:00", "end": "21:00"}],
    "grace_period": "00:00:00", "expires_after": "1 day", "max_automatic_messages": 1,
    "steps": [{"mode": "freeform", "step_key": "first_contact"}],
    "published_at": "2026-10-01T00:00:00.000000Z",
}


def control_event(created_at: str, version: int, generation: int, to_state: str | None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "scope_key": SCOPE, "scope_version": version, "runtime_generation": generation,
        "created_at": created_at,
    }
    if to_state is None:
        row.update(event_type="pilot_scope_version_activated", reason_code="pilot_scope_version_activated")
    else:
        row.update(event_type="pilot_runtime_state_changed", reason_code="pilot_runtime_state_changed",
                   to_state=to_state)
    return row


# El scope v4 activado y armado antes de la ventana.
ARMED_V4 = [
    control_event("2026-10-08T22:42:19.000000Z", 4, 1, None),
    control_event("2026-10-08T22:50:00.000000Z", 4, 2, "armed"),
]


def cart(number: int, anchor: str, *, who: int, intent: int | None = None,
         event_type: str = "PURCHASE_OUT_OF_SHOPPING_CART") -> dict[str, Any]:
    """Un carrito (o un pago fallido) que Hotmart creo en ``anchor`` y llego un segundo despues."""
    row: dict[str, Any] = {
        "id": uid(number), "event_type": event_type, "received_at": shifted(anchor, seconds=1),
        "processing_status": "processed", "creation_date_ms": int(at(anchor).timestamp() * 1000),
        "product_id": PRODUCT, "offer_code": OFFER, "person_key": person(who), "funnel_ref": "att1",
    }
    if intent is not None:
        row["cart_correlation"] = {"outcome": "resolved", "purchase_intent_id": uid(intent),
                                   "reason_code": "exact_email_and_phone", "observed_at": anchor}
    return row


def contact(who: int) -> dict[str, Any]:
    return {"id": uid(9000 + who), "person_keys": [person(who)], "digits_keys": [], "email_keys": []}


def case_of(event: dict[str, Any], *, who: int, version: int = 4, closed_at: str | None = None,
            status: str = "sequence_exhausted") -> dict[str, Any]:
    anchor = z(datetime.fromtimestamp(event["creation_date_ms"] / 1000, timezone.utc))
    row: dict[str, Any] = {
        "id": uid(1000 + int(event["id"][-6:])), "contact_id": uid(9000 + who),
        "abandonment_event_id": event["id"], "external_product_id": PRODUCT, "offer_code": OFFER,
        "status": status, "grace_expires_at": anchor, "created_at": shifted(event["received_at"], seconds=1),
        "policy_key": POLICY, "policy_version": 1, "has_conversation": closed_at is not None,
        "binding": {"scope_key": SCOPE, "scope_version": version, "source_event_id": event["id"],
                    "audience_mode": "consented_intent", "bound_at": event["received_at"]},
        "eventos": [{"webhook_event_id": event["id"], "event_role": "cart_abandonment", "observed_at": anchor}],
    }
    if closed_at is not None:
        row["closed_at"] = closed_at
    return row


def action_of(case: dict[str, Any], *, sent_at: str | None = None, status: str | None = None,
              tramos: list[dict[str, Any]] | None = None, updated_at: str | None = None) -> dict[str, Any]:
    anchor = case["grace_expires_at"]
    if tramos is None:
        tramos = []
        if sent_at is not None:
            tramos.append({"n": 1, "primero": {
                "attempt_number": 1, "mode": "approved_template", "phase": "completed",
                "outcome": "accepted_by_chatwoot", "reason_code": "accepted_by_chatwoot",
                "started_at": shifted(sent_at, seconds=-1), "request_started_at": sent_at,
                "accepted_at": shifted(sent_at, seconds=1)}})
    if status is None:
        status = "accepted_by_chatwoot" if sent_at is not None else "pending"
    return {
        "id": uid(2000 + int(case["id"][-6:]) - 1000), "recovery_case_id": case["id"],
        "action_type": "first_contact_review", "status": status, "due_at": anchor,
        "expires_at": shifted(anchor, days=1), "step_key": "first_contact",
        "anchor_type": "cart_abandonment", "anchor_observed_at": anchor,
        "execution_attempt_count": sum(tramo["n"] for tramo in tramos),
        "terminal_reason": status if status != "pending" else None,
        "created_at": case["created_at"], "updated_at": updated_at or sent_at or case["created_at"],
        "intentos": tramos,
    }


def authorization(number: int, authorized_at: str, local_day: str, *, version: int = 4) -> dict[str, Any]:
    return {"scope_key": SCOPE, "scope_version": version, "action_id": uid(7000 + number),
            "contact_id": uid(9999), "local_budget_date": local_day, "runtime_generation": 2,
            "authorized_at": authorized_at}


def build(*, events: list[dict[str, Any]] = (), cases: list[dict[str, Any]] = (),
          actions: list[dict[str, Any]] = (), authorizations: list[dict[str, Any]] = (),
          first_contacts: list[dict[str, Any]] = (), contacts: list[dict[str, Any]] = (),
          control: list[dict[str, Any]] | None = None,
          scopes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Una captura minima con la forma de la v2, y sus conteos recontados."""
    data: dict[str, Any] = {
        "_captured": {"query": QUERY_NAME, "desde": DESDE, "captured_at": CAPTURED_AT, "conteos": {}},
        "config": {
            "pilot_scope_versions": (scopes or [scope_row(4)]) + [PRECHECKOUT_SCOPE],
            "followup_policy_versions": [POLICY_ROW],
            "pilot_runtime_controls": [],
            "pilot_proactive_contact_caps": [],
            "pilot_control_events_control_level": control or ARMED_V4,
        },
        "eventos": list(events), "conflictos_de_compra": [], "intenciones": [],
        "contactos": list(contacts), "casos": list(cases), "acciones": list(actions),
        "autorizaciones": list(authorizations), "reevaluaciones": [],
        "primer_contacto": list(first_contacts),
    }
    data["_captured"]["conteos"] = count_sections(data)
    return data


def one_cart(anchor: str, *, version: int = 4, sent_at: str | None = None, **extra: Any) -> tuple[Capture, dict]:
    event = cart(1, anchor, who=1, intent=101)
    case = case_of(event, who=1, version=version, closed_at=sent_at)
    action = action_of(case, sent_at=sent_at)
    capture = Capture.from_json(build(events=[event], cases=[case], actions=[action],
                                      contacts=[contact(1)], **extra))
    return capture, capture.actions[0]


# ---------------------------------------------------------------------------
# El cargador y el control.
# ---------------------------------------------------------------------------

def test_the_loader_rejects_counts_that_do_not_match_the_rows() -> None:
    data = build(events=[cart(1, "2026-10-10T18:00:00.000000Z", who=1)])
    data["_captured"]["conteos"]["disparos_desde"] = 2
    with pytest.raises(CaptureError, match="disparos_desde"):
        Capture.from_json(data)


def test_the_loader_rejects_a_key_outside_the_closed_list() -> None:
    data = build()
    data["eventos"] = [{**cart(1, "2026-10-10T18:00:00.000000Z", who=1), "phone": "x"}]
    with pytest.raises(CaptureError, match="clave fuera de la lista"):
        Capture.from_json(data)


def test_the_control_follows_the_events_and_an_activation_leaves_it_inactive() -> None:
    capture = Capture.from_json(build(control=ARMED_V4 + [
        control_event("2026-10-10T17:00:00.000000Z", 4, 3, "paused"),
        control_event("2026-10-10T17:00:05.000000Z", 5, 4, None),
        control_event("2026-10-10T17:00:10.000000Z", 5, 5, "armed"),
    ]))
    assert control_state_at(capture, SCOPE, at("2026-10-09T12:00:00Z")) == (4, "armed")
    assert control_state_at(capture, SCOPE, at("2026-10-10T17:00:00Z")) == (4, "paused")
    assert control_state_at(capture, SCOPE, at("2026-10-10T17:00:07Z")) == (5, "inactive")
    assert control_state_at(capture, SCOPE, at("2026-10-10T17:00:10Z")) == (5, "armed")
    with pytest.raises(CaptureError, match="sin estado antes del instante"):
        control_state_at(capture, SCOPE, at("2026-10-08T00:00:00Z"))
    with pytest.raises(ValueError, match="instant_without_zone"):
        control_state_at(capture, SCOPE, datetime(2026, 10, 10, 12))


# ---------------------------------------------------------------------------
# eligible_at.
# ---------------------------------------------------------------------------

def test_a_day_cart_is_eligible_at_its_anchor() -> None:
    # 18:00Z son las 12:00 en CDMX: la franja esta abierta y la gracia es 0.
    capture, action = one_cart("2026-10-10T18:00:00.000000Z")
    assert eligible_at(capture, action) == at("2026-10-10T18:00:00Z")


def test_a_night_cart_waits_for_nine_in_mexico_city() -> None:
    # 05:00Z son las 23:00 del dia anterior en CDMX: espera a las 09:00 (15:00Z).
    capture, action = one_cart("2026-10-10T05:00:00.000000Z")
    assert eligible_at(capture, action) == at("2026-10-10T15:00:00Z")


def test_the_end_minute_of_the_window_is_still_open() -> None:
    # 21:00:30 en CDMX: el minuto de fin entra entero (to_char HH24:MI).
    capture, action = one_cart("2026-10-11T03:00:30.000000Z")
    assert eligible_at(capture, action) == at("2026-10-11T03:00:30Z")


def test_a_paused_control_waits_for_the_next_armed() -> None:
    paused = ARMED_V4 + [
        control_event("2026-10-10T17:00:00.000000Z", 4, 3, "paused"),
        control_event("2026-10-10T18:30:00.000000Z", 4, 4, "armed"),
    ]
    capture, action = one_cart("2026-10-10T18:00:00.000000Z", control=paused)
    assert eligible_at(capture, action) == at("2026-10-10T18:30:00Z")


def test_a_control_rearmed_at_night_waits_for_the_window_too() -> None:
    paused = ARMED_V4 + [
        control_event("2026-10-10T17:00:00.000000Z", 4, 3, "paused"),
        control_event("2026-10-11T04:00:00.000000Z", 4, 4, "armed"),
    ]
    capture, action = one_cart("2026-10-10T18:00:00.000000Z", control=paused)
    assert eligible_at(capture, action) == at("2026-10-11T15:00:00Z")


def test_a_control_paused_until_it_expires_never_sends() -> None:
    paused = ARMED_V4 + [control_event("2026-10-10T17:00:00.000000Z", 4, 3, "paused")]
    capture, action = one_cart("2026-10-10T18:00:00.000000Z", control=paused)
    assert eligible_at(capture, action) is None


def test_the_day_cap_spent_before_the_instant_waits_for_the_next_local_day() -> None:
    # Dos autorizaciones del 10/10 local antes del carrito, con un tope de 2 por dia.
    spent = [authorization(1, "2026-10-10T15:00:01.000000Z", "2026-10-10"),
             authorization(2, "2026-10-10T16:00:00.000000Z", "2026-10-10")]
    capture, action = one_cart("2026-10-10T20:00:00.000000Z", authorizations=spent,
                               scopes=[scope_row(4, per_day=2)])
    assert eligible_at(capture, action) == at("2026-10-11T15:00:00Z")


def test_the_cap_counts_only_the_authorizations_observed_before_the_instant() -> None:
    # La segunda autorizacion del dia llega despues del carrito: a la hora del
    # carrito queda cupo, y no se cuenta lo que todavia no paso.
    spent = [authorization(1, "2026-10-10T15:00:01.000000Z", "2026-10-10"),
             authorization(2, "2026-10-10T21:00:00.000000Z", "2026-10-10")]
    capture, action = one_cart("2026-10-10T20:00:00.000000Z", authorizations=spent,
                               scopes=[scope_row(4, per_day=2)])
    assert eligible_at(capture, action) == at("2026-10-10T20:00:00Z")


def test_the_cap_of_another_local_day_does_not_count() -> None:
    # 2026-10-10T03:00Z es el 09/10 en CDMX: esas dos son de otro dia local.
    spent = [authorization(1, "2026-10-10T02:00:00.000000Z", "2026-10-09"),
             authorization(2, "2026-10-10T03:00:00.000000Z", "2026-10-09")]
    capture, action = one_cart("2026-10-10T18:00:00.000000Z", authorizations=spent,
                               scopes=[scope_row(4, per_day=2)])
    assert eligible_at(capture, action) == at("2026-10-10T18:00:00Z")


def test_the_total_cap_spent_never_sends() -> None:
    spent = [authorization(1, "2026-10-09T15:00:01.000000Z", "2026-10-09"),
             authorization(2, "2026-10-09T16:00:00.000000Z", "2026-10-09")]
    capture, action = one_cart("2026-10-10T18:00:00.000000Z", authorizations=spent,
                               scopes=[scope_row(4, per_day=2, total=2)])
    assert eligible_at(capture, action) is None


def test_a_new_version_does_not_stop_what_was_running_and_its_caps_are_the_runs() -> None:
    # El carrito llega con la v4 y el tope del dia de la v4 (2) gastado. Se
    # activa la v5 (80 por dia): el legado lo rechazaria por la version hasta
    # vencer; el motor sigue con la v4 y espera al dia siguiente.
    control = ARMED_V4 + [
        control_event("2026-10-11T00:20:23.000000Z", 4, 3, "paused"),
        control_event("2026-10-11T00:20:27.000000Z", 5, 4, None),
        control_event("2026-10-11T00:20:37.000000Z", 5, 5, "armed"),
    ]
    spent = [authorization(1, "2026-10-10T15:00:01.000000Z", "2026-10-10"),
             authorization(2, "2026-10-10T16:00:00.000000Z", "2026-10-10")]
    scopes = [scope_row(4, per_day=2), scope_row(5, per_day=80)]
    capture, action = one_cart("2026-10-10T22:00:00.000000Z", control=control, authorizations=spent,
                               scopes=scopes)
    assert eligible_at(capture, action) == at("2026-10-11T15:00:00Z")
    # Los topes son los de la version del recorrido: a las 00:21Z (18:21 del
    # 10/10 en CDMX), uno de la v4 sigue esperando al dia siguiente y uno de la
    # v5 sale en el momento, sobre la misma bolsa.
    capture, action = one_cart("2026-10-11T00:21:00.000000Z", control=control, authorizations=spent,
                               scopes=scopes)
    assert eligible_at(capture, action) == at("2026-10-11T15:00:00Z")
    capture, action = one_cart("2026-10-11T00:21:00.000000Z", version=5, control=control,
                               authorizations=spent, scopes=scopes)
    assert eligible_at(capture, action) == at("2026-10-11T00:21:00Z")


# ---------------------------------------------------------------------------
# P3 en el reporte.
# ---------------------------------------------------------------------------

def test_a_send_one_tenth_of_a_second_later_than_e_is_on_time() -> None:
    capture, _ = one_cart("2026-10-10T18:00:00.000000Z", sent_at="2026-10-10T18:00:00.100000Z")
    report = parity_report(capture, [])
    assert report.ok()
    assert report.sends == 1 and report.delay["max"] == pytest.approx(0.1)


def test_never_before_allows_the_second_between_the_clocks_and_nothing_more() -> None:
    # El ancla es el reloj de Hotmart: R puede quedar hasta un segundo antes.
    capture, _ = one_cart("2026-10-10T18:00:00.000000Z", sent_at="2026-10-10T17:59:59.400000Z")
    assert parity_report(capture, []).ok()
    capture, _ = one_cart("2026-10-10T18:00:00.000000Z", sent_at="2026-10-10T17:59:58.500000Z")
    report = parity_report(capture, [])
    assert not report.ok()
    assert report.never_before_violations == (uid(1),)


def test_a_send_before_e_cannot_be_named() -> None:
    capture, _ = one_cart("2026-10-10T05:00:00.000000Z", sent_at="2026-10-10T05:00:10.000000Z")
    named = [{"disparo": uid(1), "diferencia": "envio_antes_de_E", "clase": "C", "nombre": "de_noche",
              "cita": "src/bridge/worker.py:705"}]
    report = parity_report(capture, named)
    assert report.never_before_violations == (uid(1),)
    kinds = {item["diferencia"] for item in report.unexplained}
    assert kinds == {"envio_antes_de_E", "nombrada_invalida:nunca_antes_no_se_acepta"}


def test_the_tolerance_is_five_minutes() -> None:
    capture, _ = one_cart("2026-10-10T18:00:00.000000Z", sent_at="2026-10-10T18:05:00.000000Z")
    assert parity_report(capture, []).ok()
    capture, _ = one_cart("2026-10-10T18:00:00.000000Z", sent_at="2026-10-10T18:05:01.000000Z")
    report = parity_report(capture, [])
    assert report.over_tolerance == (uid(1),)
    assert [item["diferencia"] for item in report.unexplained] == ["envio_fuera_de_tolerancia"]


def test_a_failure_before_the_request_explains_the_delay() -> None:
    event = cart(1, "2026-10-10T18:00:00.000000Z", who=1, intent=101)
    case = case_of(event, who=1, closed_at="2026-10-10T18:07:00.000000Z")
    tramos = [
        {"n": 1, "primero": {"attempt_number": 1, "mode": "approved_template", "phase": "finalized",
                             "outcome": "failed_before_request", "reason_code": "chatwoot_unavailable",
                             "started_at": "2026-10-10T18:00:05.000000Z",
                             "finalized_next_attempt_at": "2026-10-10T18:01:05.000000Z"}},
        {"n": 1, "primero": {"attempt_number": 2, "mode": "approved_template", "phase": "completed",
                             "outcome": "accepted_by_chatwoot", "reason_code": "accepted_by_chatwoot",
                             "started_at": "2026-10-10T18:06:59.000000Z",
                             "request_started_at": "2026-10-10T18:07:00.000000Z"}},
    ]
    action = action_of(case, tramos=tramos, status="accepted_by_chatwoot",
                       updated_at="2026-10-10T18:07:01.000000Z")
    capture = Capture.from_json(build(events=[event], cases=[case], actions=[action], contacts=[contact(1)]))
    [timing] = send_timings(capture)
    assert timing.explained_by == "reintento_tras_fallo_antes_del_pedido"
    report = parity_report(capture, [])
    assert report.over_tolerance == (uid(1),)
    assert report.ok()


def _expired_cart_capture() -> Capture:
    event = cart(1, "2026-10-10T18:00:00.000000Z", who=1, intent=101)
    case = case_of(event, who=1, status="expired", closed_at="2026-10-11T18:00:10.000000Z")
    action = action_of(case, status="expired", updated_at="2026-10-11T18:00:10.000000Z")
    return Capture.from_json(build(events=[event], cases=[case], actions=[action], contacts=[contact(1)]))


def test_an_eligible_cart_that_expired_without_a_send_is_a_difference() -> None:
    capture = _expired_cart_capture()
    report = parity_report(capture, [])
    assert [(item["ref"], item["diferencia"]) for item in report.unexplained] == [(uid(1), "elegible_sin_envio")]
    named = [{"disparo": uid(1), "diferencia": "elegible_sin_envio", "clase": "C",
              "nombre": "lo_en_curso_sigue", "caso": "G21"}]
    report = parity_report(capture, named)
    assert report.ok() and report.named == {"lo_en_curso_sigue": 1} and report.named_total == 1


def test_a_cart_cut_before_e_is_not_a_p3_difference() -> None:
    event = cart(1, "2026-10-10T05:00:00.000000Z", who=1, intent=101)
    case = case_of(event, who=1, status="won", closed_at="2026-10-10T06:00:00.000000Z")
    action = action_of(case, status="cancelled", updated_at="2026-10-10T06:00:00.000000Z")
    capture = Capture.from_json(build(events=[event], cases=[case], actions=[action], contacts=[contact(1)]))
    assert parity_report(capture, []).ok()


# ---------------------------------------------------------------------------
# Las diferencias con nombre.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("change", "reason"), [
    ({"caso": None}, "sin_caso_ni_cita"),
    ({"cita": "src/bridge/worker.py:1054-1070"}, "caso_y_cita"),
    ({"caso": "G25"}, "caso_invalido"),
    ({"caso": None, "cita": "worker.py"}, "cita_invalida"),
    ({"clase": "—"}, "clase_invalida"),
    ({"nombre": "Lo-En-Curso"}, "nombre_invalido"),
    ({"disparo": "carrito-1"}, "disparo_sin_uuid"),
    ({"diferencia": "otra"}, "diferencia_desconocida"),
    ({"telefono": "x"}, "clave_desconocida"),
])
def test_a_named_difference_without_its_case_or_its_citation_explains_nothing(
        change: dict[str, Any], reason: str) -> None:
    entry = {"disparo": uid(1), "diferencia": "elegible_sin_envio", "clase": "C",
             "nombre": "lo_en_curso_sigue", "caso": "G21"}
    entry.update(change)
    entry = {key: value for key, value in entry.items() if value is not None}
    report = parity_report(_expired_cart_capture(), [entry])
    assert not report.ok()
    kinds = sorted(item["diferencia"] for item in report.unexplained)
    assert kinds == sorted(["elegible_sin_envio", f"nombrada_invalida:{reason}"])
    assert report.named_total == 0


def test_a_named_difference_that_the_parity_does_not_see_is_reported() -> None:
    capture, _ = one_cart("2026-10-10T18:00:00.000000Z", sent_at="2026-10-10T18:00:05.000000Z")
    named = [{"disparo": uid(1), "diferencia": "elegible_sin_envio", "clase": "C",
              "nombre": "lo_en_curso_sigue", "caso": "G21"}]
    report = parity_report(capture, named)
    assert [item["diferencia"] for item in report.unexplained] == ["nombrada_sin_diferencia"]


def test_a_named_difference_counts_once() -> None:
    entry = {"disparo": uid(1), "diferencia": "elegible_sin_envio", "clase": "C",
             "nombre": "lo_en_curso_sigue", "caso": "G21"}
    report = parity_report(_expired_cart_capture(), [entry, dict(entry)])
    assert [item["diferencia"] for item in report.unexplained] == ["nombrada_repetida"]
    assert report.named_total == 1


# ---------------------------------------------------------------------------
# P6, quien le gana a quien.
# ---------------------------------------------------------------------------

def plan(intent: int, who: int | None, created_at: str, *, status: str, reason: str | None,
         closed_at: str | None = None, sent_at: str | None = None, number: int = 1) -> dict[str, Any]:
    row: dict[str, Any] = {
        "purchase_intent_id": uid(intent), "outcome": "planned", "reason_code": "first_contact_scheduled",
        "created_at": created_at, "recovery_case_id": uid(5000 + number), "accion_status": status,
        "accion_due_at": shifted(created_at, hours=1),
    }
    if who is not None:
        row["contact_id"] = uid(9000 + who)
    if reason is not None:
        row["accion_terminal_reason"] = reason
    if closed_at is not None:
        row["accion_cerrada_at"] = closed_at
    if sent_at is not None:
        row["request_started_at"] = sent_at
    return row


def test_a_first_contact_cedes_to_a_cart_of_its_intent_from_another_phone() -> None:
    # El formulario es de la persona 1; el carrito llega desde otro telefono
    # (persona 2), con la correlacion resuelta a la misma intencion.
    event = cart(1, "2026-10-10T17:30:00.000000Z", who=2, intent=101)
    case = case_of(event, who=2, closed_at="2026-10-10T17:30:15.000000Z")
    action = action_of(case, sent_at="2026-10-10T17:30:15.000000Z")
    first = plan(101, 1, "2026-10-10T17:00:00.000000Z", status="cancelled", reason="superseded_by_provider_event",
                 closed_at="2026-10-10T18:00:00.000000Z")
    capture = Capture.from_json(build(events=[event], cases=[case], actions=[action],
                                      contacts=[contact(1), contact(2)], first_contacts=[first]))
    [ceded] = supersessions(capture)
    assert (ceded.winner_ref, ceded.matched_by) == (uid(1), "intencion")
    report = parity_report(capture, [])
    assert report.ok() and report.supersessions == 1
    assert report.supersessions_by == {"persona": 0, "intencion": 1}


def test_matching_only_by_person_misses_the_cart_from_another_phone(monkeypatch: pytest.MonkeyPatch) -> None:
    event = cart(1, "2026-10-10T17:30:00.000000Z", who=2, intent=101)
    case = case_of(event, who=2, closed_at="2026-10-10T17:30:15.000000Z")
    first = plan(101, 1, "2026-10-10T17:00:00.000000Z", status="cancelled", reason="superseded_by_provider_event",
                 closed_at="2026-10-10T18:00:00.000000Z")
    capture = Capture.from_json(build(events=[event], cases=[case], actions=[action_of(case, sent_at=case["closed_at"])],
                                      contacts=[contact(1), contact(2)], first_contacts=[first]))
    monkeypatch.setattr(flow_parity, "MATCH_ORDER", ("persona",))
    report = parity_report(capture, [])
    assert report.supersessions_without_winner == (uid(5001),)
    assert [item["diferencia"] for item in report.unexplained] == ["cesion_sin_ganador"]


def test_a_first_contact_cedes_to_its_persons_cart_while_the_cart_is_running() -> None:
    # Otro producto de intencion: el carrito correlaciono a otra intencion (102),
    # pero su caso, del mismo contacto, esta en curso cuando el primer contacto no se planifica.
    event = cart(1, "2026-10-10T05:00:00.000000Z", who=1, intent=102)
    case = case_of(event, who=1, closed_at="2026-10-10T15:00:10.000000Z")
    action = action_of(case, sent_at="2026-10-10T15:00:10.000000Z")
    first = {"purchase_intent_id": uid(101), "contact_id": uid(9001), "outcome": "not_planned",
             "reason_code": "superseded_by_provider_event", "created_at": "2026-10-10T06:00:00.000000Z"}
    capture = Capture.from_json(build(events=[event], cases=[case], actions=[action],
                                      contacts=[contact(1)], first_contacts=[first]))
    [ceded] = supersessions(capture)
    assert (ceded.plan_ref, ceded.winner_ref, ceded.matched_by) == (uid(101), uid(1), "persona")


def test_a_persons_cart_already_sent_does_not_win_without_the_intent() -> None:
    # El caso del carrito ya termino (salio a las 18:00:10) cuando el primer
    # contacto cede a las 19:00, y el carrito es de otra intencion: no hay
    # ganador. Es la regla del legado (20261001000200:297-313) y de le_gana_a.
    event = cart(1, "2026-10-10T18:00:00.000000Z", who=1, intent=102)
    case = case_of(event, who=1, closed_at="2026-10-10T18:00:10.000000Z")
    action = action_of(case, sent_at="2026-10-10T18:00:10.000000Z")
    first = plan(101, 1, "2026-10-10T17:00:00.000000Z", status="cancelled", reason="superseded_by_provider_event",
                 closed_at="2026-10-10T19:00:00.000000Z")
    capture = Capture.from_json(build(events=[event], cases=[case], actions=[action],
                                      contacts=[contact(1)], first_contacts=[first]))
    report = parity_report(capture, [])
    assert report.supersessions_without_winner == (uid(5001),)


def test_a_cart_that_wins_a_waiting_first_contact_that_still_went_out_is_a_difference() -> None:
    # El primer contacto esperaba (lo planificaron a las 17:00) y el carrito de
    # su intencion llego a las 17:30; aun asi salio a las 18:00.
    event = cart(1, "2026-10-10T17:30:00.000000Z", who=1, intent=101)
    case = case_of(event, who=1, closed_at="2026-10-10T17:30:15.000000Z")
    action = action_of(case, sent_at="2026-10-10T17:30:15.000000Z")
    first = plan(101, 1, "2026-10-10T17:00:00.000000Z", status="accepted_by_chatwoot",
                 reason="accepted_by_chatwoot", closed_at="2026-10-10T18:00:01.000000Z",
                 sent_at="2026-10-10T18:00:00.000000Z")
    capture = Capture.from_json(build(events=[event], cases=[case], actions=[action],
                                      contacts=[contact(1)], first_contacts=[first]))
    assert winners_without_cancel(capture) == ((uid(1), uid(5001)),)
    report = parity_report(capture, [])
    assert report.winners_without_cancel == (uid(1),)
    assert [item["diferencia"] for item in report.unexplained] == ["ganador_sin_cancelacion"]


def test_a_cart_of_the_same_person_that_never_ran_does_not_win() -> None:
    # Un carrito sin caso (la correlacion no resolvio) de la misma persona no le
    # gana al primer contacto que esperaba: sale, como hoy.
    event = {**cart(1, "2026-10-10T17:30:00.000000Z", who=1), "processing_status": "failed",
             "processing_error": "pilot_scope_rejected:pilot_audience_intent_unresolved"}
    first = plan(101, 1, "2026-10-10T17:00:00.000000Z", status="accepted_by_chatwoot",
                 reason="accepted_by_chatwoot", closed_at="2026-10-10T18:00:01.000000Z",
                 sent_at="2026-10-10T18:00:00.000000Z")
    capture = Capture.from_json(build(events=[event], contacts=[contact(1)], first_contacts=[first]))
    report = parity_report(capture, [])
    assert report.ok() and report.winners_without_cancel == ()
    assert report.reached_flow == 1 and report.firings == {
        "carrito/pilot_scope_rejected:pilot_audience_intent_unresolved": 1}


def test_a_case_whose_event_is_not_its_contacts_person_is_a_finding() -> None:
    event = cart(1, "2026-10-10T18:00:00.000000Z", who=2, intent=101)
    case = case_of(event, who=1, closed_at="2026-10-10T18:00:10.000000Z")
    action = action_of(case, sent_at="2026-10-10T18:00:10.000000Z")
    capture = Capture.from_json(build(events=[event], cases=[case], actions=[action], contacts=[contact(1)]))
    report = parity_report(capture, [])
    assert [item["diferencia"] for item in report.unexplained] == ["persona_no_coincide"]
