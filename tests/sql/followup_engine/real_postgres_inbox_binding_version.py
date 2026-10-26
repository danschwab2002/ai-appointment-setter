"""The inbox change of an instance (scripts/publish_inbox_binding_version.py), end to end, on a real PostgreSQL.

The script runs on the VPS with ``docker exec ... psql`` against the database of
the instance and, read only, against the database of Chatwoot. Here ``docker``
is a shim that sends ``exec ... psql`` to the local ``psql`` (the instance to
this database, Chatwoot to a second one) and the terminal types the code the
script shows. The whole migration chain is applied; nothing else is replaced.

The instance is the shape of Maribel on 2026-10-25 (read in a read only session
of maribel_db that day): one active binding v1 of ``lancemos``, its inbound
scope v1, its handoff policy v1, its catalog row, its purchase policy, and two
pilot scopes (recovery and first contact) with their controls ``inactive``, all
on the provisional inbox 12 of account 3. The Chatwoot side is the capture
tests/fixtures/inbox_binding_version/chatwoot_inboxes_20261025.json: inbox 12
(Channel::Api, account 3) and inbox 11 (WhatsApp Cloud of ATT1, account 2), plus
two synthetic ones built from those shapes: 13, the WhatsApp inbox of the number
in account 3 (inbox 11 with account 3), and 14, another Channel::Api of
account 3. The values of the business come from the ATT1 fixture
(tests/fixtures/instances/att1): only the inbox and the account matter here.

What it proves, in order:

1. the same inbox, an inbox of another account, a Channel::Api that never had
   the binding and an inbox that does not exist are refused, and nothing changes;
2. with a control ``armed`` it refuses (cases in flight); once ``paused`` it
   goes on, and the activation leaves it ``inactive`` (never armed);
3. dry run (no ``--aplicar``) with inbox 13: it shows the plan and writes
   nothing (every table counted before and after);
4. ``--aplicar`` with a wrong code writes nothing;
5. ``--aplicar`` publishes binding v2 (active, inbox 13; v1 retired), the
   purchase policy v2, inbound scope v2 with its catalog row, handoff policy v2,
   and v2 of both pilot scopes with ``chatwoot-inbox:13``, each control pointing
   to it, ``inactive``; the RPC the bridge reads resolves v2;
6. running it again with 13 is refused (same inbox);
7. going back with inbox 12 (the provisional Channel::Api, which had a binding)
   publishes v3 on inbox 12;
8. every psql call carried its values in the COPY block, never in the SQL text.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "fixtures"
STORE_SPEC = importlib.util.spec_from_file_location(
    "real_postgres_flow_store", ROOT / "tests/sql/followup_engine/real_postgres_flow_store.py")
SCRIPT_SPEC = importlib.util.spec_from_file_location(
    "publish_inbox_binding_version", ROOT / "scripts/publish_inbox_binding_version.py")
assert STORE_SPEC and STORE_SPEC.loader and SCRIPT_SPEC and SCRIPT_SPEC.loader
store = importlib.util.module_from_spec(STORE_SPEC)
STORE_SPEC.loader.exec_module(store)
script = importlib.util.module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = script
SCRIPT_SPEC.loader.exec_module(script)

CAPTURE = json.loads((FIXTURES / "inbox_binding_version/chatwoot_inboxes_20261025.json").read_text(encoding="utf-8"))
MANIFEST = tomllib.loads((FIXTURES / "instances/att1/instancia.toml").read_text(encoding="utf-8"))
PILOT = json.loads((FIXTURES / "instances/att1/politica-piloto.json").read_text(encoding="utf-8"))
TENANT = "lancemos"
FUNNEL = "maribel"
ACCOUNT = 3
OLD_INBOX = 12
NEW_INBOX = 13
ENV = {"CONTENEDOR_DB": "maribel_db", "CONTENEDOR_CHATWOOT_DB": "chatwoot-db", "BASE_CHATWOOT_DB": "infra",
       "CONTENEDOR_BRIDGE": "maribel_appointment-bridge"}
SCOPES = ("maribel-recuperacion", "maribel-primer-contacto")
TABLES = ("commercial_ally_runtime_bindings", "commercial_ally_hotmart_purchase_policies",
          "inbound_commercial_scope_versions", "checkout_offer_catalog", "human_handoff_projection_policies",
          "pilot_scope_versions", "pilot_runtime_controls", "pilot_control_events")


def chatwoot_env() -> dict[str, str]:
    env = store.pg_env()
    env["PGDATABASE"] = env["PGDATABASE"] + "_chatwoot"
    return env


class ShimDocker:
    """``docker``, sin docker: psql local, y un /ready que no se pide en este arnes."""

    def __init__(self) -> None:
        self.scripts: list[str] = []

    def run(self, argv, stdin=""):
        argv = list(argv)
        if argv[:1] == ["ps"]:
            return subprocess.CompletedProcess(argv, 0, argv[2].removeprefix("name=") + "\n", "")
        if argv[:2] == ["exec", "-i"] and argv[3:5] == ["psql", "-X"]:
            container, cleaned, rest = argv[2], [store.PSQL], iter(argv[4:])
            for arg in rest:
                if arg in ("-U", "-d"):
                    next(rest)
                    continue
                cleaned.append(arg)
            self.scripts.append(stdin)
            env = store.pg_env() if container == "maribel_db" else chatwoot_env()
            return subprocess.run(cleaned, input=stdin, text=True, capture_output=True, env=env, check=False)
        raise AssertionError(f"unexpected docker call: {argv[:3]}")


class ShimTerminal:
    def __init__(self) -> None:
        self.wrong_next = False

    def ask(self, prompt: str) -> str:
        code = re.search(r"escribi ([0-9a-f]+)", prompt)
        assert code, prompt
        if self.wrong_next:
            self.wrong_next = False
            return "000000"
        return code.group(1)


def seed() -> None:
    lit = store.literal
    inst, hot, offer = MANIFEST["instancia"], MANIFEST["hotmart"], MANIFEST["hotmart"]["ofertas"][0]
    policy = PILOT["first_contact"]["policy"]
    page = offer["url"].split("://", 1)[1]
    host, path = page.split("/", 1)
    store.query(f"""
      insert into public.commercial_ally_runtime_bindings
        (tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name, lead_site, lead_landing_id,
         lead_page_host, lead_page_path, product_hotlink, product_name, product_price, currency, offer_code,
         consent_copy_version, hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id,
         inbound_scope_key, inbound_scope_version)
      values ({lit(TENANT)}, {lit(FUNNEL)}, 1, 'active', {lit(FUNNEL)}, {lit(inst['marca'])},
        {lit(offer['site'])}, {lit(offer['landing_id'])}, {lit(host)}, {lit('/' + path)},
        {lit(hot['hotlink'])}, {lit(hot['product_name'])}, {lit(hot['precio'])}::numeric, {lit(hot['moneda'])},
        {lit(offer['codigo'])}, {lit(MANIFEST['consentimiento']['copy_version'])}, {hot['product_id']},
        {ACCOUNT}, {OLD_INBOX}, 'maribel-inbound', 1);
      insert into public.commercial_ally_hotmart_purchase_policies (tenant_ref, funnel_ref, binding_version, enabled, max_lookback)
      values ({lit(TENANT)}, {lit(FUNNEL)}, 1, true, '30 days'::interval);
      insert into public.inbound_commercial_scope_versions
        (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id, external_product_id,
         offer_code, approved_by, approved_at, published_at)
      values ('maribel-inbound', 1, 'published', {lit(TENANT)}, {ACCOUNT}, {OLD_INBOX}, {lit(hot['hotlink'])},
        {lit(offer['codigo'])}, 'aprovisionar-maribel-v1', now(), now());
      insert into public.checkout_offer_catalog
        (tenant_ref, funnel_ref, scope_key, scope_version, product_ref, landing_ref, offer_code,
         checkout_base_url, checkout_mode, default_for_inbound, status, version, approved_by, approved_at)
      values ({lit(TENANT)}, {lit(FUNNEL)}, 'maribel-inbound', 1, {lit(hot['hotlink'])}, {lit(offer['landing_id'])},
        {lit(offer['codigo'])}, {lit('https://pay.hotmart.com/' + hot['hotlink'])}, 10, true, 'active', 1,
        'aprovisionar-maribel-v1', now());
      insert into public.human_handoff_projection_policies
        (policy_key, policy_version, expected_team_id, note_template_key, note_template_version,
         private_note_body, active, inbound_scope_key, inbound_scope_version)
      values ('maribel-derivacion-entrante', 1, 3, 'maribel-nota-derivacion', 1,
        'Derivacion automatica del setter.', true, 'maribel-inbound', 1);
      insert into public.followup_policy_versions
        (policy_key, version, status, purpose, timezone, business_windows, grace_period, expires_after,
         max_automatic_messages, steps, approved_by, approved_at, published_at)
      values ({lit(policy['policy_key'])}, {policy['version']}, 'published', {lit(policy['purpose'])},
        {lit(inst['zona_horaria'])}, {lit(policy['business_windows'])}, {lit(policy['grace_period'])}::interval,
        {lit(policy['expires_after'])}::interval, {policy['max_automatic_messages']}, {lit(policy['steps'])},
        'aprovisionar-maribel-v1', now(), now());
    """)
    for key in SCOPES:
        store.query(f"""
          insert into public.pilot_scope_versions
            (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id, channel,
             channel_provider, channel_account_ref, source, source_event_type, additional_source_event_types,
             external_product_id, offer_code, additional_offer_codes, purpose, policy_key, policy_version,
             timezone, max_cohort_contacts, max_outbound_request_starts_total,
             max_outbound_request_starts_per_day, audience_mode, approved_by, approved_at, published_at)
          values ({lit(key)}, 1, 'published', {lit(TENANT)}, {ACCOUNT}, {OLD_INBOX}, 'whatsapp', 'waba',
            {lit(f'chatwoot-inbox:{OLD_INBOX}')}, 'hotmart', 'PURCHASE_OUT_OF_SHOPPING_CART', '{{}}'::text[],
            {lit(str(hot['product_id']))}, {lit(offer['codigo'])}, '{{}}'::text[], 'cart_recovery',
            {lit(policy['policy_key'])}, {policy['version']}, {lit(inst['zona_horaria'])}, 5, 20, 20,
            'manual_cohort', 'aprovisionar-maribel-v1', now(), now());
          insert into public.pilot_runtime_controls (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
          values ({lit(key)}, 1, 'inactive', 0, 'aprovisionar-maribel-v1', 'desarmado');
        """)


def install() -> None:
    # La guarda del arnes del almacen: ALLOW_DISPOSABLE_DATABASE=flow-store y una base vacia que
    # empieza con flow_store (flow_store_inbox_binding_ci en la CI).
    store.install_schema()
    seed()
    chatwoot = chatwoot_env()["PGDATABASE"]
    store.query(f'drop database if exists "{chatwoot}"')
    store.query(f'create database "{chatwoot}"')
    whatsapp = CAPTURE["channel_whatsapp"][0]
    templates = json.dumps(whatsapp["message_templates"], ensure_ascii=True).replace("'", "''")
    inboxes = [*CAPTURE["inboxes"],
               {**CAPTURE["inboxes"][0], "id": NEW_INBOX, "account_id": ACCOUNT, "channel_id": 30},
               {**CAPTURE["inboxes"][1], "id": 14, "channel_id": 31}]
    sql = ["create table inboxes (id integer primary key, account_id integer, channel_type text, "
           "channel_id integer, enable_auto_assignment boolean);",
           "create table channel_whatsapp (id integer primary key, account_id integer, phone_number text, "
           "provider text, message_templates jsonb);",
           f"insert into channel_whatsapp values ({whatsapp['id']}, {whatsapp['account_id']}, '+10000000000', "
           f"'{whatsapp['provider']}', '{templates}'::jsonb);",
           f"insert into channel_whatsapp values (30, {ACCOUNT}, '+10000000001', 'whatsapp_cloud', '{templates}'::jsonb);"]
    for inbox in inboxes:
        sql.append(f"insert into inboxes values ({inbox['id']}, {inbox['account_id']}, '{inbox['channel_type']}', "
                   f"{inbox['channel_id']}, {str(inbox['enable_auto_assignment']).lower()});")
    result = subprocess.run([store.PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1"], input="\n".join(sql) + "\n",
                            text=True, capture_output=True, env=chatwoot_env(), check=False)
    store.require(result.returncode == 0, result.stderr)


def run(ops, *argv: str) -> tuple[int, str]:
    ops.out = io.StringIO()
    code = script.main(list(argv), ops=ops)
    return code, ops.out.getvalue()


def snapshot() -> dict[str, str]:
    return {table: store.query(f"select count(*) || ':' || coalesce(md5(string_agg(t::text, '|' order by t::text)), '') "
                               f"from public.{table} t") for table in TABLES}


def base(inbox: int) -> list[str]:
    return ["--tenant", TENANT, "--inbox-nuevo", str(inbox), "--quien", "dan", "--motivo", "llego el numero"]


def main() -> None:
    install()
    docker, terminal = ShimDocker(), ShimTerminal()
    ops = script.Ops(script.Config.from_env(ENV, needs_bridge=False), docker=docker, terminal=terminal)
    before = snapshot()

    # 1. lo que no sirve se frena sin escribir.
    for inbox, expected in ((OLD_INBOX, "ya tiene el binding"), (11, "es de la cuenta 2"),
                            (14, "no de WhatsApp"), (99, "no existe")):
        code, out = run(ops, *base(inbox))
        store.require(code == 1 and expected in out, f"inbox {inbox}: {code} {out}")
    store.require(snapshot() == before, "a refused inbox changed something")

    # 2. un control armado frena.
    armed = store.query("select runtime_state from public.set_lancemos_pilot_runtime_state("
                        "'maribel-recuperacion', 1, 0, 'armed', 'test', 'armado para la prueba')")
    store.require(armed == "armed", armed)
    code, out = run(ops, *base(NEW_INBOX))
    store.require(code == 1 and "inbox_binding_control_armed" in out, out)
    paused = store.query("select runtime_state from public.set_lancemos_pilot_runtime_state("
                         "'maribel-recuperacion', 1, 1, 'paused', 'test', 'pausado')")
    store.require(paused == "paused", paused)
    before = snapshot()

    # 3. en seco: el plan, y nada escrito.
    code, out = run(ops, *base(NEW_INBOX))
    store.require(code == 0, out)
    for line in ("binding maribel: v1 (retired) -> v2 (active)", "scope entrante maribel-inbound: v1 -> v2",
                 "derivacion maribel-derivacion-entrante: v1 -> v2", "catalogo de ofertas: 1 filas",
                 "scope del piloto maribel-primer-contacto: v1 -> v2", "scope del piloto maribel-recuperacion: v1 -> v2",
                 "politica de compra maribel: copiada de v1 a v2", "CHATWOOT_INBOX_ID=13",
                 "LANCEMOS_PILOT_CHANNEL_ACCOUNT_REF=chatwoot-inbox:13", "CHATWOOT_CUT_B_SCOPE_VERSION=2",
                 "HANDOFF_PROJECTION_POLICY_VERSION=2", "[instancia].binding_version = 2",
                 "[chatwoot].inbox_id = 13", "[inbound].scope_version = 2", "EN SECO"):
        store.require(line in out, f"dry run without {line!r}:\n{out}")
    store.require(snapshot() == before, "the dry run changed something")

    # 4. un codigo equivocado no escribe.
    terminal.wrong_next = True
    code, out = run(ops, *base(NEW_INBOX), "--aplicar")
    store.require(code == 1 and "el codigo no coincide" in out, out)
    store.require(snapshot() == before, "a wrong code changed something")

    # 5. aplicar.
    code, out = run(ops, *base(NEW_INBOX), "--aplicar")
    store.require(code == 0 and "APLICADO" in out, out)
    bindings = store.query("select string_agg(binding_version || ':' || status || ':' || chatwoot_inbox_id || ':' "
                           "|| inbound_scope_version, ',' order by binding_version) "
                           "from public.commercial_ally_runtime_bindings")
    store.require(bindings == "1:retired:12:1,2:active:13:2", bindings)
    resolved = store.query("select chatwoot_inbox_id from public.resolve_commercial_ally_runtime_binding("
                           "'lancemos', 'maribel', 2)")
    store.require(resolved == "13", resolved)
    store.require(store.query("select count(*) from public.commercial_ally_hotmart_purchase_policies "
                              "where binding_version = 2 and enabled") == "1", "purchase policy v2")
    store.require(store.query("select chatwoot_inbox_id || ':' || status from public.inbound_commercial_scope_versions "
                              "where scope_key = 'maribel-inbound' and version = 2") == "13:published", "inbound v2")
    store.require(store.query("select count(*) from public.checkout_offer_catalog where scope_key = 'maribel-inbound' and scope_version = 2 "
                              "and status = 'active' and default_for_inbound") == "1", "catalog v2")
    store.require(store.query("select inbound_scope_version || ':' || active from public.human_handoff_projection_policies "
                              "where policy_key = 'maribel-derivacion-entrante' and policy_version = 2") == "2:true",
                  "handoff v2")
    for key, state in zip(SCOPES, ("inactive", "inactive"), strict=True):
        row = store.query(f"select c.scope_version || ':' || c.runtime_state || ':' || s.channel_account_ref "
                          f"from public.pilot_runtime_controls c join public.pilot_scope_versions s "
                          f"on s.scope_key = c.scope_key and s.version = c.scope_version where c.scope_key = '{key}'")
        store.require(row == f"2:{state}:chatwoot-inbox:13", f"{key}: {row}")

    # 6. otra vez con el mismo inbox: frena.
    code, out = run(ops, *base(NEW_INBOX), "--aplicar")
    store.require(code == 1 and "ya tiene el binding" in out, out)

    # 7. volver a la bandeja provisoria.
    code, out = run(ops, *base(OLD_INBOX), "--aplicar")
    store.require(code == 0 and "binding maribel: v2 (retired) -> v3 (active)" in out, out)
    store.require(store.query("select chatwoot_inbox_id || ':' || inbound_scope_version from "
                              "public.commercial_ally_runtime_bindings where status = 'active'") == "12:3", "v3")

    # 8. los valores fueron por COPY, nunca en el texto del SQL.
    for sql in docker.scripts:
        body = sql.split("\\.\n", 1)[1]
        for literal in ("'lancemos'", "llego el numero", "'dan'"):
            store.require(literal not in body, f"{literal} in the SQL text")
    print("inbox_binding_version_real_postgres=OK")


if __name__ == "__main__":
    main()
