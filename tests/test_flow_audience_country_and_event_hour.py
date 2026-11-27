"""4.5 «Segmentar por pais» y 4.4 «La hora del evento en varias zonas» (flow-audience-trigger-v1 0.8.0).

Contrato: docs/contracts/flow-audience-trigger-v1.md; la base: 20261126700000.

Datos reales (Maribel, maribel_db y Chatwoot, solo lectura, 2026-11-26, sin telefonos):

* La definicion de ``aviso_fecha_prueba`` v1 y su ``audience_fingerprint`` en la base
  (``tests/fixtures/flow_audience/maribel_aviso_fecha_prueba_definition_20261126.json``): un flujo viejo,
  sin las claves nuevas, tiene la misma huella que guardo la base al armar sus ocurrencias.
* El recordatorio de la clase del 22/11 (``maribel_forever_clase_recordatorio_01``, 1.704 envios) por
  indicativo: CO 1.195, MX 174, +1 111, PE 78, EC 58, CL 23, AR 16, CR 16, ES 15, VE 3, PY 2, UY 1 y 12
  sueltos de otros indicativos. Su texto decia la hora en una sola zona: «a las 7:00 p. m. (hora Colombia)».
* La ultima ocurrencia de ``aviso_diario_prueba`` (cada:2026-11-25) por prefijo: 52 328, 57 9, +1 con codigos
  de area de Estados Unidos (713, 786, 954, 480, 646, 408, 331, 772, 619, 415) y uno de Canada (236).
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from bridge import flow_audience as fa
from bridge import flow_definition as fd
from bridge.flow_engine import native_not_supported
from bridge.phones import phone_prefix_country, whatsapp_phone_country
from bridge.template_request import EXAMPLE_EVENT_HOUR, _examples

FIXTURES = Path(__file__).parent / "fixtures"
MARIBEL = json.loads((FIXTURES / "flow_audience" / "maribel_aviso_fecha_prueba_definition_20261126.json")
                     .read_text(encoding="utf-8"))
CDMX = "America/Mexico_City"
NOW = datetime(2026, 12, 1, 16, 0, tzinfo=timezone.utc)
CLASS = {"el": "2026-11-22T19:00", "zona": "America/Bogota"}


def difusion(**changes: Any) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "esquema": fd.SCHEMA, "flujo": "difusion", "nombre": "Recordatorio de la clase", "estado": "borrador",
        "dispara": {"fecha": {"el": "2026-12-01T10:00", "zona": CDMX}},
        "para": {"es": "prospecta"}, "entra": {"permiso": "formulario"},
        "pasos": [{"plantilla": "clase_recordatorio", "idioma": "es_MX", "variables": ["nombre"]}],
    }
    definition.update(changes)
    return definition


def codes(definition: dict[str, Any]) -> set[tuple[str, str]]:
    return {(finding.code, finding.path) for finding in fd.check_definition(definition)}


def person(n: int, phone: str) -> fa.Candidate:
    return fa.Candidate(contact_id=f"00000000-0000-4000-8000-{n:012d}", phone=phone, labels=frozenset(),
                        opted_out=False, form_at=NOW - timedelta(days=3), customer_products=frozenset())


# Un telefono de cada caso (numeros de ficcion con el indicativo real).
CO, MX, MX_WA, AR, US, CA, PR, ES, NANP_NONE, BAD = (
    "573001112233", "525511112233", "5215511112233", "5491111112233", "17135551234", "12365551234",
    "17875551234", "34611112233", "15555551234", "0011")


# -- El pais del telefono (D-45.2) -------------------------------------------------

@pytest.mark.parametrize("phone, country", [
    (CO, "CO"), (MX, "MX"), (MX_WA, "MX"), (AR, "AR"), ("541111112233", "AR"), (US, "US"), (CA, "CA"),
    (PR, "PR"), ("18095551234", "DO"), (ES, "ES"), ("51911112233", "PE"), ("593911112233", "EC"),
    ("56911112233", "CL"), ("50611112233", "CR"), (NANP_NONE, None), (BAD, None), (None, None), ("", None),
])
def test_the_country_of_a_phone_is_its_calling_code_and_inside_plus_one_its_area_code(
        phone: str | None, country: str | None) -> None:
    assert whatsapp_phone_country(phone) == country


def test_the_country_only_looks_at_the_first_four_digits() -> None:
    # Lo que permite contar por pais desde el prefijo que da la base, sin telefonos.
    for phone in (CO, MX, MX_WA, AR, US, CA, PR, ES, NANP_NONE):
        assert phone_prefix_country(phone[:4]) == whatsapp_phone_country(phone)
    assert phone_prefix_country("57") is None


# -- 4.5: la forma -----------------------------------------------------------------

def test_countries_are_a_valid_part_of_para() -> None:
    assert codes(difusion(para={"es": "prospecta", "paises": ["CO", "MX"]})) == set()
    assert codes(difusion(para={"es": "prospecta", "excluye_paises": ["US"]})) == set()
    assert codes(difusion(para={"es": "prospecta", "paises": ["CO"], "excluye_paises": ["US"]})) == set()


def test_before_the_change_paises_was_an_unknown_key() -> None:
    assert ("unknown_key", "/para/paises") not in codes(difusion(para={"es": "prospecta", "paises": ["CO"]}))
    assert ("unknown_key", "/para/excluye_paises") not in codes(
        difusion(para={"es": "prospecta", "excluye_paises": ["CO"]}))


@pytest.mark.parametrize("para, path", [
    ({"paises": ["co"]}, "/para/paises"),
    ({"paises": ["XX"]}, "/para/paises"),
    ({"paises": ["CO", "CO"]}, "/para/paises"),
    ({"paises": []}, "/para/paises"),
    ({"paises": "CO"}, "/para/paises"),
    ({"paises": ["COL"]}, "/para/paises"),
    ({"excluye_paises": ["mx"]}, "/para/excluye_paises"),
    ({"paises": ["CO", "MX"], "excluye_paises": ["MX"]}, "/para/excluye_paises"),
])
def test_each_bad_country_list_is_found_where_it_is(para: dict[str, Any], path: str) -> None:
    assert ("invalid_value", path) in codes(difusion(para={"es": "prospecta", **para}))


# -- 4.5: el filtro ----------------------------------------------------------------

def test_with_paises_only_those_countries_enter_and_an_unknown_country_stays_out() -> None:
    spec = fa.audience_spec(difusion(para={"es": "prospecta", "paises": ["CO", "MX"]}))
    members, omitted = fa.select_members(spec, [
        person(1, CO), person(2, MX_WA), person(3, AR), person(4, US), person(5, NANP_NONE), person(6, MX),
    ], now=NOW)
    assert [member.contact_id[-1] for member in members] == ["1", "2", "6"]
    assert omitted == {"pais": 3}


def test_excluye_paises_leaves_out_those_and_the_unknown_but_tells_canada_from_the_us() -> None:
    spec = fa.audience_spec(difusion(para={"es": "prospecta", "excluye_paises": ["US"]}))
    members, omitted = fa.select_members(spec, [
        person(1, US), person(2, CA), person(3, PR), person(4, NANP_NONE), person(5, CO),
    ], now=NOW)
    assert [member.contact_id[-1] for member in members] == ["2", "3", "5"]
    assert omitted == {"pais_excluido": 1, "pais": 1}


def test_the_country_filter_counts_the_real_audience_of_the_22_11_reminder() -> None:
    # El reparto por indicativo del recordatorio del 22/11 (los 1.692 de los 12 paises de arriba, sin los 12
    # sueltos de otros indicativos). Con paises [MX], salen los 174.
    spread = {"57": 1195, "52": 174, "1713": 111, "51": 78, "593": 58, "56": 23, "54": 16, "506": 16,
              "34": 15, "58": 3, "595": 2, "598": 1}
    candidates = []
    for prefix, n in spread.items():
        for _ in range(n):
            index = len(candidates)
            candidates.append(person(index, (prefix + "9" * 12)[:12] if not prefix.startswith("1")
                                     else prefix + "5551234"))
    spec = fa.audience_spec(difusion(para={"es": "prospecta", "paises": ["MX"]}, ritmo={"por_ocurrencia": 5000}))
    members, omitted = fa.select_members(spec, candidates, now=NOW)
    assert len(members) == 174
    assert len(candidates) == 1692
    assert omitted == {"pais": 1692 - 174}


def test_an_old_flow_without_the_keys_is_byte_for_byte_the_one_from_before() -> None:
    # La definicion real de Maribel: la huella es la que guardo la base, y la lista no mira el pais.
    definition = MARIBEL["definition"]
    assert "paises" not in definition["para"] and "evento" not in definition
    spec = fa.audience_spec(definition)
    assert spec.fingerprint == MARIBEL["_capture"]["audience_fingerprint"]
    assert spec.countries == () and spec.excluded_countries == ()
    members, omitted = fa.select_members(spec, [person(1, BAD), person(2, NANP_NONE), person(3, CO)], now=NOW)
    assert len(members) == 3 and omitted == {}
    assert codes(copy.deepcopy(definition) | {"estado": "borrador"}) - {("version_set_by_cycle", "/version")} == set()


def test_the_fingerprint_changes_with_the_countries() -> None:
    base = fa.audience_spec(difusion()).fingerprint
    assert fa.audience_spec(difusion(para={"es": "prospecta", "paises": ["CO"]})).fingerprint != base


# -- 4.4: la forma -----------------------------------------------------------------

def _with_hour(**changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {"evento": dict(CLASS), "pasos": [
        {"plantilla": "clase_recordatorio", "idioma": "es_MX", "variables": ["nombre", "hora_evento"]}]}
    values.update(changes)
    return difusion(**values)


def test_an_audience_with_its_event_and_the_hour_variable_is_valid() -> None:
    assert codes(_with_hour()) == set()
    assert "hora_evento" in fd.VARIABLES


@pytest.mark.parametrize("definition, expected", [
    (difusion(pasos=[{"plantilla": "clase", "variables": ["hora_evento"]}]), ("invalid_value", "/pasos/0/variables/0")),
    (_with_hour(dispara={"evento": "carrito"}), ("invalid_value", "/evento")),
    (_with_hour(evento={"el": "2026-11-22 19:00", "zona": "America/Bogota"}), ("invalid_value", "/evento/el")),
    (_with_hour(evento={"el": "2026-11-22T19:00", "zona": "Marte/Olimpo"}), ("invalid_value", "/evento/zona")),
    (_with_hour(evento={"el": "2026-11-22T19:00"}), ("required_key_missing", "/evento/zona")),
    (_with_hour(evento={"el": "2026-11-22T19:00", "zona": "America/Bogota", "lugar": "zoom"}),
     ("unknown_key", "/evento/lugar")),
    (_with_hour(evento="2026-11-22T19:00"), ("invalid_value", "/evento")),
])
def test_each_bad_event_is_found_where_it_is(definition: dict[str, Any], expected: tuple[str, str]) -> None:
    assert expected in codes(definition)


def test_the_native_engine_runs_the_hour_only_in_an_audience() -> None:
    import test_e483_native_audience_source as audience

    definition = audience._audience(evento=dict(CLASS))
    for step in definition["pasos"]:
        if "plantilla" in step:
            step["variables"] = ["nombre", "hora_evento"]
    assert native_not_supported(definition, audience=True) == ()
    cart = copy.deepcopy(definition)
    cart.pop("para")
    cart.pop("evento")
    cart["dispara"] = {"evento": "carrito"}
    assert any("hora_evento" in code or "variables_differ" in code for code in native_not_supported(cart))


def test_a_template_request_knows_an_example_of_the_hour() -> None:
    class _Context:
        price = None
        currency = None
        slip_buttons = ()
        product_name = "El Protocolo"

    assert _examples(_Context())["hora_evento"] == EXAMPLE_EVENT_HOUR  # type: ignore[arg-type]


# -- 4.4: la hora en la zona de cada persona ---------------------------------------

def _event(**value: str) -> fa.AudienceEvent:
    event = fa.audience_event({"evento": value or dict(CLASS)})
    assert event is not None
    return event


@pytest.mark.parametrize("phone, text", [
    (CO, "19:00 (hora de Colombia)"),
    (MX_WA, "18:00 (hora de México)"),
    (AR, "21:00 (hora de Argentina)"),
    ("51911112233", "19:00 (hora de Perú)"),
    ("56911112233", "21:00 (hora de Chile)"),
    ("50611112233", "18:00 (hora de Costa Rica)"),
    (PR, "20:00 (hora de Puerto Rico)"),
    # D-44.4: en Espana la clase del domingo a las 19:00 de Bogota es el lunes a la 01:00.
    (ES, "01:00 del 23/11 (hora de España)"),
    # D-44.3: Estados Unidos y Canada tienen varias zonas; un pais no reconocido tampoco tiene la suya.
    (US, "19:00 (hora de Colombia)"),
    (CA, "19:00 (hora de Colombia)"),
    (NANP_NONE, "19:00 (hora de Colombia)"),
    (None, "19:00 (hora de Colombia)"),
])
def test_the_class_of_the_22_11_in_the_zone_of_each_person(phone: str | None, text: str) -> None:
    assert fa.event_hour_text(_event(), phone=phone) == text


def test_the_fallback_names_a_zone_outside_the_table_by_its_city() -> None:
    assert fa.event_hour_text(_event(el="2026-12-01T20:00", zona="America/New_York"), phone=US) == \
        "20:00 (hora de New York)"
    assert fa.event_hour_text(_event(el="2026-12-01T20:00", zona=CDMX), phone=US) == "20:00 (hora de México)"


def test_daylight_saving_in_spain_moves_the_hour_of_a_mexico_city_event() -> None:
    # Mexico no cambia la hora desde 2022; Espana pasa de CEST (UTC+2) a CET (UTC+1) el 25/10/2026.
    before = _event(el="2026-10-20T20:00", zona=CDMX)
    after = _event(el="2026-10-27T20:00", zona=CDMX)
    assert fa.event_hour_text(before, phone=ES) == "04:00 del 21/10 (hora de España)"
    assert fa.event_hour_text(after, phone=ES) == "03:00 del 28/10 (hora de España)"


def test_daylight_saving_in_chile_moves_the_hour_of_a_mexico_city_event() -> None:
    # Chile continental: UTC-4 en invierno, UTC-3 en verano (desde el 06/09/2026).
    winter = _event(el="2026-06-01T20:00", zona=CDMX)
    summer = _event(el="2026-12-01T20:00", zona=CDMX)
    assert fa.event_hour_text(winter, phone="56911112233") == "22:00 (hora de Chile)"
    assert fa.event_hour_text(summer, phone="56911112233") == "23:00 (hora de Chile)"


def test_the_event_is_read_with_its_zone_or_not_at_all() -> None:
    event = _event()
    assert event.at == datetime(2026, 11, 22, 19, 0, tzinfo=ZoneInfo("America/Bogota"))
    assert fa.audience_event({}) is None
    assert fa.audience_event({"evento": {"el": "2026-11-22T19:00"}}) is None
    assert fa.audience_event({"evento": {"el": "hoy", "zona": "America/Bogota"}}) is None


# -- 4.4: la lectura de la definicion ------------------------------------------------

def test_the_store_gives_the_definition_of_that_version_only() -> None:
    from bridge.flow_audience_store import SupabaseAudienceStore
    from bridge.flow_audience_sweeper import AudienceFlow

    flow = "0000000b-0000-4000-8000-00000000d1f0"
    store = SupabaseAudienceStore(base_url="https://x", service_role_key="k", instance={})

    async def flows() -> list[AudienceFlow]:
        return [AudienceFlow(flow_id=flow, version=2, flow_key="clase", definition={"evento": CLASS})]

    store.read_audience_flows = flows  # type: ignore[method-assign]
    assert asyncio.run(store.read_flow_definition(flow, 2)) == {"evento": CLASS}
    assert asyncio.run(store.read_flow_definition(flow, 1)) is None
    assert asyncio.run(store.read_flow_definition("0000000b-0000-4000-8000-000000000000", 2)) is None


# -- 4.4: el envio del paso nativo con hora_evento ---------------------------------
#
# Los dobles son los de test_flow_engine_native_dispatch.py (el inbox 11 de ATT1, la base real de PGlite para
# la cola del paso). La plantilla del recordatorio no existe en Meta: se deriva de la bienvenida de ese
# archivo (que a su vez se deriva de la captura del 10/10 del inbox 11) con otro nombre y otro cuerpo de dos
# variables. Es lo unico inventado. La persona del doble es un +1 de Washington (12025550124): Estados Unidos
# tiene varias zonas, asi que su hora es la del evento con el nombre de su zona (D-44.3).

import test_flow_engine_native_dispatch as native  # noqa: E402

from bridge.worker import (  # noqa: E402
    FLOW_ENGINE_STEP_UNSUPPORTED,
    NATIVE_EVENT_HOUR_READ_UNAVAILABLE,
    NATIVE_EVENT_HOUR_UNAVAILABLE,
    NATIVE_EVENT_MISSING,
    NATIVE_EVENT_PASSED,
)

REMINDER = "att1_recordatorio_clase_01"
REMINDER_BODY = "Hola {{1}}, tu clase en vivo es a las {{2}}. Te esperamos."
REMINDER_TEMPLATE = {
    **copy.deepcopy(native.WELCOME_TEMPLATE), "name": REMINDER,
    "components": [{"type": "BODY", "text": REMINDER_BODY,
                    "example": {"body_text": [["Edith", EXAMPLE_EVENT_HOUR]]}}],
}
EVENT_DEFINITION = {"evento": {"el": "2026-12-01T20:00", "zona": CDMX}}


class _Reader:
    def __init__(self, definition: Any = None, *, error: Exception | None = None) -> None:
        self.definition = EVENT_DEFINITION if definition is None else definition
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, flow_id: str, version: int) -> Any:
        self.calls.append((flow_id, version))
        if self.error is not None:
            raise self.error
        return self.definition


def _send(tmp_path: Path, *, reader: _Reader | None, variables: tuple[str, ...] = ("nombre", "hora_evento"),
          set_after: bool = False) -> tuple[native._NativeAuthority, native._NativeChatwoot]:
    authority = native._NativeAuthority(step=native._step(template_name=REMINDER, template_variables=variables))
    chatwoot = native._NativeChatwoot()
    chatwoot.catalog.append(copy.deepcopy(REMINDER_TEMPLATE))
    dispatcher = native._dispatcher(authority, chatwoot, tmp_path)
    if reader is not None:
        dispatcher.set_audience_event_reader(reader)
    native._run(dispatcher)
    return authority, chatwoot


def test_the_hour_of_the_event_goes_in_the_template_in_the_zone_of_the_person(tmp_path: Path) -> None:
    reader = _Reader()
    authority, chatwoot = _send(tmp_path, reader=reader)

    [message] = native._templates(chatwoot)
    assert message["template_params"]["name"] == REMINDER
    assert message["content"] == "Hola Edith García Pérez, tu clase en vivo es a las 20:00 (hora de México). Te " \
        "esperamos."
    assert reader.calls == [(native.FLOW_ID, 1)]


def test_without_the_audience_reader_the_step_closes_for_good_and_sends_nothing(tmp_path: Path) -> None:
    # FLOW_AUDIENCE_TRIGGERS_ENABLED apagado: app.py no arma la lectura.
    authority, chatwoot = _send(tmp_path, reader=None)

    assert native._templates(chatwoot) == []
    [closed] = authority.finalizations
    assert closed["reason_code"] == NATIVE_EVENT_HOUR_UNAVAILABLE and closed["next_attempt_at"] is None


@pytest.mark.parametrize("definition, reason", [
    ({"para": {"es": "prospecta"}}, NATIVE_EVENT_MISSING),
    ({"evento": {"el": "2026-12-01T20:00"}}, NATIVE_EVENT_MISSING),
    # El reloj del doble es el 30/09/2026: un evento del 29/09 ya paso.
    ({"evento": {"el": "2026-09-29T20:00", "zona": CDMX}}, NATIVE_EVENT_PASSED),
])
def test_a_step_without_a_future_event_closes_for_good(tmp_path: Path, definition: dict[str, Any],
                                                       reason: str) -> None:
    authority, chatwoot = _send(tmp_path, reader=_Reader(definition))

    assert native._templates(chatwoot) == []
    [closed] = authority.finalizations
    assert closed["reason_code"] == reason and closed["next_attempt_at"] is None


def test_a_version_that_is_no_longer_active_has_no_event(tmp_path: Path) -> None:
    class _Gone(_Reader):
        async def __call__(self, flow_id: str, version: int) -> Any:
            self.calls.append((flow_id, version))
            return None

    authority, chatwoot = _send(tmp_path, reader=_Gone())
    assert native._templates(chatwoot) == []
    assert authority.finalizations[0]["reason_code"] == NATIVE_EVENT_MISSING


def test_a_reading_that_does_not_answer_is_retried(tmp_path: Path) -> None:
    authority, chatwoot = _send(tmp_path, reader=_Reader(error=RuntimeError("base caida")))

    assert native._templates(chatwoot) == []
    [closed] = authority.finalizations
    assert closed["reason_code"] == NATIVE_EVENT_HOUR_READ_UNAVAILABLE and closed["next_attempt_at"] is not None


def test_a_step_without_the_hour_never_reads_the_event(tmp_path: Path) -> None:
    # Lo que ya corre: un paso con nombre y producto sale como antes, sin leer nada nuevo.
    reader = _Reader()
    authority = native._NativeAuthority()
    chatwoot = native._NativeChatwoot()
    dispatcher = native._dispatcher(authority, chatwoot, tmp_path)
    dispatcher.set_audience_event_reader(reader)
    native._run(dispatcher)

    assert len(native._templates(chatwoot)) == 1
    assert reader.calls == []


def test_the_hour_with_a_personal_coupon_is_still_not_a_step_the_bridge_builds(tmp_path: Path) -> None:
    authority, chatwoot = _send(tmp_path, reader=_Reader(), variables=("nombre", "hora_evento", "precio"))
    assert native._templates(chatwoot) == []
    assert authority.finalizations[0]["reason_code"] == FLOW_ENGINE_STEP_UNSUPPORTED


def test_the_reader_is_wired_only_with_the_audience_flag() -> None:
    # app.py: la lectura del evento existe solo si existe el disparo por audiencia (FLOW_AUDIENCE_TRIGGERS_ENABLED).
    source = (Path(__file__).resolve().parents[1] / "src" / "bridge" / "app.py").read_text(encoding="utf-8")
    block = source.split("app.state.flow_audience = flow_audience", 1)[1].split("\n\n", 1)[0]
    assert "if flow_audience is not None and durable_dispatcher is not None:" in block
    assert "durable_dispatcher.set_audience_event_reader(flow_audience.store.read_flow_definition)" in block
    assert source.count("set_audience_event_reader(") == 1


def test_the_skill_of_the_ae_knows_the_countries_and_the_hour() -> None:
    skill = (Path(__file__).resolve().parents[1] / "profiles" / "automation-expert" / "skills" / "flujos"
             / "programar-un-envio" / "SKILL.md").read_text(encoding="utf-8")
    assert "para.paises" in skill and "para.excluye_paises" in skill
    assert "hora_evento" in skill and "evento: {el:" in skill and "20:00 (hora de México)" in skill


# -- Lo que ve quien aprueba: la simulacion y la pagina de aprobacion ---------------

def test_the_new_flow_summary_tells_the_countries_and_the_event_and_the_page_says_them() -> None:
    from test_e621_new_audience_flow_simulation import _item

    from bridge.flow_approval import _simulation_summary
    from bridge.flow_change_simulation import build_new_audience_flow_simulation

    [item] = fd.parse_items([_item("fecha", para={"es": "clienta", "paises": ["CO", "MX"], "excluye_paises": ["US"]},
                                   evento=dict(CLASS))])
    built = build_new_audience_flow_simulation([item])
    summary = built["summary"]
    assert summary["audiencia"]["paises"] == ["CO", "MX"] and summary["audiencia"]["excluye_paises"] == ["US"]
    assert summary["evento"] == CLASS
    page = _simulation_summary(built)
    assert ", de CO, MX (por el número), sin US (por el número)" in page
    assert "El mensaje habla del evento del 2026-11-22 a las 19:00 (America/Bogota)" in page


def test_without_the_keys_the_new_flow_summary_is_the_one_from_before() -> None:
    from test_e621_new_audience_flow_simulation import _item

    from bridge.flow_change_simulation import build_new_audience_flow_simulation

    [item] = fd.parse_items([_item("fecha")])
    summary = build_new_audience_flow_simulation([item])["summary"]
    assert set(summary["audiencia"]) == {"es", "producto"} and "evento" not in summary


def test_the_reach_of_a_flow_by_country_is_a_maximum_because_the_base_does_not_see_the_country() -> None:
    from test_e622_audience_reach_simulation import _aviso_flow, _prender, _reach

    from bridge.flow_approval import _simulation_summary
    from bridge.flow_change_operator import world_from_store
    from bridge.flow_change_simulation import build_native_audience_reach_simulation

    world = world_from_store({"flows": [_aviso_flow("fecha", para={"es": "clienta", "paises": ["MX"]})]})
    [item] = fd.parse_items([_prender("fecha")])
    built = build_native_audience_reach_simulation(_reach(), [item], world=world)
    assert built["summary"]["audiencia"]["paises"] == ["MX"]
    assert built["summary"]["por_que"][0] == "pais_del_telefono"
    page = _simulation_summary(built)
    assert ", de MX (por el número)" in page and "el país lo mira la lista por el número" in page
