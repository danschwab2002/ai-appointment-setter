"""flow-change-cycle-v1 1.10.0 (E-208, M16 P6, P7 y P9): el catalogo, la lista de propuestas y el mensaje.

Sobre el arnes de tests/test_flow_change_api_app.py: la app con el cliente real
del almacen y PostgREST emulado. La lista sale de la salida real de
list_flow_changes_v1 (migracion 20261030000300) sobre la cadena de migraciones
en PGlite (tests/fixtures/flow_change_list/list_flow_changes_v1.json, de
validate_flow_change_list.mjs --emit-fixture). El catalogo es el de la bandeja
11 de ATT1 leido de Chatwoot el 2026-10-21, y para las formas de Maribel y de la
ficha, las capturas de las bandejas 13 (2026-10-30) y 11 (2026-10-19).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest

from bridge.flow_change_preview import message_as_seen, template_view
from test_flow_change_api_app import AUTH, CATALOG, PREFIX, FakeChatwoot, PostgrestEmulator, _client, _propose_body

FIXTURES = Path(__file__).parent / "fixtures"
LISTED = json.loads((FIXTURES / "flow_change_list" / "list_flow_changes_v1.json").read_text())
MARIBEL = json.loads((FIXTURES / "chatwoot_message_templates_inbox_13_bienvenida_20261030.json").read_text())
FICHA = json.loads((FIXTURES / "chatwoot_message_templates_inbox_11_ficha_recordatorios_20261019.json").read_text())


class ListingEmulator(PostgrestEmulator):
    """El emulador del arnes, con list_flow_changes_v1 tal como la contesta la base."""

    def __init__(self) -> None:
        super().__init__()
        self.listing_fails = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/list_flow_changes_v1"):
            body = json.loads(request.content)
            self.requests.append(("list_flow_changes_v1", body, dict(request.headers)))
            if self.listing_fails:
                return httpx.Response(503, json={"message": "upstream"})
            if body["p_instance"] != {"tenant_ref": "lancemos", "ally_ref": "att1"}:
                return httpx.Response(400, json={"code": "22023", "message": "flow_store_instance_not_bound"})
            listed = copy.deepcopy(LISTED["open" if body["p_only_open"] else "all"])
            listed["proposals"] = listed["proposals"][: body["p_limit"]]
            return httpx.Response(200, json=listed)
        return super().handler(request)


# ------------------------------------------------------------------- P7: la lista de propuestas


def test_the_list_asks_the_open_ones_with_the_role_key_and_answers_them() -> None:
    emulator = ListingEmulator()
    client, _ = _client(emulator)
    response = client.get(f"{PREFIX}/flow-changes", headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["contract"], body["contract_version"]) == ("flow-change-cycle-v1", "1.10.0")
    function, sent, headers = emulator.requests[-1]
    assert function == "list_flow_changes_v1"
    assert sent == {"p_instance": {"tenant_ref": "lancemos", "ally_ref": "att1"}, "p_only_open": True, "p_limit": 20}
    # Con la clave del rol del agente, nunca con la de service_role.
    assert headers["apikey"] == emulator_key()
    data = body["data"]
    assert data["estado"] == "abiertas" and data["open"] == 2 and data["open_limit"] == 10
    assert [row["effective_status"] for row in data["proposals"]] == ["borrador", "validada"]
    assert all("requested_by_ref" not in row for row in data["proposals"])


def emulator_key() -> str:
    from test_flow_change_api_app import DB_KEY

    return DB_KEY


def test_all_the_proposals_with_the_expired_one_and_the_operator_one_without_purpose() -> None:
    emulator = ListingEmulator()
    client, _ = _client(emulator)
    response = client.get(f"{PREFIX}/flow-changes?estado=todas&limit=50", headers=AUTH)
    assert response.status_code == 200, response.text
    assert emulator.requests[-1][1]["p_only_open"] is False and emulator.requests[-1][1]["p_limit"] == 50
    rows = response.json()["data"]["proposals"]
    assert "vencida" in [row["effective_status"] for row in rows]
    operator = [row for row in rows if row["requested_by_kind"] == "operador"]
    assert operator and all(row["purpose"] is None for row in operator)
    assert [row["status"] for row in rows].count("descartada") == 1


def test_a_list_query_that_does_not_close_is_refused_with_its_reason_before_the_base() -> None:
    emulator = ListingEmulator()
    client, _ = _client(emulator)
    for query, field in (("estado=cerradas", "estado"), ("limit=0", "limit"), ("limit=51", "limit"),
                         ("limit=dos", "limit"), ("limit=%EF%BC%95", "limit")):
        response = client.get(f"{PREFIX}/flow-changes?{query}", headers=AUTH)
        assert response.status_code == 422, (query, response.text)
        assert response.json()["detail"] == "invalid_query"
        assert response.json()["reason"]["field"] == field
    assert not any(function == "list_flow_changes_v1" for function, _, _ in emulator.requests)


def test_the_list_needs_the_change_token_and_says_when_the_base_does_not_answer() -> None:
    emulator = ListingEmulator()
    client, _ = _client(emulator)
    assert client.get(f"{PREFIX}/flow-changes").status_code == 401
    assert client.get(f"{PREFIX}/flow-changes", headers={"Authorization": "Bearer " + "e" * 40}).status_code == 401
    emulator.listing_fails = True
    response = client.get(f"{PREFIX}/flow-changes", headers=AUTH)
    assert response.status_code == 503
    assert response.json()["detail"] == "automation_expert_change_unavailable"


# ------------------------------------------------------------------- P6: el catalogo


def test_the_templates_route_lists_the_inbox_catalog_approved_first() -> None:
    chatwoot = FakeChatwoot()
    client, _ = _client(chatwoot=chatwoot)
    response = client.get(f"{PREFIX}/templates", headers=AUTH)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert chatwoot.inboxes == [11]
    assert data["total"] == len(CATALOG["message_templates"]) and data["truncated"] is False
    templates = data["templates"]
    approved = [view["aprobada"] for view in templates]
    assert approved == sorted(approved, reverse=True)
    by_name = {(view["nombre"], view["idioma"]): view for view in templates}
    raw = next(t for t in CATALOG["message_templates"] if t["status"] == "APPROVED")
    view = by_name[(raw["name"], raw["language"].strip())]
    body = next(c["text"] for c in raw["components"] if c["type"] == "BODY")
    assert view["cuerpo"] == body and view["categoria"] == raw.get("category")
    assert len(data["catalog_sha256"]) == 64


def test_without_chatwoot_the_catalog_is_not_listed() -> None:
    client, _ = _client(chatwoot=FakeChatwoot(failure=httpx.ConnectError("caido")))
    response = client.get(f"{PREFIX}/templates", headers=AUTH)
    assert response.status_code == 503
    assert response.json()["detail"] == "template_catalog_unavailable"
    assert client.get(f"{PREFIX}/templates").status_code == 401


def test_a_template_view_has_its_markers_its_parts_and_whether_its_link_changes() -> None:
    welcome = template_view(MARIBEL["message_templates"][0])
    assert welcome["nombre"] == "maribel_bienvenida_01" and welcome["aprobada"] is True
    assert welcome["categoria"] == "UTILITY" and welcome["marcadores"] == 1
    assert welcome["encabezado"] == {"formato": "TEXT", "texto": "Notificación"}
    assert welcome["botones"] == [{"tipo": "URL", "texto": "Completar mi formulario",
                                   "link": "https://forms.gle/1ESdBdR2fLYd5Eqt7", "link_cambia": False}]
    slip = template_view(FICHA["message_templates"][0])
    assert slip["marcadores"] == 4
    assert slip["botones"][0]["link_cambia"] is True and slip["botones"][1]["tipo"] == "QUICK_REPLY"


# ------------------------------------------------------------------- P9: el mensaje como lo ve el lead


def test_the_message_as_the_lead_sees_it_names_each_variable() -> None:
    step = {"plantilla": "att1_ficha_efectivo_01", "idioma": "es_MX",
            "variables": ["nombre", "tienda", "producto", "vencimiento"], "boton": {"link": "ficha"}}
    seen = message_as_seen(FICHA["message_templates"][0], step)
    assert seen["cuerpo"].startswith("Hola, [nombre]. Te dejo aquí tu ficha para pagar [tienda] en [producto]")
    assert "{{" not in seen["cuerpo"]
    assert seen["botones"][0] == {"tipo": "URL", "texto": "Ver mi ficha",
                                  "link": "https://customer-local-latam.ebanx.com/print/oxxo?hash=[link de la ficha]"}
    assert seen["botones"][1] == {"tipo": "QUICK_REPLY", "texto": "No más mensajes"}
    # Un marcador sin variable queda a la vista, como lo frena el validador.
    short = message_as_seen(FICHA["message_templates"][0], {**step, "variables": ["nombre"]})
    assert "[sin variable]" in short["cuerpo"]
    welcome = message_as_seen(MARIBEL["message_templates"][0], {"variables": ["producto"]})
    assert welcome["encabezado"] == "Notificación"
    assert welcome["cuerpo"].startswith("Tu inscripción en [producto] ha sido confirmada.")


def test_validate_shows_each_message_of_the_proposal_as_the_lead_sees_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # La identidad del validador sale de la imagen, como en test_flow_change_api_app.
    monkeypatch.setenv("SETTER_VERSION", "1.23.0")
    monkeypatch.setenv("GIT_SHA", "d125c67f57bfdafbac6cb19d82dd357c83c3c1e8")
    client, _ = _client()
    proposed = client.post(f"{PREFIX}/flow-changes", headers=AUTH, json=_propose_body())
    assert proposed.status_code == 201, proposed.text
    data = proposed.json()["data"]
    validated = client.post(f"{PREFIX}/flow-changes/{data['proposal_id']}/validate", headers=AUTH,
                            json={"revision": data["revision"], "requested_by": {"slack_user": "U0123456789"}})
    assert validated.status_code == 200, validated.text
    messages = validated.json()["data"]["preview"]["mensajes"]
    assert messages, "la vista previa no trae los mensajes"
    assert all(message["flujo"] == "carrito" and message["ruta"].startswith("/pasos/") for message in messages)
    found = [message for message in messages if message["encontrada"]]
    assert found and all("{{" not in (message["como_lo_ve"]["cuerpo"] or "") for message in found)
    assert any("[nombre]" in (message["como_lo_ve"]["cuerpo"] or "") for message in found)


def test_messages_leave_the_preview_with_their_gap_when_it_would_not_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    """E-213, menor 5: si sin los diffs la vista previa todavia pasa el tope, salen los mensajes."""
    from bridge import flow_change_preview

    monkeypatch.setenv("SETTER_VERSION", "1.23.0")
    monkeypatch.setenv("GIT_SHA", "d125c67f57bfdafbac6cb19d82dd357c83c3c1e8")
    # El tope bajado para la prueba: lo que queda sin diffs ni mensajes entra; con mensajes, no.
    monkeypatch.setattr(flow_change_preview, "PREVIEW_MAX_BYTES", 3000)
    client, _ = _client()
    data = client.post(f"{PREFIX}/flow-changes", headers=AUTH, json=_propose_body()).json()["data"]
    validated = client.post(f"{PREFIX}/flow-changes/{data['proposal_id']}/validate", headers=AUTH,
                            json={"revision": data["revision"], "requested_by": {"slack_user": "U0123456789"}})
    assert validated.status_code == 200, validated.text
    preview = validated.json()["data"]["preview"]
    assert preview["mensajes"] == [] and preview["mensajes_count"] > 0
    assert "messages_truncated" in [gap["code"] for gap in preview["huecos"]]
