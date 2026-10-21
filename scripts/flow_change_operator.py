#!/usr/bin/env python3
"""El comando del operador del almacen de flujos: ver, validar, aprobar y publicar,
pausar, descartar y volver atras un flujo.

Diseno: el almacen, §10.2 («El comando del operador»), y el contrato del ciclo
(docs/contracts/flow-change-cycle-v1.md). Corre en el VPS, como archivo (no
por la entrada estandar de ``ssh``: ``docker exec -i`` se la comeria), solo
con la biblioteca estandar, con el molde de ``piloto.sh``.

**Lo corre una persona, en una terminal.** Toda escritura pide un codigo que
el script muestra y lee por ``/dev/tty``; sin terminal se niega. La garantia
real es ser root en el VPS: el codigo ataja un error, no a un root.
**Andy no corre ``publicar``, ``volver``, ``prender``, ``apagar``, ``pausar``,
``descartar``, ``registrar`` ni ``ruta``.**

Subcomandos::

    estado
    listar
    ver <propuesta>
    proponer --archivo <items.json> --quien <q> --motivo "..."
    validar <propuesta> --quien <q>
    publicar <propuesta> --quien <q> [--sin-simulacion]
    volver <producto>/<flujo> <version> --quien <q> --motivo "..." [--sin-simulacion]
    prender <producto>/<flujo> --quien <q> --motivo "..." [--sin-simulacion]
    apagar <producto>/<flujo> --quien <q> --motivo "..." [--sin-simulacion]
    pausar <producto>/<flujo> --quien <q> --motivo "..."
    registrar <producto>/<flujo> --quien <q> --motivo "..."
    ruta <producto>/<flujo> --tipo carrito|pago_fallido|ficha --scope <scope del legado> --quien <q> --motivo "..."
    descartar <propuesta> --quien <q> --motivo "..."
    descartar --todo-del-agente --quien <q> --motivo "..."

Variables (las pone el envoltorio de la instancia; todas obligatorias salvo
las que dicen su valor por defecto):

* ``CONTENEDOR_DB``: filtro de ``docker ps --filter name=`` que tiene que dar
  exactamente un contenedor (el VPS es compartido: ``head -1`` no sirve).
* ``USUARIO_DB`` (``postgres``) y ``BASE_DB`` (``postgres``): el duenio de la
  base, que es quien puede ejecutar las ``operator_*``.
* ``CONTENEDOR_CHATWOOT_DB``, ``USUARIO_CHATWOOT_DB`` (``postgres``),
  ``BASE_CHATWOOT_DB`` y ``BANDEJA_CHATWOOT``: de donde se lee el catalogo de
  plantillas (``channel_whatsapp.message_templates``), en una sesion de solo
  lectura.
* ``IMAGEN_BRIDGE``: la imagen del bridge fijada por digest
  (``nombre@sha256:<64 hex>`` o ``sha256:<64 hex>``). El validador corre en un
  contenedor nuevo desde ahi, sin red y de solo lectura, nunca en el bridge
  vivo (V2#4).
* ``TENANT``, ``ALIADA`` y ``ZONA``: la instancia.
* ``APROBADORES``: los nombres que pueden escribir, separados por comas
  (``dan`` en ATT1). ``--quien`` tiene que estar en la lista.

El SQL va siempre por la entrada estandar de ``psql -X``; los valores, en una
tabla temporal cargada con ``COPY ... FROM STDIN``, nunca pegados en el texto
del SQL. Todo lo que se imprime pasa por ``json.dumps(ensure_ascii=True)``.
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
from datetime import datetime, timedelta, timezone
from typing import Any, TextIO

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_BLOCKED = 3

OPEN_STATUSES = ("borrador", "validada", "aprobada")
GAP_SIMULATION = "simulation_not_built"
REVALIDATE_AFTER = timedelta(minutes=8)  # la base acepta hasta 10 (c_revalidation_window)
MODULE = "bridge.flow_change_operator"
_IMAGE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._/:-]*@)?sha256:[0-9a-f]{64}$")
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_FLOW = re.compile(r"^([a-z0-9][a-z0-9-]{0,127})/([a-z][a-z0-9_]{1,39})$")
_PG_ERROR = re.compile(r"ERROR:\s+([a-z][a-z0-9_]{1,79})\b")
_INT = re.compile(r"^-?[0-9]+$")
# La primera linea de ``estado`` mientras el almacen no gobierne envios (§10.2).
NOT_GOVERNING = ("el almacen todavia no gobierna ningun envio: los flujos de la instancia "
                 "siguen en piloto.sh hasta que su ruta pase a motor")
AGENT_TEXT_OPEN = "--- escrito por quien propuso, no verificado ---"
AGENT_TEXT_CLOSE = "--- fin del texto de quien propuso ---"


class Refused(Exception):
    """El comando no sigue: el motivo se imprime y sale con ``code``."""

    def __init__(self, message: str, code: int = EXIT_REFUSED) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Lo de afuera: docker y la terminal. Las pruebas los reemplazan.
# ---------------------------------------------------------------------------

class Docker:
    """``docker`` con los argumentos como lista, sin un shell en el medio."""

    def run(self, argv: Sequence[str], stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(["docker", *argv], input=stdin, text=True, capture_output=True, check=False)


class Terminal:
    """La terminal de quien corre el comando. Sin ``/dev/tty``, no hay confirmacion."""

    def ask(self, prompt: str) -> str:
        # Una tty no admite seek: abierta en texto con "r+" da UnsupportedOperation, que es un
        # OSError y se leia como "sin terminal". Por eso se abre dos veces, para escribir y para leer.
        try:
            with open("/dev/tty", "w", encoding="utf-8") as out, open("/dev/tty", encoding="utf-8") as tty:
                out.write(prompt)
                out.flush()
                return tty.readline().strip()
        except OSError as error:
            raise Refused("sin terminal no se confirma: este comando lo corre una persona") from error


@dataclass
class Config:
    db_filter: str
    db_user: str
    db_name: str
    chatwoot_filter: str
    chatwoot_user: str
    chatwoot_db: str
    inbox: int
    image: str
    tenant: str
    ally: str
    zone: str
    approvers: tuple[str, ...]

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise Refused(f"falta la variable {name}", EXIT_USAGE)
            return value

        image = required("IMAGEN_BRIDGE")
        if not _IMAGE.fullmatch(image):
            raise Refused("IMAGEN_BRIDGE tiene que estar fijada por digest (sha256:<64 hex>)", EXIT_USAGE)
        inbox = required("BANDEJA_CHATWOOT")
        if not inbox.isdigit():
            raise Refused("BANDEJA_CHATWOOT tiene que ser un numero", EXIT_USAGE)
        approvers = tuple(a.strip() for a in required("APROBADORES").split(",") if a.strip())
        if not approvers or not all(_NAME.fullmatch(a) for a in approvers):
            raise Refused("APROBADORES tiene que ser una lista de nombres separados por comas", EXIT_USAGE)
        return cls(
            db_filter=required("CONTENEDOR_DB"),
            db_user=env.get("USUARIO_DB", "postgres").strip() or "postgres",
            db_name=env.get("BASE_DB", "postgres").strip() or "postgres",
            chatwoot_filter=required("CONTENEDOR_CHATWOOT_DB"),
            chatwoot_user=env.get("USUARIO_CHATWOOT_DB", "postgres").strip() or "postgres",
            chatwoot_db=required("BASE_CHATWOOT_DB"),
            inbox=int(inbox),
            image=image,
            tenant=required("TENANT"),
            ally=required("ALIADA"),
            zone=required("ZONA"),
            approvers=approvers,
        )

    @property
    def instance(self) -> dict[str, str]:
        return {"tenant_ref": self.tenant, "ally_ref": self.ally}


@dataclass
class Ops:
    config: Config
    docker: Docker = field(default_factory=Docker)
    terminal: Terminal = field(default_factory=Terminal)
    out: TextIO = sys.stdout
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    code: Callable[[], str] = lambda: secrets.token_hex(3)
    _containers: dict[str, str] = field(default_factory=dict)

    # -- salida ------------------------------------------------------------
    def say(self, line: str = "") -> None:
        self.out.write(line + "\n")

    def show(self, label: str, value: object) -> None:
        self.say(f"{label}: {json.dumps(value, ensure_ascii=True, sort_keys=True)}")

    # -- contenedores ------------------------------------------------------
    def container(self, name_filter: str) -> str:
        if name_filter in self._containers:
            return self._containers[name_filter]
        listed = self.docker.run(["ps", "--filter", f"name={name_filter}", "--format", "{{.Names}}"])
        names = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
        if listed.returncode or len(names) != 1:
            raise Refused(f"el filtro {name_filter!r} tiene que dar exactamente un contenedor (dio {len(names)})")
        self._containers[name_filter] = names[0]
        return names[0]

    # -- psql ----------------------------------------------------------------
    def _psql(self, container: str, user: str, database: str, script: str) -> list[str]:
        result = self.docker.run(
            ["exec", "-i", container, "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
             "-U", user, "-d", database],
            stdin=script,
        )
        if result.returncode:
            found = _PG_ERROR.search(result.stderr or "")
            raise Refused(f"la base rechazo el pedido: {found.group(1) if found else 'error de psql'}")
        return [line for line in result.stdout.splitlines() if line.strip()]

    def sql(self, statements: str, args: Mapping[str, object] | None = None, *, read_only: bool = False) -> list[str]:
        """Corre ``statements`` en la base de la instancia; los valores van en ``pg_temp._args``."""
        script = _args_block(args or {})
        if read_only:
            script += "begin transaction read only;\n" + statements + "\ncommit;\n"
        else:
            script += statements + "\n"
        return self._psql(self.container(self.config.db_filter), self.config.db_user, self.config.db_name, script)

    def sql_json(self, statement: str, args: Mapping[str, object] | None = None, *, read_only: bool = False) -> Any:
        lines = self.sql(statement, args, read_only=read_only)
        if len(lines) != 1:
            raise Refused("la base devolvio una respuesta inesperada")
        return json.loads(lines[0])

    # -- lecturas ----------------------------------------------------------
    def read_world(self) -> dict[str, Any]:
        """La instancia con sus productos y el almacen con todas las versiones, en una lectura."""
        args = {"tenant": self.config.tenant, "ally": self.config.ally}
        lines = self.sql(SQL_PRODUCTS + "\n" + SQL_STORE, args, read_only=True)
        if len(lines) != 2:
            raise Refused("la base devolvio una respuesta inesperada")
        products, flows = json.loads(lines[0]), json.loads(lines[1])
        if not products:
            raise Refused("la instancia no tiene un producto con binding activo")
        return {
            "instance": {**self.config.instance, "products": products, "time_zone": self.config.zone},
            "store": {"flows": flows},
        }

    def read_proposal(self, proposal_id: str) -> dict[str, Any]:
        _require_uuid(proposal_id)
        args = {"tenant": self.config.tenant, "ally": self.config.ally, "id": proposal_id}
        found = self.sql_json(SQL_PROPOSAL, args, read_only=True)
        if found is None:
            raise Refused("no hay una propuesta con ese id en esta instancia")
        return found

    def read_catalog(self) -> dict[str, Any] | None:
        script = _args_block({"inbox": self.config.inbox}) + (
            "begin transaction read only;\n" + SQL_CATALOG + "\ncommit;\n")
        lines = self._psql(self.container(self.config.chatwoot_filter), self.config.chatwoot_user,
                           self.config.chatwoot_db, script)
        if len(lines) != 1 or lines[0] in ("", "null"):
            return None
        templates = json.loads(lines[0])
        return {"message_templates": templates} if isinstance(templates, list) else None

    # -- el validador, en un contenedor nuevo ----------------------------------
    def module(self, subcommand: str, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        result = self.docker.run(
            ["run", "--rm", "-i", "--read-only", "--network", "none", "--cap-drop", "ALL",
             "--security-opt", "no-new-privileges", "--user", "65534:65534",
             "-e", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint", "python", self.config.image,
             "-m", MODULE, subcommand],
            stdin=json.dumps(payload, ensure_ascii=True),
        )
        try:
            out = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError) as error:
            raise Refused("el validador no devolvio una respuesta (mira la imagen fijada)") from error
        if result.returncode not in (0, 3) or not isinstance(out, dict):
            raise Refused(f"el validador rechazo la entrada: {out.get('reason_code') if isinstance(out, dict) else '?'}")
        return result.returncode, out

    def read_engine_inputs(self, items: object) -> dict[str, Any]:
        """La lectura del motor y la historia del producto, o por que no se pudieron leer."""
        products = {item.get("producto") for item in items if isinstance(item, Mapping)} \
            if isinstance(items, list) else set()
        if len(products) != 1:
            return {"simulation_unavailable": "simulation_multi_product"}
        args = {"tenant": self.config.tenant, "ally": self.config.ally}
        try:
            engine: dict[str, Any] | None = None
            after = None
            for _ in range(ENGINE_PAGES_MAX):
                page = self.sql_json(SQL_ENGINE, {**args, "after": after}, read_only=True)
                if not isinstance(page, dict) or not isinstance(page.get("flows"), list):
                    raise Refused("la lectura del motor no tiene la forma del contrato")
                engine = page if engine is None else {**engine, "flows": engine["flows"] + page["flows"]}
                after = page.get("next")
                if after is None:
                    break
            else:
                raise Refused("la lectura del motor tiene demasiadas paginas")
            history = self.sql_json(SQL_HISTORY, {**args, "product": next(iter(products))}, read_only=True)
        except Refused:
            return {"simulation_unavailable": "flow_engine_unavailable"}
        return {"engine": engine, "history": history}

    def validate(self, proposal: Mapping[str, Any], world: Mapping[str, Any],
                 catalog: Mapping[str, Any] | None) -> dict[str, Any]:
        payload = {**world, **self.read_engine_inputs(proposal["items"]), "catalog": catalog, "proposal": {
            key: proposal[key] for key in ("id", "revision", "items", "draft_base", "purpose", "requested_by_kind")}}
        _, out = self.module("validar", payload)
        return out

    # -- confirmar -----------------------------------------------------------
    def confirm(self, what: str) -> None:
        code = self.code()
        answer = self.terminal.ask(f"\nPara {what}, escribi este codigo: {code}\n> ")
        if answer != code:
            raise Refused("el codigo no coincide: no se escribio nada")

    def check_approver(self, who: str) -> None:
        if who not in self.config.approvers:
            raise Refused(f"{who!r} no esta en APROBADORES", EXIT_USAGE)


# ---------------------------------------------------------------------------
# El SQL. Los valores salen de pg_temp._args, nunca del texto.
# ---------------------------------------------------------------------------

def _arg(key: str) -> str:
    return f"(select v from pg_temp._args where k = '{key}')"


def _copy_escape(value: str) -> str:
    return (value.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r"))


def _args_block(args: Mapping[str, object]) -> str:
    lines = ["create temp table _args (k text primary key, v text);", "copy pg_temp._args (k, v) from stdin;"]
    for key, value in args.items():
        if not re.fullmatch(r"[a-z_]{1,32}", key):
            raise ValueError("arg_key_invalid")
        if value is None:
            text = "\\N"
        elif isinstance(value, (dict, list)):
            text = _copy_escape(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        else:
            text = _copy_escape(str(value))
        lines.append(f"{key}\t{text}")
    lines.append("\\.")
    return "\n".join(lines) + "\n"


INSTANCE_SQL = f"jsonb_build_object('tenant_ref', {_arg('tenant')}, 'ally_ref', {_arg('ally')})"

SQL_PRODUCTS = f"""select coalesce(jsonb_agg(distinct b.funnel_ref order by b.funnel_ref), '[]'::jsonb)::text
  from public.commercial_ally_runtime_bindings b
 where b.tenant_ref = {_arg('tenant')} and b.ally_ref = {_arg('ally')} and b.status = 'active';"""

SQL_STORE = f"""select coalesce(jsonb_agg(jsonb_build_object(
    'flow_id', c.flow_id, 'product_ref', c.product_ref, 'flow_key', c.flow_key, 'state', c.state,
    'active_version', c.active_version, 'generation', c.generation, 'changed_at', c.changed_at,
    'changed_by_kind', c.changed_by_kind,
    'versions', coalesce((select jsonb_object_agg(d.version::text, d.definition)
                            from public.flow_definitions d where d.flow_id = c.flow_id), '{{}}'::jsonb))
    order by c.product_ref, c.flow_key), '[]'::jsonb)::text
  from public.flow_controls c
 where c.tenant_ref = {_arg('tenant')} and c.ally_ref = {_arg('ally')};"""

_PROPOSAL_FIELDS = """'id', p.id, 'status', p.status,
    'effective_status', case when p.status in ('validada', 'aprobada') and p.expires_at is not null
                              and clock_timestamp() >= p.expires_at then 'vencida' else p.status end,
    'revision', p.revision, 'items', p.items, 'draft_base', p.draft_base, 'purpose', p.purpose,
    'requested_by_kind', p.requested_by_kind, 'fingerprint', p.fingerprint,
    'validated_at', p.validated_at, 'expires_at', p.expires_at, 'created_at', p.created_at,
    'approved_by_ref', p.approved_by_ref"""

SQL_PROPOSAL = f"""select coalesce((select jsonb_build_object({_PROPOSAL_FIELDS},
    'validation', p.validation)
  from public.flow_change_proposals p
 where p.id = {_arg('id')}::uuid and p.tenant_ref = {_arg('tenant')} and p.ally_ref = {_arg('ally')}),
 'null'::jsonb)::text;"""

SQL_OPEN = f"""select coalesce(jsonb_agg(jsonb_build_object({_PROPOSAL_FIELDS}) order by p.created_at), '[]'::jsonb)::text
  from public.flow_change_proposals p
 where p.tenant_ref = {_arg('tenant')} and p.ally_ref = {_arg('ally')}
   and p.status in ('borrador', 'validada', 'aprobada');"""

# Las rutas de los flujos de la instancia (20261021000200). Solo para mostrarlas en ``estado``.
SQL_ROUTES = f"""select coalesce(jsonb_agg(jsonb_build_object(
    'product_ref', c.product_ref, 'flow_key', c.flow_key, 'executor', r.executor, 'legacy_kind', r.legacy_kind,
    'legacy_scope_key', r.legacy_scope_key) order by c.product_ref, c.flow_key), '[]'::jsonb)::text
  from public.flow_engine_routes r join public.flow_controls c on c.flow_id = r.flow_id
 where c.tenant_ref = {_arg('tenant')} and c.ally_ref = {_arg('ally')};"""
# Los tipos del legado que frena la guarda del almacen (operator_create_flow_route_v1).
ROUTE_KINDS = ("carrito", "pago_fallido", "ficha")

# Las dos lecturas del motor (flow-engine-v1, secciones 1 y 2), para la simulacion de la vista previa.
# La historia se pide con el now() de la base: 8 dias menos 5 minutos, lo que la funcion acepta.
SQL_ENGINE = f"""select public.read_flow_engine_v1({INSTANCE_SQL}, {_arg('after')})::text;"""
SQL_HISTORY = f"""select public.read_flow_engine_history_v1({INSTANCE_SQL}, {_arg('product')},
    now() - interval '8 days' + interval '5 minutes')::text;"""
ENGINE_PAGES_MAX = 10

SQL_CATALOG = f"""select c.message_templates::jsonb::text
  from public.inboxes i join public.channel_whatsapp c on c.id = i.channel_id
 where i.id = {_arg('inbox')}::integer and i.channel_type = 'Channel::Whatsapp';"""


def _call(function: str, *params: tuple[str, str]) -> str:
    """``select public.<function>(...)::text`` con cada parametro leido de ``_args`` y su tipo."""
    rendered = ", ".join("null" if key == "" else f"{_arg(key)}::{kind}" for key, kind in params)
    return f"select public.{function}({rendered})::text;"


# ---------------------------------------------------------------------------
# El diff lo calcula este script, con la biblioteca estandar (V2#4): no le cree
# a la vista previa de la imagen.
# ---------------------------------------------------------------------------

_MISSING = object()


def _pointer(segments: Sequence[object]) -> str:
    return "".join("/" + str(s).replace("~", "~0").replace("/", "~1") for s in segments)


def json_diff(before: object, after: object) -> list[tuple[str, object, object]]:
    changes: list[tuple[str, object, object]] = []

    def walk(segments: tuple[object, ...], left: object, right: object) -> None:
        if isinstance(left, dict) and isinstance(right, dict):
            for key in list(left) + [k for k in right if k not in left]:
                walk(segments + (key,), left.get(key, _MISSING), right.get(key, _MISSING))
            return
        if isinstance(left, list) and isinstance(right, list):
            for index in range(max(len(left), len(right))):
                walk(segments + (index,), left[index] if index < len(left) else _MISSING,
                     right[index] if index < len(right) else _MISSING)
            return
        if left is _MISSING and right is _MISSING:
            return
        if left is _MISSING or right is _MISSING or json.dumps(left, sort_keys=True) != json.dumps(right, sort_keys=True):
            changes.append((_pointer(segments), None if left is _MISSING else left,
                            None if right is _MISSING else right))

    walk((), before, after)
    return [change for change in changes if change[0] not in ("/estado", "/version")]


def _flow_of(world: Mapping[str, Any], flow_id: str) -> Mapping[str, Any] | None:
    for flow in world["store"]["flows"]:
        if flow["flow_id"] == flow_id:
            return flow
    return None


def show_items(ops: Ops, items: Sequence[Mapping[str, Any]], world: Mapping[str, Any]) -> None:
    for item in items:
        flow = _flow_of(world, item.get("flow_id", ""))
        ref = f"{item.get('producto')}/{item.get('flujo')}"
        if flow is None:
            ops.say(f"* {ref}: el flujo no esta en el almacen")
            continue
        active = flow.get("active_version")
        tipo = item.get("tipo")
        ops.say(f"* {ref}: {tipo} (hoy: version {active}, {flow.get('state')}, generacion {flow.get('generation')})")
        if tipo in ("prender", "apagar"):
            ops.show("  estado despues", "prendido" if tipo == "prender" else "apagado")
            continue
        before = flow["versions"].get(str(active), {}) if active is not None else {}
        if tipo == "volver":
            target = flow["versions"].get(str(item.get("version")))
            if target is None:
                ops.say(f"  la version {item.get('version')} no existe")
                continue
            after: object = target
        else:
            after = item.get("definicion", {})
        for path, left, right in json_diff(before, after):
            ops.say(f"  {json.dumps(path, ensure_ascii=True)}: {json.dumps(left, ensure_ascii=True, sort_keys=True)}"
                    f" -> {json.dumps(right, ensure_ascii=True, sort_keys=True)}")
        ops.show("  en_curso", item.get("en_curso"))


def show_agent_text(ops: Ops, text: object) -> None:
    ops.say(AGENT_TEXT_OPEN)
    ops.say(json.dumps(text, ensure_ascii=True))
    ops.say(AGENT_TEXT_CLOSE)


def show_validation(ops: Ops, validation: Mapping[str, Any]) -> None:
    ops.show("bloqueos", [(b.get("code"), b.get("path")) for b in validation["result"]["blocks"]])
    ops.show("advertencias", [(w.get("code"), w.get("path")) for w in validation["result"]["warnings"]])
    ops.show("huecos", [gap.get("code") for gap in validation["preview"].get("huecos", [])])


# ---------------------------------------------------------------------------
# Los subcomandos.
# ---------------------------------------------------------------------------

def cmd_estado(ops: Ops, _args: argparse.Namespace) -> int:
    ops.say(NOT_GOVERNING)
    world = ops.read_world()
    ops.show("instancia", world["instance"])
    try:
        found = ops.sql_json(SQL_ROUTES, {"tenant": ops.config.tenant, "ally": ops.config.ally}, read_only=True)
    except Refused:
        found = None
    routes = {(route["product_ref"], route["flow_key"]): route for route in found} \
        if isinstance(found, list) and all(isinstance(route, dict) for route in found) else None
    for flow in world["store"]["flows"]:
        ops.say(f"* {flow['product_ref']}/{flow['flow_key']}: {flow['state']}, version {flow['active_version']}, "
                f"generacion {flow['generation']}, versiones {sorted(int(v) for v in flow['versions'])}")
        route = None if routes is None else routes.get((flow["product_ref"], flow["flow_key"]))
        if routes is None:
            ops.say("  ruta: no se pudo leer (la base no tiene la migracion del motor)")
        elif route is None:
            ops.say("  ruta: ninguna; el estado de aca no frena nada del legado")
        else:
            ops.say(f"  ruta: {route['executor']} ({route['legacy_kind']}, {route['legacy_scope_key']}); "
                    "pausarlo o apagarlo aca frena el legado")
    if not world["store"]["flows"]:
        ops.say("* no hay flujos registrados")
    return EXIT_OK


def cmd_listar(ops: Ops, _args: argparse.Namespace) -> int:
    rows = ops.sql_json(SQL_OPEN, {"tenant": ops.config.tenant, "ally": ops.config.ally}, read_only=True)
    for row in rows:
        refs = [f"{i.get('producto')}/{i.get('flujo')}:{i.get('tipo')}" for i in row["items"]]
        ops.say(f"* {row['id']} {row['effective_status']} r{row['revision']} {row['requested_by_kind']} "
                f"{row['created_at']} {json.dumps(refs, ensure_ascii=True)}")
    if not rows:
        ops.say("* no hay propuestas abiertas")
    return EXIT_OK


def cmd_ver(ops: Ops, args: argparse.Namespace) -> int:
    proposal = ops.read_proposal(args.propuesta)
    world = ops.read_world()
    ops.show("propuesta", {k: proposal[k] for k in ("id", "effective_status", "revision", "requested_by_kind",
                                                    "fingerprint", "expires_at", "approved_by_ref")})
    show_items(ops, proposal["items"], world)
    show_agent_text(ops, proposal["purpose"])
    if proposal["effective_status"] in OPEN_STATUSES:
        ops.say("la validacion de ahora (recalculada; la guardada no se muestra como verificada):")
        show_validation(ops, ops.validate(proposal, world, ops.read_catalog()))
    return EXIT_OK


def _record(ops: Ops, proposal: Mapping[str, Any], validation: Mapping[str, Any], who: str) -> dict[str, Any]:
    args = {"tenant": ops.config.tenant, "ally": ops.config.ally, "id": proposal["id"],
            "revision": proposal["revision"], "base": validation["validated_base"],
            "validator": validation["validator"], "result": validation["result"],
            "preview": validation["preview"], "who": who}
    return ops.sql_json(INSTANCE_ARGS + _call(
        "operator_record_flow_change_validation_v1", ("instance", "jsonb"), ("id", "uuid"),
        ("revision", "integer"), ("base", "jsonb"), ("validator", "jsonb"), ("result", "jsonb"),
        ("preview", "jsonb"), ("who", "text")), args)


# El p_instance de las operator_*, armado una vez en _args.
INSTANCE_ARGS = (f"insert into pg_temp._args (k, v) select 'instance', {INSTANCE_SQL}::text;\n")


def cmd_validar(ops: Ops, args: argparse.Namespace) -> int:
    ops.check_approver(args.quien)
    proposal = ops.read_proposal(args.propuesta)
    if proposal["requested_by_kind"] != "operador":
        raise Refused("una propuesta del Automation Expert la valida su ruta del bridge, no este comando")
    if proposal["status"] != "borrador":
        raise Refused(f"la propuesta esta {proposal['effective_status']}, no en borrador")
    world = ops.read_world()
    validation = ops.validate(proposal, world, ops.read_catalog())
    show_validation(ops, validation)
    recorded = _record(ops, proposal, validation, args.quien)
    ops.show("resultado", recorded)
    return EXIT_OK if recorded.get("status") == "validada" else EXIT_BLOCKED


def _publish(ops: Ops, proposal_id: str, who: str, without_simulation: bool) -> int:
    """El sí y la publicacion de una propuesta validada o aprobada (§10.2, ``publicar``)."""
    proposal = ops.read_proposal(proposal_id)
    if proposal["effective_status"] not in ("validada", "aprobada"):
        raise Refused(f"la propuesta esta {proposal['effective_status']}: solo se publica una validada o aprobada")
    world = ops.read_world()
    catalog = ops.read_catalog()
    validation = ops.validate(proposal, world, catalog)
    validated_at = ops.now()
    ops.show("propuesta", {k: proposal[k] for k in ("id", "effective_status", "revision", "requested_by_kind",
                                                    "fingerprint", "expires_at")})
    show_items(ops, proposal["items"], world)
    show_validation(ops, validation)
    show_agent_text(ops, proposal["purpose"])
    if not validation["ok"]:
        raise Refused("la validacion de ahora tiene bloqueos: no se publica", EXIT_BLOCKED)
    if validation.get("catalog_sha256") is None:
        raise Refused("no se pudo leer el catalogo de plantillas: no se publica")
    # Aprobar registra la simulacion de la validacion guardada (validation.preview.simulacion), no la de
    # ahora: sin una construida ahi, se publica solo con --sin-simulacion.
    stored = ((proposal.get("validation") or {}).get("preview") or {}).get("simulacion")
    if not (isinstance(stored, Mapping) and stored.get("status") == "built"):
        if not without_simulation:
            reason = stored.get("reason_code") if isinstance(stored, Mapping) else GAP_SIMULATION
            raise Refused(f"la validacion guardada no trae simulacion ({reason}): para publicar igual, --sin-simulacion")
    ops.confirm(f"aprobar y publicar {proposal['id'][:8]}")
    instance = {"tenant": ops.config.tenant, "ally": ops.config.ally, "id": proposal["id"],
                "fingerprint": proposal["fingerprint"], "who": who}
    if proposal["effective_status"] == "validada":
        approved = ops.sql_json(INSTANCE_ARGS + _call(
            "operator_approve_flow_change_v1", ("instance", "jsonb"), ("id", "uuid"), ("fingerprint", "text"),
            ("who", "text")), instance)
        ops.show("aprobacion", approved)
        if approved.get("ok") is not True:
            return EXIT_BLOCKED
    if ops.now() - validated_at > REVALIDATE_AFTER:
        validation = ops.validate(proposal, world, catalog)
        validated_at = ops.now()
        if not validation["ok"]:
            raise Refused("la revalidacion tiene bloqueos: queda aprobada sin publicar", EXIT_BLOCKED)
    revalidation = {
        "catalog_sha256": validation["catalog_sha256"], "fingerprint": proposal["fingerprint"],
        "result": validation["result"], "validator": validation["validator"],
        "validated_at": validated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    published = ops.sql_json(INSTANCE_ARGS + _call(
        "operator_publish_flow_change_v1", ("instance", "jsonb"), ("id", "uuid"), ("fingerprint", "text"),
        ("revalidation", "jsonb")), {**instance, "revalidation": revalidation})
    ops.show("publicacion", published)
    if published.get("ok") is not True:
        return EXIT_BLOCKED
    ops.say("despues:")
    cmd_estado(ops, argparse.Namespace())
    return EXIT_OK


def cmd_publicar(ops: Ops, args: argparse.Namespace) -> int:
    ops.check_approver(args.quien)
    return _publish(ops, args.propuesta, args.quien, args.sin_simulacion)


def _save_and_validate(ops: Ops, items: list[dict[str, Any]], draft_base: Mapping[str, Any], who: str,
                       reason: str) -> tuple[dict[str, Any], dict[str, Any]]:
    args = {"tenant": ops.config.tenant, "ally": ops.config.ally, "items": items, "base": dict(draft_base),
            "purpose": reason, "who": who}
    saved = ops.sql_json(INSTANCE_ARGS + _call(
        "operator_save_flow_change_draft_v1", ("instance", "jsonb"), ("", ""), ("", ""), ("items", "jsonb"),
        ("base", "jsonb"), ("purpose", "text"), ("who", "text")), args)
    ops.show("borrador", saved)
    if saved.get("ok") is not True:
        raise Refused("la base no guardo el borrador", EXIT_BLOCKED)
    proposal = ops.read_proposal(saved["proposal_id"])
    validation = ops.validate(proposal, ops.read_world(), ops.read_catalog())
    show_validation(ops, validation)
    recorded = _record(ops, proposal, validation, who)
    ops.show("validacion", recorded)
    if recorded.get("status") != "validada":
        raise Refused(f"la propuesta {saved['proposal_id']} quedo en borrador con bloqueos", EXIT_BLOCKED)
    return saved, recorded


def _find_flow(world: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
    match = _FLOW.fullmatch(ref)
    if not match:
        raise Refused("el flujo va como <producto>/<flujo>", EXIT_USAGE)
    for flow in world["store"]["flows"]:
        if (flow["product_ref"], flow["flow_key"]) == match.groups():
            return flow
    raise Refused(f"no hay un flujo {ref} en esta instancia")


def cmd_estado_o_version(ops: Ops, args: argparse.Namespace) -> int:
    """``volver``, ``prender`` y ``apagar``: la propuesta y su sí en un comando (V1#16)."""
    ops.check_approver(args.quien)
    world = ops.read_world()
    flow = _find_flow(world, args.flujo)
    item: dict[str, Any] = {"producto": flow["product_ref"], "flujo": flow["flow_key"],
                            "flow_id": flow["flow_id"], "tipo": args.comando}
    if args.comando == "volver":
        if str(args.version) not in flow["versions"] or args.version == flow["active_version"]:
            raise Refused("la version de destino tiene que existir y no ser la activa")
        item.update(version=args.version, en_curso="seguir")
    draft_base = {flow["flow_id"]: {"active_version": flow["active_version"]}}
    saved, _ = _save_and_validate(ops, [item], draft_base, args.quien, args.motivo)
    return _publish(ops, saved["proposal_id"], args.quien, args.sin_simulacion)


def cmd_proponer(ops: Ops, args: argparse.Namespace) -> int:
    ops.check_approver(args.quien)
    try:
        with open(args.archivo, encoding="utf-8") as handle:
            items = json.load(handle)
    except (OSError, ValueError) as error:
        raise Refused("no se pudo leer el archivo de items", EXIT_USAGE) from error
    world = ops.read_world()
    code, normalized = ops.module("normalizar", {**world, "items": items, "catalog": ops.read_catalog()})
    if code != 0:
        ops.show("bloqueos", [(b.get("code"), b.get("path")) for b in normalized["result"]["blocks"]])
        raise Refused("los items tienen bloqueos: no se guardo nada", EXIT_BLOCKED)
    saved, recorded = _save_and_validate(ops, normalized["items"], normalized["draft_base"], args.quien, args.motivo)
    ops.say(f"para publicarla: publicar {saved['proposal_id']} --quien {args.quien}")
    return EXIT_OK


def cmd_pausar(ops: Ops, args: argparse.Namespace) -> int:
    ops.check_approver(args.quien)
    flow = _find_flow(ops.read_world(), args.flujo)
    generation = flow["generation"]
    ops.say(f"{args.flujo}: {flow['state']}, version {flow['active_version']}, generacion {generation}")
    ops.confirm(f"pausar {args.flujo}")
    params = {"tenant": ops.config.tenant, "ally": ops.config.ally, "product": flow["product_ref"],
              "flow": flow["flow_key"], "generation": generation, "who": args.quien, "reason": args.motivo}
    paused = ops.sql_json(INSTANCE_ARGS + _call(
        "operator_pause_flow_v1", ("instance", "jsonb"), ("product", "text"), ("flow", "text"),
        ("generation", "bigint"), ("who", "text"), ("reason", "text")), params)
    ops.show("pausa", paused)
    return EXIT_OK if paused.get("ok") is True else EXIT_BLOCKED


def cmd_registrar(ops: Ops, args: argparse.Namespace) -> int:
    """Registra un flujo nuevo en el almacen: queda apagado y sin version (operator_register_flow_v1).

    Despues va su v1 con ``proponer`` (una ``version`` y un ``prender`` en la misma propuesta).
    """
    ops.check_approver(args.quien)
    match = _FLOW.fullmatch(args.flujo)
    if not match:
        raise Refused("el flujo va como <producto>/<flujo>", EXIT_USAGE)
    world = ops.read_world()
    product, key = match.groups()
    if product not in world["instance"]["products"]:
        raise Refused(f"el producto {product} no tiene un binding activo en esta instancia")
    for flow in world["store"]["flows"]:
        if (flow["product_ref"], flow["flow_key"]) == (product, key):
            raise Refused(f"{args.flujo} ya esta registrado: {flow['flow_id']}")
    ops.say(f"{args.flujo}: se registra apagado y sin version; el almacen no lo ejecuta ni frena nada")
    ops.confirm(f"registrar {args.flujo}")
    params = {"tenant": ops.config.tenant, "ally": ops.config.ally, "product": product, "flow": key,
              "who": args.quien, "reason": args.motivo}
    registered = ops.sql_json(INSTANCE_ARGS + _call(
        "operator_register_flow_v1", ("instance", "jsonb"), ("product", "text"), ("flow", "text"),
        ("who", "text"), ("reason", "text")), params)
    ops.show("registro", registered)
    return EXIT_OK if registered.get("ok") is True else EXIT_BLOCKED


def cmd_ruta(ops: Ops, args: argparse.Namespace) -> int:
    """Crea la ruta de un flujo en ``legado`` (operator_create_flow_route_v1, contrato flow-engine-v1 §3).

    Desde ahi, la guarda del legado lee el estado del flujo en el almacen: pausarlo
    o apagarlo aca frena lo que manda el legado. Por eso el flujo tiene que estar
    prendido: con una ruta sobre un flujo apagado, el legado se frena al instante.
    """
    ops.check_approver(args.quien)
    if args.tipo not in ROUTE_KINDS:
        raise Refused(f"--tipo va como {', '.join(ROUTE_KINDS)}", EXIT_USAGE)
    flow = _find_flow(ops.read_world(), args.flujo)
    if flow["state"] != "prendido" or flow["active_version"] is None:
        raise Refused(f"{args.flujo} esta {flow['state']} (version {flow['active_version']}): con la ruta, el legado "
                      "se frenaria al instante; primero publica su version prendida")
    ops.say(f"{args.flujo}: prendido en la version {flow['active_version']}")
    ops.say(f"la ruta va en legado ({args.tipo}, {args.scope}): el legado sigue mandando lo mismo, pero desde ahora "
            f"pausar o apagar {args.flujo} en el almacen frena al legado")
    ops.confirm(f"crear la ruta de {args.flujo}")
    params = {"tenant": ops.config.tenant, "ally": ops.config.ally, "product": flow["product_ref"],
              "flow": flow["flow_key"], "kind": args.tipo, "scope": args.scope, "who": args.quien,
              "reason": args.motivo}
    created = ops.sql_json(INSTANCE_ARGS + _call(
        "operator_create_flow_route_v1", ("instance", "jsonb"), ("product", "text"), ("flow", "text"),
        ("kind", "text"), ("scope", "text"), ("who", "text"), ("reason", "text")), params)
    ops.show("ruta", created)
    return EXIT_OK if created.get("ok") is True else EXIT_BLOCKED


def _discard(ops: Ops, proposal_id: str, who: str, reason: str) -> dict[str, Any]:
    params = {"tenant": ops.config.tenant, "ally": ops.config.ally, "id": proposal_id, "who": who,
              "reason": reason}
    return ops.sql_json(INSTANCE_ARGS + _call(
        "operator_discard_flow_change_v1", ("instance", "jsonb"), ("id", "uuid"), ("who", "text"),
        ("reason", "text")), params)


def cmd_descartar(ops: Ops, args: argparse.Namespace) -> int:
    ops.check_approver(args.quien)
    if args.todo_del_agente:
        rows = ops.sql_json(SQL_OPEN, {"tenant": ops.config.tenant, "ally": ops.config.ally}, read_only=True)
        targets = [row["id"] for row in rows if row["requested_by_kind"] == "automation_expert"]
        if not targets:
            ops.say("no hay propuestas abiertas del Automation Expert")
            return EXIT_OK
        ops.show("se descartan", targets)
    else:
        if args.propuesta is None:
            raise Refused("falta la propuesta (o --todo-del-agente)", EXIT_USAGE)
        targets = [ops.read_proposal(args.propuesta)["id"]]
    ops.confirm(f"descartar {len(targets)} propuesta(s)")
    worst = EXIT_OK
    for proposal_id in targets:
        result = _discard(ops, proposal_id, args.quien, args.motivo)
        ops.show(proposal_id, result)
        if result.get("ok") is not True:
            worst = EXIT_BLOCKED
    return worst


def _require_uuid(value: str) -> None:
    if not _UUID.fullmatch(value or ""):
        raise Refused("la propuesta va con su id (uuid)", EXIT_USAGE)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="flow_change_operator", description=__doc__.split("\n\n")[0])
    sub = root.add_subparsers(dest="comando", required=True)

    def writer(name: str) -> argparse.ArgumentParser:
        command = sub.add_parser(name)
        command.add_argument("--quien", required=True)
        return command

    sub.add_parser("estado")
    sub.add_parser("listar")
    sub.add_parser("ver").add_argument("propuesta")
    command = writer("proponer")
    command.add_argument("--archivo", required=True)
    command.add_argument("--motivo", required=True)
    writer("validar").add_argument("propuesta")
    command = writer("publicar")
    command.add_argument("propuesta")
    command.add_argument("--sin-simulacion", action="store_true")
    for name in ("volver", "prender", "apagar"):
        command = writer(name)
        command.add_argument("flujo")
        if name == "volver":
            command.add_argument("version", type=int)
        command.add_argument("--motivo", required=True)
        command.add_argument("--sin-simulacion", action="store_true")
    command = writer("pausar")
    command.add_argument("flujo")
    command.add_argument("--motivo", required=True)
    command = writer("registrar")
    command.add_argument("flujo")
    command.add_argument("--motivo", required=True)
    command = writer("ruta")
    command.add_argument("flujo")
    command.add_argument("--tipo", required=True)
    command.add_argument("--scope", required=True)
    command.add_argument("--motivo", required=True)
    command = writer("descartar")
    command.add_argument("propuesta", nargs="?")
    command.add_argument("--todo-del-agente", action="store_true")
    command.add_argument("--motivo", required=True)
    return root


COMMANDS: dict[str, Callable[[Ops, argparse.Namespace], int]] = {
    "estado": cmd_estado, "listar": cmd_listar, "ver": cmd_ver, "proponer": cmd_proponer,
    "validar": cmd_validar, "publicar": cmd_publicar, "volver": cmd_estado_o_version,
    "prender": cmd_estado_o_version, "apagar": cmd_estado_o_version, "pausar": cmd_pausar,
    "descartar": cmd_descartar, "registrar": cmd_registrar, "ruta": cmd_ruta,
}


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None,
         ops: Ops | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if ops is None:
            ops = Ops(Config.from_env(os.environ if env is None else env))
        return COMMANDS[args.comando](ops, args)
    except Refused as refused:
        (ops.out if ops is not None else sys.stdout).write(f"no: {refused}\n")
        return refused.code


if __name__ == "__main__":
    raise SystemExit(main())
