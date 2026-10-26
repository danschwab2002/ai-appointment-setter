#!/usr/bin/env python3
"""Pasa una instancia de un inbox de Chatwoot a otro publicando una version nueva del binding.

El caso que lo motiva: una aliada instalada con la bandeja provisoria de Chatwoot
(``Channel::Api``, sin numero) que tiene que pasar al inbox de WhatsApp Cloud del
numero cuando llega. El binding publicado no se edita (docs/instalar.md, paso 8),
asi que el cambio es una version nueva (``binding_version = n + 1``) con el
``chatwoot_inbox_id`` nuevo. Y el inbox no esta solo en el binding: esta tambien
en el scope entrante, en los scopes del piloto y en lo que cuelga de ellos. Este
comando publica todo eso junto, en una transaccion:

* el binding ``n + 1`` de cada producto (``funnel_ref``) activo del tenant, igual
  campo por campo salvo el inbox y la version del scope entrante; el ``n`` queda
  ``retired`` (la base admite un solo ``active`` por tenant y funnel);
* la politica de compra de Hotmart de cada binding, copiada a la version nueva;
* una version nueva de cada scope entrante que nombran esos bindings, con el
  inbox nuevo, sus filas activas de ``checkout_offer_catalog`` y una version
  nueva de cada politica de derivacion activa que lo nombra;
* una version nueva de cada scope del piloto cuyo control apunta a una version
  con el inbox viejo, con el inbox y el ``channel_account_ref`` nuevos, y el
  control apuntado a ella con ``activate_lancemos_pilot_scope_version`` (que
  exige el control en ``inactive`` o ``paused``, lo deja ``inactive`` y no lo
  arma: se arma despues con ``piloto.sh armar``); y una version
  nueva de cada politica de derivacion activa que nombra al scope viejo.

Las filas viejas no se tocan (son inmutables una vez publicadas) salvo el
binding ``n``, que pasa a ``retired``. Volver atras es correr el mismo comando
con el inbox viejo: publica ``n + 2`` apuntando al de antes.

Frena sin escribir nada si el inbox nuevo no existe en la cuenta de Chatwoot del
binding o no es de WhatsApp, si es el mismo que el actual, si los bindings
activos del tenant no comparten un solo inbox, si algun control del piloto con
el inbox viejo esta ``armed`` (casos en vuelo: se pausa antes), o si hay
politicas de descuento publicadas para el binding vigente (no se copian solas).

Uso, en el VPS como root, desde el arbol del producto de la version que corre::

    python3 scripts/publish_inbox_binding_version.py --tenant <t> --inbox-nuevo <N> --quien <q> --motivo "..."
    python3 scripts/publish_inbox_binding_version.py ... --aplicar [--acepto-eventos-de-la-version-vieja]
    python3 scripts/publish_inbox_binding_version.py --tenant <t> --inbox-nuevo <N> --verificar

* Sin ``--aplicar`` (en seco): corre la transaccion entera, verifica y hace
  ``rollback``. Muestra el plan: que version pasa a cual, que depende del inbox,
  las variables del bridge y los campos del manifiesto que hay que cambiar.
  El inbox nuevo tiene que ser de WhatsApp (``Channel::Whatsapp``) y de la
  misma cuenta; uno que no lo es solo se acepta si ya tuvo un binding de este
  tenant (volver a la bandeja provisoria).
* ``--aplicar``: lo mismo con ``commit``, despues de un codigo que se lee de la
  terminal (lo corre una persona). Si hay eventos o correlaciones de Hotmart
  guardados con la version vieja del binding, exige
  ``--acepto-eventos-de-la-version-vieja``: al retirarla, la planificacion de
  esos eventos deja de resolver su binding (docs/instalar.md, «Por que se
  completa la fila»).
* ``--verificar``: despues del cambio del manifiesto, de las variables y del
  redeploy, mira por presencia que el binding activo tenga el inbox nuevo y que
  el ``/ready`` del bridge responda 200 con ``commercial_ally_binding=active``
  (que el bridge solo da si la fila es igual a su manifiesto, inbox incluido).

**No toca las variables del bridge ni el manifiesto, ni reinicia nada.** Entre
``--aplicar`` y el redeploy el bridge viejo no resuelve su binding: ``/ready`` da
503 ``commercial_ally_binding_unavailable``. Por eso se aplica con el manifiesto
y las variables ya preparados, y el redeploy va enseguida.

Variables (como ``flow_change_operator.py``):

* ``CONTENEDOR_DB``: filtro de ``docker ps --filter name=`` de la base de la
  instancia; tiene que dar exactamente un contenedor. ``USUARIO_DB`` y
  ``BASE_DB`` (``postgres``): el duenio de las tablas.
* ``CONTENEDOR_CHATWOOT_DB``, ``BASE_CHATWOOT_DB`` y ``USUARIO_CHATWOOT_DB``
  (``postgres``): la base de Chatwoot, leida en una sesion de solo lectura.
* ``CONTENEDOR_BRIDGE`` (solo ``--verificar``) y ``PUERTO_BRIDGE`` (``8000``).

El SQL va por la entrada estandar de ``psql -X``; los valores, en una tabla
temporal cargada con ``COPY ... FROM STDIN``, nunca pegados en el texto. No
imprime telefonos, tokens ni secretos: del canal de WhatsApp dice solo si tiene
numero cargado.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TextIO

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2

_PG_ERROR = re.compile(r"ERROR:\s+([a-z][a-z0-9_]{1,79})\b")
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_TENANT = re.compile(r"^[a-z0-9_-]{1,100}$")

# Lo que dice cada freno de la base, en castellano.
REASONS = {
    "inbox_binding_no_active_binding": "el tenant no tiene ningun binding activo en esta base",
    "inbox_binding_mixed_inboxes": "los bindings activos del tenant no comparten un solo inbox",
    "inbox_binding_same_inbox": "el inbox nuevo es el que ya tiene el binding activo",
    "inbox_binding_account_mismatch": "los bindings activos no son todos de la misma cuenta de Chatwoot",
    "inbox_binding_discount_policies_unsupported":
        "hay politicas de descuento publicadas para el binding vigente: este comando no las copia",
    "inbox_binding_control_armed":
        "un control del piloto con el inbox viejo esta armed: hay que pausarlo antes (piloto.sh pausar)",
    "inbox_binding_inbound_scope_mismatch":
        "el scope entrante que nombra el binding no esta publicado con el inbox viejo",
    "inbox_binding_pilot_activation_refused": "la base no dejo apuntar un control a su version nueva",
    "inbox_binding_verification_failed": "la verificacion del final no cerro: no quedo nada",
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
    chatwoot_filter: str
    chatwoot_user: str
    chatwoot_db: str
    bridge_filter: str
    bridge_port: int

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, needs_bridge: bool) -> Config:
        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise Refused(f"falta la variable {name}", EXIT_USAGE)
            return value

        port = env.get("PUERTO_BRIDGE", "8000").strip() or "8000"
        if not port.isdigit():
            raise Refused("PUERTO_BRIDGE tiene que ser un numero", EXIT_USAGE)
        return cls(
            db_filter=required("CONTENEDOR_DB"),
            db_user=env.get("USUARIO_DB", "postgres").strip() or "postgres",
            db_name=env.get("BASE_DB", "postgres").strip() or "postgres",
            chatwoot_filter=required("CONTENEDOR_CHATWOOT_DB"),
            chatwoot_user=env.get("USUARIO_CHATWOOT_DB", "postgres").strip() or "postgres",
            chatwoot_db=required("BASE_CHATWOOT_DB"),
            bridge_filter=required("CONTENEDOR_BRIDGE") if needs_bridge else env.get("CONTENEDOR_BRIDGE", ""),
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
# El SQL. Todo lo que cambia va en SQL_PUBLISH, en una transaccion que Python
# cierra con commit (--aplicar) o rollback (en seco).
# ---------------------------------------------------------------------------

SQL_CHATWOOT_INBOX = f"""select coalesce((select jsonb_build_object(
    'id', i.id, 'account_id', i.account_id, 'channel_type', i.channel_type,
    'enable_auto_assignment', i.enable_auto_assignment,
    'provider', c.provider,
    'phone_number_present', coalesce(nullif(btrim(c.phone_number), ''), null) is not null,
    'templates_approved', (select count(*) from jsonb_array_elements(
        case when jsonb_typeof(c.message_templates::jsonb) = 'array' then c.message_templates::jsonb
             else '[]'::jsonb end) t where t ->> 'status' = 'APPROVED'),
    'template_names', (select coalesce(jsonb_agg(distinct t ->> 'name'), '[]'::jsonb) from jsonb_array_elements(
        case when jsonb_typeof(c.message_templates::jsonb) = 'array' then c.message_templates::jsonb
             else '[]'::jsonb end) t where t ->> 'status' = 'APPROVED'))
  from public.inboxes i left join public.channel_whatsapp c
    on i.channel_type = 'Channel::Whatsapp' and c.id = i.channel_id
 where i.id = {_arg('inbox')}::bigint), 'null'::jsonb)::text;"""

SQL_CURRENT = f"""select jsonb_build_object(
    'bindings', coalesce((select jsonb_agg(jsonb_build_object(
        'funnel_ref', b.funnel_ref, 'binding_version', b.binding_version,
        'chatwoot_account_id', b.chatwoot_account_id, 'chatwoot_inbox_id', b.chatwoot_inbox_id,
        'inbound_scope_key', b.inbound_scope_key, 'inbound_scope_version', b.inbound_scope_version)
        order by b.funnel_ref)
      from public.commercial_ally_runtime_bindings b
     where b.tenant_ref = {_arg('tenant')} and b.status = 'active'), '[]'::jsonb),
    'previous_inboxes', coalesce((select jsonb_agg(distinct b.chatwoot_inbox_id)
      from public.commercial_ally_runtime_bindings b
     where b.tenant_ref = {_arg('tenant')} and b.status = 'retired'), '[]'::jsonb),
    'controls', coalesce((select jsonb_agg(jsonb_build_object(
        'scope_key', c.scope_key, 'scope_version', c.scope_version, 'runtime_state', c.runtime_state,
        'chatwoot_inbox_id', s.chatwoot_inbox_id, 'channel_account_ref', s.channel_account_ref)
        order by c.scope_key)
      from public.pilot_runtime_controls c
      join public.pilot_scope_versions s on s.scope_key = c.scope_key and s.version = c.scope_version
     where s.tenant_key = {_arg('tenant')}), '[]'::jsonb))::text;"""

SQL_PUBLISH = f"""begin;
lock table public.commercial_ally_runtime_bindings in share row exclusive mode;
create temp table _salida (plan jsonb) on commit drop;
do $publicar$
declare
    c_tenant   text   := {_arg('tenant')};
    c_new      bigint := {_arg('inbox')}::bigint;
    c_actor    text   := {_arg('actor')};
    c_reason   text   := {_arg('reason')};
    c_approved text   := 'inbox-binding:' || {_arg('actor')};
    c_new_ref  text   := 'chatwoot-inbox:' || {_arg('inbox')};
    v_old      bigint;
    v_old_ref  text;
    v_n        integer;
    v_accounts integer;
    v_version  integer;
    v_row      record;
    v_res      record;
    v_inbound_map jsonb := '{{}}'::jsonb;   -- "key/version vieja" -> version nueva
    v_pilot_map   jsonb := '{{}}'::jsonb;
    v_bindings jsonb := '[]'::jsonb;
    v_inbound  jsonb := '[]'::jsonb;
    v_pilot    jsonb := '[]'::jsonb;
    v_handoff  jsonb := '[]'::jsonb;
    v_purchase jsonb := '[]'::jsonb;
    v_catalog  integer := 0;
    v_old_versions jsonb := '[]'::jsonb;
begin
    select count(*), count(distinct chatwoot_inbox_id), count(distinct chatwoot_account_id), min(chatwoot_inbox_id)
      into v_n, v_version, v_accounts, v_old
      from public.commercial_ally_runtime_bindings
     where tenant_ref = c_tenant and status = 'active';
    if v_n = 0 then
        raise exception using errcode = 'P0001', message = 'inbox_binding_no_active_binding';
    end if;
    if v_version <> 1 then
        raise exception using errcode = 'P0001', message = 'inbox_binding_mixed_inboxes';
    end if;
    if v_accounts <> 1 then
        raise exception using errcode = 'P0001', message = 'inbox_binding_account_mismatch';
    end if;
    if v_old = c_new then
        raise exception using errcode = 'P0001', message = 'inbox_binding_same_inbox';
    end if;
    v_old_ref := 'chatwoot-inbox:' || v_old;

    if exists (
        select 1 from public.commercial_ally_discount_policy_versions d
          join public.commercial_ally_runtime_bindings b
            on b.tenant_ref = d.tenant_ref and b.funnel_ref = d.funnel_ref
           and b.binding_version = d.binding_version and b.status = 'active'
         where d.tenant_ref = c_tenant and d.status = 'published'
    ) then
        raise exception using errcode = 'P0001', message = 'inbox_binding_discount_policies_unsupported';
    end if;
    if exists (
        select 1 from public.pilot_runtime_controls c
          join public.pilot_scope_versions s on s.scope_key = c.scope_key and s.version = c.scope_version
         where s.tenant_key = c_tenant and s.chatwoot_inbox_id = v_old and c.runtime_state = 'armed'
    ) then
        raise exception using errcode = 'P0001', message = 'inbox_binding_control_armed';
    end if;

    -- 1. Los scopes entrantes que nombran los bindings activos, sus catalogos y sus derivaciones.
    for v_row in
        select distinct s.*
          from public.commercial_ally_runtime_bindings b
          join public.inbound_commercial_scope_versions s
            on s.scope_key = b.inbound_scope_key and s.version = b.inbound_scope_version
         where b.tenant_ref = c_tenant and b.status = 'active'
    loop
        if v_row.status <> 'published' or v_row.chatwoot_inbox_id <> v_old then
            raise exception using errcode = 'P0001', message = 'inbox_binding_inbound_scope_mismatch';
        end if;
        select max(version) + 1 into v_version
          from public.inbound_commercial_scope_versions where scope_key = v_row.scope_key;
        insert into public.inbound_commercial_scope_versions
        select (jsonb_populate_record(null::public.inbound_commercial_scope_versions,
                to_jsonb(v_row) || jsonb_build_object(
                    'version', v_version, 'chatwoot_inbox_id', c_new, 'status', 'published',
                    'approved_by', c_approved, 'approved_at', now(), 'published_at', now(),
                    'created_at', now()))).*;
        v_inbound_map := v_inbound_map || jsonb_build_object(v_row.scope_key || '/' || v_row.version, v_version);
        v_inbound := v_inbound || jsonb_build_object('scope_key', v_row.scope_key,
            'from_version', v_row.version, 'to_version', v_version);

        with copied as (
            insert into public.checkout_offer_catalog
            select (jsonb_populate_record(null::public.checkout_offer_catalog,
                    to_jsonb(k) || jsonb_build_object(
                        'id', gen_random_uuid(), 'scope_version', v_version,
                        'approved_by', c_approved, 'approved_at', now(), 'created_at', now()))).*
              from public.checkout_offer_catalog k
             where k.tenant_ref = c_tenant and k.scope_key = v_row.scope_key
               and k.scope_version = v_row.version and k.status = 'active'
            returning 1
        )
        select v_catalog + count(*) into v_catalog from copied;
    end loop;

    for v_row in
        select p.* from public.human_handoff_projection_policies p
         where p.active and p.inbound_scope_key is not null
           and v_inbound_map ? (p.inbound_scope_key || '/' || p.inbound_scope_version)
    loop
        select max(policy_version) + 1 into v_version
          from public.human_handoff_projection_policies where policy_key = v_row.policy_key;
        insert into public.human_handoff_projection_policies
        select (jsonb_populate_record(null::public.human_handoff_projection_policies,
                to_jsonb(v_row) || jsonb_build_object(
                    'id', gen_random_uuid(), 'policy_version', v_version,
                    'inbound_scope_version', (v_inbound_map ->> (v_row.inbound_scope_key || '/' || v_row.inbound_scope_version))::integer,
                    'created_at', now()))).*;
        v_handoff := v_handoff || jsonb_build_object('policy_key', v_row.policy_key,
            'from_version', v_row.policy_version, 'to_version', v_version, 'scope_key', v_row.inbound_scope_key);
    end loop;

    -- 2. Los bindings: la version nueva entra approved, la vieja se retira y la nueva se activa.
    for v_row in
        select b.* from public.commercial_ally_runtime_bindings b
         where b.tenant_ref = c_tenant and b.status = 'active'
         order by b.funnel_ref
    loop
        select max(binding_version) + 1 into v_version
          from public.commercial_ally_runtime_bindings
         where tenant_ref = v_row.tenant_ref and funnel_ref = v_row.funnel_ref;
        insert into public.commercial_ally_runtime_bindings
        select (jsonb_populate_record(null::public.commercial_ally_runtime_bindings,
                to_jsonb(v_row) || jsonb_build_object(
                    'binding_version', v_version, 'status', 'approved', 'chatwoot_inbox_id', c_new,
                    'inbound_scope_version', (v_inbound_map ->> (v_row.inbound_scope_key || '/' || v_row.inbound_scope_version))::integer,
                    'created_at', now(), 'updated_at', now()))).*;
        update public.commercial_ally_runtime_bindings
           set status = 'retired', updated_at = now()
         where tenant_ref = v_row.tenant_ref and funnel_ref = v_row.funnel_ref
           and binding_version = v_row.binding_version;
        update public.commercial_ally_runtime_bindings
           set status = 'active', updated_at = now()
         where tenant_ref = v_row.tenant_ref and funnel_ref = v_row.funnel_ref
           and binding_version = v_version;
        v_bindings := v_bindings || jsonb_build_object('funnel_ref', v_row.funnel_ref,
            'from_version', v_row.binding_version, 'to_version', v_version,
            'from_inbox', v_row.chatwoot_inbox_id, 'to_inbox', c_new,
            'inbound_scope_key', v_row.inbound_scope_key);
        v_old_versions := v_old_versions || jsonb_build_object('funnel_ref', v_row.funnel_ref,
            'binding_version', v_row.binding_version);

        insert into public.commercial_ally_hotmart_purchase_policies
        select (jsonb_populate_record(null::public.commercial_ally_hotmart_purchase_policies,
                to_jsonb(p) || jsonb_build_object('binding_version', v_version,
                    'created_at', now(), 'updated_at', now()))).*
          from public.commercial_ally_hotmart_purchase_policies p
         where p.tenant_ref = v_row.tenant_ref and p.funnel_ref = v_row.funnel_ref
           and p.binding_version = v_row.binding_version;
        if found then
            v_purchase := v_purchase || jsonb_build_object('funnel_ref', v_row.funnel_ref,
                'from_version', v_row.binding_version, 'to_version', v_version);
        end if;
    end loop;

    -- 3. Los scopes del piloto cuyo control apunta a una version con el inbox viejo.
    for v_row in
        select s.*, c.generation as control_generation, c.runtime_state as control_state
          from public.pilot_runtime_controls c
          join public.pilot_scope_versions s on s.scope_key = c.scope_key and s.version = c.scope_version
         where s.tenant_key = c_tenant and s.chatwoot_inbox_id = v_old
         order by s.scope_key
    loop
        select max(version) + 1 into v_version from public.pilot_scope_versions where scope_key = v_row.scope_key;
        insert into public.pilot_scope_versions
        select (jsonb_populate_record(null::public.pilot_scope_versions,
                (to_jsonb(v_row) - 'control_generation' - 'control_state') || jsonb_build_object(
                    'version', v_version, 'status', 'published', 'chatwoot_inbox_id', c_new,
                    'channel_account_ref', c_new_ref, 'approved_by', c_approved,
                    'approved_at', now(), 'published_at', now(), 'created_at', now(),
                    'updated_at', now()))).*;
        select * into v_res from public.activate_lancemos_pilot_scope_version(
            v_row.scope_key, v_version, v_row.control_generation, c_actor, c_reason);
        if v_res.scope_version is distinct from v_version or not coalesce(v_res.changed, false)
           or v_res.runtime_state is distinct from 'inactive' then
            raise exception using errcode = 'P0001', message = 'inbox_binding_pilot_activation_refused';
        end if;
        v_pilot_map := v_pilot_map || jsonb_build_object(v_row.scope_key || '/' || v_row.version, v_version);
        v_pilot := v_pilot || jsonb_build_object('scope_key', v_row.scope_key, 'from_version', v_row.version,
            'to_version', v_version, 'from_state', v_row.control_state, 'runtime_state', v_res.runtime_state,
            'from_ref', v_row.channel_account_ref, 'to_ref', c_new_ref);
    end loop;

    for v_row in
        select p.* from public.human_handoff_projection_policies p
         where p.active and p.scope_key is not null
           and v_pilot_map ? (p.scope_key || '/' || p.scope_version)
    loop
        select max(policy_version) + 1 into v_version
          from public.human_handoff_projection_policies where policy_key = v_row.policy_key;
        insert into public.human_handoff_projection_policies
        select (jsonb_populate_record(null::public.human_handoff_projection_policies,
                to_jsonb(v_row) || jsonb_build_object(
                    'id', gen_random_uuid(), 'policy_version', v_version,
                    'scope_version', (v_pilot_map ->> (v_row.scope_key || '/' || v_row.scope_version))::integer,
                    'created_at', now()))).*;
        v_handoff := v_handoff || jsonb_build_object('policy_key', v_row.policy_key,
            'from_version', v_row.policy_version, 'to_version', v_version, 'scope_key', v_row.scope_key);
    end loop;

    -- 4. La verificacion: por presencia, cada binding activo con el inbox nuevo y resoluble por la RPC
    -- que lee el bridge, su scope entrante publicado con el inbox nuevo, y cada control movido en una
    -- version con el inbox y la referencia nuevos.
    if exists (
        select 1 from public.commercial_ally_runtime_bindings b
         where b.tenant_ref = c_tenant and b.status = 'active'
           and (b.chatwoot_inbox_id <> c_new
                or (select count(*) from public.resolve_commercial_ally_runtime_binding(
                        b.tenant_ref, b.funnel_ref, b.binding_version)) <> 1
                or not exists (select 1 from public.inbound_commercial_scope_versions s
                                where s.scope_key = b.inbound_scope_key and s.version = b.inbound_scope_version
                                  and s.status = 'published' and s.chatwoot_inbox_id = c_new))
    ) or (select count(*) from public.commercial_ally_runtime_bindings b
           where b.tenant_ref = c_tenant and b.status = 'active') <> jsonb_array_length(v_bindings)
      or exists (
        select 1 from jsonb_array_elements(v_pilot) m
          join public.pilot_runtime_controls c on c.scope_key = m ->> 'scope_key'
          join public.pilot_scope_versions s on s.scope_key = c.scope_key and s.version = c.scope_version
         where c.scope_version <> (m ->> 'to_version')::integer
            or s.chatwoot_inbox_id <> c_new or s.channel_account_ref <> c_new_ref
    ) then
        raise exception using errcode = 'P0001', message = 'inbox_binding_verification_failed';
    end if;

    insert into pg_temp._salida values (jsonb_build_object(
        'tenant_ref', c_tenant, 'from_inbox', v_old, 'to_inbox', c_new,
        'from_ref', v_old_ref, 'to_ref', c_new_ref,
        'bindings', v_bindings, 'purchase_policies', v_purchase, 'inbound_scopes', v_inbound,
        'catalog_rows', v_catalog, 'handoff_policies', v_handoff, 'pilot_scopes', v_pilot,
        'pilot_scopes_untouched', (select coalesce(jsonb_agg(jsonb_build_object(
                'scope_key', c.scope_key, 'scope_version', c.scope_version,
                'chatwoot_inbox_id', s.chatwoot_inbox_id) order by c.scope_key), '[]'::jsonb)
            from public.pilot_runtime_controls c
            join public.pilot_scope_versions s on s.scope_key = c.scope_key and s.version = c.scope_version
           where s.tenant_key = c_tenant and not (v_pilot_map ? (s.scope_key || '/' || s.version))
             and s.chatwoot_inbox_id <> c_new),
        'old_version_rows', jsonb_build_object(
            'hotmart_event_bindings', (select count(*) from public.commercial_ally_hotmart_event_bindings e
                join jsonb_array_elements(v_old_versions) o
                  on e.funnel_ref = o ->> 'funnel_ref' and e.binding_version = (o ->> 'binding_version')::integer
               where e.tenant_ref = c_tenant),
            'hotmart_purchase_correlations', (select count(*) from public.portable_hotmart_purchase_correlations e
                join jsonb_array_elements(v_old_versions) o
                  on e.funnel_ref = o ->> 'funnel_ref' and e.binding_version = (o ->> 'binding_version')::integer
               where e.tenant_ref = c_tenant),
            'post_inbound_discount_bindings', (select count(*) from public.commercial_ally_post_inbound_discount_bindings e
                join jsonb_array_elements(v_old_versions) o
                  on e.funnel_ref = o ->> 'funnel_ref' and e.binding_version = (o ->> 'binding_version')::integer
               where e.tenant_ref = c_tenant)),
        'old_inbox_rows', jsonb_build_object(
            'channel_identities', (select count(*) from public.channel_identities i
               where i.metadata ->> 'inbox_id' = v_old::text),
            'commercial_cases', (select count(*) from public.commercial_cases k
               where k.inbound_scope_key is not null
                 and v_inbound_map ? (k.inbound_scope_key || '/' || k.inbound_scope_version)),
            'daily_feedback_schedules', (select count(*) from public.daily_feedback_schedules d
               where d.chatwoot_inbox_id = v_old and d.enabled))
    ));
end
$publicar$;
select plan::text from pg_temp._salida;
"""


@dataclass
class Ops:
    config: Config
    docker: Docker = field(default_factory=Docker)
    terminal: Terminal = field(default_factory=Terminal)
    out: TextIO = sys.stdout
    code: Callable[[], str] = lambda: secrets.token_hex(3)
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

    def _psql(self, container: str, user: str, database: str, script: str) -> list[str]:
        result = self.docker.run(
            ["exec", "-i", container, "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
             "-U", user, "-d", database],
            stdin=script,
        )
        if result.returncode:
            found = _PG_ERROR.search(result.stderr or "")
            code = found.group(1) if found else "error de psql"
            raise Refused(f"la base rechazo el pedido: {code}" + (f" ({REASONS[code]})" if code in REASONS else ""))
        return [line for line in result.stdout.splitlines() if line.strip()]

    def _one_json(self, lines: list[str]) -> Any:
        if len(lines) != 1:
            raise Refused("la base devolvio una respuesta inesperada")
        return json.loads(lines[0])

    def read_inbox(self, inbox: int) -> dict[str, Any] | None:
        script = _args_block({"inbox": inbox}) + "begin transaction read only;\n" + SQL_CHATWOOT_INBOX + "\ncommit;\n"
        found = self._one_json(self._psql(self.container(self.config.chatwoot_filter), self.config.chatwoot_user,
                                          self.config.chatwoot_db, script))
        return found if isinstance(found, dict) else None

    def read_current(self, tenant: str) -> dict[str, Any]:
        script = _args_block({"tenant": tenant}) + "begin transaction read only;\n" + SQL_CURRENT + "\ncommit;\n"
        return self._one_json(self._psql(self.container(self.config.db_filter), self.config.db_user,
                                         self.config.db_name, script))

    def publish(self, tenant: str, inbox: int, actor: str, reason: str, *, apply: bool) -> dict[str, Any]:
        script = _args_block({"tenant": tenant, "inbox": inbox, "actor": actor, "reason": reason})
        script += SQL_PUBLISH + ("commit;\n" if apply else "rollback;\n")
        return self._one_json(self._psql(self.container(self.config.db_filter), self.config.db_user,
                                         self.config.db_name, script))

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
            "keep = ('status', 'detail', 'reason_code', 'commercial_ally_binding', 'commercial_ally_bindings',\n"
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
# Lo que se valida antes de escribir y lo que se le dice a quien lo corre.
# ---------------------------------------------------------------------------

def check_inbox(found: dict[str, Any] | None, inbox: int, current: dict[str, Any]) -> list[str]:
    """Frena si el inbox nuevo no sirve; devuelve los avisos que no frenan."""
    bindings = current.get("bindings") or []
    if not bindings:
        raise Refused(REASONS["inbox_binding_no_active_binding"])
    accounts = {b.get("chatwoot_account_id") for b in bindings}
    inboxes = {b.get("chatwoot_inbox_id") for b in bindings}
    if len(accounts) != 1:
        raise Refused(REASONS["inbox_binding_account_mismatch"])
    if len(inboxes) != 1:
        raise Refused(REASONS["inbox_binding_mixed_inboxes"])
    if inbox in inboxes:
        raise Refused(REASONS["inbox_binding_same_inbox"])
    if found is None:
        raise Refused(f"el inbox {inbox} no existe en Chatwoot")
    account = next(iter(accounts))
    if found.get("account_id") != account:
        raise Refused(f"el inbox {inbox} es de la cuenta {found.get('account_id')} de Chatwoot, "
                      f"y el binding es de la cuenta {account}")
    warnings: list[str] = []
    if found.get("channel_type") != "Channel::Whatsapp":
        # Volver atras a la bandeja de antes (la provisoria, Channel::Api) se permite: es un inbox que
        # ya tuvo un binding de este tenant. Uno nuevo tiene que ser de WhatsApp.
        if inbox not in (current.get("previous_inboxes") or []):
            raise Refused(f"el inbox {inbox} es {found.get('channel_type')}, no de WhatsApp (Channel::Whatsapp)")
        warnings.append(f"el inbox {inbox} es {found.get('channel_type')}: se vuelve a una bandeja que ya "
                        "tuvo el binding, sin numero de WhatsApp")
        return warnings
    if found.get("provider") != "whatsapp_cloud":
        warnings.append(f"el canal del inbox es {found.get('provider')!r}, no whatsapp_cloud")
    if not found.get("phone_number_present"):
        warnings.append("el canal de WhatsApp del inbox no tiene numero cargado")
    if found.get("enable_auto_assignment"):
        warnings.append("la asignacion automatica del inbox esta prendida: con ella el bridge no contesta "
                        "(guia, paso 8.1.4); apagarla en el panel antes del entrante")
    if not found.get("templates_approved"):
        warnings.append("el inbox no tiene plantillas aprobadas: los flujos de salida no van a tener que mandar")
    return warnings


def env_changes(plan: Mapping[str, Any]) -> list[str]:
    lines = [f"CHATWOOT_INBOX_ID={plan['to_inbox']}",
             f"LANCEMOS_PILOT_CHANNEL_ACCOUNT_REF={plan['to_ref']}"]
    for scope in plan.get("inbound_scopes") or []:
        lines.append(f"CHATWOOT_CUT_B_SCOPE_VERSION={scope['to_version']}  (scope entrante {scope['scope_key']})")
    for policy in plan.get("handoff_policies") or []:
        lines.append(f"HANDOFF_PROJECTION_POLICY_VERSION={policy['to_version']}  (si HANDOFF_PROJECTION_POLICY_KEY="
                     f"{policy['policy_key']})")
    for scope in plan.get("pilot_scopes") or []:
        lines.append(f"la variable *_SCOPE_VERSION cuyo *_SCOPE_KEY es {scope['scope_key']} = {scope['to_version']}"
                     " (LANCEMOS_PILOT_SCOPE_VERSION, LANCEMOS_PILOT_PRECHECKOUT_SCOPE_VERSION, ...)")
    return lines


def manifest_changes(plan: Mapping[str, Any]) -> list[str]:
    lines = [f"[chatwoot].inbox_id = {plan['to_inbox']}"]
    bindings = plan.get("bindings") or []
    if len(bindings) == 1:
        lines.append(f"[instancia].binding_version = {bindings[0]['to_version']}")
    else:
        for binding in bindings:
            lines.append(f"binding_version = {binding['to_version']} del producto con funnel_ref "
                         f"{binding['funnel_ref']!r}")
    for scope in plan.get("inbound_scopes") or []:
        lines.append(f"[inbound].scope_version = {scope['to_version']}  (scope {scope['scope_key']})")
    return lines


def show_plan(ops: Ops, plan: Mapping[str, Any], inbox_info: Mapping[str, Any], warnings: list[str],
              *, applied: bool) -> None:
    say = ops.say
    say(f"inbox: {plan['from_inbox']} -> {plan['to_inbox']} (cuenta {inbox_info.get('account_id')}, "
        f"{inbox_info.get('channel_type')}, {inbox_info.get('templates_approved')} plantillas aprobadas)")
    for b in plan.get("bindings") or []:
        say(f"binding {b['funnel_ref']}: v{b['from_version']} (retired) -> v{b['to_version']} (active)")
    for p in plan.get("purchase_policies") or []:
        say(f"politica de compra {p['funnel_ref']}: copiada de v{p['from_version']} a v{p['to_version']}")
    for s in plan.get("inbound_scopes") or []:
        say(f"scope entrante {s['scope_key']}: v{s['from_version']} -> v{s['to_version']}")
    say(f"catalogo de ofertas: {plan.get('catalog_rows', 0)} filas copiadas al scope entrante nuevo")
    for h in plan.get("handoff_policies") or []:
        say(f"derivacion {h['policy_key']}: v{h['from_version']} -> v{h['to_version']} (de {h['scope_key']})")
    for s in plan.get("pilot_scopes") or []:
        say(f"scope del piloto {s['scope_key']}: v{s['from_version']} -> v{s['to_version']}, "
            f"control {s['from_state']} -> {s['runtime_state']} (no se arma), {s['from_ref']} -> {s['to_ref']}")
    for s in plan.get("pilot_scopes_untouched") or []:
        say(f"aviso: el scope del piloto {s['scope_key']} v{s['scope_version']} queda con el inbox "
            f"{s['chatwoot_inbox_id']}: no es el viejo ni el nuevo")
    old = plan.get("old_version_rows") or {}
    say("guardado con la version vieja del binding (deja de planificarse al retirarla): "
        + ", ".join(f"{k}={v}" for k, v in old.items()))
    rows = plan.get("old_inbox_rows") or {}
    say("atado al inbox o al scope viejo: " + ", ".join(f"{k}={v}" for k, v in rows.items()))
    if rows.get("channel_identities"):
        say("aviso: hay contactos vistos en el inbox viejo; en el nuevo dan channel_identity_inbox_mismatch")
    if rows.get("daily_feedback_schedules"):
        say("aviso: la revision diaria tiene un horario con el inbox viejo; se recrea con el nuevo")
    for warning in warnings:
        say(f"aviso: {warning}")
    say("")
    say("variables del bridge que hay que cambiar a mano (con respaldo; generar.py conserva las que existen):")
    for line in env_changes(plan):
        say(f"  {line}")
    say("en el manifiesto (instancia.toml, por PR al repo de la instancia):")
    for line in manifest_changes(plan):
        say(f"  {line}")
    say("hace falta redeploy del bridge: lee el manifiesto y las variables al arrancar. "
        "Hasta el redeploy, /ready da 503 commercial_ally_binding_unavailable.")
    say(f"volver atras: este mismo comando con --inbox-nuevo {plan['from_inbox']} (publica otra version).")
    say("")
    say("APLICADO: commit." if applied else "EN SECO: todo corrio y verifico, y se hizo rollback. No quedo nada.")


def cmd_publish(ops: Ops, args: argparse.Namespace) -> int:
    current = ops.read_current(args.tenant)
    inbox_info = ops.read_inbox(args.inbox_nuevo)
    warnings = check_inbox(inbox_info, args.inbox_nuevo, current)
    assert inbox_info is not None
    plan = ops.publish(args.tenant, args.inbox_nuevo, args.quien, args.motivo, apply=False)
    if not args.aplicar:
        show_plan(ops, plan, inbox_info, warnings, applied=False)
        return EXIT_OK
    old = plan.get("old_version_rows") or {}
    pending = sum(int(v or 0) for v in old.values())
    if pending and not args.acepto_eventos_de_la_version_vieja:
        show_plan(ops, plan, inbox_info, warnings, applied=False)
        raise Refused(f"hay {pending} filas de Hotmart guardadas con la version vieja del binding: "
                      "corre otra vez con --acepto-eventos-de-la-version-vieja si se acepta que no se planifiquen")
    code = ops.code()
    answer = ops.terminal.ask(f"para publicar el inbox {args.inbox_nuevo} en {args.tenant}, escribi {code}: ")
    if answer != code:
        raise Refused("el codigo no coincide: no se escribio nada")
    applied = ops.publish(args.tenant, args.inbox_nuevo, args.quien, args.motivo, apply=True)
    show_plan(ops, applied, inbox_info, warnings, applied=True)
    return EXIT_OK


def cmd_verify(ops: Ops, args: argparse.Namespace) -> int:
    current = ops.read_current(args.tenant)
    failures: list[str] = []
    bindings = current.get("bindings") or []
    if not bindings:
        failures.append("no hay binding activo")
    for b in bindings:
        ops.say(f"binding {b['funnel_ref']}: v{b['binding_version']} activo con el inbox {b['chatwoot_inbox_id']}")
        if b["chatwoot_inbox_id"] != args.inbox_nuevo:
            failures.append(f"el binding activo de {b['funnel_ref']} tiene el inbox {b['chatwoot_inbox_id']}")
    expected_ref = f"chatwoot-inbox:{args.inbox_nuevo}"
    for c in current.get("controls") or []:
        if c.get("chatwoot_inbox_id") != args.inbox_nuevo or c.get("channel_account_ref") != expected_ref:
            failures.append(f"el control {c['scope_key']} apunta a v{c['scope_version']} con "
                            f"{c.get('channel_account_ref')}")
    code, ready = ops.ready()
    ops.say(f"/ready: {code} {json.dumps(ready, ensure_ascii=True, sort_keys=True)}")
    if code != 200 or ready.get("commercial_ally_binding") != "active":
        failures.append("el /ready no da 200 con commercial_ally_binding=active (falta el manifiesto, "
                        "las variables o el redeploy)")
    for failure in failures:
        ops.say(f"falta: {failure}")
    ops.say("VERIFICADO" if not failures else "NO VERIFICADO")
    return EXIT_OK if not failures else EXIT_REFUSED


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="publish_inbox_binding_version", description=__doc__.split("\n\n")[0])
    root.add_argument("--tenant", required=True)
    root.add_argument("--inbox-nuevo", required=True, type=int)
    root.add_argument("--quien")
    root.add_argument("--motivo")
    mode = root.add_mutually_exclusive_group()
    mode.add_argument("--aplicar", action="store_true")
    mode.add_argument("--verificar", action="store_true")
    root.add_argument("--acepto-eventos-de-la-version-vieja", action="store_true")
    return root


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None,
         ops: Ops | None = None) -> int:
    args = parser().parse_args(argv)
    out = ops.out if ops is not None else sys.stdout
    try:
        if not _TENANT.fullmatch(args.tenant):
            raise Refused("--tenant tiene que ser un tenant_ref ([a-z0-9_-])", EXIT_USAGE)
        if args.inbox_nuevo <= 0:
            raise Refused("--inbox-nuevo tiene que ser un id de inbox", EXIT_USAGE)
        if not args.verificar:
            if not args.quien or not _NAME.fullmatch(args.quien):
                raise Refused("--quien es obligatorio: un nombre corto ([a-z0-9_.-])", EXIT_USAGE)
            if not args.motivo or not args.motivo.strip():
                raise Refused("--motivo es obligatorio", EXIT_USAGE)
        if ops is None:
            ops = Ops(Config.from_env(os.environ if env is None else env, needs_bridge=args.verificar))
        return cmd_verify(ops, args) if args.verificar else cmd_publish(ops, args)
    except Refused as refused:
        out.write(f"no: {refused}\n")
        return refused.code


if __name__ == "__main__":
    raise SystemExit(main())
