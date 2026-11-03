"""Las respuestas a la pregunta de entrada de una campana (20261103001500, E-317), leida como texto.

El comportamiento lo prueba ``tests/sql/followup_engine/validate_segment_entry_answers.mjs`` con la cadena
entera en PGlite. Aca se fija lo que no tiene que romperse en silencio:

* es una sola transaccion con ``lock_timeout``, la ultima de la cadena, y aborta si ya corrio;
* crea una tabla con RLS y dos funciones ``security definer``, solo para ``service_role``, y no reemplaza
  ni borra nada de antes;
* no guarda nada de la persona: ni telefono, ni mail, ni nombre, ni texto;
* el validador de PGlite es el ultimo de la cadena de npm.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261103001500_segment_entry_answers_v1.sql"
PACKAGE = ROOT / "tests" / "sql" / "followup_engine" / "package.json"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return "\n".join(line for line in _sql().splitlines() if not line.lstrip().startswith("--"))


def test_it_is_one_transaction_with_lock_timeout_and_the_last() -> None:
    sql = _sql()
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    # El origen de un recorrido nativo para la campana por landing (20261103001700, E-321): una lectura de service_role.
    assert names[-1] == "20261103001700_flow_engine_native_campaign_origin_v1.sql"
    assert names[-2] == MIGRATION.name
    assert names[-3] == "20261103001300_flow_engine_native_segment_label_v1.sql"
    assert sql.count("\nbegin;\n") == 1 and sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s';" in sql
    assert "message = 'segment_entry_answers_already_applied'" in sql


def test_it_creates_one_table_and_two_functions_and_replaces_nothing() -> None:
    code = _code().lower()
    assert code.count("create table ") == 1 and "create table public.segment_entry_answers" in code
    assert "alter table public.segment_entry_answers enable row level security" in code
    assert sorted(re.findall(r"create function public\.(\w+)\(", code)) == [
        "get_segment_entry_answers_v1",
        "record_segment_entry_answer_v1",
    ]
    assert code.count("security definer") == 2
    for forbidden in ("create or replace", "drop ", "alter function", "update public.", "delete from"):
        assert forbidden not in code, forbidden


def test_only_service_role_executes_and_the_table_has_no_api_grants() -> None:
    code = " ".join(_code().split())
    assert "execute format('grant execute on function %s to service_role', v_function);" in code
    assert "revoke all on table public.segment_entry_answers from public;" in code
    assert "execute format('revoke all on table public.segment_entry_answers from %I', v_role);" in code
    assert "message = 'segment_entry_answers_acl_unexpected'" in code
    assert "grant select" not in code.lower() and "grant all" not in code.lower()


def test_nothing_of_the_person_is_stored() -> None:
    table = re.search(r"create table public\.segment_entry_answers \((.*?)\n\);", _sql(), re.S)
    assert table is not None
    columns = [line.strip().split()[0] for line in table.group(1).strip().splitlines()
               if line.strip() and not line.strip().startswith("unique")]
    assert columns == ["id", "chatwoot_account_id", "chatwoot_inbox_id", "chatwoot_conversation_id",
                       "segment_key", "answer", "answered_at"]
    assert "check (answer in ('si', 'no'))" in table.group(1)


def test_the_validator_is_the_last_of_the_npm_chain() -> None:
    chain = json.loads(PACKAGE.read_text(encoding="utf-8"))["scripts"]["test"]
    assert chain.rstrip().endswith(
        " && node validate_flow_engine_native_segment_label.mjs && node validate_segment_entry_answers.mjs && node validate_flow_engine_native_campaign_origin.mjs --check")
