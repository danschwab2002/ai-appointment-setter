#!/usr/bin/env python3
"""El SQL que siembra cada producto de ``[[productos]]`` que no es el principal (E-350).

El aprovisionamiento de la instancia (``despliegue/base/aprovisionar.sql`` del repo de la
instancia) siembra el principal. Cada producto mas lleva sus propias filas: su binding, su
catalogo de ofertas en el scope entrante, un scope de intencion de compra por oferta y la
politica de compra. Las filas salen del manifiesto con el mismo codigo que lee el bridge
(``InstanceManifest.to_commercial_ally_configs``), asi que ``/ready`` las compara igual.

Lo imprime por stdout, para pasarselo a ``psql`` como el dueno de las tablas::

    python3 scripts/provision_instance_product.py --manifiesto instancia.toml --quien <q> \\
        | psql -U postgres -d postgres -v ON_ERROR_STOP=1 --single-transaction

Idempotente: cada insert va con ``on conflict do nothing`` y al final compara lo que quedo
contra lo que declara el manifiesto, abortando la transaccion si algo no coincide (una fila
vieja distinta no se pisa). No toca al principal ni prende nada: los flujos de cada producto
se publican por el camino de siempre (el Automation Expert).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bridge.commercial_ally import CommercialAllyConfig  # noqa: E402
from bridge.instance_manifest import InstanceManifest  # noqa: E402

# Los mismos valores que aprovisionar.sql usa para el principal.
INTENT_LOOKBACK = "24 hours"
PURCHASE_LOOKBACK = "7 days"
CHECKOUT_MODE = 10


def _text(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _text_array(values: Sequence[str]) -> str:
    if not values:
        return "'{}'::text[]"
    return "array[" + ", ".join(_text(value) for value in values) + "]::text[]"


def _landings_json(config: CommercialAllyConfig) -> str:
    payload = [
        {
            "offer_code": landing.offer_code,
            "site": landing.site,
            "landing_id": landing.landing_id,
            "page_host": landing.page_host,
            "page_path": landing.page_path,
        }
        for landing in config.additional_offer_landings
    ]
    return _text(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) + "::jsonb"


def product_sql(config: CommercialAllyConfig, actor: str) -> str:
    """Las filas de un producto que no es el principal, con su verificacion."""

    tenant, funnel = _text(config.tenant_ref), _text(config.funnel_ref)
    scope, scope_version = _text(config.inbound_scope_key), config.inbound_scope_version
    hotlink, who = _text(config.product_hotlink), _text(actor)
    product_id = _text(config.hotmart_product_id)
    extras = list(config.additional_offer_codes)
    landings = {landing.offer_code: landing.landing_id for landing in config.additional_offer_landings}
    offers = [(config.offer_code, config.lead_landing_id, True)] + [
        (code, landings.get(code, config.lead_landing_id), False) for code in extras
    ]
    lines = [
        f"-- El producto {config.funnel_ref}: binding, catalogo, intencion de compra y politica de compra.",
        f"""insert into public.commercial_ally_runtime_bindings (
    tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name, lead_site, lead_landing_id,
    lead_page_host, lead_page_path, product_hotlink, product_name, product_price, currency, offer_code,
    consent_copy_version, hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id, inbound_scope_key,
    inbound_scope_version, additional_offer_codes, additional_offer_landings
) values (
    {tenant}, {funnel}, {config.binding_version}, 'active', {_text(config.ally_ref)}, {_text(config.lead_ally_name)},
    {_text(config.lead_site)}, {_text(config.lead_landing_id)}, {_text(config.lead_page_host)}, {_text(config.lead_page_path)},
    {hotlink}, {_text(config.product_name)}, {_text(config.product_price)}, {_text(config.currency)}, {_text(config.offer_code)},
    {_text(config.consent_copy_version)}, {config.hotmart_product_id}, {config.chatwoot_account_id}, {config.chatwoot_inbox_id},
    {scope}, {scope_version}, {_text_array(extras)}, {_landings_json(config)}
) on conflict do nothing;""",
    ]
    for code, landing, is_default in offers:
        lines.append(f"""insert into public.checkout_offer_catalog (
    tenant_ref, funnel_ref, scope_key, scope_version, product_ref, landing_ref, offer_code, checkout_base_url,
    checkout_mode, default_for_inbound, status, version, approved_by, approved_at
) values (
    {tenant}, {funnel}, {scope}, {scope_version}, {hotlink}, {_text(landing)}, {_text(code)},
    {_text('https://pay.hotmart.com/' + config.product_hotlink)}, {CHECKOUT_MODE}, {str(is_default).lower()}, 'active', 1, {who}, now()
) on conflict do nothing;""")
        lines.append(f"""insert into public.hotmart_purchase_intent_scopes (
    tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref, offer_ref, max_lookback, active
) values (
    {tenant}, {funnel}, {product_id}, {hotlink}, {_text(code)}, interval '{INTENT_LOOKBACK}', true
) on conflict do nothing;""")
    lines.append(f"""insert into public.commercial_ally_hotmart_purchase_policies (
    tenant_ref, funnel_ref, binding_version, enabled, max_lookback
) values (
    {tenant}, {funnel}, {config.binding_version}, true, interval '{PURCHASE_LOOKBACK}'
) on conflict do nothing;""")
    expected_offers = [code for code, _, _ in offers]
    lines.append(f"""do $verificar$
declare
    v_binding public.commercial_ally_runtime_bindings%rowtype;
    v_n bigint;
begin
    select * into v_binding from public.commercial_ally_runtime_bindings
     where tenant_ref = {tenant} and funnel_ref = {funnel} and binding_version = {config.binding_version};
    if not found or v_binding.status <> 'active' or v_binding.hotmart_product_id <> {config.hotmart_product_id}
       or v_binding.product_hotlink <> {hotlink} or v_binding.product_name <> {_text(config.product_name)}
       or v_binding.offer_code <> {_text(config.offer_code)}
       or v_binding.additional_offer_codes <> {_text_array(extras)} then
        raise exception 'provision_instance_product: el binding de {config.funnel_ref} no quedo igual al manifiesto';
    end if;
    select count(*) into v_n from public.checkout_offer_catalog
     where tenant_ref = {tenant} and funnel_ref = {funnel} and scope_key = {scope} and scope_version = {scope_version}
       and offer_code = any({_text_array(expected_offers)}) and status = 'active';
    if v_n <> {len(expected_offers)} then
        raise exception 'provision_instance_product: faltan ofertas de {config.funnel_ref} en el catalogo (%)', v_n;
    end if;
    select count(*) into v_n from public.hotmart_purchase_intent_scopes
     where tenant_ref = {tenant} and funnel_ref = {funnel} and active;
    if v_n <> {len(expected_offers)} then
        raise exception 'provision_instance_product: faltan los scopes de intencion de {config.funnel_ref} (%)', v_n;
    end if;
    perform 1 from public.resolve_commercial_ally_runtime_binding({tenant}, {funnel}, {config.binding_version});
    if not found then
        raise exception 'provision_instance_product: resolve_commercial_ally_runtime_binding no ve {config.funnel_ref}';
    end if;
end
$verificar$;""")
    return "\n".join(lines)


def build_sql(manifest: InstanceManifest, actor: str) -> str:
    """Todo el SQL de los productos que no son el principal, o error si no hay ninguno."""

    configs = manifest.to_commercial_ally_configs()
    extras = configs[1:]
    if not extras:
        raise SystemExit("El manifiesto no declara productos ademas del principal: no hay nada que sembrar.")
    ids = [config.hotmart_product_id for config in configs]
    if len(set(ids)) != len(ids):
        repeated = sorted({product_id for product_id in ids if ids.count(product_id) > 1})
        raise SystemExit(
            "No soportado: dos productos con el mismo producto de Hotmart "
            f"({', '.join(map(str, repeated))}) partido por oferta. La compra, el corte por producto y la "
            "etiqueta de la plantilla necesitan un producto de Hotmart por producto de la instancia. "
            "No se genero nada."
        )
    return "\n\n".join(product_sql(config, actor) for config in extras) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--manifiesto", required=True, type=Path)
    parser.add_argument("--quien", required=True, help="quien lo aprueba (queda en approved_by)")
    args = parser.parse_args(argv)
    manifest = InstanceManifest.from_toml_file(args.manifiesto)
    sys.stdout.write(build_sql(manifest, args.quien))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
