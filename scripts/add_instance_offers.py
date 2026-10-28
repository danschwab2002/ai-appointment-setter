#!/usr/bin/env python3
"""Suma una o varias ofertas a una instancia que ya corre, sin version nueva de nada.

El caso que lo motiva: Maribel, 2026-10-28. La landing organica
``/epma/evg/vsl/org-a`` usa la oferta ``mmliigg5`` del mismo producto, que la
instancia no tiene: el formulario da ``422 ghl_landing_unknown`` y un carrito de
esa landing moriria en ``pilot_offer_mismatch``. Una oferta no esta en un solo
lugar. Este comando la suma, en una transaccion, a todo lo que la nombra:

* el binding activo de su producto (``commercial_ally_runtime_bindings``):
  ``additional_offer_codes`` y ``additional_offer_landings`` quedan iguales a los
  que arma el bridge con el manifiesto nuevo. **Se completa la fila, no se
  publica un ``binding_version`` nuevo**: la base admite un solo binding activo
  por producto, los eventos de Hotmart ya admitidos guardan su version en la
  procedencia y las RPC de planificacion exigen que siga activa; retirar la
  version dejaria sin planificar lo que ya entro (docs/instalar.md, «Por que se
  completa la fila»). La politica de compra y las de descuento van por version
  del binding, asi que tampoco se tocan;
* un scope de intencion de compra activo por oferta
  (``hotmart_purchase_intent_scopes``), con el ``max_lookback`` de la oferta por
  defecto;
* una fila activa del catalogo de ofertas del scope entrante del binding
  (``checkout_offer_catalog``, que solo admite filas nuevas), copiada de la fila
  por defecto con la landing de la oferta, sin marcarla por defecto;
* cada scope del piloto del producto al que apunta un control no cerrado (el
  carrito, el primer contacto, el pago rechazado...): la oferta se agrega al
  final de su ``additional_offer_codes``, **en la misma version**. Es la unica
  edicion que la base admite sobre un scope publicado (migracion
  ``20261028000300``), y la pide la transaccion para cada fila. Una version
  nueva perderia casos: el arranque de cada envio se autoriza con la version
  con la que se planifico el caso, y un control que apunta a otra lo rechaza
  (``pilot_scope_version_mismatch``). Por eso **ningun control se pausa, se
  activa ni se arma**: el carrito no se frena, y la generacion de cada control
  queda igual.

Lo que se suma sale del manifiesto nuevo (``instancia.toml`` con las ofertas ya
agregadas, el del PR mergeado), leido con el mismo codigo que el bridge
(``bridge.instance_manifest``). El comando exige que la base sea exactamente ese
manifiesto **sin** las ofertas pedidas (o ya con ellas: entonces no hace nada).
Cualquier otra diferencia (otro precio, otro inbox, una oferta sacada o
reordenada) frena sin escribir: no es una oferta nueva y este comando no la
resuelve.

Uso, en el VPS como root, desde el arbol del producto de la version que corre::

    python3 scripts/add_instance_offers.py --manifiesto <instancia.toml nuevo> --ofertas <a,b> --quien <q>
    python3 scripts/add_instance_offers.py ... --aplicar
    python3 scripts/add_instance_offers.py --manifiesto <instancia.toml nuevo> --ofertas <a,b> --verificar

* Sin ``--aplicar`` (en seco): corre la transaccion entera, verifica y hace
  ``rollback``. Dice exactamente que fila crea o cambia, y si con el manifiesto
  nuevo el bridge daria ``commercial_ally_binding: active``.
* ``--aplicar``: lo mismo con ``commit``, despues de un codigo que se lee de la
  terminal (lo corre una persona). Mide cuanto duro la transaccion.
* ``--verificar``: solo lectura. Que cada binding del manifiesto nuevo resuelva
  igual campo por campo (la comparacion del ``/ready`` del bridge, con el mismo
  codigo), y que cada oferta este en su scope de intencion, en el catalogo y en
  cada scope del piloto del producto. Con ``CONTENEDOR_BRIDGE``, mira ademas el
  ``/ready`` del bridge que corre (despues del redeploy).

**No toca el manifiesto ni las variables, ni reinicia nada.** El bridge lee el
manifiesto al arrancar: entre ``--aplicar`` y el redeploy, el bridge viejo sigue
atendiendo todo lo de antes (la base no cambio nada de lo que el ya usa), pero su
``/ready`` da 503 ``commercial_ally_binding_unavailable`` porque la fila ya tiene
la oferta y su manifiesto no. Al reves es peor: un bridge que arranca con el
manifiesto nuevo antes del ``--aplicar`` no resuelve su binding, y con
``SLACK_CONNECTOR_PROJECTION_ENABLED=true`` no arranca. Por eso, entre el merge
del manifiesto y este comando, nadie sincroniza el repo de la instancia ni
reinicia el bridge; y despues del ``--aplicar``, el redeploy va enseguida.

Variables (como ``publish_inbox_binding_version.py``):

* ``CONTENEDOR_DB``: filtro de ``docker ps --filter name=`` de la base de la
  instancia; tiene que dar exactamente un contenedor. ``USUARIO_DB`` y
  ``BASE_DB`` (``postgres``): el duenio de las tablas.
* ``CONTENEDOR_BRIDGE`` (opcional, solo ``--verificar``) y ``PUERTO_BRIDGE``
  (``8000``).

El SQL va por la entrada estandar de ``psql -X``; los valores, en una tabla
temporal cargada con ``COPY ... FROM STDIN``, nunca pegados en el texto. No lee
ni imprime telefonos, tokens ni secretos.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

# El codigo del manifiesto y del binding es el del bridge, del mismo arbol del
# producto: la prediccion del /ready no puede separarse de lo que hace el bridge.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bridge.commercial_ally import CommercialAllyConfig  # noqa: E402
from bridge.instance_manifest import InstanceManifest, ManifestError  # noqa: E402

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2

_PG_ERROR = re.compile(r"ERROR:\s+([a-z][a-z0-9_]{1,79})\b")
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_OFFER = re.compile(r"^[A-Za-z0-9]{4,32}$")

# Lo que dice cada freno de la base, en castellano.
REASONS = {
    "add_offer_binding_changed": "el binding de la base cambio entre la lectura y la escritura: correr otra vez",
    "add_offer_default_intent_scope_missing":
        "la oferta por defecto no tiene scope de intencion activo: no hay de donde copiar el max_lookback",
    "add_offer_intent_scope_taken":
        "la oferta ya tiene un scope de intencion activo de otro tenant, funnel o hotlink",
    "add_offer_catalog_default_missing":
        "el scope entrante del binding no tiene una fila por defecto activa en el catalogo",
    "add_offer_pilot_scope_changed": "un scope del piloto cambio entre la lectura y la escritura: correr otra vez",
    "add_offer_verification_failed": "la verificacion del final no cerro: no quedo nada",
    "published_pilot_scope_is_immutable":
        "la base no deja sumar la oferta al scope del piloto: falta la migracion 20261028000300",
}


class Refused(Exception):
    """El comando no sigue: el motivo se imprime y sale con ``code``."""

    def __init__(self, message: str, code: int = EXIT_REFUSED) -> None:
        super().__init__(message)
        self.code = code


class Docker:
    """``docker`` con los argumentos como lista, sin un shell en el medio."""

    def run(self, argv: Sequence[str], stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(["docker", *argv], input=stdin, text=True, capture_output=True, check=False)


class Terminal:
    """La terminal de quien corre el comando. Sin ``/dev/tty``, no hay confirmacion."""

    def ask(self, prompt: str) -> str:
        try:
            with open("/dev/tty", "w", encoding="utf-8") as out, open("/dev/tty", encoding="utf-8") as tty:
                out.write(prompt)
                out.flush()
                return tty.readline().strip()
        except OSError as error:
            raise Refused("sin terminal no se confirma: --aplicar lo corre una persona") from error


@dataclass
class Config:
    db_filter: str
    db_user: str
    db_name: str
    bridge_filter: str
    bridge_port: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        db_filter = env.get("CONTENEDOR_DB", "").strip()
        if not db_filter:
            raise Refused("falta la variable CONTENEDOR_DB", EXIT_USAGE)
        port = env.get("PUERTO_BRIDGE", "8000").strip() or "8000"
        if not port.isdigit():
            raise Refused("PUERTO_BRIDGE tiene que ser un numero", EXIT_USAGE)
        return cls(
            db_filter=db_filter,
            db_user=env.get("USUARIO_DB", "postgres").strip() or "postgres",
            db_name=env.get("BASE_DB", "postgres").strip() or "postgres",
            bridge_filter=env.get("CONTENEDOR_BRIDGE", "").strip(),
            bridge_port=int(port),
        )


def _copy_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def _args_block(args: Mapping[str, object]) -> str:
    lines = ["create temp table _args (k text primary key, v text);", "copy pg_temp._args (k, v) from stdin;"]
    for key, value in args.items():
        if not re.fullmatch(r"[a-z_]{1,32}", key):
            raise ValueError("arg_key_invalid")
        lines.append(f"{key}\t{chr(92) + 'N' if value is None else _copy_escape(str(value))}")
    lines.append("\\.")
    return "\n".join(lines) + "\n"


def _arg(key: str) -> str:
    return f"(select v from pg_temp._args where k = '{key}')"


# ---------------------------------------------------------------------------
# El SQL. La lectura va en una transaccion de solo lectura; lo que cambia, en
# SQL_APPLY, en una transaccion que Python cierra con commit (--aplicar) o
# rollback (en seco). El pedido llega como un JSON en _args.plan.
# ---------------------------------------------------------------------------

# Lo que la base tiene de cada binding pedido (la fila que lee el bridge), y de
# cada oferta: su scope de intencion, su fila del catalogo y los scopes del
# piloto del producto con su control.
SQL_READ = f"""select jsonb_build_object(
    'bindings', coalesce((select jsonb_agg(jsonb_build_object(
        'tenant_ref', p ->> 'tenant_ref', 'funnel_ref', p ->> 'funnel_ref',
        'binding_version', (p ->> 'binding_version')::integer,
        'row', (select to_jsonb(r) from public.resolve_commercial_ally_runtime_binding(
                    p ->> 'tenant_ref', p ->> 'funnel_ref', (p ->> 'binding_version')::integer) r limit 1),
        'status', (select b.status from public.commercial_ally_runtime_bindings b
                    where b.tenant_ref = p ->> 'tenant_ref' and b.funnel_ref = p ->> 'funnel_ref'
                      and b.binding_version = (p ->> 'binding_version')::integer),
        'offers', coalesce((select jsonb_agg(jsonb_build_object(
            'offer_code', o ->> 'offer_code',
            'intent_scopes', coalesce((select jsonb_agg(jsonb_build_object(
                    'tenant_ref', s.tenant_ref, 'funnel_ref', s.funnel_ref,
                    'purchase_intent_product_ref', s.purchase_intent_product_ref,
                    'max_lookback', s.max_lookback::text))
                from public.hotmart_purchase_intent_scopes s
                join public.commercial_ally_runtime_bindings b
                  on b.tenant_ref = p ->> 'tenant_ref' and b.funnel_ref = p ->> 'funnel_ref'
                 and b.binding_version = (p ->> 'binding_version')::integer
               where s.active and s.hotmart_product_id = b.hotmart_product_id::text
                 and s.offer_ref = o ->> 'offer_code'), '[]'::jsonb),
            'catalog', coalesce((select jsonb_agg(jsonb_build_object(
                    'landing_ref', k.landing_ref, 'default_for_inbound', k.default_for_inbound))
                from public.checkout_offer_catalog k
                join public.commercial_ally_runtime_bindings b
                  on b.tenant_ref = p ->> 'tenant_ref' and b.funnel_ref = p ->> 'funnel_ref'
                 and b.binding_version = (p ->> 'binding_version')::integer
               where k.tenant_ref = b.tenant_ref and k.scope_key = b.inbound_scope_key
                 and k.scope_version = b.inbound_scope_version and k.product_ref = b.product_hotlink
                 and k.offer_code = o ->> 'offer_code' and k.status = 'active'), '[]'::jsonb)))
          from jsonb_array_elements(p -> 'offers') o), '[]'::jsonb),
        'pilot_scopes', coalesce((select jsonb_agg(jsonb_build_object(
                'scope_key', s.scope_key, 'version', s.version, 'source', s.source,
                'offer_code', s.offer_code, 'additional_offer_codes', to_jsonb(s.additional_offer_codes),
                'runtime_state', c.runtime_state, 'generation', c.generation) order by s.scope_key)
            from public.pilot_runtime_controls c
            join public.pilot_scope_versions s on s.scope_key = c.scope_key and s.version = c.scope_version
            join public.commercial_ally_runtime_bindings b
              on b.tenant_ref = p ->> 'tenant_ref' and b.funnel_ref = p ->> 'funnel_ref'
             and b.binding_version = (p ->> 'binding_version')::integer
           where s.status = 'published' and c.runtime_state <> 'closed'
             and s.tenant_key = b.tenant_ref and s.external_product_id = b.hotmart_product_id::text),
          '[]'::jsonb)))
      from jsonb_array_elements({_arg('plan')}::jsonb) p), '[]'::jsonb))::text;"""

SQL_APPLY = f"""begin;
set local lock_timeout = '5s';
set local statement_timeout = '30s';
create temp table _salida (plan jsonb) on commit drop;
do $sumar$
declare
    c_plan     jsonb := {_arg('plan')}::jsonb;
    c_actor    text  := {_arg('actor')};
    c_approved text  := 'add-offers:' || {_arg('actor')};
    v_p        jsonb;
    v_o        jsonb;
    v_b        public.commercial_ally_runtime_bindings%rowtype;
    v_s        record;
    v_n        integer;
    v_lookback interval;
    v_default  public.checkout_offer_catalog%rowtype;
    v_add      text[];
    v_bindings jsonb := '[]'::jsonb;
    v_intents  jsonb := '[]'::jsonb;
    v_catalog  jsonb := '[]'::jsonb;
    v_pilot    jsonb := '[]'::jsonb;
    v_untouched jsonb := '[]'::jsonb;
    v_offers   text[];
begin
    for v_p in select * from jsonb_array_elements(c_plan) loop
        select b.* into v_b from public.commercial_ally_runtime_bindings b
         where b.tenant_ref = v_p ->> 'tenant_ref' and b.funnel_ref = v_p ->> 'funnel_ref'
           and b.binding_version = (v_p ->> 'binding_version')::integer and b.status = 'active'
         for update;
        if not found then
            raise exception using errcode = 'P0001', message = 'add_offer_binding_changed';
        end if;

        -- 1. El binding: la misma fila, con las listas del manifiesto nuevo. El where exige las de antes.
        if v_b.additional_offer_codes is distinct from
               array(select jsonb_array_elements_text(v_p -> 'new_codes')) then
            if v_b.additional_offer_codes is distinct from
                   array(select jsonb_array_elements_text(v_p -> 'old_codes'))
               or v_b.additional_offer_landings is distinct from (v_p -> 'old_landings') then
                raise exception using errcode = 'P0001', message = 'add_offer_binding_changed';
            end if;
            update public.commercial_ally_runtime_bindings
               set additional_offer_codes = array(select jsonb_array_elements_text(v_p -> 'new_codes')),
                   additional_offer_landings = v_p -> 'new_landings',
                   updated_at = now()
             where tenant_ref = v_b.tenant_ref and funnel_ref = v_b.funnel_ref
               and binding_version = v_b.binding_version and status = 'active';
            get diagnostics v_n = row_count;
            if v_n <> 1 then
                raise exception using errcode = 'P0001', message = 'add_offer_binding_changed';
            end if;
            v_bindings := v_bindings || jsonb_build_object('funnel_ref', v_b.funnel_ref,
                'binding_version', v_b.binding_version,
                'from_codes', to_jsonb(v_b.additional_offer_codes), 'to_codes', v_p -> 'new_codes');
        end if;

        -- 2. El scope de intencion de cada oferta, con el max_lookback de la oferta por defecto.
        select s.max_lookback into v_lookback from public.hotmart_purchase_intent_scopes s
         where s.active and s.hotmart_product_id = v_b.hotmart_product_id::text and s.offer_ref = v_b.offer_code
           and s.tenant_ref = v_b.tenant_ref and s.funnel_ref = v_b.funnel_ref
           and s.purchase_intent_product_ref = v_b.product_hotlink;
        if not found then
            raise exception using errcode = 'P0001', message = 'add_offer_default_intent_scope_missing';
        end if;
        for v_o in select * from jsonb_array_elements(v_p -> 'offers') loop
            select count(*) into v_n from public.hotmart_purchase_intent_scopes s
             where s.active and s.hotmart_product_id = v_b.hotmart_product_id::text
               and s.offer_ref = v_o ->> 'offer_code'
               and (s.tenant_ref <> v_b.tenant_ref or s.funnel_ref <> v_b.funnel_ref
                    or s.purchase_intent_product_ref <> v_b.product_hotlink);
            if v_n > 0 then
                raise exception using errcode = 'P0001', message = 'add_offer_intent_scope_taken';
            end if;
            if not exists (select 1 from public.hotmart_purchase_intent_scopes s
                            where s.active and s.hotmart_product_id = v_b.hotmart_product_id::text
                              and s.offer_ref = v_o ->> 'offer_code') then
                insert into public.hotmart_purchase_intent_scopes (
                    tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref,
                    offer_ref, max_lookback, active
                ) values (
                    v_b.tenant_ref, v_b.funnel_ref, v_b.hotmart_product_id::text, v_b.product_hotlink,
                    v_o ->> 'offer_code', v_lookback, true
                );
                v_intents := v_intents || jsonb_build_object('offer_code', v_o ->> 'offer_code',
                    'max_lookback', v_lookback::text);
            end if;
        end loop;

        -- 3. El catalogo del scope entrante del binding: solo filas nuevas, copiadas de la por defecto.
        select k.* into v_default from public.checkout_offer_catalog k
         where k.tenant_ref = v_b.tenant_ref and k.scope_key = v_b.inbound_scope_key
           and k.scope_version = v_b.inbound_scope_version and lower(k.product_ref) = lower(v_b.product_hotlink)
           and k.status = 'active' and k.default_for_inbound;
        if not found then
            raise exception using errcode = 'P0001', message = 'add_offer_catalog_default_missing';
        end if;
        for v_o in select * from jsonb_array_elements(v_p -> 'offers') loop
            if not exists (select 1 from public.checkout_offer_catalog k
                            where k.tenant_ref = v_b.tenant_ref and k.scope_key = v_b.inbound_scope_key
                              and k.scope_version = v_b.inbound_scope_version
                              and k.product_ref = v_default.product_ref
                              and k.offer_code = v_o ->> 'offer_code' and k.status = 'active') then
                insert into public.checkout_offer_catalog (
                    tenant_ref, funnel_ref, scope_key, scope_version, product_ref, landing_ref, offer_code,
                    checkout_base_url, checkout_mode, default_for_inbound, status, version,
                    approved_by, approved_at
                ) values (
                    v_default.tenant_ref, v_default.funnel_ref, v_default.scope_key, v_default.scope_version,
                    v_default.product_ref, v_o ->> 'landing_id', v_o ->> 'offer_code',
                    v_default.checkout_base_url, v_default.checkout_mode, false, 'active',
                    coalesce((select max(k.version) from public.checkout_offer_catalog k
                               where k.tenant_ref = v_default.tenant_ref and k.scope_key = v_default.scope_key
                                 and k.scope_version = v_default.scope_version
                                 and k.product_ref = v_default.product_ref
                                 and k.offer_code = v_o ->> 'offer_code'), 0) + 1,
                    c_approved, now()
                );
                v_catalog := v_catalog || jsonb_build_object('offer_code', v_o ->> 'offer_code',
                    'landing_ref', v_o ->> 'landing_id', 'scope_key', v_default.scope_key,
                    'scope_version', v_default.scope_version);
            end if;
        end loop;

        -- 4. Cada scope del piloto del producto al que apunta un control no cerrado: la oferta al final,
        -- en la misma version. El control no se toca (ni su estado ni su generacion).
        v_offers := array(select o ->> 'offer_code' from jsonb_array_elements(v_p -> 'offers') o);
        for v_s in
            select s.scope_key, s.version, s.offer_code, s.additional_offer_codes, c.runtime_state, c.generation
              from public.pilot_runtime_controls c
              join public.pilot_scope_versions s on s.scope_key = c.scope_key and s.version = c.scope_version
             where s.status = 'published' and c.runtime_state <> 'closed'
               and s.tenant_key = v_b.tenant_ref and s.external_product_id = v_b.hotmart_product_id::text
             order by s.scope_key
             for update of s
        loop
            v_add := array(select x from unnest(v_offers) with ordinality as t(x, n)
                            where x <> v_s.offer_code and x <> all(v_s.additional_offer_codes) order by n);
            if cardinality(v_add) = 0 then
                v_untouched := v_untouched || jsonb_build_object('scope_key', v_s.scope_key,
                    'version', v_s.version, 'runtime_state', v_s.runtime_state);
                continue;
            end if;
            perform set_config('pilot_scope.offer_append', v_s.scope_key || '/' || v_s.version, true);
            update public.pilot_scope_versions
               set additional_offer_codes = v_s.additional_offer_codes || v_add
             where scope_key = v_s.scope_key and version = v_s.version
               and additional_offer_codes = v_s.additional_offer_codes;
            get diagnostics v_n = row_count;
            perform set_config('pilot_scope.offer_append', '', true);
            if v_n <> 1 then
                raise exception using errcode = 'P0001', message = 'add_offer_pilot_scope_changed';
            end if;
            v_pilot := v_pilot || jsonb_build_object('scope_key', v_s.scope_key, 'version', v_s.version,
                'runtime_state', v_s.runtime_state, 'generation', v_s.generation,
                'from_codes', to_jsonb(v_s.additional_offer_codes), 'added', to_jsonb(v_add));
        end loop;

        -- 5. La verificacion, por presencia: la fila que lee el bridge con las listas nuevas, y cada oferta en
        -- su scope de intencion, en el catalogo y en cada scope del piloto del producto.
        if (select count(*) from public.resolve_commercial_ally_runtime_binding(
                v_b.tenant_ref, v_b.funnel_ref, v_b.binding_version) r
             where r.additional_offer_codes = array(select jsonb_array_elements_text(v_p -> 'new_codes'))
               and r.additional_offer_landings = v_p -> 'new_landings') <> 1
           or exists (
            select 1 from unnest(v_offers) x
             where not exists (select 1 from public.hotmart_purchase_intent_scopes s
                                where s.active and s.hotmart_product_id = v_b.hotmart_product_id::text
                                  and s.offer_ref = x and s.tenant_ref = v_b.tenant_ref
                                  and s.funnel_ref = v_b.funnel_ref)
                or not exists (select 1 from public.checkout_offer_catalog k
                                where k.tenant_ref = v_b.tenant_ref and k.scope_key = v_b.inbound_scope_key
                                  and k.scope_version = v_b.inbound_scope_version and k.offer_code = x
                                  and k.status = 'active')
                or exists (select 1 from public.pilot_runtime_controls c
                             join public.pilot_scope_versions s
                               on s.scope_key = c.scope_key and s.version = c.scope_version
                            where s.status = 'published' and c.runtime_state <> 'closed'
                              and s.tenant_key = v_b.tenant_ref
                              and s.external_product_id = v_b.hotmart_product_id::text
                              and x <> s.offer_code and x <> all(s.additional_offer_codes))) then
            raise exception using errcode = 'P0001', message = 'add_offer_verification_failed';
        end if;
    end loop;

    insert into pg_temp._salida values (jsonb_build_object(
        'bindings', v_bindings, 'intent_scopes', v_intents, 'catalog_rows', v_catalog,
        'pilot_scopes', v_pilot, 'pilot_scopes_already', v_untouched,
        'resolved', (select coalesce(jsonb_agg(jsonb_build_object(
                'funnel_ref', p ->> 'funnel_ref',
                'row', (select to_jsonb(r) from public.resolve_commercial_ally_runtime_binding(
                            p ->> 'tenant_ref', p ->> 'funnel_ref', (p ->> 'binding_version')::integer) r
                         limit 1))), '[]'::jsonb)
            from jsonb_array_elements(c_plan) p)));
end
$sumar$;
select plan::text from pg_temp._salida;
"""


@dataclass
class Ops:
    config: Config
    docker: Docker = field(default_factory=Docker)
    terminal: Terminal = field(default_factory=Terminal)
    out: TextIO = sys.stdout
    code: Callable[[], str] = lambda: secrets.token_hex(3)
    clock: Callable[[], float] = time.monotonic
    _containers: dict[str, str] = field(default_factory=dict)

    def say(self, line: str = "") -> None:
        self.out.write(line + "\n")

    def container(self, name_filter: str) -> str:
        if name_filter in self._containers:
            return self._containers[name_filter]
        listed = self.docker.run(["ps", "--filter", f"name={name_filter}", "--format", "{{.Names}}"])
        names = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
        if listed.returncode or len(names) != 1:
            raise Refused(f"el filtro {name_filter!r} tiene que dar exactamente un contenedor (dio {len(names)})")
        self._containers[name_filter] = names[0]
        return names[0]

    def _psql(self, script: str) -> Any:
        result = self.docker.run(
            ["exec", "-i", self.container(self.config.db_filter), "psql", "-X", "-q", "-A", "-t",
             "-v", "ON_ERROR_STOP=1", "-U", self.config.db_user, "-d", self.config.db_name],
            stdin=script,
        )
        if result.returncode:
            found = _PG_ERROR.search(result.stderr or "")
            code = found.group(1) if found else "error de psql"
            raise Refused(f"la base rechazo el pedido: {code}" + (f" ({REASONS[code]})" if code in REASONS else ""))
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            raise Refused("la base devolvio una respuesta inesperada")
        return json.loads(lines[0])

    def read(self, plan: list[dict[str, Any]]) -> dict[str, Any]:
        script = _args_block({"plan": json.dumps(plan)}) + "begin transaction read only;\n" + SQL_READ + "\ncommit;\n"
        return self._psql(script)

    def apply(self, plan: list[dict[str, Any]], actor: str, *, commit: bool) -> tuple[dict[str, Any], float]:
        script = _args_block({"plan": json.dumps(plan), "actor": actor})
        script += SQL_APPLY + ("commit;\n" if commit else "rollback;\n")
        started = self.clock()
        result = self._psql(script)
        return result, self.clock() - started

    def ready(self) -> tuple[int, dict[str, Any]]:
        """El ``/ready`` del bridge desde adentro de su contenedor: el codigo y las claves que importan."""
        probe = (
            "import json, urllib.request, urllib.error\n"
            f"url = 'http://127.0.0.1:{self.config.bridge_port}/ready'\n"
            "try:\n"
            "    r = urllib.request.urlopen(url, timeout=15); code, body = r.status, r.read()\n"
            "except urllib.error.HTTPError as e:\n"
            "    code, body = e.code, e.read()\n"
            "try:\n"
            "    data = json.loads(body)\n"
            "except ValueError:\n"
            "    data = {}\n"
            "keep = ('status', 'detail', 'commercial_ally_binding', 'commercial_ally_bindings',\n"
            "        'instance_ally', 'instance_product_version')\n"
            "print(json.dumps({'code': code, 'ready': {k: data.get(k) for k in keep if k in data}}))\n"
        )
        result = self.docker.run(["exec", "-i", self.container(self.config.bridge_filter), "python", "-"],
                                 stdin=probe)
        try:
            out = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError) as error:
            raise Refused("no se pudo leer el /ready del bridge") from error
        return int(out.get("code", 0)), dict(out.get("ready") or {})


# ---------------------------------------------------------------------------
# El pedido: del manifiesto nuevo y de lo que tiene la base.
# ---------------------------------------------------------------------------

def _landings_json(config: CommercialAllyConfig) -> list[dict[str, str]]:
    return [dataclasses.asdict(landing) for landing in config.additional_offer_landings]


def _without(config: CommercialAllyConfig, codes: Sequence[str]) -> CommercialAllyConfig:
    """El binding del manifiesto sin esas ofertas: lo que la base tiene que tener antes."""
    return dataclasses.replace(
        config,
        additional_offer_codes=tuple(c for c in config.additional_offer_codes if c not in codes),
        additional_offer_landings=tuple(
            landing for landing in config.additional_offer_landings if landing.offer_code not in codes),
    )


def _differences(a: CommercialAllyConfig, b: CommercialAllyConfig) -> list[str]:
    return [item.name for item in dataclasses.fields(a) if getattr(a, item.name) != getattr(b, item.name)]


def load_targets(manifest_path: Path, offers: Sequence[str]) -> list[tuple[CommercialAllyConfig, list[str]]]:
    """Cada binding del manifiesto que declara alguna de las ofertas, con las suyas."""
    try:
        manifest = InstanceManifest.from_toml_file(manifest_path)
    except ManifestError as error:
        raise Refused(f"el manifiesto no valida: {error}") from error
    configs = manifest.to_commercial_ally_configs()
    targets: list[tuple[CommercialAllyConfig, list[str]]] = []
    for config in configs:
        mine = [code for code in config.additional_offer_codes if code in offers]
        if mine:
            targets.append((config, mine))
    for code in offers:
        if any(code == config.offer_code for config in configs):
            raise Refused(f"{code} es la oferta por defecto de un binding: este comando solo suma ofertas adicionales")
        if not any(code in mine for _, mine in targets):
            raise Refused(f"la oferta {code} no esta en el manifiesto ({manifest_path}): se agrega ahi primero")
    return targets


def build_plan(targets: Sequence[tuple[CommercialAllyConfig, list[str]]]) -> list[dict[str, Any]]:
    plan = []
    for config, mine in targets:
        landing = {item.offer_code: item for item in config.additional_offer_landings}
        plan.append({
            "tenant_ref": config.tenant_ref, "funnel_ref": config.funnel_ref,
            "binding_version": config.binding_version,
            "new_codes": list(config.additional_offer_codes), "new_landings": _landings_json(config),
            "offers": [{"offer_code": code, "landing_id": landing[code].landing_id} for code in mine],
        })
    return plan


def check_current(targets: Sequence[tuple[CommercialAllyConfig, list[str]]], current: Mapping[str, Any],
                  plan: list[dict[str, Any]]) -> None:
    """Frena si la base no es el manifiesto nuevo sin (algunas de) las ofertas pedidas.

    Completa en ``plan`` las listas de antes, que el SQL exige en su ``where``.
    """
    by_funnel = {b["funnel_ref"]: b for b in current.get("bindings") or []}
    for (config, mine), item in zip(targets, plan, strict=True):
        found = by_funnel.get(config.funnel_ref) or {}
        if found.get("row") is None:
            raise Refused(f"el binding {config.funnel_ref} v{config.binding_version} no esta activo en la base "
                          f"(estado: {found.get('status')})")
        try:
            row = CommercialAllyConfig.from_binding_row(found["row"])
        except ValueError as error:
            raise Refused(f"la fila del binding {config.funnel_ref} no se lee como la lee el bridge") from error
        missing = [code for code in mine if code not in row.additional_offer_codes]
        before = _without(config, missing)
        if row != before:
            raise Refused(f"el binding {config.funnel_ref} de la base no es el manifiesto nuevo sin "
                          f"{', '.join(missing) or 'las ofertas'}: difiere en {', '.join(_differences(row, before))}. "
                          "Este comando solo suma ofertas al final; lo otro es otra cosa")
        item["old_codes"] = list(row.additional_offer_codes)
        item["old_landings"] = _landings_json(row)


def predicted_ready(targets: Sequence[tuple[CommercialAllyConfig, list[str]]],
                    resolved: Sequence[Mapping[str, Any]]) -> list[str]:
    """Lo que el bridge con el manifiesto nuevo veria en su ``/ready``: la lista de fallas."""
    rows = {item.get("funnel_ref"): item.get("row") for item in resolved}
    failures = []
    for config, _ in targets:
        row = rows.get(config.funnel_ref)
        if row is None:
            failures.append(f"el binding {config.funnel_ref} v{config.binding_version} no resuelve")
            continue
        try:
            got = CommercialAllyConfig.from_binding_row(row)
        except ValueError:
            failures.append(f"la fila del binding {config.funnel_ref} no se lee como la lee el bridge")
            continue
        if row.get("status") != "active":
            failures.append(f"el binding {config.funnel_ref} no esta active")
        elif got != config:
            failures.append(f"el binding {config.funnel_ref} difiere del manifiesto en "
                            f"{', '.join(_differences(got, config))}")
    return failures


# ---------------------------------------------------------------------------
# Los comandos.
# ---------------------------------------------------------------------------

def show_plan(ops: Ops, result: Mapping[str, Any], *, applied: bool, seconds: float) -> None:
    say = ops.say
    for b in result.get("bindings") or []:
        say(f"binding {b['funnel_ref']} v{b['binding_version']} (la misma fila, sigue active): "
            f"additional_offer_codes {b['from_codes']} -> {b['to_codes']}")
    for s in result.get("intent_scopes") or []:
        say(f"scope de intencion nuevo: oferta {s['offer_code']}, activo, max_lookback {s['max_lookback']}")
    for k in result.get("catalog_rows") or []:
        say(f"catalogo: fila nueva de {k['offer_code']} (landing {k['landing_ref']}) en "
            f"{k['scope_key']} v{k['scope_version']}, no por defecto")
    for s in result.get("pilot_scopes") or []:
        say(f"scope del piloto {s['scope_key']} v{s['version']} (la misma version): suma {s['added']} "
            f"a {s['from_codes']}; control {s['runtime_state']}, generacion {s['generation']}, sin tocar")
    for s in result.get("pilot_scopes_already") or []:
        say(f"scope del piloto {s['scope_key']} v{s['version']}: ya tenia las ofertas")
    if not any(result.get(k) for k in ("bindings", "intent_scopes", "catalog_rows", "pilot_scopes")):
        say("nada que hacer: la base ya tiene todas las ofertas pedidas")
    say("controles del piloto: ninguno se pausa, se activa ni se arma. Carrito frenado: 0 s.")
    say("")
    if applied:
        say(f"APLICADO: commit. La transaccion duro {seconds:.2f} s.")
        say("ahora: sincronizar el repo de la instancia (el manifiesto nuevo) y el redeploy del bridge, enseguida. "
            "Hasta el redeploy, el /ready del bridge viejo da 503 commercial_ally_binding_unavailable.")
    else:
        say(f"EN SECO: todo corrio y verifico, y se hizo rollback. No quedo nada. (La transaccion duro {seconds:.2f} s.)")


def cmd_publish(ops: Ops, args: argparse.Namespace, targets: list[tuple[CommercialAllyConfig, list[str]]]) -> int:
    plan = build_plan(targets)
    check_current(targets, ops.read(plan), plan)
    dry, seconds = ops.apply(plan, args.quien, commit=False)
    failures = predicted_ready(targets, dry.get("resolved") or [])
    if failures:
        show_plan(ops, dry, applied=False, seconds=seconds)
        raise Refused("con el manifiesto nuevo el bridge no daria commercial_ally_binding=active: " + "; ".join(failures))
    if not args.aplicar:
        show_plan(ops, dry, applied=False, seconds=seconds)
        ops.say("con el manifiesto nuevo, el bridge daria /ready 200 con commercial_ally_binding=active.")
        return EXIT_OK
    code = ops.code()
    answer = ops.terminal.ask(f"para sumar {', '.join(args.ofertas)}, escribi {code}: ")
    if answer != code:
        raise Refused("el codigo no coincide: no se escribio nada")
    applied, seconds = ops.apply(plan, args.quien, commit=True)
    show_plan(ops, applied, applied=True, seconds=seconds)
    return EXIT_OK


def cmd_verify(ops: Ops, args: argparse.Namespace, targets: list[tuple[CommercialAllyConfig, list[str]]]) -> int:
    plan = build_plan(targets)
    current = ops.read(plan)
    failures = predicted_ready(targets, current.get("bindings") or [])
    for b in current.get("bindings") or []:
        ops.say(f"binding {b['funnel_ref']} v{b['binding_version']}: {b.get('status')}")
        for offer in b.get("offers") or []:
            intents = [s for s in offer.get("intent_scopes") or []
                       if s.get("tenant_ref") == b["tenant_ref"] and s.get("funnel_ref") == b["funnel_ref"]]
            ops.say(f"  oferta {offer['offer_code']}: {len(intents)} scope de intencion activo, "
                    f"{len(offer.get('catalog') or [])} fila activa en el catalogo")
            if len(intents) != 1:
                failures.append(f"la oferta {offer['offer_code']} no tiene su scope de intencion activo")
            if not offer.get("catalog"):
                failures.append(f"la oferta {offer['offer_code']} no esta en el catalogo del scope entrante")
        for scope in b.get("pilot_scopes") or []:
            codes = [scope["offer_code"], *(scope.get("additional_offer_codes") or [])]
            ops.say(f"  scope del piloto {scope['scope_key']} v{scope['version']} ({scope['runtime_state']}, "
                    f"generacion {scope['generation']}): {codes}")
            for offer in b.get("offers") or []:
                if offer["offer_code"] not in codes:
                    failures.append(f"el scope del piloto {scope['scope_key']} v{scope['version']} no tiene "
                                    f"{offer['offer_code']}")
    if not failures:
        ops.say("prediccion: con el manifiesto nuevo, el bridge daria /ready 200 con commercial_ally_binding=active")
    if ops.config.bridge_filter:
        code, ready = ops.ready()
        ops.say(f"/ready del bridge que corre: {code} {json.dumps(ready, ensure_ascii=True, sort_keys=True)}")
        if code != 200 or ready.get("commercial_ally_binding") != "active":
            failures.append("el /ready del bridge que corre no da 200 con commercial_ally_binding=active "
                            "(falta sincronizar el manifiesto o el redeploy)")
    for failure in failures:
        ops.say(f"falta: {failure}")
    ops.say("VERIFICADO" if not failures else "NO VERIFICADO")
    return EXIT_OK if not failures else EXIT_REFUSED


def _offers(value: str) -> list[str]:
    codes = [code.strip() for code in value.split(",") if code.strip()]
    if not codes or any(not _OFFER.fullmatch(code) for code in codes) or len(set(codes)) != len(codes):
        raise argparse.ArgumentTypeError("--ofertas: codigos de oferta de Hotmart, separados por comas, sin repetir")
    return codes


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="add_instance_offers", description=__doc__.split("\n\n")[0])
    root.add_argument("--manifiesto", required=True, type=Path)
    root.add_argument("--ofertas", required=True, type=_offers)
    root.add_argument("--quien")
    mode = root.add_mutually_exclusive_group()
    mode.add_argument("--aplicar", action="store_true")
    mode.add_argument("--verificar", action="store_true")
    return root


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None,
         ops: Ops | None = None) -> int:
    args = parser().parse_args(argv)
    out = ops.out if ops is not None else sys.stdout
    try:
        if not args.verificar:
            if not args.quien or not _NAME.fullmatch(args.quien):
                raise Refused("--quien es obligatorio: un nombre corto ([a-z0-9_.-])", EXIT_USAGE)
        targets = load_targets(args.manifiesto, args.ofertas)
        if ops is None:
            ops = Ops(Config.from_env(os.environ if env is None else env))
        return cmd_verify(ops, args, targets) if args.verificar else cmd_publish(ops, args, targets)
    except Refused as refused:
        out.write(f"no: {refused}\n")
        return refused.code


if __name__ == "__main__":
    raise SystemExit(main())
