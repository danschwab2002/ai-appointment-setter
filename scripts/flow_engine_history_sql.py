#!/usr/bin/env python3
"""Arma la consulta de read_flow_engine_history_v1 desde la captura v3, sin copiarla a mano.

Contrato ``docs/contracts/flow-engine-v1.md``, seccion 2: la lectura de la
historia devuelve la captura de la paridad con su forma cerrada, restringida a
la instancia, al producto y a ``p_since``. La consulta de esa forma es
``tests/fixtures/flow_engine/att1_recovery_capture_query_v3.sql`` (la T2.2),
que corre Andy en att1-db. La funcion de la base es esa misma consulta con:

* la cabecera cambiada: los CTE ``p``, ``w`` y ``v`` (la activacion de la v7
  de att1-recuperacion y 7 dias de siembra) pasan a un ``v`` con los
  parametros de la funcion: la siembra es ``p_since``, la ventana empieza un
  dia despues, el scope del carrito es el de su ruta, y la sal sigue siendo
  aleatoria por llamada;
* los dos textos fijos que dicen de donde sale (``origin`` y ``desde_origen``);
* el filtro del producto en las tablas que no lo heredan de otra seccion: los
  eventos y los casos por el producto de Hotmart del binding; el primer
  contacto, las autorizaciones, los scopes, los controles y sus eventos por los
  scopes del producto; las politicas por las de esos scopes y las del motor;
  el tope por persona por el tenant; y los conflictos de compra por el
  producto de su evento. El censo de ``webhook_events`` queda como esta: es de
  la base de la instancia, sin personas ni ventana.

La migracion 20261022000300 le suma despues, por ancla sobre la definicion
vigente, las autorizaciones del motor (en la lista, el conteo y los contactos).
Esas anclas son texto de esta salida, y la migracion aborta si alguna falta: si
cambia la salida, hay que mirar tambien esa migracion.

Cada reemplazo tiene que aparecer exactamente una vez en la consulta: si la v3
cambia y un ancla se pierde, esto falla en voz alta. La migracion lleva la
salida entre dos marcas, y ``--check`` (que corre pytest) verifica que sea
exactamente esta.

    uv run python scripts/flow_engine_history_sql.py --check
    uv run python scripts/flow_engine_history_sql.py --write

No hace red ni lee el entorno.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUERY = ROOT / "tests" / "fixtures" / "flow_engine" / "att1_recovery_capture_query_v3.sql"
MIGRATION = ROOT / "supabase" / "migrations" / "20261021000200_flow_engine_routes_v1.sql"
BEGIN = "    -- flow_engine_history_query: begin\n"
END = "    -- flow_engine_history_query: end\n"

HEAD_START = "with p as (\n"
HEAD_END = "    select w.*, w.desde - interval '7 days' as siembra from w\n),\n"
NEW_HEAD = """\
with v as (
    -- read_flow_engine_history_v1: los parametros de la funcion en lugar de
    -- los CTE p, w y v de la consulta v3. La siembra es p_since y la ventana
    -- empieza un dia despues; la sal es aleatoria por llamada y no sale.
    select clock_timestamp() as captured_at,
           gen_random_uuid()::text as sal,
           v_cart_scope_key as scope_key,
           p_since + interval '1 day' as desde,
           p_since as siembra,
           v_product_ids as product_ids,
           v_scope_keys as scope_keys,
           v_policy_keys as policy_keys,
           v_tenant_ref as tenant_ref
),
"""

# (nombre, texto de la v3, texto de la funcion). El orden es el de la consulta.
SUBSTITUTIONS: tuple[tuple[str, str, str], ...] = (
    (
        "eventos_del_producto",
        "       and e.received_at >= v.siembra\n),\nev as (\n",
        "       and e.received_at >= v.siembra\n"
        "       and e.payload #>> '{data,product,id}' = any(v.product_ids)\n),\nev as (\n",
    ),
    (
        "casos_del_producto",
        "     where rc.source = 'hotmart'\n",
        "     where rc.source = 'hotmart'\n"
        "       and rc.external_product_id = any(v.product_ids)\n",
    ),
    (
        "primer_contacto_del_producto",
        "    select x.* from public.portable_precheckout_first_contact_plans x, v"
        " where x.created_at >= v.siembra\n",
        "    select x.* from public.portable_precheckout_first_contact_plans x, v"
        " where x.created_at >= v.siembra\n"
        "       and x.scope_key = any(v.scope_keys)\n",
    ),
    (
        "contactos_de_las_autorizaciones_del_producto",
        "                    union select a.contact_id from public.pilot_outbound_request_authorizations a)\n",
        "                    union select a.contact_id from public.pilot_outbound_request_authorizations a\n"
        "                           where a.scope_key = any((select v.scope_keys from v)::text[]))\n",
    ),
    (
        "origen",
        "      'origin', 'att1-db (stack setter-att1), psql con default_transaction_read_only',\n",
        "      'origin', 'read_flow_engine_history_v1 (flow-engine-v1)',\n",
    ),
    (
        "origen_de_la_ventana",
        "      'desde_origen', (select 'pilot_scope_version_activated:' || scope_key || ':v>=' || desde_version from v),\n",
        "      'desde_origen', (select 'read_flow_engine_history_v1:' || scope_key || ':p_since+1d' from v),\n",
    ),
    (
        "conteo_de_las_autorizaciones_del_producto",
        "          'autorizaciones', (select count(*) from public.pilot_outbound_request_authorizations),\n",
        "          'autorizaciones', (select count(*) from public.pilot_outbound_request_authorizations a\n"
        "                              where a.scope_key = any((select v.scope_keys from v)::text[])),\n",
    ),
    (
        "scopes_del_producto",
        "          order by s.scope_key, s.version), '[]'::json) from public.pilot_scope_versions s),\n",
        "          order by s.scope_key, s.version), '[]'::json) from public.pilot_scope_versions s\n"
        "         where s.scope_key = any((select v.scope_keys from v)::text[])),\n",
    ),
    (
        "politicas_del_producto",
        "          order by f.policy_key, f.version), '[]'::json) from public.followup_policy_versions f),\n",
        "          order by f.policy_key, f.version), '[]'::json) from public.followup_policy_versions f\n"
        "         where f.policy_key = any((select v.policy_keys from v)::text[])),\n",
    ),
    (
        "controles_del_producto",
        "          order by c.scope_key), '[]'::json) from public.pilot_runtime_controls c),\n",
        "          order by c.scope_key), '[]'::json) from public.pilot_runtime_controls c\n"
        "         where c.scope_key = any((select v.scope_keys from v)::text[])),\n",
    ),
    (
        "tope_por_persona_del_tenant",
        "          from public.pilot_proactive_contact_caps k),\n",
        "          from public.pilot_proactive_contact_caps k\n"
        "         where k.tenant_key = (select v.tenant_ref from v)),\n",
    ),
    (
        "eventos_de_control_del_producto",
        "         where e.contact_id is null and e.action_id is null and e.attempt_id is null)),\n",
        "         where e.contact_id is null and e.action_id is null and e.attempt_id is null\n"
        "           and e.scope_key = any((select v.scope_keys from v)::text[]))),\n",
    ),
    (
        "conflictos_de_compra_del_producto",
        "      from public.hotmart_purchase_semantic_conflicts k),\n",
        "      from public.hotmart_purchase_semantic_conflicts k\n"
        "     where exists (select 1 from public.webhook_events we\n"
        "                    where we.id = k.existing_event_id\n"
        "                      and we.payload #>> '{data,product,id}' = any((select v.product_ids from v)::text[]))),\n",
    ),
    (
        "autorizaciones_del_producto",
        "      ) order by a.authorized_at, a.id), '[]'::json) from public.pilot_outbound_request_authorizations a),\n",
        "      ) order by a.authorized_at, a.id), '[]'::json) from public.pilot_outbound_request_authorizations a\n"
        "     where a.scope_key = any((select v.scope_keys from v)::text[])),\n",
    ),
)


class HistoryQueryError(ValueError):
    """La consulta v3 cambio y un ancla ya no aparece exactamente una vez."""


def _once(text: str, needle: str, label: str) -> int:
    count = text.count(needle)
    if count != 1:
        raise HistoryQueryError(f"{label}: aparece {count} veces en la consulta v3")
    return text.index(needle)


def generate(query: str) -> str:
    """El cuerpo de la consulta de read_flow_engine_history_v1, sin el punto y coma final."""
    start = _once(query, HEAD_START, "cabecera")
    end = _once(query, HEAD_END, "fin de la cabecera") + len(HEAD_END)
    if end <= start:
        raise HistoryQueryError("cabecera: el fin esta antes del comienzo")
    body = NEW_HEAD + query[end:]
    for name, old, new in SUBSTITUTIONS:
        _once(body, old, name)
        body = body.replace(old, new)
    body = body.rstrip()
    if not body.endswith(");"):
        raise HistoryQueryError("final: la consulta v3 no termina en «);»")
    return body[:-1] + "\n"


def embedded(migration: str) -> str:
    """Lo que la migracion lleva entre las dos marcas."""
    start = _once(migration, BEGIN, "marca de comienzo") + len(BEGIN)
    end = _once(migration, END, "marca de fin")
    return migration[start:end]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="sale con 1 si la migracion no lleva la consulta generada")
    mode.add_argument("--write", action="store_true", help="escribe la consulta generada en la migracion")
    mode.add_argument("--print", action="store_true", help="imprime la consulta generada")
    args = parser.parse_args(argv)
    generated = generate(QUERY.read_text(encoding="utf-8"))
    if args.print:
        sys.stdout.write(generated)
        return 0
    migration = MIGRATION.read_text(encoding="utf-8")
    current = embedded(migration)
    if args.check:
        if current != generated:
            print("la migracion no lleva la consulta generada desde la v3", file=sys.stderr)
            return 1
        print("flow_engine_history_sql=OK")
        return 0
    start = migration.index(BEGIN) + len(BEGIN)
    MIGRATION.write_text(migration[:start] + generated + migration[start + len(current):], encoding="utf-8")
    print("flow_engine_history_sql=escrita")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
