"""Adding offers to a running instance (scripts/add_instance_offers.py), end to end, on a real PostgreSQL.

The script runs on the VPS with ``docker exec ... psql`` against the database of
the instance. Here ``docker`` is a shim that sends ``exec ... psql`` to the
local ``psql``, answers the ``/ready`` probe with what the test sets, and the
terminal types the code the script shows. The whole migration chain is applied;
nothing else is replaced.

The instance is the shape of Maribel on 2026-10-28: the manifest is
tests/fixtures/instances/maribel/instancia.toml (setter-instancia-maribel,
origin/main 4b07f72), the binding v2 (active, one offer u8ikyx78; v1 retired),
its intent scope and purchase policy, the inbound scope v2 with its catalog row,
and three pilot scopes of the product with their controls: maribel-recuperacion
v4 (armed), maribel-primer-contacto v4 (inactive) and maribel-pago-rechazado v1
(armed). The new manifests are that file with ``[[hotmart.ofertas]]`` blocks
appended, as the instance PR does.

What it proves, in order:

1. an offer that the manifest does not declare, the default offer, and a
   manifest that differs from the base in something else (the price) are
   refused, and nothing changes;
2. dry run with mmliigg5: it shows the plan, predicts the ``/ready`` and writes
   nothing (every table counted before and after);
3. ``--aplicar`` with a wrong code writes nothing;
4. ``--aplicar`` completes the same binding row (still v2, still active), adds
   the intent scope, the catalog row and the offer to the three pilot scopes in
   their versions; no control changes version, state or generation;
5. running it again does nothing;
6. a second manifest with s4y32uzp and o11h7tn3 too adds those two, after
   mmliigg5, in one transaction;
7. ``--verificar`` gives VERIFICADO against the new manifest, NO VERIFICADO
   against a manifest the base does not have, and with ``CONTENEDOR_BRIDGE`` it
   reads the ``/ready`` of the running bridge (200 VERIFICADO, 503 not);
8. every psql call carried its values in the COPY block, never in the SQL text.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STORE_SPEC = importlib.util.spec_from_file_location(
    "real_postgres_flow_store", ROOT / "tests/sql/followup_engine/real_postgres_flow_store.py")
SCRIPT_SPEC = importlib.util.spec_from_file_location(
    "add_instance_offers", ROOT / "scripts/add_instance_offers.py")
assert STORE_SPEC and STORE_SPEC.loader and SCRIPT_SPEC and SCRIPT_SPEC.loader
store = importlib.util.module_from_spec(STORE_SPEC)
STORE_SPEC.loader.exec_module(store)
script = importlib.util.module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = script
SCRIPT_SPEC.loader.exec_module(script)

MANIFEST = (ROOT / "tests/fixtures/instances/maribel/instancia.toml").read_text(encoding="utf-8")
ENV = {"CONTENEDOR_DB": "maribel_db"}
TABLES = ("commercial_ally_runtime_bindings", "hotmart_purchase_intent_scopes", "checkout_offer_catalog",
          "pilot_scope_versions", "pilot_runtime_controls", "pilot_control_events",
          "commercial_ally_hotmart_purchase_policies")
OFFERS = {
    "mmliigg5": ("epma-vsl-org-a", "https://www.elprotocolo.net/epma/evg/vsl/org-a", "organico"),
    "s4y32uzp": ("epma-ret-ads-a1", "https://www.elprotocolo.net/epma/evg/ret/ads-a1", "pauta"),
    "o11h7tn3": ("epma-ret-ads-a2", "https://www.elprotocolo.net/epma/evg/ret/ads-a2", "pauta"),
}


def manifest_with(*codes: str, text: str = MANIFEST) -> Path:
    blocks = [text.replace('\n[chatwoot]\n', '\n' + "".join(
        f'[[hotmart.ofertas]]\ncodigo = "{code}"\nsite = "elprotocolo"\nlanding_id = "{OFFERS[code][0]}"\n'
        f'url = "{OFFERS[code][1]}"\norigen = "{OFFERS[code][2]}"\n\n' for code in codes) + '[chatwoot]\n', 1)]
    handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8")
    handle.write(blocks[0])
    handle.close()
    return Path(handle.name)


class ShimDocker:
    """``docker``, sin docker: psql local, y un /ready con el codigo que pone la prueba."""

    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.ready_code = 200

    def run(self, argv, stdin=""):
        argv = list(argv)
        if argv[:1] == ["ps"]:
            return subprocess.CompletedProcess(argv, 0, argv[2].removeprefix("name=") + "\n", "")
        if argv[:2] == ["exec", "-i"] and argv[3:5] == ["psql", "-X"]:
            cleaned, rest = [store.PSQL], iter(argv[4:])
            for arg in rest:
                if arg in ("-U", "-d"):
                    next(rest)
                    continue
                cleaned.append(arg)
            self.scripts.append(stdin)
            return subprocess.run(cleaned, input=stdin, text=True, capture_output=True, env=store.pg_env(),
                                  check=False)
        if argv[:2] == ["exec", "-i"] and argv[3:5] == ["python", "-"]:
            ready = {"status": "ready", "commercial_ally_binding": "active"} if self.ready_code == 200 else {
                "detail": "commercial_ally_binding_unavailable"}
            return subprocess.CompletedProcess(argv, 0, json.dumps({"code": self.ready_code, "ready": ready}) + "\n", "")
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
    common = ("tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name, lead_site, lead_landing_id, "
              "lead_page_host, lead_page_path, product_hotlink, product_name, product_price, currency, offer_code, "
              "consent_copy_version, hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id, "
              "inbound_scope_key, inbound_scope_version")
    values = ("'lancemos', 'maribel', {v}, {s}, 'maribel', 'El Protocolo', 'elprotocolo', 'epma-vsl-ads-a', "
              "'www.elprotocolo.net', '/epma/evg/vsl/ads-a', 'F107441770M', 'El Protocolo Biohacking +', 1397, 'MXN', "
              "'u8ikyx78', 'maribel-whatsapp-contact-v1', 8443991, 3, {inbox}, 'maribel-inbound', {inbound}")
    store.query(f"""
      insert into public.commercial_ally_runtime_bindings ({common}) values
        ({values.format(v=1, s=lit('retired'), inbox=12, inbound=1)}),
        ({values.format(v=2, s=lit('active'), inbox=13, inbound=2)});
      insert into public.commercial_ally_hotmart_purchase_policies (tenant_ref, funnel_ref, binding_version, enabled, max_lookback)
      values ('lancemos', 'maribel', 1, true, '7 days'), ('lancemos', 'maribel', 2, true, '7 days');
      insert into public.hotmart_purchase_intent_scopes
        (tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref, offer_ref, max_lookback, active)
      values ('lancemos', 'maribel', '8443991', 'F107441770M', 'u8ikyx78', '24 hours', true);
      insert into public.inbound_commercial_scope_versions
        (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id, external_product_id,
         offer_code, approved_by, approved_at, published_at)
      values ('maribel-inbound', 1, 'published', 'lancemos', 3, 12, 'F107441770M', 'u8ikyx78', 'aprovisionar', now(), now()),
             ('maribel-inbound', 2, 'published', 'lancemos', 3, 13, 'F107441770M', 'u8ikyx78', 'inbox-binding:dan', now(), now());
      insert into public.checkout_offer_catalog
        (tenant_ref, funnel_ref, scope_key, scope_version, product_ref, landing_ref, offer_code,
         checkout_base_url, checkout_mode, default_for_inbound, status, version, approved_by, approved_at)
      values ('lancemos', 'maribel', 'maribel-inbound', 2, 'F107441770M', 'epma-vsl-ads-a', 'u8ikyx78',
              'https://pay.hotmart.com/F107441770M', 10, true, 'active', 1, 'inbox-binding:dan', now());
      insert into public.followup_policy_versions
        (policy_key, version, status, purpose, timezone, business_windows, grace_period, expires_after,
         max_automatic_messages, steps, approved_by, approved_at, published_at)
      values ('maribel-recuperacion-un-toque', 1, 'published', 'cart_recovery', 'America/Mexico_City',
              '[{{"days":[1,2,3,4,5,6,7],"start":"09:00","end":"21:00"}}]'::jsonb, '0 seconds', '1 day', 1,
              '[{{"mode":"freeform","step_key":"first_contact"}}]'::jsonb, 'aprovisionar', now(), now());
    """.replace("{{", "{").replace("}}", "}"))
    scopes = (("maribel-recuperacion", 4, "hotmart", "PURCHASE_OUT_OF_SHOPPING_CART", "manual_cohort"),
              ("maribel-primer-contacto", 4, "landing", "PRECHECKOUT_FORM_SUBMITTED", "consented_intent_in_cohort"),
              ("maribel-pago-rechazado", 1, "hotmart", "PURCHASE_CANCELED", "manual_cohort"))
    for key, last, source, event, mode in scopes:
        for version in range(1, last + 1):
            store.query(f"""
              insert into public.pilot_scope_versions
                (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id, channel,
                 channel_provider, channel_account_ref, source, source_event_type, additional_source_event_types,
                 external_product_id, offer_code, additional_offer_codes, purpose, policy_key, policy_version,
                 timezone, max_cohort_contacts, max_outbound_request_starts_total,
                 max_outbound_request_starts_per_day, audience_mode, approved_by, approved_at, published_at)
              values ({lit(key)}, {version}, 'published', 'lancemos', 3, 13, 'whatsapp', 'waba', 'chatwoot-inbox:13',
                {lit(source)}, {lit(event)}, '{{}}'::text[], '8443991', 'u8ikyx78', '{{}}'::text[], 'cart_recovery',
                'maribel-recuperacion-un-toque', 1, 'America/Mexico_City', 20, 3000, 150, {lit(mode)},
                'aprovisionar', now(), now());""")
        store.query(f"""insert into public.pilot_runtime_controls
            (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
            values ({lit(key)}, {last}, 'inactive', 0, 'aprovisionar', 'desarmado')""")
    for key, last in (("maribel-recuperacion", 4), ("maribel-pago-rechazado", 1)):
        state = store.query(f"select runtime_state from public.set_lancemos_pilot_runtime_state("
                            f"{lit(key)}, {last}, 0, 'armed', 'test', 'armado como en Maribel')")
        store.require(state == "armed", state)


def run(ops, *argv: str) -> tuple[int, str]:
    ops.out = io.StringIO()
    code = script.main(list(argv), ops=ops)
    return code, ops.out.getvalue()


def snapshot() -> dict[str, str]:
    return {table: store.query(f"select count(*) || ':' || coalesce(md5(string_agg(t::text, '|' order by t::text)), '') "
                               f"from public.{table} t") for table in TABLES}


def controls() -> str:
    return store.query("select string_agg(scope_key || ':' || scope_version || ':' || runtime_state || ':' || generation,"
                       " ',' order by scope_key) from public.pilot_runtime_controls")


def main() -> None:
    store.install_schema()
    seed()
    docker, terminal = ShimDocker(), ShimTerminal()
    ops = script.Ops(script.Config.from_env(ENV), docker=docker, terminal=terminal)
    one = manifest_with("mmliigg5")
    three = manifest_with("mmliigg5", "s4y32uzp", "o11h7tn3")
    base = ["--quien", "dan"]
    before, controls_before = snapshot(), controls()

    # 1. lo que no es sumar una oferta se frena sin escribir.
    code, out = run(ops, "--manifiesto", str(one), "--ofertas", "zzzz9999", *base)
    store.require(code == 1 and "no esta en el manifiesto" in out, out)
    code, out = run(ops, "--manifiesto", str(one), "--ofertas", "u8ikyx78", *base)
    store.require(code == 1 and "oferta por defecto" in out, out)
    other_price = manifest_with("mmliigg5", text=MANIFEST.replace('precio = "1397"', 'precio = "1497"', 1))
    code, out = run(ops, "--manifiesto", str(other_price), "--ofertas", "mmliigg5", *base)
    store.require(code == 1 and "difiere en product_price" in out, out)
    store.require(snapshot() == before, "a refusal changed something")

    # 2. en seco.
    code, out = run(ops, "--manifiesto", str(one), "--ofertas", "mmliigg5", *base)
    store.require(code == 0, out)
    for line in ("binding maribel v2 (la misma fila, sigue active): additional_offer_codes [] -> ['mmliigg5']",
                 "scope de intencion nuevo: oferta mmliigg5, activo, max_lookback 24:00:00",
                 "catalogo: fila nueva de mmliigg5 (landing epma-vsl-org-a) en maribel-inbound v2, no por defecto",
                 "scope del piloto maribel-recuperacion v4 (la misma version): suma ['mmliigg5'] a []; "
                 "control armed, generacion 1, sin tocar",
                 "scope del piloto maribel-primer-contacto v4 (la misma version)",
                 "scope del piloto maribel-pago-rechazado v1 (la misma version)",
                 "Carrito frenado: 0 s.", "EN SECO",
                 "el bridge daria /ready 200 con commercial_ally_binding=active"):
        store.require(line in out, f"dry run without {line!r}:\n{out}")
    store.require(snapshot() == before, "the dry run changed something")

    # 3. un codigo equivocado no escribe.
    terminal.wrong_next = True
    code, out = run(ops, "--manifiesto", str(one), "--ofertas", "mmliigg5", *base, "--aplicar")
    store.require(code == 1 and "el codigo no coincide" in out, out)
    store.require(snapshot() == before, "a wrong code changed something")

    # 4. aplicar.
    code, out = run(ops, "--manifiesto", str(one), "--ofertas", "mmliigg5", *base, "--aplicar")
    store.require(code == 0 and "APLICADO: commit. La transaccion duro" in out, out)
    store.require(store.query("select string_agg(binding_version || ':' || status, ',' order by binding_version) "
                              "from public.commercial_ally_runtime_bindings") == "1:retired,2:active", "bindings")
    store.require(store.query("select array_to_string(additional_offer_codes, ',') || ':' "
                              "|| (additional_offer_landings -> 0 ->> 'page_path') from public.resolve_commercial_ally_runtime_binding"
                              "('lancemos', 'maribel', 2)") == "mmliigg5:/epma/evg/vsl/org-a", "binding row")
    store.require(store.query("select string_agg(scope_key || '/' || version, ',' order by scope_key) "
                              "from public.pilot_scope_versions where additional_offer_codes = '{mmliigg5}'")
                  == "maribel-pago-rechazado/1,maribel-primer-contacto/4,maribel-recuperacion/4", "pilot scopes")
    store.require(controls() == controls_before, f"a control changed: {controls()}")

    # 5. otra vez: nada.
    after = snapshot()
    code, out = run(ops, "--manifiesto", str(one), "--ofertas", "mmliigg5", *base, "--aplicar")
    store.require(code == 0 and "nada que hacer" in out, out)
    store.require(snapshot() == after, "a second run changed something")

    # 6. las otras dos, juntas, despues de mmliigg5.
    code, out = run(ops, "--manifiesto", str(three), "--ofertas", "mmliigg5,s4y32uzp,o11h7tn3", *base, "--aplicar")
    store.require(code == 0 and "['mmliigg5'] -> ['mmliigg5', 's4y32uzp', 'o11h7tn3']" in out, out)
    store.require(store.query("select count(*) from public.pilot_scope_versions "
                              "where additional_offer_codes = '{mmliigg5,s4y32uzp,o11h7tn3}'") == "3", "three offers")
    store.require(store.query("select count(*) from public.checkout_offer_catalog where status = 'active' "
                              "and scope_key = 'maribel-inbound' and scope_version = 2") == "4", "catalog")
    store.require(controls() == controls_before, f"a control changed: {controls()}")

    # 7. verificar.
    code, out = run(ops, "--manifiesto", str(three), "--ofertas", "mmliigg5,s4y32uzp,o11h7tn3", "--verificar")
    store.require(code == 0 and out.rstrip().endswith("VERIFICADO") and "prediccion" in out, out)
    code, out = run(ops, "--manifiesto", str(other_price), "--ofertas", "mmliigg5", "--verificar")
    store.require(code == 1 and "difiere del manifiesto en product_price" in out and "NO VERIFICADO" in out, out)
    with_bridge = script.Ops(script.Config.from_env({**ENV, "CONTENEDOR_BRIDGE": "maribel_appointment-bridge"}),
                             docker=docker, terminal=terminal)
    code, out = run(with_bridge, "--manifiesto", str(three), "--ofertas", "mmliigg5,s4y32uzp,o11h7tn3", "--verificar")
    store.require(code == 0 and "/ready del bridge que corre: 200" in out, out)
    docker.ready_code = 503
    code, out = run(with_bridge, "--manifiesto", str(three), "--ofertas", "mmliigg5", "--verificar")
    store.require(code == 1 and "NO VERIFICADO" in out, out)

    # 8. los valores fueron por COPY, nunca en el texto del SQL.
    for sql in docker.scripts:
        body = sql.split("\\.\n", 1)[1]
        for literal in ("lancemos", "mmliigg5", "'dan'", "epma-vsl-org-a"):
            store.require(literal not in body, f"{literal} in the SQL text")
    for path in (one, three, other_price):
        path.unlink()
    print("add_instance_offers_real_postgres=OK")


if __name__ == "__main__":
    main()
