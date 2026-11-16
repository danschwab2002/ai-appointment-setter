"""Publicar con siembra, pausar, cerrar y descartar el producto del AE; una campana viva por producto (E-575, MP-08).

La forma de 20261115000200 (lo que hace de verdad lo prueba ``validate_agent_products_publish.mjs`` en PGlite)
y la ruta de descartar del AE sobre el cliente real (``ProductStore``) con PostgREST emulado.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
from test_agent_product_routes import ASKER, PREFIX, TOKEN, Db, _ae, _app, _card

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261115000200_agent_products_publish_v1.sql"
PROVISION = ROOT / "scripts" / "provision_instance_product.py"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _function(name: str) -> str:
    sql = _sql()
    start = re.search(rf"create (?:or replace )?function public\.{name}\(", sql)
    assert start is not None, name
    return sql[start.start():sql.index("$function$;", start.start())]


# ------------------------------------------------------------------ la migracion


def test_it_is_one_transaction_and_is_the_last() -> None:
    sql = _sql()
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    assert names[-3] == MIGRATION.name
    assert names[-4] == "20261115000100_agent_products_v1.sql"
    assert sql.count("\nbegin;\n") == 1 and sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s';" in sql
    assert "agent_products_publish_already_applied" in sql and "agent_products_publish_prerequisite_missing" in sql


def test_it_only_rewrites_the_two_campaign_functions_it_names() -> None:
    replaced = re.findall(r"create or replace function public\.([a-z0-9_]+)\(", _sql())
    assert replaced == ["approver_publish_agent_campaign_v1", "approver_resume_agent_campaign_v1"]
    drops = re.findall(r"\bdrop (index|constraint|table|function) ([a-z0-9_.]+)", _sql())
    assert drops == [("constraint", "agent_products_closed_by_kind_check"), ("index", "public.agent_campaigns_one_live")]


def test_one_live_campaign_per_product() -> None:
    sql = _sql()
    assert ("create unique index agent_campaigns_one_live\n"
            "    on public.agent_campaigns (tenant_ref, ally_ref, live_scope) where status = 'publicada';") in sql
    # Sin productos del AE el lugar es siempre '' (el de hoy).
    assert "add column live_scope text not null default ''" in sql
    scope = _function("_agent_campaign_live_scope")
    assert "from public.agent_products p" in scope and "p.status <> 'descartada'" in scope
    publish = _function("approver_publish_agent_campaign_v1")
    assert "c.live_scope = v_scope and c.id <> v_campaign.id" in publish
    assert "live_scope = v_scope" in publish.split("set status = 'publicada'", 1)[1]
    resume = _function("approver_resume_agent_campaign_v1")
    # El barrido de las vencidas se queda en su lugar: no termina la viva de otro producto.
    loop = resume.split("for v_other in", 1)[1].split("loop", 1)[0]
    assert "c.live_scope = v_scope" in loop


def test_publishing_seeds_what_provision_instance_product_prints() -> None:
    seed = _function("_agent_product_seed")
    for table in ("commercial_ally_runtime_bindings", "checkout_offer_catalog", "hotmart_purchase_intent_scopes",
                  "commercial_ally_hotmart_purchase_policies"):
        assert f"insert into public.{table}" in seed, table
        assert f"insert into public.{table}" in PROVISION.read_text(encoding="utf-8"), table
    # Los mismos valores que el script y que aprovisionar.sql.
    script = PROVISION.read_text(encoding="utf-8")
    assert 'INTENT_LOOKBACK = "24 hours"' in script and "interval '24 hours', true" in seed
    assert 'PURCHASE_LOOKBACK = "7 days"' in script and "interval '7 days'" in seed
    assert "CHECKOUT_MODE = 10" in script and "'https://pay.hotmart.com/' || v_hotlink, 10" in seed
    # El precio y la moneda del binding son los de la oferta por defecto del minimo.
    assert "(v_default ->> 'precio')::numeric, v_default ->> 'moneda'" in seed
    publish = _function("approver_publish_agent_product_v1")
    assert publish.index("_agent_product_seed_conflict") < publish.index("_agent_product_seed(v_product")
    assert publish.index("_agent_product_seed(v_product") < publish.index("set status = 'publicada'")
    assert "_agent_product_fingerprint(v_product.id" in publish and "_agent_product_expire_if_due" in publish


def test_pause_and_close_pause_flows_and_campaigns_first() -> None:
    for name in ("approver_pause_agent_product_v1", "approver_close_agent_product_v1"):
        body = _function(name)
        assert body.index("_agent_product_pause_dependents") < body.index("update public.agent_products"), name
    dependents = _function("_agent_product_pause_dependents")
    assert "public._flow_change_pause(" in dependents and "c.state = 'prendido'" in dependents
    assert "c.live_scope = p_product.product_key" in dependents
    assert "_agent_product_pause_dependents" not in _function("approver_resume_agent_product_v1")


def test_each_public_function_has_only_its_role() -> None:
    acl = _sql().split("do $acl$", 1)[1]
    ae = acl.split("to_regrole('automation_expert_change')", 1)[1].split("end if;", 1)[0]
    assert re.findall(r"public\.([a-z0-9_]+)\(", ae) == ["discard_agent_product_v1"]
    approver = acl.split("to_regrole('flow_change_approver')", 1)[1].split("end if;", 1)[0]
    assert sorted(re.findall(r"public\.([a-z0-9_]+)\(", approver)) == sorted([
        "approver_get_agent_product_v1", "approver_publish_agent_product_v1", "approver_pause_agent_product_v1",
        "approver_resume_agent_product_v1", "approver_close_agent_product_v1", "approver_discard_agent_product_v1"])
    service = acl.split("to_regrole('service_role') is not null then", 1)[1].split("end if;", 1)[0]
    assert re.findall(r"public\.([a-z0-9_]+)\(", service) == ["get_live_agent_products_v1"]


def test_the_validator_is_last_in_the_npm_chain() -> None:
    chain = json.loads((ROOT / "tests/sql/followup_engine/package.json").read_text(encoding="utf-8"))["scripts"]["test"]
    assert chain.endswith("node validate_agent_products.mjs && node validate_agent_products_publish.mjs && node validate_flow_audience_reach.mjs && node validate_permission_registry.mjs")


# ------------------------------------------------------------------ descartar, la ruta del AE


class DiscardDb(Db):
    """El emulador de E-569 mas descartar de 20261115000200."""

    def _discard_agent_product_v1(self, body: dict) -> httpx.Response:
        row = self.rows.get(body["p_product_id"])
        if row is None:
            return httpx.Response(400, json={"code": "P0002", "message": "agent_product_not_found"})
        assert body["p_requested_by_ref"] == ASKER["slack_user"] and body["p_reason"]
        if row["status"] == "descartada":
            return httpx.Response(200, json={"ok": True, "replayed": True})
        if row["status"] not in ("borrador", "validada"):
            return httpx.Response(200, json={"ok": False, "reason_code": "agent_product_final",
                                             "status": row["status"]})
        row["status"] = "descartada"
        return httpx.Response(200, json={"ok": True, "replayed": False})


def _saved(client: Any) -> str:
    response = _ae(client, "POST", "", {"product": _card(), "purpose": "Sumar", "requested_by": ASKER})
    assert response.status_code == 201, response.text
    return response.json()["data"]["product_id"]


def test_the_agent_discards_its_draft_and_repeating_is_replayed() -> None:
    db = DiscardDb()
    client, _ = _app(db=db)
    product_id = _saved(client)

    response = _ae(client, "POST", f"/{product_id}/discard", {"reason": "me equivoque de producto",
                                                              "requested_by": ASKER})
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"product_id": product_id, "status": "descartada", "replayed": False}
    again = _ae(client, "POST", f"/{product_id}/discard", {"reason": "otra vez", "requested_by": ASKER})
    assert again.status_code == 200 and again.json()["data"]["replayed"] is True
    assert db.calls.count("discard_agent_product_v1") == 2


def test_the_agent_cannot_discard_what_was_published() -> None:
    db = DiscardDb()
    client, _ = _app(db=db)
    product_id = _saved(client)
    db.rows[product_id]["status"] = "publicada"

    response = _ae(client, "POST", f"/{product_id}/discard", {"reason": "no va", "requested_by": ASKER})
    assert response.status_code == 409 and response.json()["detail"] == "agent_product_final"


def test_discarding_needs_the_key_a_reason_without_identifiers_and_a_known_product() -> None:
    db = DiscardDb()
    client, _ = _app(db=db)
    product_id = _saved(client)
    calls = len(db.calls)

    assert _ae(client, "POST", f"/{product_id}/discard", {"reason": "no", "requested_by": ASKER},
               token="z" * 48).status_code == 401
    assert _ae(client, "POST", f"/{product_id}/discard", {"requested_by": ASKER}).status_code == 422
    assert _ae(client, "POST", f"/{product_id}/discard", {"reason": "  ", "requested_by": ASKER}).status_code == 422
    phone = _ae(client, "POST", f"/{product_id}/discard", {"reason": "llamar al 5512345678", "requested_by": ASKER})
    assert phone.status_code == 422 and "identifier_not_allowed" in phone.text
    assert _ae(client, "POST", "/no-es-un-uuid/discard", {"reason": "no", "requested_by": ASKER}).status_code == 422
    assert len(db.calls) == calls
    missing = _ae(client, "POST", "/00000000-0000-4000-8000-000000000000/discard",
                  {"reason": "no", "requested_by": ASKER})
    assert missing.status_code == 404


def test_the_route_exists_only_where_the_others_do() -> None:
    client, _ = _app()
    paths = {route.path for route in client.app.routes}
    assert f"{PREFIX}/{{product_id}}/discard" in paths
    assert TOKEN  # la misma clave que las demas rutas del AE
