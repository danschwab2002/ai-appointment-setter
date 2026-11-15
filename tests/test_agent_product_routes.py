"""Las rutas del AE para un producto nuevo y su flag (product-card-v1, E-569, MP-07).

El cliente real de la base (``ProductStore`` sobre
PostgREST emulado). El emulador sigue lo que le importa a las rutas de
20261115000100 (el borrador, la version, la revision, el congelado y ``taken``);
lo que la base hace de verdad lo prueba PGlite (``validate_agent_products.mjs``).

Las rutas sobre una app minima (``register_agent_product_routes``); el flag en ``create_app`` va con el
cableado de ``app.py`` (ver el PR).
"""

from __future__ import annotations

import base64
import copy
import json
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from test_agent_products import ATT1, FY

from bridge import agent_product_api
from bridge.flow_change_api import EntryRefused

PREFIX = "/internal/automation-expert/v1/products"
TOKEN = "p" * 48


def _jwt(claims: dict) -> str:
    part = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{part({'alg': 'RS256', 'kid': 'k'})}.{part(claims)}.firma"


AE_KEY = _jwt({"role": "automation_expert_change"})
CHANGE = {"automation_expert_change_enabled": True, "automation_expert_change_token": TOKEN,
          "automation_expert_change_db_key": AE_KEY, "automation_expert_read_enabled": True,
          "automation_expert_read_token": "r" * 48, "chatwoot_account_id": 2, "chatwoot_inbox_id": 11}


class Db:
    """Las cuatro funciones del agente de 20261115000100, con lo que les importa a las rutas."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.rows: dict[str, dict[str, Any]] = {}
        self.taken_extra: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {AE_KEY}"
        name = request.url.path.rsplit("/", 1)[1]
        self.calls.append(name)
        body = json.loads(request.content)
        assert body["p_instance"] == {"tenant_ref": "lancemos", "ally_ref": "att1"}
        return getattr(self, "_" + name)(body)

    def _save_agent_product_draft_v1(self, body: dict) -> httpx.Response:
        if body["p_product_id"] is None:
            key = body["p_minimum"]["clave"]
            version = 1 + max((row["product_version"] for row in self.rows.values() if row["product_key"] == key),
                              default=0)
            product_id = str(uuid.uuid4())
            self.rows[product_id] = {"product_id": product_id, "status": "borrador", "revision": 1,
                                     "product_key": key, "product_version": version}
        else:
            product_id = body["p_product_id"]
            row = self.rows[product_id]
            if row["revision"] != body["p_expected_revision"]:
                return httpx.Response(200, json={"ok": False, "reason_code": "agent_product_revision_mismatch"})
            row["revision"] += 1
            row["status"] = "borrador"
        row = self.rows[product_id]
        row.update({"minimo": body["p_minimum"], "texto_libre": body["p_free_text"],
                    "advertencias": body["p_warnings"], "purpose": body["p_purpose"]})
        return httpx.Response(200, json={"ok": True, "product_id": product_id, "revision": row["revision"],
                                         "product_version": row["product_version"], "status": "borrador"})

    def _record_agent_product_validation_v1(self, body: dict) -> httpx.Response:
        row = self.rows[body["p_product_id"]]
        assert body["p_validator"]["contract"] == "product-card-v1"
        row["validation"] = {"validator": body["p_validator"], "result": body["p_result"],
                             "preview": body["p_preview"]}
        if body["p_result"]["blocks"]:
            return httpx.Response(200, json={"ok": True, "status": "borrador",
                                             "blocks": len(body["p_result"]["blocks"])})
        row.update({"status": "validada", "fingerprint": "f" * 64, "advertencias": body["p_result"]["warnings"]})
        return httpx.Response(200, json={"ok": True, "status": "validada", "fingerprint": "f" * 64,
                                         "expires_at": "2026-11-16T03:00:00Z"})

    def _get_agent_product_v1(self, body: dict) -> httpx.Response:
        row = self.rows.get(body["p_product_id"])
        if row is None:
            return httpx.Response(400, json={"code": "P0002", "message": "agent_product_not_found"})
        return httpx.Response(200, json=copy.deepcopy(row))

    def _list_agent_products_v1(self, body: dict) -> httpx.Response:
        taken = [{"product_id": row["product_id"], "product_key": row["product_key"], "status": row["status"],
                  "nombre": row["minimo"]["nombre"], "alias": row["minimo"]["alias"],
                  "hotmart_product_id": row["minimo"]["hotmart"]["producto_id"],
                  "offer_codes": [offer["codigo"] for offer in row["minimo"]["ofertas"]],
                  "incluye_productos": row["minimo"]["relaciones"]["incluye_productos"]}
                 for row in self.rows.values()] + self.taken_extra
        products = [{"product_id": row["product_id"], "status": row["status"], "product_key": row["product_key"]}
                    for row in self.rows.values()][: body["p_limit"]]
        return httpx.Response(200, json={"products": products, "taken": taken, "open": len(self.rows),
                                         "open_limit": 5, "database_at": "2026-11-15T03:00:00Z"})


@pytest.fixture(autouse=True)
def _image_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETTER_VERSION", "v1.39.0")
    monkeypatch.setenv("GIT_SHA", "57e9792")


def _app(*, db: Db | None = None) -> tuple[TestClient, Db]:
    """Las rutas sobre una app minima, como las registra create_app con el flag prendido."""
    db = db or Db()
    store = agent_product_api.ProductStore(base_url="https://att1-rest.internal", db_key=AE_KEY,
                                           transport=httpx.MockTransport(db.handler))
    app = FastAPI()

    @app.exception_handler(EntryRefused)
    async def entry_refused(_: Any, error: EntryRefused) -> JSONResponse:  # el de flow_change_api, en create_app
        return JSONResponse(status_code=422, content={"detail": error.code, "reason": error.reason})

    agent_product_api.register_agent_product_routes(app, token=TOKEN, store=store, manifest=ATT1, git_sha="57e9792")
    return TestClient(app, base_url="https://setter-att1.example.test"), db


def _ae(client: TestClient, method: str, path: str = "", body: dict | None = None, *,
        token: str = TOKEN) -> httpx.Response:
    return client.request(method, f"{PREFIX}{path}", json=body, headers={"Authorization": f"Bearer {token}"})


def _card(**changes: Any) -> dict[str, Any]:
    card = copy.deepcopy(FY["card"])
    card.update(changes)
    return card


ASKER = {"slack_user": "U01ASKER00"}


# ------------------------------------------------------------------ el arranque


@pytest.mark.parametrize(("overrides", "message"), [
    ({"automation_expert_change_enabled": False}, "requires AUTOMATION_EXPERT_CHANGE_ENABLED"),
    ({"instance_manifest": None}, "requires an instance manifest"),
])
def test_the_flag_needs_the_change_routes_and_a_manifest(overrides: dict, message: str) -> None:
    settings = SimpleNamespace(**{"agent_products_enabled": True, "automation_expert_change_enabled": True,
                                  "instance_manifest": ATT1, **overrides})
    with pytest.raises(ValueError, match=message):
        agent_product_api.check_settings(settings)


def test_off_the_check_asks_for_nothing() -> None:
    agent_product_api.check_settings(SimpleNamespace(agent_products_enabled=False))
    agent_product_api.check_settings(SimpleNamespace())


# ------------------------------------------------------------------ prendido


def test_the_routes_need_the_key() -> None:
    client, db = _app()
    assert _ae(client, "POST", "/preview", {"product": _card()}, token="z" * 48).status_code == 401
    assert db.calls == []


def test_the_preview_validates_and_renders_without_saving() -> None:
    client, db = _app()
    response = _ae(client, "POST", "/preview", {"product": _card()})

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert response.json()["contract"] == "product-card-v1"
    assert data["ok"] is True and data["blocks"] == [] and data["warnings"] == []
    assert data["preview"]["render"].startswith("# Producto: Black Forever Young\n")
    assert data["preview"]["rendered_bytes"] <= 6000
    assert db.calls == ["list_agent_products_v1"]


def test_the_preview_reports_blocks_and_refuses_what_is_not_a_card() -> None:
    client, _ = _app()
    text = FY["card"]["texto_libre"].replace("USD 199,", "USD 149,")
    data = _ae(client, "POST", "/preview", {"product": _card(texto_libre=text)}).json()["data"]
    assert data["ok"] is False
    assert {"code": "free_text_amount_not_declared", "field": "texto_libre", "detail": "149 USD"} in data["blocks"]

    refused = _ae(client, "POST", "/preview", {"product": {**_card(), "precio": "199"}})
    assert refused.status_code == 422 and "product_card_invalid" in refused.text


def test_proposing_saves_the_minimum_and_validating_freezes_it() -> None:
    client, db = _app()
    saved = _ae(client, "POST", "", {"product": _card(), "purpose": "Sumar Forever Young", "requested_by": ASKER})

    assert saved.status_code == 201, saved.text
    data = saved.json()["data"]
    assert data["status"] == "borrador" and data["revision"] == 1 and data["product_version"] == 1
    row = db.rows[data["product_id"]]
    assert row["minimo"]["alias"] == ["forever young", "black forever young", "black de ana lucia"]
    assert "texto_libre" not in row["minimo"] and row["texto_libre"] == FY["card"]["texto_libre"]
    assert db.calls == ["list_agent_products_v1", "save_agent_product_draft_v1"]

    validated = _ae(client, "POST", f"/{data['product_id']}/validate", {"revision": 1, "requested_by": ASKER})
    assert validated.status_code == 200, validated.text
    assert validated.json()["data"]["status"] == "validada"
    assert row["validation"]["validator"] == {"contract": "product-card-v1", "rules_version": 1,
                                              "product_version": "v1.39.0", "git_sha": "57e9792"}
    # La ficha que se esta validando no choca consigo misma en taken.
    assert row["validation"]["result"] == {"blocks": [], "warnings": []}

    read = _ae(client, "GET", f"/{data['product_id']}").json()["data"]
    assert read["status"] == "validada" and read["minimo"] == row["minimo"]
    listed = _ae(client, "GET", "?estado=todas&limit=5").json()["data"]
    assert [product["product_id"] for product in listed["products"]] == [data["product_id"]]


def test_a_draft_with_blocks_is_not_saved() -> None:
    client, db = _app()
    response = _ae(client, "POST", "", {"product": _card(alias=[]), "purpose": "Sumar Forever Young",
                                        "requested_by": ASKER})

    assert response.status_code == 422
    assert response.json()["detail"] == "product_draft_blocked"
    assert {"code": "aliases_missing", "field": "alias"} in response.json()["data"]["blocks"]
    assert "save_agent_product_draft_v1" not in db.calls


def test_another_product_of_the_base_with_the_same_alias_blocks_the_proposal() -> None:
    db = Db()
    db.taken_extra.append({"product_id": str(uuid.uuid4()), "product_key": "otro", "status": "publicada",
                           "nombre": "Otro", "alias": ["Black de Ana Lucía"], "hotmart_product_id": 9000020,
                           "offer_codes": ["otrop001"], "incluye_productos": []})
    client, _ = _app(db=db)
    response = _ae(client, "POST", "", {"product": _card(), "purpose": "Sumar Forever Young",
                                        "requested_by": ASKER})
    assert response.status_code == 422
    assert {"code": "alias_taken", "field": "alias[2]"} in response.json()["data"]["blocks"]


def test_the_black_of_att1_collides_with_the_principal_of_the_instance() -> None:
    client, _ = _app()
    card = _card(clave="black-november", nombre="Black November", alias=["black november"],
                 hotmart={"producto_id": 5071808, "hotlink": "D98014973Y"},
                 ofertas=[{"codigo": "wmkq4ba9", "precio": "47", "moneda": "USD", "por_defecto": True}],
                 texto_libre="## Precio y cómo se paga\n\nUSD 47.")
    data = _ae(client, "POST", "/preview", {"product": card}).json()["data"]
    assert {"code": "hotmart_product_taken", "field": "hotmart.producto_id"} in data["blocks"]


def test_validating_needs_the_identity_of_the_image(monkeypatch: pytest.MonkeyPatch) -> None:
    client, db = _app()
    saved = _ae(client, "POST", "", {"product": _card(), "purpose": "Sumar", "requested_by": ASKER}).json()["data"]
    monkeypatch.delenv("GIT_SHA")
    response = _ae(client, "POST", f"/{saved['product_id']}/validate", {"revision": 1, "requested_by": ASKER})
    assert response.status_code == 503
    assert "record_agent_product_validation_v1" not in db.calls


def test_validating_an_old_revision_or_a_frozen_one_is_refused() -> None:
    client, _ = _app()
    saved = _ae(client, "POST", "", {"product": _card(), "purpose": "Sumar", "requested_by": ASKER}).json()["data"]
    stale = _ae(client, "POST", f"/{saved['product_id']}/validate", {"revision": 2, "requested_by": ASKER})
    assert stale.status_code == 409 and stale.json()["detail"] == "agent_product_revision_mismatch"
    assert _ae(client, "POST", f"/{saved['product_id']}/validate",
               {"revision": 1, "requested_by": ASKER}).status_code == 200
    again = _ae(client, "POST", f"/{saved['product_id']}/validate", {"revision": 1, "requested_by": ASKER})
    assert again.status_code == 409 and again.json()["detail"] == "agent_product_not_draft"


def test_redoing_needs_the_id_and_the_revision_together_and_a_purpose_without_identifiers() -> None:
    client, _ = _app()
    alone = _ae(client, "POST", "", {"product": _card(), "purpose": "Sumar", "requested_by": ASKER, "revision": 1})
    assert alone.status_code == 422
    phone = _ae(client, "POST", "", {"product": _card(), "purpose": "llamar al 5512345678", "requested_by": ASKER})
    assert phone.status_code == 422 and "identifier_not_allowed" in phone.text
    bad_id = _ae(client, "GET", "/no-es-un-uuid")
    assert bad_id.status_code == 422 and bad_id.json()["detail"] == "invalid_product_id"


def test_validating_finds_a_collision_that_appeared_after_saving() -> None:
    db = Db()
    client, _ = _app(db=db)
    saved = _ae(client, "POST", "", {"product": _card(), "purpose": "Sumar", "requested_by": ASKER}).json()["data"]
    db.taken_extra.append({"product_id": str(uuid.uuid4()), "product_key": "otro", "status": "borrador",
                           "nombre": "Otro", "alias": ["forever young"], "hotmart_product_id": 9000020,
                           "offer_codes": ["otrop001"], "incluye_productos": []})
    response = _ae(client, "POST", f"/{saved['product_id']}/validate", {"revision": 1, "requested_by": ASKER})

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "borrador"
    assert {"code": "alias_taken", "field": "alias[0]"} in response.json()["data"]["blocks"]
    assert db.rows[saved["product_id"]]["validation"]["result"]["blocks"]


def test_redoing_a_draft_bumps_the_revision() -> None:
    client, db = _app()
    saved = _ae(client, "POST", "", {"product": _card(), "purpose": "Sumar", "requested_by": ASKER}).json()["data"]
    redone = _ae(client, "POST", "", {"product": _card(nombre="Forever Young"), "purpose": "Sumar",
                                      "requested_by": ASKER, "product_id": saved["product_id"], "revision": 1})
    assert redone.status_code == 201, redone.text
    assert redone.json()["data"]["revision"] == 2
    assert db.rows[saved["product_id"]]["minimo"]["nombre"] == "Forever Young"
    stale = _ae(client, "POST", "", {"product": _card(), "purpose": "Sumar", "requested_by": ASKER,
                                     "product_id": saved["product_id"], "revision": 1})
    assert stale.status_code == 409 and stale.json()["detail"] == "agent_product_revision_mismatch"
