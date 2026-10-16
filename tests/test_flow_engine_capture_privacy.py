"""La captura de ATT1 del repo no tiene datos personales ni de operador.

Diseno del motor de flujos (2026-10-16), §5.3 y §6.2. Fixture
(``tests/fixtures/flow_engine/``):

* ``att1_recovery_capture_20261016.json``: el fixture recortado de la captura
  v2 de att1-db (stack setter-att1), leida el 2026-10-16 a las 21:03:26Z en
  una sesion de solo lectura (``default_transaction_read_only``) con
  ``att1_recovery_capture_query_v2.sql``, que esta tal cual se corrio. Lo armo
  ``scripts/flow_engine_capture_fixture.py`` (§6.4); ``_captured.fixture``
  dice el sha256 de la captura completa, que vive en los anexos del diseno.

Dos redes. La primera es la forma cerrada de ``bridge.flow_capture``: cada
seccion con su lista de claves y cada clave con su forma. La segunda es un
barrido que no usa esa tabla: ninguna arroba y ningun numero de nueve cifras
fuera de las formas fijas (uuid, hex de 16, sha256, hora y fecha), ningun
nombre de columna de los que traen un dato o un operador, y ningun entero
largo salvo los milisegundos de Hotmart. Existe para que un error en la tabla
no tape un dato.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import re
import unicodedata
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from bridge import flow_capture as fc
from bridge.flow_definition import loads_strict

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "flow_engine" / "att1_recovery_capture_20261016.json"
RAW = FIXTURE_PATH.read_bytes()
FIXTURE: dict[str, Any] = loads_strict(RAW)
SCRIPT = ROOT / "scripts" / "flow_engine_capture_fixture.py"
SPEC = importlib.util.spec_from_file_location("flow_engine_capture_fixture", SCRIPT)
assert SPEC and SPEC.loader
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)

# Un telefono falso de Mexico, como lo manda Hotmart (§5.4, rojo 9). Ninguna
# salida puede llevar sus cifras.
PHONE = "+5215512345678"
PHONE_DIGITS = "5512345678"

# Las formas fijas, escritas aca otra vez a proposito: la segunda red no lee la
# tabla del modulo.
_FIXED = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[0-9a-f]{16}|[0-9a-f]{64}"
    r"|[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z)?"
)
_SEPARATORS = re.compile(r"[ \t\-.()+/]")
# Los nombres de columna que traen un dato de una persona o de un operador
# (§6.2, «Lo que no sale nunca»): ninguno puede aparecer en el fixture.
FORBIDDEN_KEYS = frozenset({
    "phone", "checkout_phone", "normalized_phone", "phone_raw", "email", "name", "first_name",
    "payload", "data_raw", "text", "content", "note", "notes", "refusal_reason",
    "external_user_id", "transaction", "approved_by", "changed_by", "change_reason",
    "actor", "requested_by", "channel_account_ref", "account_id", "inbox_id",
    "existing_semantic_tuple", "incoming_semantic_tuple", "incoming_payload", "sal",
})


def _walk(value: Any, path: str = "") -> Iterator[tuple[str, str | None, Any]]:
    """Cada hoja con su camino y el nombre de su clave."""
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
    """La segunda red: lo que tiene forma de dato, sin mirar la tabla de formas."""
    findings = []
    for path, key, value in _walk(data):
        if isinstance(value, str):
            if "@" in value:
                findings.append(f"{path}: arroba")
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
# 1. El fixture del repo, en verde.
# ---------------------------------------------------------------------------

def test_the_repo_fixture_has_the_closed_shape() -> None:
    assert fc.shape_problems(FIXTURE) == []
    assert fc.is_fixture(FIXTURE)


def test_the_restored_fixture_keeps_the_closed_shape() -> None:
    restored = fc.restore_fixture(FIXTURE)
    assert fc.shape_problems(restored) == []
    for section, values in FIXTURE["_captured"]["fixture"]["constantes"].items():
        assert all(row[key] == value for row in restored[section] for key, value in values.items())


def test_nothing_in_the_repo_fixture_looks_like_a_person_or_an_operator() -> None:
    assert _independent_findings(FIXTURE) == []
    assert _independent_findings(fc.restore_fixture(FIXTURE)) == []


# Las pruebas de este archivo comparan caminos y numeros, nunca valores: si un
# dato se colara, la falla de pytest no lo copia al log de la CI.

def test_the_people_are_only_salted_keys_of_sixteen_hex() -> None:
    person_fields = {"person_key", "digits_key", "email_key", "person_keys", "digits_keys", "email_keys"}
    seen = [(path, isinstance(value, str) and re.fullmatch(r"[0-9a-f]{16}", value) is not None)
            for path, key, value in _walk(FIXTURE) if key in person_fields]
    assert [path for path, ok in seen if not ok] == []
    # Todos los eventos con telefono, todas las intenciones y todos los contactos.
    assert len(seen) > 1000


def test_the_repo_fixture_is_compact_sorted_and_under_one_megabyte() -> None:
    size = len(RAW)
    assert size < fc.FIXTURE_MAX_BYTES == 1_000_000
    assert size == FIXTURE["_captured"]["fixture"]["fixture_bytes"]
    # Releer y volver a escribir da los mismos bytes: compacto, con las claves
    # ordenadas y nada mas (se comparan los sha256, no el texto).
    assert hashlib.sha256(fc.serialize(FIXTURE)).hexdigest() == hashlib.sha256(RAW).hexdigest()
    text = RAW.decode("utf-8")
    assert [hex(ord(c)) for c in text if unicodedata.category(c) in ("Cc", "Cf") and c != "\n"] == []
    assert text.count("+00:00") == 0


def test_the_cli_says_green_on_the_repo_fixture(capsys: pytest.CaptureFixture[str]) -> None:
    assert CLI.main(["--check-privacy", str(FIXTURE_PATH)]) == 0
    assert "privacidad: verde (fixture" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 2. Las dos redes tienen dientes: un dato metido en una copia no pasa, y el
#    mensaje dice el camino sin el valor.
# ---------------------------------------------------------------------------

def _set(path: tuple[Any, ...], value: Any) -> Callable[[dict[str, Any]], None]:
    def apply(data: dict[str, Any]) -> None:
        target: Any = data
        for step in path[:-1]:
            target = target[step]
        target[path[-1]] = value
    return apply


INJECTIONS = [
    # (nombre, mutacion, camino que se espera, motivo que se espera)
    ("telefono como clave de persona", _set(("eventos", 0, "person_key"), PHONE),
     "eventos[0].person_key", "no tiene la forma clave_de_persona"),
    ("telefono en una columna nueva", _set(("casos", 0, "phone"), PHONE),
     "casos[0].phone", "clave fuera de la lista de caso"),
    ("telefono como codigo de oferta", _set(("eventos", 0, "offer_code"), PHONE[1:]),
     "eventos[0].offer_code", "tiene nueve o mas digitos seguidos"),
    ("telefono con espacios en un texto de la consulta",
     _set(("_captured", "redactions", 0), "telefono +52 1 55 1234 5678"),
     "_captured.redactions[0]", "tiene nueve o mas digitos seguidos"),
    # Diez cifras entran en la forma del id de producto: las frena la regla de los digitos.
    ("telefono como constante", _set(("_captured", "fixture", "constantes", "eventos", "product_id"), PHONE_DIGITS),
     "_captured.fixture.constantes.eventos.product_id", "tiene nueve o mas digitos seguidos"),
    ("telefono como numero", _set(("acciones", 0, "execution_attempt_count"), int(PHONE[1:])),
     "acciones[0].execution_attempt_count", "entero fuera de rango"),
    ("telefono como milisegundos", _set(("eventos", 0, "creation_date_ms"), int(PHONE[1:])),
     "eventos[0].creation_date_ms", "milisegundos_de_epoca fuera de rango"),
    ("telefono como clave de un objeto", _set(("casos", 0, PHONE), 1),
     "casos[0].<clave con forma no permitida>", "clave fuera de la lista de caso"),
    ("telefono en una lista de claves", _set(("contactos", 0, "person_keys"), [PHONE]),
     "contactos[0].person_keys[0]", "no tiene la forma clave_de_persona"),
    ("mail en una intencion", _set(("intenciones", 0, "offer_ref"), "lead@ejemplo.com"),
     "intenciones[0].offer_ref", "tiene una arroba"),
    ("operador en la configuracion", _set(("config", "pilot_scope_versions", 0, "approved_by"), "dan"),
     "config.pilot_scope_versions[0].approved_by", "clave fuera de la lista de version_del_scope"),
    ("operador en un evento de control",
     _set(("config", "pilot_control_events_control_level", 0, "actor"), "dan"),
     "config.pilot_control_events_control_level[0].actor", "clave fuera de la lista de evento_de_control"),
    ("texto libre en una reevaluacion", _set(("reevaluaciones", 0, "data", "note"), "lo llame"),
     "reevaluaciones[0].data.note", "clave fuera de la lista de datos_de_la_reevaluacion"),
    ("texto libre como motivo", _set(("eventos", 0, "processing_error"), "Lo llame al 55 1234 5678"),
     "eventos[0].processing_error", "no tiene la forma motivo"),
    # Los bordes de la primera red (§5.3: ningun texto con 9 digitos seguidos).
    # Nueve cifras: un movil de Chile, Espana o Peru, o uno local cortado.
    ("telefono de 9 cifras", _set(("eventos", 0, "offer_code"), "912345678"),
     "eventos[0].offer_code", "tiene nueve o mas digitos seguidos"),
    ("telefono con guiones en un texto de la consulta", _set(("_captured", "redactions", 0), "55-1234-5678"),
     "_captured.redactions[0]", "tiene nueve o mas digitos seguidos"),
    # En minuscula y con espacios: lo frena el espacio, no la mayuscula.
    ("texto libre en minuscula como motivo", _set(("eventos", 0, "processing_error"), "lo llame ayer"),
     "eventos[0].processing_error", "no tiene la forma motivo"),
    # Un nombre con tilde en un texto de la consulta: solo ASCII imprimible.
    ("nombre con tilde en un texto de la consulta", _set(("_captured", "redactions", 0), "Ramón"),
     "_captured.redactions[0]", "no tiene la forma texto_de_la_consulta"),
    ("un nulo en una lista de claves", _set(("contactos", 0, "person_keys"), [None]),
     "contactos[0].person_keys[0]", "un nulo dentro de una lista"),
    ("una hora con forma que no existe", _set(("eventos", 0, "received_at"), "2026-02-30T10:00:00.000000Z"),
     "eventos[0].received_at", "hora_utc no es una hora que exista"),
    ("una seccion que no existe", _set(("secuencias",), []), "secuencias", "clave fuera de la lista de captura"),
]


@pytest.mark.parametrize(("name", "mutate", "path", "reason"), INJECTIONS, ids=[i[0] for i in INJECTIONS])
def test_a_planted_datum_breaks_the_shape_and_the_message_hides_it(
    name: str, mutate: Callable[[dict[str, Any]], None], path: str, reason: str,
) -> None:
    data = copy.deepcopy(FIXTURE)
    mutate(data)
    problems = fc.shape_problems(data)
    assert fc.ShapeProblem(path, reason) in problems, [str(p) for p in problems]
    with pytest.raises(fc.CaptureShapeError) as caught:
        fc.validate_shape(data)
    for text in [str(caught.value), *map(str, problems)]:
        assert PHONE_DIGITS not in text and "1234" not in text and "@" not in text


@pytest.mark.parametrize("path", [("eventos", 0, "person_key"), ("_captured", "redactions", 0)])
def test_the_independent_sweep_also_catches_a_phone(path: tuple[Any, ...]) -> None:
    data = copy.deepcopy(FIXTURE)
    _set(path, PHONE if path[-1] == "person_key" else "llamar al +52 1 55 1234 5678")(data)
    assert _independent_findings(data)


def test_the_cli_says_red_on_a_copy_with_a_phone_and_does_not_print_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    data = copy.deepcopy(FIXTURE)
    data["eventos"][3]["person_key"] = PHONE
    data["casos"][1]["phone"] = PHONE
    copy_path = tmp_path / "fixture_con_telefono.json"
    copy_path.write_bytes(fc.serialize(data))
    assert CLI.main(["--check-privacy", str(copy_path)]) == 1
    out = capsys.readouterr().out
    assert "privacidad: rojo (fixture" in out and "2 problemas" in out
    assert "eventos[3].person_key: no tiene la forma clave_de_persona" in out
    assert "casos[1].phone: clave fuera de la lista de caso" in out
    assert PHONE_DIGITS not in out
