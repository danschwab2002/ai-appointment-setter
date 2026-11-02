"""El seguimiento a 24 h nativo en la base (20261101000200, E-251, P22-2 de M16), leido como texto.

El comportamiento lo prueba ``tests/sql/followup_engine/validate_flow_engine_native_silence.mjs`` con la
cadena entera en PGlite. Aca se fija lo que no tiene que romperse en silencio:

* todo resultado que escriben la portable y la reserva nativa lo conoce el bridge
  (``FOLLOWUP_CLAIM_OUTCOMES``): el cliente falla cerrado ante uno desconocido y el barrido del legado
  quedaria en ``claim_failed`` (el riesgo para ATT1 del diseno, 1.5);
* es una sola transaccion con ``lock_timeout``, la ultima de la cadena, y aborta si ya corrio;
* la nativa se deriva de la portable con reemplazos contados y falla con 55000 si no coinciden;
* no nombra el evento de adopcion (solo la 000400 lo escribe y la 000500 lo nombra entero).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from bridge.supabase import FOLLOWUP_CLAIM_OUTCOMES

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261101000200_flow_engine_native_silence_v1.sql"
PACKAGE = ROOT / "tests" / "sql" / "followup_engine" / "package.json"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return "\n".join(line for line in _sql().splitlines() if not line.lstrip().startswith("--"))


def test_it_is_one_transaction_with_lock_timeout_and_the_last() -> None:
    sql = _sql()
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    # Despues van los frenos del seguimiento despues de Z y con una ficha
    # vigente (20261101000300, E-263), que parchan las dos reservas.
    assert names[-1] == "20261101000300_followup_stops_after_last_touch_and_pending_payment_v1.sql"
    assert names[-2] == MIGRATION.name
    assert sql.count("\nbegin;\n") == 1 and sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s';" in sql
    assert "message = 'flow_engine_native_silence_already_applied'" in sql


def test_every_outcome_the_claims_write_is_known_to_the_bridge() -> None:
    # Dentro de los textos de reemplazo las comillas van dobladas.
    written = set(re.findall(r"outcome := ''([a-z_]+)'';", _sql()))
    assert written == {
        "blocked_followup_limit", "blocked_flow_not_running", "blocked_flow_version",
        "blocked_out_of_cohort", "blocked_flow_cap", "blocked_native_flow",
    }
    assert written <= FOLLOWUP_CLAIM_OUTCOMES


def test_the_native_claim_is_derived_with_counted_replacements() -> None:
    code = " ".join(_code().split())
    assert "v_definition := pg_get_functiondef(v_portable::regprocedure);" in code
    assert ("if length(v_definition) - length(replace(v_definition, v_anchors[v_index], '')) "
            "<> length(v_anchors[v_index]) then") in code
    assert "message = 'unexpected_conversation_followup_claim_definition'" in code
    # Se deriva antes de la barrera: la portable que lee no la tiene.
    assert code.index("$silence_claim$") < code.index("'flow_engine_native_silence_barrier')")
    assert "position('blocked_native_flow' in v_definition) > 0" in code


def test_only_service_role_executes_the_native_claim() -> None:
    code = _code()
    assert "execute format('revoke all on function %s from public', c_native);" in code
    assert "execute format('grant execute on function %s to service_role', c_native);" in code
    assert "message = 'flow_engine_native_silence_acl_unexpected'" in code


def test_the_native_claim_is_in_the_effective_handoff_acl_allowlist() -> None:
    # La reserva nativa lee human_handoff_requests (el predicado de la portable) y la ejecuta
    # service_role: el chequeo exhaustivo de Postgres real (validate_handoff_postgres.py, en CI)
    # la tiene que nombrar, como a la portable (20261010000100).
    handoff = (ROOT / "tests" / "sql" / "followup_engine" / "validate_handoff_postgres.py").read_text(encoding="utf-8")
    assert "'claim_flow_engine_silence_v1'," in handoff


def test_it_does_not_name_the_adoption_event() -> None:
    assert "inbound_adopted_" + "template_conversation" not in _sql()


def test_the_validator_is_the_last_of_the_npm_chain() -> None:
    chain = json.loads(PACKAGE.read_text(encoding="utf-8"))["scripts"]["test"]
    # Despues va solo el de los frenos del seguimiento (20261101000300, E-263).
    assert chain.rstrip().endswith(
        " && node validate_flow_engine_native_silence.mjs && node validate_followup_stops.mjs"
    )
