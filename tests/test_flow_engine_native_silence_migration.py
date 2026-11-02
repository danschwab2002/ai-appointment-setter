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
    # La renovacion por respuesta y el cupon del turno (20261102000100, E-267).
    # Las solicitudes de plantilla de Meta (20261102000200, E-268, F-146 partes 1 y 2).
    # El barrido del seguimiento a 24 h nativo (20261102000300, E-269).
    # El reciclado de un telefono de prueba (20261102000400, E-270, F-147 PR 1).
    # Los rechazos de Meta y el unico reintento del 131049 (20261103000500, E-284).
    # El historial de la pagina de aprobacion (20261103000600, E-295).
    # Si la persona ya es cliente (20261103000700, E-297).
    # Las cuatro ramas del pago rechazado nativo (20261103000900, E-306).
    assert names[-1] == "20261103000900_flow_engine_native_payment_failure_branches_v1.sql"
    assert names[-2] == "20261103000700_customer_status_v1.sql"
    assert names[-3] == "20261103000600_flow_approval_history_v1.sql"
    assert names[-4] == "20261103000500_meta_delivery_rejections_v1.sql"
    assert names[-5] == "20261102000400_identity_recycle_v1.sql"
    assert names[-6] == "20261102000300_flow_engine_native_silence_sweep_v1.sql"
    assert names[-7] == "20261102000200_meta_template_requests_v1.sql"
    assert names[-8] == "20261102000100_recovery_coupon_by_reply_v1.sql"
    assert names[-9] == "20261101000300_followup_stops_after_last_touch_and_pending_payment_v1.sql"
    assert names[-10] == MIGRATION.name
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
        " && node validate_flow_engine_native_silence.mjs && node validate_followup_stops.mjs && node validate_recovery_coupon_by_reply.mjs"
        " && node validate_meta_template_requests.mjs --check && node validate_flow_engine_native_silence_sweep.mjs"
        " && node validate_identity_recycle.mjs && node validate_meta_delivery_rejections.mjs && node validate_flow_approval_history.mjs && node validate_customer_status.mjs && node validate_flow_engine_native_payment_failure_branches.mjs --check"
    )
