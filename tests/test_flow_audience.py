"""El disparo por audiencia (E-484, H6): la forma, la lectura, las ocurrencias y la lista.

Contrato: docs/contracts/flow-audience-trigger-v1.md. Diseno: mapa-ae-todos-los-flujos.md §2 (vault).
No hay borde externo: todo es puro (sin base, sin Chatwoot, sin reloj).
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from bridge import flow_audience as fa
from bridge import flow_definition as fd

CDMX = "America/Mexico_City"


def difusion(**changes: Any) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "esquema": fd.SCHEMA, "flujo": "difusion", "nombre": "Recordatorio de Black", "estado": "borrador",
        "dispara": {"fecha": {"el": "2026-12-01T10:00", "zona": CDMX}},
        "para": {"es": "prospecta"}, "entra": {"permiso": "formulario"},
        "pasos": [{"plantilla": "black_recordatorio", "idioma": "es_MX", "variables": ["nombre"]}],
    }
    definition.update(changes)
    return definition


def codes(definition: dict[str, Any]) -> set[tuple[str, str]]:
    return {(finding.code, finding.path) for finding in fd.check_definition(definition)}


# -- La forma (regla 1) ------------------------------------------------------

def test_the_three_forms_are_valid_shapes() -> None:
    assert codes(difusion()) == set()
    assert codes(difusion(dispara={"cada": {"dias": ["lun", "jue"], "hora": "10:00", "zona": CDMX,
                                            "desde": "2026-12-01", "hasta": "2026-12-31"}})) == set()
    assert codes(difusion(dispara={"manual": {}}, para={"es": "clienta"}, entra={"permiso": "compra"})) == set()


def test_before_e484_a_date_trigger_was_an_unknown_key() -> None:
    # Sin el cambio, dispara.fecha y para eran unknown_key: el esquema no los conocia.
    assert ("unknown_key", "/dispara/fecha") not in codes(difusion())
    assert ("unknown_key", "/para") not in codes(difusion())


@pytest.mark.parametrize("change, expected", [
    (lambda d: d["dispara"]["fecha"].update(el="2026-12-01 10:00"), ("invalid_value", "/dispara/fecha/el")),
    (lambda d: d["dispara"]["fecha"].update(zona="Marte/Olimpo"), ("invalid_value", "/dispara/fecha/zona")),
    (lambda d: d["dispara"]["fecha"].pop("zona"), ("required_key_missing", "/dispara/fecha/zona")),
    (lambda d: d["dispara"].update(fecha={"el": "2026-12-01T10:00", "zona": CDMX, "otra": 1}),
     ("unknown_key", "/dispara/fecha/otra")),
    (lambda d: d.update(dispara={"cada": {"dias": ["lunes"], "hora": "10:00", "zona": CDMX}}),
     ("invalid_value", "/dispara/cada/dias")),
    (lambda d: d.update(dispara={"cada": {"dias": ["lun", "lun"], "hora": "10:00", "zona": CDMX}}),
     ("invalid_value", "/dispara/cada/dias")),
    (lambda d: d.update(dispara={"cada": {"dias": ["lun"], "hora": "25:00", "zona": CDMX}}),
     ("invalid_value", "/dispara/cada/hora")),
    (lambda d: d.update(dispara={"cada": {"dias": ["lun"], "hora": "10:00", "zona": CDMX,
                                          "desde": "2026-12-31", "hasta": "2026-12-01"}}),
     ("invalid_value", "/dispara/cada/hasta")),
    (lambda d: d.update(dispara={"manual": {"ya": True}}), ("unknown_key", "/dispara/manual/ya")),
    (lambda d: d.update(para={"es": "todas"}), ("invalid_value", "/para/es")),
    (lambda d: d.update(para={}), ("required_key_missing", "/para/es")),
    (lambda d: d.update(para={"es": "prospecta", "formulario_en": "1 mes"}), ("invalid_value", "/para/formulario_en")),
    (lambda d: d.update(para={"es": "prospecta", "etiquetas": ["Black 2026"]}), ("invalid_value", "/para/etiquetas")),
    (lambda d: d.update(para={"es": "prospecta", "etiquetas": ["black"], "excluye": ["black"]}),
     ("invalid_value", "/para/excluye")),
    (lambda d: d.update(ritmo={"por_hora": 0}), ("invalid_value", "/ritmo/por_hora")),
    (lambda d: d.update(ritmo={"por_ocurrencia": fa.MAX_PER_OCCURRENCE + 1}), ("invalid_value", "/ritmo/por_ocurrencia")),
    (lambda d: d.update(ritmo={"por_dia": 5}), ("unknown_key", "/ritmo/por_dia")),
])
def test_each_bad_shape_is_found_where_it_is(change: Any, expected: tuple[str, str]) -> None:
    definition = difusion()
    change(definition)
    assert expected in codes(definition)


def test_trigger_of_an_audience_is_its_form() -> None:
    assert fd.trigger_of(difusion()) == fd.Trigger("audiencia", "fecha")
    assert fd.trigger_of(difusion(dispara={"manual": {}})) == fd.Trigger("audiencia", "manual")
    assert fd.trigger_of(difusion(dispara={"manual": {}, "evento": "compra"})) is None
    assert fd.trigger_of(difusion()).is_audience


def test_normalizing_writes_the_defaults_dan_has_to_review() -> None:
    instance = fd.InstanceView(tenant_ref="maribel", ally_ref="maribel", products=frozenset({"el-protocolo"}),
                               time_zone=CDMX)
    raw = difusion()
    for key in ("esquema", "flujo", "estado"):
        raw.pop(key)
    result = fd.normalize_definition(raw, new_flow=True, instance=instance, other_schedules=(), catalog=None)
    normalized = result.definition
    assert normalized["ritmo"] == {"por_hora": 200, "por_ocurrencia": 1000}
    assert normalized["para"]["formulario_en"] == "30d"
    assert normalized["topes"]["repeticion"] == {"una_por": "persona", "otra_despues_de": "7d"}
    assert normalized["termina"]["si"] == ["compra"]
    assert "vence" not in normalized["termina"]
    assert list(normalized)[:list(normalized).index("entra")][-2:] == ["dispara", "para"]


# -- La lectura --------------------------------------------------------------

def test_the_spec_reads_the_definition_with_its_defaults() -> None:
    spec = fa.audience_spec(difusion())
    assert (spec.form, spec.zone, spec.status, spec.permission) == ("fecha", CDMX, "prospecta", "formulario")
    assert (spec.per_hour, spec.per_occurrence, spec.form_window_days) == (200, 1000, 30)
    assert len(spec.fingerprint) == 64


def test_the_spec_refuses_a_permission_that_does_not_match_the_audience() -> None:
    with pytest.raises(fa.AudienceSpecInvalid, match="permiso"):
        fa.audience_spec(difusion(entra={"permiso": "conversacion"}))
    with pytest.raises(fa.AudienceSpecInvalid, match="permiso"):
        fa.audience_spec(difusion(para={"es": "clienta"}))
    with pytest.raises(fa.AudienceSpecInvalid, match="no_es_audiencia"):
        fa.audience_spec(difusion(dispara={"evento": "carrito"}))


def test_the_fingerprint_changes_with_the_audience_and_not_with_the_text() -> None:
    base = fa.audience_fingerprint(difusion())
    assert fa.audience_fingerprint(difusion(nombre="Otro nombre")) == base
    assert fa.audience_fingerprint(difusion(para={"es": "prospecta", "etiquetas": ["black"]})) != base


# -- Las ocurrencias ---------------------------------------------------------

def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def test_a_date_fires_once_inside_its_grace() -> None:
    spec = fa.audience_spec(difusion())  # 10:00 CDMX = 16:00 UTC
    assert fa.due_occurrence(spec, now=at("2026-12-01T15:59")) is None
    occurrence = fa.due_occurrence(spec, now=at("2026-12-01T16:00"))
    assert occurrence == fa.Occurrence("fecha:2026-12-01T16:00Z", at("2026-12-01T16:00"))
    assert fa.due_occurrence(spec, now=at("2026-12-01T21:59")) == occurrence
    assert fa.due_occurrence(spec, now=at("2026-12-01T22:00")) is None


def test_every_gives_one_occurrence_per_local_day_on_the_days_asked() -> None:
    spec = fa.audience_spec(difusion(dispara={"cada": {
        "dias": ["lun", "jue"], "hora": "21:00", "zona": CDMX, "desde": "2026-12-03", "hasta": "2026-12-10"}}))
    # 2026-12-03 es jueves: 21:00 CDMX = 03:00 UTC del 04; la clave es el dia local.
    assert fa.due_occurrence(spec, now=at("2026-12-04T03:30")) == fa.Occurrence("cada:2026-12-03", at("2026-12-04T03:00"))
    # Martes: no es un dia pedido.
    assert fa.due_occurrence(spec, now=at("2026-12-09T03:30")) is None
    # Jueves 10 dentro de hasta; jueves 17 fuera.
    assert fa.due_occurrence(spec, now=at("2026-12-11T03:30")) is not None
    assert fa.due_occurrence(spec, now=at("2026-12-18T03:30")) is None
    # Lunes 30/11, antes de desde.
    assert fa.due_occurrence(spec, now=at("2026-12-01T03:30")) is None


def test_manual_never_comes_due_by_itself() -> None:
    spec = fa.audience_spec(difusion(dispara={"manual": {}}))
    assert fa.due_occurrence(spec, now=at("2026-12-01T16:00")) is None


def test_now_must_carry_its_zone() -> None:
    with pytest.raises(ValueError):
        fa.due_occurrence(fa.audience_spec(difusion()), now=datetime(2026, 12, 1, 16, 0))


# -- La lista ----------------------------------------------------------------

NOW = at("2026-12-01T16:00")


def person(n: int, **changes: Any) -> fa.Candidate:
    values: dict[str, Any] = {
        "contact_id": f"00000000-0000-4000-8000-{n:012d}", "phone": f"52155500{n:05d}",
        "labels": frozenset(), "opted_out": False, "form_at": NOW - timedelta(days=3),
        "customer_products": frozenset(),
    }
    values.update(changes)
    return fa.Candidate(**values)


def test_only_people_with_permission_enter_and_the_opt_out_rules() -> None:
    spec = fa.audience_spec(difusion(para={"es": "prospecta", "etiquetas": ["black"], "excluye": ["vip"]}))
    members, omitted = fa.select_members(spec, [
        person(1, labels=frozenset({"black"})),
        person(2, labels=frozenset({"black"}), opted_out=True),
        person(3, labels=frozenset({"black"}), form_at=None),
        person(4, labels=frozenset({"black"}), form_at=NOW - timedelta(days=31)),
        person(5),
        person(6, labels=frozenset({"black", "vip"})),
        person(7, labels=frozenset({"black"}), customer_products=frozenset({"el-protocolo"})),
        person(1, labels=frozenset({"black"})),
    ], now=NOW)
    assert [member.contact_id[-1] for member in members] == ["1"]
    assert omitted == {"baja": 1, "sin_permiso": 2, "etiquetas": 1, "excluida": 1, "estado": 1, "repetida": 1}


def test_clientas_of_the_product_and_the_cap_per_occurrence() -> None:
    spec = fa.audience_spec(difusion(dispara={"manual": {}}, entra={"permiso": "compra"},
                                     para={"es": "clienta", "producto": "el-protocolo"},
                                     ritmo={"por_ocurrencia": 2}))
    members, omitted = fa.select_members(spec, [
        person(1, customer_products=frozenset({"el-protocolo"}), form_at=None),
        person(2, customer_products=frozenset({"otro"})),
        person(3, customer_products=frozenset({"el-protocolo"})),
        person(4, customer_products=frozenset({"el-protocolo"})),
    ], now=NOW)
    assert [member.contact_id[-1] for member in members] == ["1", "3"]
    assert omitted == {"estado": 1, "tope_por_ocurrencia": 1}


def test_the_module_is_pure() -> None:
    source = (fa.__file__ and open(fa.__file__, encoding="utf-8").read()) or ""
    for forbidden in ("import httpx", "import asyncio", "supabase", "datetime.now", "time.time"):
        assert forbidden not in source, forbidden
    assert copy.deepcopy(fa.AUDIENCE_FORMS) == ("fecha", "cada", "manual")
