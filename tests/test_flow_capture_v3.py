"""La forma cerrada de la captura v3 (T2.2): la privacidad, sus rojos y el fixture.

Especificacion: «T2.2: la captura v3 y la paridad del carrito de tres toques»
(2026-10-21, en el vault). La v3 es la salida de
``tests/fixtures/flow_engine/att1_recovery_capture_query_v3.sql``
(``captura-paridad-recuperacion-v3``); su forma es ``flow_capture.FORM_V3``,
junto a la v2, que no cambia.

Fixture (``tests/fixtures/flow_engine/``):

* ``att1_recovery_capture_v3_sintetico.json``: una captura v3 armada a mano,
  sin ninguna base de por medio (uuid ``00000000-0000-4000-8000-<n>`` y claves
  de persona que son el numero en hex). Cinco carritos de la ventana, desde la
  activacion de la v7 (2026-10-19T18:13Z): A1 y A2 enviados con Z esperando;
  una respuesta a A1 que cancela A2; A1 que espera dos veces al cupon y sale
  con nuestro link, comprado; «Quiero mi 10 %» que adelanta Z, que espera una
  vez al cupon 2 y sale; y la misma renovacion con el cupon 2 muerto, que
  cierra Z sin mensaje. Ademas, un pago rechazado como hecho de la persona,
  un carrito de la siembra (la v6) y un primer contacto que cede ante el
  primer carrito.

Las pruebas comparan caminos y numeros, nunca valores.
"""

from __future__ import annotations

import copy
import importlib.util
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from bridge import flow_capture as fc
from bridge.flow_definition import loads_strict

ROOT = Path(__file__).resolve().parents[1]
FLOW_ENGINE = ROOT / "tests" / "fixtures" / "flow_engine"
SYNTHETIC_PATH = FLOW_ENGINE / "att1_recovery_capture_v3_sintetico.json"
SYNTHETIC: dict[str, Any] = loads_strict(SYNTHETIC_PATH.read_bytes())
V2_FIXTURE: dict[str, Any] = loads_strict((FLOW_ENGINE / "att1_recovery_capture_20261016.json").read_bytes())
QUERY_V3 = (FLOW_ENGINE / "att1_recovery_capture_query_v3.sql").read_text(encoding="utf-8")
SCRIPT = ROOT / "scripts" / "flow_engine_capture_fixture.py"
SPEC = importlib.util.spec_from_file_location("flow_engine_capture_fixture_v3", SCRIPT)
assert SPEC and SPEC.loader
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)

PHONE = "+5215512345678"
PHONE_DIGITS = "5512345678"
COUPON_CODE = "ATT3F9A1B2C4D5E"
URL = "https://pay.hotmart.com/B12345678C?off=gopi6lh7&checkoutMode=10&src=hermes&sck=hermes~v1~X"

# La segunda red, sin la tabla del modulo: las formas fijas, escritas otra vez.
_FIXED = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[0-9a-f]{16}|[0-9a-f]{64}"
    r"|[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z)?"
)
_SEPARATORS = re.compile(r"[ \t\-.()+/]")
# Lo que la v3 suma a «lo que no sale nunca»: el codigo del cupon, la URL, el
# sck, el ulid, los ids de Chatwoot y el texto de una respuesta.
FORBIDDEN_KEYS = frozenset({
    "phone", "email", "name", "payload", "text", "content", "note", "refusal_reason",
    "external_user_id", "transaction", "approved_by", "changed_by", "change_reason", "actor",
    "code", "hotmart_coupon_id", "checkout_url_final", "sck_value", "original_sck",
    "issuance_ulid", "chatwoot_account_id", "chatwoot_inbox_id", "chatwoot_conversation_id",
    "chatwoot_message_id", "trigger_external_message_id", "sal",
})


def _walk(value: Any, path: str = "") -> Iterator[tuple[str, str | None, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")
    else:
        yield path, path.rsplit(".", 1)[-1].split("[", 1)[0] if path else None, value


def _keys(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _keys(child)


def _independent_findings(data: Any) -> list[str]:
    findings = []
    for path, key, value in _walk(data):
        if isinstance(value, str):
            if "@" in value or "://" in value or "hermes~" in value:
                findings.append(f"{path}: forma de dato")
            if not _FIXED.fullmatch(value) and re.search(r"[0-9]{9,}", _SEPARATORS.sub("", value)):
                findings.append(f"{path}: nueve cifras")
        elif isinstance(value, int) and not isinstance(value, bool) and abs(value) >= 10**8:
            if not (key and key.endswith("_ms") and 1_500_000_000_000 <= value <= 2_500_000_000_000):
                findings.append(f"{path}: entero largo")
    for key in _keys(data):
        if key in FORBIDDEN_KEYS or not re.fullmatch(r"[a-z_][a-z0-9_]*", key):
            findings.append(f"clave {key if re.fullmatch(r'[a-z_][a-z0-9_]*', key) else '<rara>'}")
    return findings


# ---------------------------------------------------------------------------
# 1. La forma v3, junto a la v2.
# ---------------------------------------------------------------------------

def test_the_synthetic_capture_has_the_closed_v3_shape_and_its_counts() -> None:
    assert fc.shape_problems(SYNTHETIC, fc.FORM_V3) == []
    assert fc.count_sections(SYNTHETIC, fc.FORM_V3) == SYNTHETIC["_captured"]["conteos"]
    assert tuple(SYNTHETIC) == fc.TOP_SECTIONS_V3
    assert _independent_findings(SYNTHETIC) == []


def test_the_v2_stays_as_it_was_and_the_two_forms_do_not_mix() -> None:
    # La v2 y su fixture no cambian: siguen en verde con la forma por defecto.
    assert fc.shape_problems(V2_FIXTURE) == []
    assert fc.shape_problems(V2_FIXTURE, fc.FORM_V2) == []
    assert fc.FORMS == {"v2": fc.FORM_V2, "v3": fc.FORM_V3}
    # La v3 no pasa por la v2 (secciones y conteos nuevos), ni la v2 por la v3
    # (la consulta se dice por su nombre, y le faltan secciones).
    v2_problems = {str(problem) for problem in fc.shape_problems(SYNTHETIC)}
    assert "secuencias: clave fuera de la lista de captura" in v2_problems
    v3_problems = {str(problem) for problem in fc.shape_problems(V2_FIXTURE, fc.FORM_V3)}
    assert "_captured.query: no tiene la forma consulta_v3" in v3_problems
    assert "cupones: falta" in v3_problems


def test_in_the_v3_only_carts_are_firings() -> None:
    v3 = [event["event_type"] for event in fc.firings(SYNTHETIC, fc.FORM_V3)]
    v2 = [event["event_type"] for event in fc.firings(SYNTHETIC)]
    assert set(v3) == {"PURCHASE_OUT_OF_SHOPPING_CART"} and len(v3) == 5
    # El pago rechazado entra como hecho de la persona: esta en los eventos y
    # la v2 lo contaria como disparo.
    assert v2.count("PURCHASE_CANCELED") == 1
    counts = fc.count_sections(SYNTHETIC, fc.FORM_V3)
    assert counts["disparos_desde"] == 5
    assert counts["casos"] == 7 and counts["casos_del_carrito"] == 6


# ---------------------------------------------------------------------------
# 2. La red tiene dientes: un dato metido en una copia no pasa, y el mensaje
#    dice el camino sin el valor.
# ---------------------------------------------------------------------------

def _set(path: tuple[Any, ...], value: Any) -> Callable[[dict[str, Any]], None]:
    def apply(data: dict[str, Any]) -> None:
        target: Any = data
        for step in path[:-1]:
            target = target[step]
        target[path[-1]] = value
    return apply


INJECTIONS = [
    ("el codigo del cupon", _set(("cupones", 0, "code"), COUPON_CODE),
     "cupones[0].code", "clave fuera de la lista de cupon"),
    ("el codigo del cupon como motivo", _set(("cupones", 0, "failure_code"), COUPON_CODE),
     "cupones[0].failure_code", "no tiene la forma motivo"),
    ("el id del cupon en Hotmart", _set(("cupones", 0, "hotmart_coupon_id"), 123456789012),
     "cupones[0].hotmart_coupon_id", "clave fuera de la lista de cupon"),
    ("un tercer cupon", _set(("cupones", 0, "ordinal"), 3),
     "cupones[0].ordinal", "ordinal_del_cupon fuera de rango"),
    ("la URL del link", _set(("links", 0, "checkout_url_final"), URL),
     "links[0].checkout_url_final", "clave fuera de la lista de link"),
    ("la URL como estado del link", _set(("links", 0, "status"), URL),
     "links[0].status", "no tiene la forma codigo"),
    ("el sck como resolucion", _set(("links", 0, "attribution_resolution"), "hermes~v1~01J"),
     "links[0].attribution_resolution", "no tiene la forma codigo"),
    ("comprado como texto", _set(("links", 0, "comprado"), "si"),
     "links[0].comprado", "se esperaba booleano"),
    ("el telefono del pedido de renovacion", _set(("renovaciones", 0, "external_user_id"), PHONE[1:]),
     "renovaciones[0].external_user_id", "clave fuera de la lista de renovacion"),
    ("un efecto que no existe", _set(("renovaciones", 0, "efecto"), "lo pidio por telefono"),
     "renovaciones[0].efecto", "no tiene la forma efecto_de_la_renovacion"),
    ("el texto de la respuesta", _set(("acciones", 2, "respuesta", "texto"), "quiero mi 10"),
     "acciones[2].respuesta.texto", "clave fuera de la lista de respuesta"),
    ("una respuesta que no es una hora", _set(("acciones", 2, "respuesta", "contesto_at"), PHONE),
     "acciones[2].respuesta.contesto_at", "no tiene la forma hora_utc"),
    ("texto libre como cierre de la secuencia", _set(("secuencias", 0, "cancel_reason"), "Lo llame al 55 1234 5678"),
     "secuencias[0].cancel_reason", "no tiene la forma motivo"),
    ("un telefono como delay", _set(("config", "followup_policy_versions", 1, "steps", 1, "delay"), PHONE),
     "config.followup_policy_versions[1].steps[1].delay", "no tiene la forma intervalo"),
    ("un delay que la consulta no leyo", _set(("config", "followup_policy_versions", 1, "steps", 1, "delay"),
                                              "no_se_lee"),
     "config.followup_policy_versions[1].steps[1].delay", "no tiene la forma intervalo"),
    ("un operador en un paso", _set(("config", "followup_policy_versions", 1, "steps", 1, "approved_by"), "dan"),
     "config.followup_policy_versions[1].steps[1].approved_by", "clave fuera de la lista de paso"),
    ("un mail como motivo del link", _set(("links", 0, "failure_code"), "lead@ejemplo.com"),
     "links[0].failure_code", "tiene una arroba"),
    ("la consulta de la v2", _set(("_captured", "query"), fc.QUERY_NAME),
     "_captured.query", "no tiene la forma consulta_v3"),
    ("un conteo que no existe", _set(("_captured", "conteos", "telefonos"), 1),
     "_captured.conteos.telefonos", "clave fuera de la lista de conteos"),
    ("una seccion que no existe", _set(("mensajes",), []), "mensajes", "clave fuera de la lista de captura"),
]


@pytest.mark.parametrize(("name", "mutate", "path", "reason"), INJECTIONS, ids=[i[0] for i in INJECTIONS])
def test_a_planted_datum_breaks_the_v3_shape_and_the_message_hides_it(
    name: str, mutate: Callable[[dict[str, Any]], None], path: str, reason: str,
) -> None:
    data = copy.deepcopy(SYNTHETIC)
    mutate(data)
    problems = fc.shape_problems(data, fc.FORM_V3)
    assert fc.ShapeProblem(path, reason) in problems, [str(p) for p in problems]
    with pytest.raises(fc.CaptureShapeError) as caught:
        fc.validate_shape(data, fc.FORM_V3)
    for text in [str(caught.value), *map(str, problems)]:
        assert PHONE_DIGITS not in text and COUPON_CODE not in text and "://" not in text
        assert "@" not in text and "hermes~" not in text


# El policy_key de un flujo del motor ('flujo:' || flow_id), el del carrito nativo de ATT1 (att1-db,
# 2026-10-26, E-127): sin guiones tiene nueve cifras seguidas. Entra solo entero y canonico.
ENGINE_POLICY_KEY = "flujo:3ad1c523-2869-43f1-82ad-ed2610f2f521"
POLICY_KEY_PATHS = [
    ("casos", 1, "policy_key"), ("secuencias", 1, "policy_key"),
    ("config", "followup_policy_versions", 1, "policy_key"),
    ("config", "pilot_scope_versions", 1, "policy_key"),
]


@pytest.mark.parametrize("path", POLICY_KEY_PATHS)
def test_the_policy_key_of_an_engine_flow_keeps_the_v3_shape(path: tuple[Any, ...]) -> None:
    data = copy.deepcopy(SYNTHETIC)
    _set(path, ENGINE_POLICY_KEY)(data)
    assert fc.shape_problems(data, fc.FORM_V3) == []


@pytest.mark.parametrize("value", [
    ENGINE_POLICY_KEY[:-1],                                   # un UUID corto
    ENGINE_POLICY_KEY + "0",                                  # un UUID largo
    ENGINE_POLICY_KEY.replace("flujo:", "flujos:"),           # otro prefijo
    ENGINE_POLICY_KEY.replace("flujo:", ""),                  # sin prefijo: no es una forma fija
    ENGINE_POLICY_KEY.replace("-", ""),                       # sin guiones
    "flujo:" + PHONE_DIGITS + "000",                          # un telefono detras de flujo:
    "att1-" + PHONE_DIGITS,                                   # una clave con un telefono
])
def test_any_other_policy_key_with_nine_digits_still_breaks_the_shape(value: str) -> None:
    data = copy.deepcopy(SYNTHETIC)
    _set(("casos", 1, "policy_key"), value)(data)
    assert fc.ShapeProblem("casos[1].policy_key", "tiene nueve o mas digitos seguidos") in \
        fc.shape_problems(data, fc.FORM_V3)


@pytest.mark.parametrize("path", [("cupones", 0, "failure_code"), ("links", 0, "status")])
def test_the_independent_sweep_also_catches_a_coupon_or_a_url(path: tuple[Any, ...]) -> None:
    data = copy.deepcopy(SYNTHETIC)
    _set(path, URL if path[-1] == "status" else f"llamar al {PHONE}")(data)
    assert _independent_findings(data)


# ---------------------------------------------------------------------------
# 3. El fixture v3 y su constructor.
# ---------------------------------------------------------------------------

SHA = "0" * 64


def test_the_v3_fixture_round_trips_and_keeps_its_counts() -> None:
    fixture = fc.build_fixture(SYNTHETIC, source_sha256=SHA, source_bytes=1, form=fc.FORM_V3)
    assert fc.shape_problems(fixture, fc.FORM_V3) == []
    restored = fc.restore_fixture(fixture)
    # Los cupones, los links y las renovaciones vuelven iguales (el sintetico ya esta en Z, sin nulos).
    for section in ("cupones", "links", "renovaciones"):
        assert restored[section] == SYNTHETIC[section], section
    # Las acciones y las secuencias quedan solo las de los casos de la ventana: sale el carrito de
    # la siembra (la v6), y las reevaluaciones, que la paridad v3 no lee.
    window = fc.window_case_ids(SYNTHETIC)
    assert len(window) == 5
    for section in ("secuencias", "acciones"):
        assert restored[section] == [row for row in SYNTHETIC[section] if row["recovery_case_id"] in window]
    assert restored["reevaluaciones"] == []
    trims = fixture["_captured"]["fixture"]["recortes"]
    expected = {**SYNTHETIC["_captured"]["conteos"],
                **{name: trim["fixture"] for name, trim in trims.items()}}
    assert fc.count_sections(restored, fc.FORM_V3) == expected
    assert trims == {
        "primer_contacto": {"captura": 1, "fixture": 1}, "intenciones": {"captura": 1, "fixture": 1},
        "secuencias": {"captura": 6, "fixture": 5}, "acciones": {"captura": 14, "fixture": 13},
        "intentos": {"captura": 14, "fixture": 13}, "intentos_tramos": {"captura": 13, "fixture": 12},
        "envios": {"captura": 10, "fixture": 9}, "reevaluaciones": {"captura": 0, "fixture": 0},
        "reevaluaciones_tramos": {"captura": 0, "fixture": 0},
    }
    assert "recorta_fuera_de_la_ventana" in fixture["_captured"]["fixture"]["transformaciones"]
    # Las constantes salen tambien de las secciones nuevas (con dos filas o mas).
    constants = fixture["_captured"]["fixture"]["constantes"]
    assert constants["secuencias"]["reason"] == "cart_abandonment"
    assert "links" not in constants


def test_the_v3_fixture_is_deterministic() -> None:
    first = fc.serialize(fc.build_fixture(SYNTHETIC, source_sha256=SHA, form=fc.FORM_V3))
    again = fc.serialize(fc.build_fixture(copy.deepcopy(SYNTHETIC), source_sha256=SHA, form=fc.FORM_V3))
    assert first == again


def test_the_v2_form_still_refuses_to_build_from_a_v3_capture() -> None:
    with pytest.raises(fc.CaptureShapeError):
        fc.build_fixture(SYNTHETIC, source_sha256=SHA)


def test_the_cli_takes_the_form_as_an_option(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert CLI.main(["--forma", "v3", "--check-privacy", str(SYNTHETIC_PATH)]) == 0
    assert "privacidad: verde (captura v3" in capsys.readouterr().out
    # Sin la opcion, la forma es la v2, y la v3 no pasa.
    assert CLI.main(["--check-privacy", str(SYNTHETIC_PATH)]) == 1
    assert "privacidad: rojo (captura" in capsys.readouterr().out
    out = tmp_path / "fixture_v3.json"
    assert CLI.main(["--forma", "v3", "--capture", str(SYNTHETIC_PATH), "--out", str(out)]) == 0
    capsys.readouterr()
    assert CLI.main(["--forma", "v3", "--check-privacy", str(out)]) == 0
    assert "privacidad: verde (fixture v3" in capsys.readouterr().out


def test_the_cli_says_red_on_a_v3_copy_with_a_coupon_and_does_not_print_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    data = copy.deepcopy(SYNTHETIC)
    data["cupones"][1]["code"] = COUPON_CODE
    data["links"][0]["status"] = URL
    copy_path = tmp_path / "con_cupon.json"
    copy_path.write_bytes(fc.serialize(data))
    assert CLI.main(["--forma", "v3", "--check-privacy", str(copy_path)]) == 1
    out = capsys.readouterr().out
    assert "2 problemas" in out
    assert "cupones[1].code: clave fuera de la lista de cupon" in out
    assert "links[0].status: no tiene la forma codigo" in out
    assert COUPON_CODE not in out and "://" not in out


# ---------------------------------------------------------------------------
# 4. La consulta v3: sus secciones son las de la forma, y no lee lo que no sale.
# ---------------------------------------------------------------------------

def test_the_v3_query_builds_the_sections_of_the_form() -> None:
    assert f"'query', '{fc.QUERY_NAME_V3}'" in QUERY_V3
    top = re.findall(r"^  '([a-z_]+)', ", QUERY_V3, flags=re.MULTILINE)
    assert tuple(top) == fc.TOP_SECTIONS_V3
    counted = re.findall(r"^          '([a-z_]+)', \(select count", QUERY_V3, flags=re.MULTILINE)
    assert set(counted) == set(fc.COUNTS_V3.fields)
    # La ventana arranca con la v7 del carrito, y sin ella falla entera.
    assert "'att1-recuperacion'::text as scope_key" in QUERY_V3
    assert "7 as desde_version" in QUERY_V3
    assert "'captura_sin_activacion:'" in QUERY_V3


def test_the_v3_query_never_selects_a_code_a_url_or_a_chatwoot_id() -> None:
    sql = "\n".join(line.split("--", 1)[0] for line in QUERY_V3.splitlines())
    forbidden = re.findall(
        r"\b(?:cu|li|rn|r|c|i)\.(code|hotmart_coupon_id|checkout_url_final|sck_value|original_sck|"
        r"issuance_ulid|external_user_id|chatwoot_[a-z_]+|trigger_external_message_id|lead_offer_code|"
        r"dropped_unsafe_fields)\b", sql)
    assert forbidden == []


# ---------------------------------------------------------------------------
# 5. El fixture real del 21/10: privacidad, peso y origen.
# ---------------------------------------------------------------------------

REAL_PATH = FLOW_ENGINE / "att1_recovery_capture_v3_20261021.json"
REAL_RAW = REAL_PATH.read_bytes()
REAL: dict[str, Any] = loads_strict(REAL_RAW)


def test_the_real_v3_fixture_has_the_closed_shape_and_nothing_that_looks_like_a_person() -> None:
    assert fc.shape_problems(REAL, fc.FORM_V3) == []
    assert _independent_findings(REAL) == []
    assert _independent_findings(fc.restore_fixture(REAL)) == []


def test_the_real_v3_fixture_is_compact_and_under_one_megabyte() -> None:
    import hashlib  # noqa: PLC0415

    assert len(REAL_RAW) < fc.FIXTURE_MAX_BYTES
    assert len(REAL_RAW) == REAL["_captured"]["fixture"]["fixture_bytes"]
    assert hashlib.sha256(fc.serialize(REAL)).hexdigest() == hashlib.sha256(REAL_RAW).hexdigest()


def test_the_real_v3_fixture_comes_from_this_query_and_keeps_its_counts() -> None:
    import hashlib  # noqa: PLC0415

    meta = REAL["_captured"]["fixture"]
    # La captura de att1-db del 2026-10-21 a las 17:16:07Z (1.959.974 bytes, en el scratchpad de la
    # sesion; va a los anexos del vault).
    assert meta["source_sha256"] == "4a7303553cc71b87eb047a44edf0d71f6dacc3fc13575879553315b1ed6be355"
    assert meta["source_bytes"] == 1_959_974
    query = (FLOW_ENGINE / "att1_recovery_capture_query_v3.sql").read_bytes()
    assert hashlib.sha256(query).hexdigest() == "4e018e579d239580ff3d80cbe2afc444e5e13465bf6e017c78b13131fa0a2655"
    assert REAL["_captured"]["desde"] == "2026-10-19T18:13:51.812205Z"
    assert REAL["_captured"]["conteos"] == {
        "eventos": 364, "disparos_desde": 72, "casos": 252, "casos_del_carrito": 249, "secuencias": 249,
        "acciones": 330, "intentos": 1344, "intentos_tramos": 318, "envios": 267, "reevaluaciones": 2740,
        "reevaluaciones_tramos": 417, "cupones": 63, "links": 84, "renovaciones": 3, "autorizaciones": 435,
        "primer_contacto": 354, "intenciones": 332, "contactos": 447,
    }
    trims = meta["recortes"]
    expected = {**REAL["_captured"]["conteos"], **{name: trim["fixture"] for name, trim in trims.items()}}
    assert fc.count_sections(fc.restore_fixture(REAL), fc.FORM_V3) == expected


def test_the_policy_key_of_an_engine_flow_also_passes_in_the_reevaluations() -> None:
    # El sintetico no tiene reevaluaciones: van sobre el fixture real del 21/10, que tiene 125.
    data = copy.deepcopy(REAL)
    assert data["reevaluaciones"]
    for row in data["reevaluaciones"]:
        row["data"]["policy_key"] = ENGINE_POLICY_KEY
    assert fc.shape_problems(data, fc.FORM_V3) == []
