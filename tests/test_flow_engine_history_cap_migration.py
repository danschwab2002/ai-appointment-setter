"""El tope de read_flow_engine_history_v1 en 4.000.000 bytes (20261021000300).

La migracion solo cambia una constante, con el metodo del ancla de
20261021000200. Estas pruebas fijan la forma; el comportamiento lo verifica
validate_flow_engine_v1.mjs en PGlite, que aplica la cadena entera.
"""

from __future__ import annotations

import re
from pathlib import Path

from bridge.flow_change_api import MAX_STORE_BYTES

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261021000300_flow_engine_history_cap_v1.sql"
ROUTES = MIGRATIONS / "20261021000200_flow_engine_routes_v1.sql"
READS = MIGRATIONS / "20261022000300_flow_engine_reads_see_motor_v1.sql"
NATIVE = MIGRATIONS / "20261024000200_flow_engine_native_v1.sql"

ANCHOR = "    c_max_bytes constant integer := 1000000;\n"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _cap() -> int:
    match = re.search(r"c_max_bytes constant integer := (\d+); -- flow_engine_history_cap_v1", _sql())
    assert match is not None
    return int(match.group(1))


def test_it_is_one_transaction_and_comes_right_after_the_routes() -> None:
    sql = _sql()
    assert "\nbegin;\n" in sql and sql.rstrip().endswith("commit;")
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    assert names[names.index(ROUTES.name) + 1] == MIGRATION.name
    # Lo que viene despues no vuelve a definir la historia del motor: la
    # lectura de /flows mas rapida (20261021000400) solo reemplaza
    # read_automation_expert_flows_v1, y el camino del motor para el carrito
    # (20261022000100, la T2.3b) no la toca. Las lecturas que cuentan lo del
    # motor (20261022000300) le suman sus autorizaciones por ancla, sin tocar
    # el tope (test_flow_engine_reads_see_motor_migration.py). El motor nativo
    # (20261024000200) le saca lo nativo por ancla, tampoco toca el tope.
    for later in names[names.index(MIGRATION.name) + 1:]:
        if later not in (READS.name, NATIVE.name):
            assert "read_flow_engine_history_v1" not in (MIGRATIONS / later).read_text(encoding="utf-8")
    assert "c_max_bytes" not in READS.read_text(encoding="utf-8")
    assert "c_max_bytes" not in NATIVE.read_text(encoding="utf-8")


def test_the_anchor_is_the_routes_constant_and_appears_once() -> None:
    # La definicion vigente de la funcion es la de 20261021000200: ninguna
    # migracion posterior la redefine.
    routes = ROUTES.read_text(encoding="utf-8")
    assert routes.count(ANCHOR) == 1
    sql = _sql()
    assert "E'" + ANCHOR.replace("\n", "\\n") + "'" in sql
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name > ROUTES.name and path not in (MIGRATION, READS, NATIVE):
            assert "read_flow_engine_history_v1" not in path.read_text(encoding="utf-8"), path.name


def test_it_only_patches_the_constant() -> None:
    sql = _sql()
    code = re.sub(r"--[^\n]*", "", sql)
    assert "create or replace function" not in code.lower()
    assert "grant " not in code.lower() and "revoke " not in code.lower()
    assert "pg_get_functiondef" in code
    # Verifica el duenio y que los permisos no cambien.
    assert "flow_engine_history_owner_mismatch" in sql
    assert "flow_engine_history_acl_changed" in sql
    assert "unexpected_flow_engine_history_definition" in sql


def test_the_cap_stays_under_the_bridge_cut() -> None:
    # El bridge corta la respuesta del RPC en MAX_STORE_BYTES con un error
    # generico. Un tope de la base por debajo conserva el motivo con nombre
    # (flow_engine_history_too_large). jsonb::text y el cuerpo de PostgREST
    # tienen la misma forma, asi que el margen de ~190 KB alcanza.
    cap = _cap()
    assert cap == 4_000_000
    assert cap < MAX_STORE_BYTES
